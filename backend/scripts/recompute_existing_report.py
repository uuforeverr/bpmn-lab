from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from statistics import mean
from typing import Any

from app.evaluation import (
    SentenceTransformerSimilarity,
    bpmn_to_reference,
    complexity_profile,
    estimate_gateway_nesting_depth,
    summarize_runs,
)
from app.process_quality import calculate_process_quality


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recompute quality metrics from stored graphs/BPMN without model generation."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    return parser.parse_args()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def reference_path(dataset: Path, sample: str) -> Path:
    return dataset / "bpmn" / f"{sample}.bpmn"


def threshold_score(run: dict[str, Any], threshold: float, field: str = "f1") -> float | None:
    for item in run.get("quality", {}).get("taskAlignment", {}).get("sensitivity", []):
        if abs(float(item.get("threshold", -1.0)) - threshold) < 1e-12:
            return float(item[field])
    return None


def metric(run: dict[str, Any], name: str) -> float | None:
    if name.startswith("taskF1@"):
        return threshold_score(run, float(name.removeprefix("taskF1@")))
    value = run.get("quality", {}).get(name)
    return None if value is None else float(value)


METRICS = [
    ("PMo-compatible Task-name Dice@0.7", "pmoTaskNameDiceAt07"),
    ("Task Semantic Alignment Score", "taskSemanticAlignmentScore"),
    ("Task F1@0.6", "taskF1@0.6"),
    ("Task F1@0.7", "taskF1@0.7"),
    ("Task F1@0.8", "taskF1@0.8"),
    ("Gateway type-role count F1", "gatewayTypeRoleF1"),
    ("Control-flow Relation F1@0.7", "controlFlowRelationF1"),
    ("Matched Task-pair coverage@0.7", "controlFlowRelationMatchedTaskPairCoverage"),
]


def average(rows: list[dict[str, Any]], name: str) -> float | None:
    values = [value for row in rows if (value := metric(row, name)) is not None]
    return mean(values) if values else None


def fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.4f}"


def make_summary(report: dict[str, Any]) -> str:
    runs = report["runs"]
    strategies = {
        strategy: [run for run in runs if run.get("strategy") == strategy and run.get("success")]
        for strategy in ("single_segment", "semantic")
    }
    lines = [
        "# Existing 20-run artifacts recomputed with retained metrics v3",
        "",
        "This is an offline recomputation from stored Graph-JSON and BPMN artifacts. It made no model-generation calls and did not change recorded token usage.",
        "",
        "## Overall macro averages",
        "",
        "| Metric | Single generation | Semantic segmentation | Semantic - single |",
        "|---|---:|---:|---:|",
    ]
    for label, name in METRICS:
        single = average(strategies["single_segment"], name)
        semantic = average(strategies["semantic"], name)
        delta = None if single is None or semantic is None else semantic - single
        lines.append(f"| {label} | {fmt(single)} | {fmt(semantic)} | {fmt(delta)} |")

    lines.extend(["", "## By reference size bucket", ""])
    for bucket in ("S", "M", "L", "XL"):
        lines.extend([
            f"### {bucket}",
            "",
            "| Metric | Single generation | Semantic segmentation | Semantic - single |",
            "|---|---:|---:|---:|",
        ])
        bucket_rows = {
            strategy: [
                run for run in rows if run.get("complexity", {}).get("sizeBucket") == bucket
            ]
            for strategy, rows in strategies.items()
        }
        for label, name in METRICS:
            single = average(bucket_rows["single_segment"], name)
            semantic = average(bucket_rows["semantic"], name)
            delta = None if single is None or semantic is None else semantic - single
            lines.append(f"| {label} | {fmt(single)} | {fmt(semantic)} | {fmt(delta)} |")
        lines.append("")

    lines.extend([
        "## Matching protocols",
        "",
        "- PMo-compatible Dice@0.7 uses PMo's stable descending greedy semantic-frequency matching.",
        "- Task Semantic Alignment Score uses a maximum-total-similarity one-to-one assignment and assigns zero contribution to unmatched Tasks.",
        "- Task P/R/F1 independently rematches at 0.6, 0.7, and 0.8, maximizing match count first and total matched similarity second.",
        "- Gateway F1 compares the six type-role count categories and does not use Task boundaries.",
        "- Control-flow Relation F1 compares order, cycle, exclusive, parallel and inclusive relations after Task alignment.",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    original = json.loads(args.input.read_text(encoding="utf-8"))
    report = deepcopy(original)
    dataset = Path(report["dataset"])
    similarity = SentenceTransformerSimilarity(local_files_only=True)
    runs = report.get("runs", [])
    references: dict[str, dict[str, Any]] = {}
    for run in runs:
        sample = str(run["sample"])
        if sample not in references:
            bpmn_path = reference_path(dataset, sample)
            references[sample] = bpmn_to_reference(bpmn_path.read_text(encoding="utf-8"))
        reference = references[sample]
        run["complexity"] = complexity_profile(
            reference, estimate_gateway_nesting_depth(reference)
        )
    for index, run in enumerate(runs, start=1):
        if not run.get("success") or not run.get("graph") or not run.get("bpmnXml"):
            continue
        sample = str(run["sample"])
        reference = references[sample]
        print(
            f"[{index}/{len(runs)}] sample={sample} strategy={run['strategy']}",
            flush=True,
        )
        run["quality"] = calculate_process_quality(
            run["graph"], reference, run["bpmnXml"], similarity, task_threshold=0.7,
        )
        run["qualityMetricsComplete"] = True
        run.pop("qualityErrorType", None)
        run.pop("qualityError", None)
        report["summary"] = summarize_runs(runs)
        report["qualityRecomputation"] = {
            "source": str(args.input.resolve()),
            "generationCallsMade": 0,
            "embeddingModel": similarity.model_name,
            "taskMetricProtocol": "PMo Dice@0.7 + continuous TSA + threshold cardinality P/R/F1 v2",
            "referenceStructureSource": "reference BPMN",
            "gatewayProtocol": "six type-role count categories",
            "controlFlowProtocol": "matched Task-pair graph relations",
            "thresholds": [0.6, 0.7, 0.8],
        }
        atomic_write(args.output, json.dumps(report, ensure_ascii=False, indent=2))

    atomic_write(args.summary, make_summary(report))
    print(f"report={args.output.resolve()}", flush=True)
    print(f"summary={args.summary.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
