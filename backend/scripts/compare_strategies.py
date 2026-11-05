from __future__ import annotations

import argparse
import json
import math
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from app.config import Settings, load_settings
from app.bpmn import graph_to_bpmn
from app.domain import Graph
from app.evaluation import (
    SentenceTransformerSimilarity,
    aggregate_token_usage,
    assess_bpmn_reference_scope,
    bpmn_to_reference,
    complexity_profile,
    estimate_gateway_nesting_depth,
    graph_to_pme,
    summarize_runs,
)
from app.llm import LlmClient, parse_json_object, prompt
from app.pipeline import Pipeline
from app.process_quality import calculate_process_quality
from app.validation import validate_graph


STRATEGIES = ("semantic", "single_segment", "oneshot")


class DatasetAdapter:
    def __init__(self, root: Path):
        self.root = root.resolve()
        if (self.root / "descriptions").is_dir() and (self.root / "pme").is_dir():
            self.kind = "pmo"
            self.description_dir = self.root / "descriptions"
            self.reference_dir = self.root / "pme"
        elif (self.root / "descriptions_en").is_dir() and (self.root / "graph_json").is_dir():
            self.kind = "synthetic"
            self.description_dir = self.root / "descriptions_en"
            self.reference_dir = self.root / "graph_json"
        else:
            raise ValueError(
                "dataset must contain descriptions/ + pme/ or descriptions_en/ + graph_json/"
            )

    def samples(self) -> list[str]:
        return sorted(path.stem for path in self.description_dir.glob("*.txt"))

    def eligible_samples(self) -> list[str]:
        if self.kind != "pmo":
            return self.samples()
        return [
            sample for sample in self.samples()
            if assess_bpmn_reference_scope(self.reference_bpmn(sample) or "")["eligible"]
        ]

    def eligibility(self, sample: str) -> dict[str, Any]:
        if self.kind != "pmo":
            return {"eligible": True, "reasons": [], "source": "synthetic_graph_json"}
        result = assess_bpmn_reference_scope(self.reference_bpmn(sample) or "")
        return {**result, "source": "reference_bpmn"}

    def description(self, sample: str) -> str:
        return (self.description_dir / f"{sample}.txt").read_text(encoding="utf-8")

    def reference(self, sample: str) -> dict[str, Any]:
        if self.kind == "pmo":
            xml = self.reference_bpmn(sample)
            if not xml:
                raise FileNotFoundError(self.root / "bpmn" / f"{sample}.bpmn")
            return bpmn_to_reference(xml)
        payload = json.loads((self.reference_dir / f"{sample}.json").read_text(encoding="utf-8"))
        return graph_to_pme(payload)

    def reference_bpmn(self, sample: str) -> str | None:
        if self.kind != "pmo":
            return None
        path = self.root / "bpmn" / f"{sample}.bpmn"
        return path.read_text(encoding="utf-8") if path.exists() else None

    def nesting_depth(self, sample: str) -> int | None:
        if self.kind == "synthetic":
            metadata_path = self.root / "metadata" / f"{sample}.json"
            if metadata_path.exists():
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                match = re.search(r"depth\s*([0-9]+)", str(metadata.get("target_complexity_cell", "")), re.I)
                return int(match.group(1)) if match else None
            return None
        return estimate_gateway_nesting_depth(self.reference(sample))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Paired semantic-segmentation versus single-generation evaluation."
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--config", type=str)
    parser.add_argument("--samples", nargs="*")
    parser.add_argument(
        "--sample-fraction", type=float,
        help="Select this fraction of eligible samples, stratified by low/medium/high complexity.",
    )
    parser.add_argument(
        "--include-unsupported", action="store_true",
        help="Allow PMo samples outside the current Graph-JSON representation scope.",
    )
    parser.add_argument("--strategies", nargs="+", choices=STRATEGIES, default=list(STRATEGIES))
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20261012)
    parser.add_argument("--max-steps", type=int, default=200)
    parser.add_argument(
        "--max-workers", type=int, default=1,
        help="Run this many independent sample/repetition/strategy trials concurrently.",
    )
    parser.add_argument("--threshold", type=float, default=0.7)
    parser.add_argument(
        "--embedding-model", default="sentence-transformers/stsb-mpnet-base-v2",
        help="Use the default for paper-comparable PME; alternatives are diagnostic only.",
    )
    parser.add_argument("--embedding-local-files-only", action="store_true")
    parser.add_argument(
        "--prompt-language", choices=("auto", "zh", "en"), default="auto",
        help="Generation language; auto uses English for the supported English-language datasets.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bpmn-output-dir", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--retry-infrastructure-failures", action="store_true",
        help="With --resume, rerun only failed trials caused by provider/transport infrastructure errors.",
    )
    parser.add_argument(
        "--retry-failures", action="store_true",
        help="With --resume, rerun every previously failed trial while preserving successful trials.",
    )
    parser.add_argument(
        "--skip-quality-metrics", action="store_true",
        help="Generation smoke-test only; do not use for paper results.",
    )
    return parser.parse_args()


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class _LockedSimilarity:
    """Serialize access to the shared embedding model and its mutable cache."""

    def __init__(self, similarity):
        self.similarity = similarity
        self.lock = threading.Lock()

    def __call__(self, left: str, right: str) -> float:
        with self.lock:
            return self.similarity(left, right)


def _persist_run_artifacts(output_dir: Path | None, run: dict[str, Any]) -> None:
    """Persist standalone artifacts before committing the run checkpoint."""
    if output_dir is None or not run.get("bpmnXml"):
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{run['sample']}-r{run['repetition']}-{run['strategy']}"
    (output_dir / f"{stem}.bpmn").write_text(run["bpmnXml"], encoding="utf-8")
    if run.get("graph") is not None:
        (output_dir / f"{stem}.graph.json").write_text(
            json.dumps(run["graph"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _is_infrastructure_failure(run: dict[str, Any]) -> bool:
    if run.get("success"):
        return False
    error_type = str(run.get("errorType", ""))
    error = str(run.get("error", ""))
    return (
        error_type in {"APIConnectionError", "APITimeoutError", "RateLimitError", "BadRequestError"}
        and any(marker in error for marker in (
            "invalid_request_error", "Connection", "timed out", "rate_limit",
        ))
    )


def stratified_subset(
    adapter: DatasetAdapter, candidates: list[str], count: int,
) -> list[str]:
    """Proportionally sample complexity levels while covering each level's score range."""
    if count >= len(candidates):
        return list(candidates)
    groups: dict[str, list[tuple[str, dict[str, Any]]]] = {"low": [], "medium": [], "high": []}
    for sample in candidates:
        reference = adapter.reference(sample)
        profile = complexity_profile(reference, adapter.nesting_depth(sample))
        groups[profile["weightedLevel"]].append((sample, profile))

    exact = {level: count * len(rows) / len(candidates) for level, rows in groups.items()}
    quotas = {level: math.floor(value) for level, value in exact.items()}
    remaining = count - sum(quotas.values())
    for level in sorted(groups, key=lambda item: (exact[item] - quotas[item], len(groups[item])), reverse=True):
        if remaining <= 0:
            break
        if quotas[level] < len(groups[level]):
            quotas[level] += 1
            remaining -= 1

    selected: list[str] = []
    for level in ("low", "medium", "high"):
        rows = sorted(
            groups[level],
            key=lambda item: (
                item[1]["weightedScore"],
                item[1]["nestingDepth"] if item[1]["nestingDepth"] is not None else -1,
                item[1]["totalNodes"],
                item[0],
            ),
        )
        quota = quotas[level]
        if quota == 1:
            selected.append(rows[len(rows) // 2][0])
        elif quota > 1:
            indices = [round(index * (len(rows) - 1) / (quota - 1)) for index in range(quota)]
            selected.extend(rows[index][0] for index in indices)
    return sorted(selected)


def _report(
    adapter: DatasetAdapter, settings: Settings, args: argparse.Namespace,
    runs: list[dict[str, Any]], schedule: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schemaVersion": "1.0",
        "dataset": str(adapter.root),
        "datasetKind": adapter.kind,
        "eligibleSamples": adapter.eligible_samples(),
        "eligibilitySource": "reference BPMN" if adapter.kind == "pmo" else "synthetic Graph-JSON",
        "eligibilityDiagnostics": {
            sample: adapter.eligibility(sample) for sample in adapter.samples()
        },
        "selectedSamples": sorted({item["sample"] for item in schedule}),
        "protocol": {
            "strategies": args.strategies,
            "repetitions": args.repetitions,
            "randomSeed": args.seed,
            "maxWorkers": args.max_workers,
            "sampleFraction": args.sample_fraction,
            "sampleSelection": (
                "explicit --samples" if args.samples
                else "proportional low/medium/high stratification with within-stratum score-range coverage"
                if args.sample_fraction else "all eligible samples"
            ),
            "randomizedBlock": "sample x repetition; strategy order shuffled within each block",
            "qualityMetricsSkipped": args.skip_quality_metrics,
            "pmeThreshold": args.threshold,
            "embeddingModel": args.embedding_model if not args.skip_quality_metrics else None,
            "requestedPromptLanguage": args.prompt_language,
            "resolvedPromptLanguage": settings.prompts.language,
            "paperComparablePme": (
                not args.skip_quality_metrics
                and args.embedding_model == "sentence-transformers/stsb-mpnet-base-v2"
                and args.threshold == 0.7
            ),
            "failurePolicy": (
                "Failures count against completion rate and receive zero in conservative bounded metrics; "
                "conditional means use successful runs only."
            ),
        },
        "models": {
            "plannerGeneratorReviewerRepair": settings.llm.model,
            "arbitrator": (
                settings.arbitrator_llm.model
                if settings.arbitrator_llm and settings.arbitrator_llm.model
                else settings.llm.model
            ),
        },
        "frozenConfiguration": {
            "main": {
                "baseUrl": settings.llm.base_url,
                "model": settings.llm.model,
                "temperature": settings.llm.temperature,
                "maxOutputTokens": settings.llm.max_output_tokens,
                "maxOutputTokensByAgent": settings.llm.max_output_tokens_by_agent,
                "thinking": settings.llm.thinking,
                "thinkingByAgent": settings.llm.thinking_by_agent,
            },
            "arbitrator": ({
                "baseUrl": settings.arbitrator_llm.base_url,
                "model": settings.arbitrator_llm.model,
                "temperature": settings.arbitrator_llm.temperature,
                "maxOutputTokens": settings.arbitrator_llm.max_output_tokens,
                "thinking": settings.arbitrator_llm.thinking,
            } if settings.arbitrator_llm else None),
            "pipeline": settings.pipeline.model_dump(),
            "promptLanguage": settings.prompts.language,
        },
        "schedule": schedule,
        "runs": runs,
        "summary": summarize_runs(runs),
    }


def run_once(
    adapter: DatasetAdapter, settings: Settings, sample: str, repetition: int,
    strategy: str, max_steps: int, similarity, threshold: float,
) -> dict[str, Any]:
    if strategy == "oneshot":
        return run_oneshot_once(
            adapter, settings, sample, repetition, similarity, threshold
        )
    text = adapter.description(sample)
    reference = adapter.reference(sample)
    complexity = complexity_profile(reference, adapter.nesting_depth(sample))
    pipeline = Pipeline(settings)
    state = pipeline.initial_state(text, strategy)
    stage = "INPUT_READY"
    started = time.perf_counter()
    error: Exception | None = None
    try:
        for _ in range(max_steps):
            if stage == "COMPLETED":
                break
            stage, state = pipeline.step(stage, state, text)
        if stage != "COMPLETED":
            raise RuntimeError(f"pipeline exceeded {max_steps} steps at {stage}")
    except Exception as exc:  # keep failed trials in the end-to-end denominator
        error = exc

    duration = round(time.perf_counter() - started, 3)
    calls = state.get("llmCalls", [])
    success = error is None and stage == "COMPLETED"
    run: dict[str, Any] = {
        "sample": sample,
        "repetition": repetition,
        "strategy": strategy,
        "success": success,
        "stage": stage,
        "durationSeconds": duration,
        "segments": len(state.get("segments", [])),
        "repairs": len(state.get("repairHistory", [])),
        "llmCalls": len(calls),
        "llmUsage": calls,
        "tokenUsage": aggregate_token_usage(calls),
        "complexity": complexity,
        "segmentPlan": state.get("segments", []),
        "structureWarnings": state.get("structureWarnings", []),
        "reviewWarnings": state.get("reviewWarnings", []),
        "repairHistory": state.get("repairHistory", []),
        "repairLimitEvents": state.get("repairLimitEvents", []),
        "reviewHistory": state.get("reviewHistory", []),
        "reviewResolutions": state.get("reviewResolutions", []),
        "arbitrationHistory": state.get("arbitrationHistory", []),
        "agentFailures": state.get("agentFailures", []),
        "agentAttemptDiagnostics": state.get("agentAttemptDiagnostics", []),
        "terminalAgentFailures": state.get("terminalAgentFailures", []),
        "generatorPatchFailure": state.get("generatorPatchFailure"),
        "pipelineSoundness": state.get("soundness"),
        "soundnessHistory": state.get("soundnessHistory", []),
        "soundnessRepairCount": state.get("soundnessRepairCount", 0),
        "soundnessRepairExhausted": state.get("soundnessRepairExhausted", False),
        "soundnessRepairBlocked": state.get("soundnessRepairBlocked", False),
    }
    if error is not None:
        run.update({"errorType": type(error).__name__, "error": str(error)})
    if success:
        graph = state["graph"]
        run["graph"] = graph
        run["bpmnXml"] = state.get("bpmnXml")
        if similarity is not None:
            try:
                if not run["bpmnXml"]:
                    raise ValueError("quality evaluation requires a generated BPMN artifact")
                run["quality"] = calculate_process_quality(
                    graph,
                    reference,
                    run["bpmnXml"],
                    similarity,
                    task_threshold=threshold,
                )
                run["qualityMetricsComplete"] = True
            except Exception as exc:
                run["qualityMetricsComplete"] = False
                run["qualityErrorType"] = type(exc).__name__
                run["qualityError"] = str(exc)
    return run


def run_oneshot_once(
    adapter: DatasetAdapter, settings: Settings, sample: str, repetition: int,
    similarity, threshold: float,
) -> dict[str, Any]:
    """Call the Generator exactly once on the complete description."""
    text = adapter.description(sample)
    reference = adapter.reference(sample)
    complexity = complexity_profile(reference, adapter.nesting_depth(sample))
    started = time.perf_counter()
    stage = "MODEL_CALL"
    error: Exception | None = None
    raw_output: str | None = None
    graph: Graph | None = None
    bpmn_xml: str | None = None
    structure_issues: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    finish_reason: str | None = None
    try:
        # Agent-level retries, review and repair are deliberately bypassed. Disable
        # transport retries as well so every trial contains one provider request.
        direct_config = settings.llm.model_copy(update={"max_transport_retries": 0})
        result = LlmClient(direct_config).complete(
            [
                {"role": "system", "content": prompt(
                    "generator_oneshot", settings.prompts.language
                )},
                {"role": "user", "content": text},
            ],
            agent="generator",
        )
        raw_output = result.content
        finish_reason = result.finish_reason
        calls.append({
            "agent": "generator",
            "usage": result.usage,
            "latencyMs": result.latency_ms,
            "toolCallCount": len(result.tool_calls),
            "finishReason": result.finish_reason,
            "contentChars": len(result.content or ""),
            "thinkingMode": result.thinking_mode,
            "model": result.model,
        })
        value = parse_json_object(result.content, "generator")
        if set(value) != {"nodes", "edges"}:
            raise ValueError("one-shot output must contain exactly nodes and edges")
        graph = Graph.model_validate(value)
        structure_issues = [
            issue.model_dump() for issue in validate_graph(graph, final=True)
        ]
        bpmn_xml = graph_to_bpmn(graph)
        stage = "COMPLETED"
    except Exception as exc:  # preserve every failed trial in the denominator
        error = exc

    run: dict[str, Any] = {
        "sample": sample,
        "repetition": repetition,
        "strategy": "oneshot",
        "success": error is None and stage == "COMPLETED",
        "stage": stage,
        "durationSeconds": round(time.perf_counter() - started, 3),
        "segments": 1,
        "repairs": 0,
        "llmCalls": len(calls),
        "llmUsage": calls,
        "tokenUsage": aggregate_token_usage(calls),
        "complexity": complexity,
        "segmentPlan": [],
        "structureWarnings": structure_issues,
        "structureIssues": structure_issues,
        "reviewWarnings": [],
        "repairHistory": [],
        "repairLimitEvents": [],
        "reviewHistory": [],
        "reviewResolutions": [],
        "arbitrationHistory": [],
        "agentFailures": [],
        "agentAttemptDiagnostics": [],
        "terminalAgentFailures": [],
        "generatorPatchFailure": None,
        "pipelineSoundness": None,
        "soundnessHistory": [],
        "soundnessRepairCount": 0,
        "soundnessRepairExhausted": False,
        "soundnessRepairBlocked": False,
        "finishReason": finish_reason,
        "rawModelOutput": raw_output,
    }
    if error is not None:
        run.update({"errorType": type(error).__name__, "error": str(error)})
        return run

    graph_payload = graph.model_dump()
    run["graph"] = graph_payload
    run["bpmnXml"] = bpmn_xml
    if similarity is not None:
        try:
            run["quality"] = calculate_process_quality(
                graph_payload,
                reference,
                bpmn_xml,
                similarity,
                task_threshold=threshold,
            )
            run["qualityMetricsComplete"] = True
        except Exception as exc:
            run["qualityMetricsComplete"] = False
            run["qualityErrorType"] = type(exc).__name__
            run["qualityError"] = str(exc)
    return run


def main() -> int:
    args = parse_args()
    if args.repetitions < 1:
        raise ValueError("--repetitions must be at least 1")
    if args.max_workers < 1:
        raise ValueError("--max-workers must be at least 1")
    if args.retry_infrastructure_failures and not args.resume:
        raise ValueError("--retry-infrastructure-failures requires --resume")
    if args.retry_failures and not args.resume:
        raise ValueError("--retry-failures requires --resume")
    if args.retry_failures and args.retry_infrastructure_failures:
        raise ValueError("choose only one retry policy")
    adapter = DatasetAdapter(args.dataset)
    all_samples = adapter.samples()
    eligible_samples = all_samples if args.include_unsupported else adapter.eligible_samples()
    samples = args.samples or eligible_samples
    missing = sorted(set(samples) - set(all_samples))
    if missing:
        raise ValueError(f"unknown samples: {', '.join(missing)}")
    unsupported = sorted(set(samples) - set(eligible_samples))
    if unsupported and not args.include_unsupported:
        raise ValueError(
            f"samples outside the current Graph-JSON scope: {', '.join(unsupported)}; "
            "pass --include-unsupported only for a sensitivity run"
        )
    if args.samples and args.sample_fraction:
        raise ValueError("--samples and --sample-fraction are mutually exclusive")
    if args.sample_fraction is not None:
        if not 0 < args.sample_fraction <= 1:
            raise ValueError("--sample-fraction must be in (0, 1]")
        sample_count = max(1, round(len(eligible_samples) * args.sample_fraction))
        samples = stratified_subset(adapter, eligible_samples, sample_count)
    config_path = args.config or str(Path(__file__).parents[1] / "config.yaml")
    settings = load_settings(config_path)
    settings.prompts.language = (
        "en" if args.prompt_language == "auto" else args.prompt_language
    )
    similarity = None if args.skip_quality_metrics else _LockedSimilarity(
        SentenceTransformerSimilarity(
            args.embedding_model, local_files_only=args.embedding_local_files_only
        )
    )

    schedule: list[dict[str, Any]] = []
    randomizer = random.Random(args.seed)
    for repetition in range(1, args.repetitions + 1):
        block_samples = list(samples)
        randomizer.shuffle(block_samples)
        for sample in block_samples:
            strategies = list(args.strategies)
            randomizer.shuffle(strategies)
            schedule.extend(
                {"sample": sample, "repetition": repetition, "strategy": strategy}
                for strategy in strategies
            )

    runs: list[dict[str, Any]] = []
    if args.resume and args.output.exists():
        existing = json.loads(args.output.read_text(encoding="utf-8"))
        runs = existing.get("runs", [])
        if args.retry_failures:
            runs = [run for run in runs if run.get("success")]
        elif args.retry_infrastructure_failures:
            runs = [run for run in runs if not _is_infrastructure_failure(run)]
        # A previous process may have stopped after its atomic checkpoint but
        # before older versions of this script wrote the standalone artifact.
        for run in runs:
            _persist_run_artifacts(args.bpmn_output_dir, run)
    schedule_order = {
        (item["sample"], item["repetition"], item["strategy"]): index
        for index, item in enumerate(schedule)
    }

    def ordered_runs() -> list[dict[str, Any]]:
        return sorted(
            runs,
            key=lambda row: schedule_order[(row["sample"], row["repetition"], row["strategy"])],
        )

    completed = {(row["sample"], row["repetition"], row["strategy"]) for row in runs}
    pending = [
        (index, item) for index, item in enumerate(schedule, start=1)
        if (item["sample"], item["repetition"], item["strategy"]) not in completed
    ]

    def execute(index: int, item: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        print(
            f"[{index}/{len(schedule)}] START sample={item['sample']} "
            f"repetition={item['repetition']} strategy={item['strategy']}",
            flush=True,
        )
        return index, run_once(
            adapter, settings, item["sample"], item["repetition"], item["strategy"],
            args.max_steps, similarity, args.threshold,
        )

    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        futures = [executor.submit(execute, index, item) for index, item in pending]
        for future in as_completed(futures):
            index, run = future.result()
            runs.append(run)
            completed.add((run["sample"], run["repetition"], run["strategy"]))
            _persist_run_artifacts(args.bpmn_output_dir, run)
            checkpoint_runs = ordered_runs()
            _atomic_write(args.output, _report(adapter, settings, args, checkpoint_runs, schedule))
            print(
                f"[{index}/{len(schedule)}] DONE success={run['success']} stage={run['stage']} "
                f"calls={run['llmCalls']} tokens={run['tokenUsage']['totalTokens']} "
                f"seconds={run['durationSeconds']} completed={len(checkpoint_runs)}/{len(schedule)}",
                flush=True,
            )
    runs = ordered_runs()
    _atomic_write(args.output, _report(adapter, settings, args, runs, schedule))
    print(f"report={args.output.resolve()}", flush=True)
    complete = all(run.get("success") for run in runs)
    if not args.skip_quality_metrics:
        complete = complete and all(run.get("qualityMetricsComplete") for run in runs)
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
