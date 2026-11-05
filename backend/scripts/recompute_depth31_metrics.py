from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable

from app.evaluation import bpmn_to_reference, complexity_profile, graph_to_pme
from app.process_quality import calculate_global_control_flow_relations


BACKEND = Path(__file__).resolve().parents[1]
RESULTS = BACKEND / "results"
ARCHIVE = RESULTS / "实验结果归档_20261023"
PREEXPERIMENT_DATA = ARCHIVE / "01_前置实验" / "数据"
FORMAL_DATA = ARCHIVE / "02_真实实验" / "数据"
ONESHOT_INPUT = PREEXPERIMENT_DATA / "oneshot_raw_150_runs.json"
OUTPUT = FORMAL_DATA
DEPTH_ORDER = ("low", "medium", "high")
STRATEGY_ORDER = ("oneshot", "single_segment", "semantic")
QUALITY_METRICS = (
    "pmoTaskNameDiceAt07",
    "gatewayTypeRoleCountDice",
    "conditionalRelationPrecisionAt06",
    "conditionalRelationRecallAt06",
    "conditionalRelationF1At06",
    "conditionalRelationPrecisionAt07",
    "conditionalRelationRecallAt07",
    "conditionalRelationF1At07",
    "conditionalRelationPrecisionAt08",
    "conditionalRelationRecallAt08",
    "conditionalRelationF1At08",
    "conditionalRelationCoverageAt06",
    "conditionalRelationCoverageAt07",
    "conditionalRelationCoverageAt08",
    "globalRelationPrecisionAt06",
    "globalRelationRecallAt06",
    "globalRelationF1At06",
    "globalRelationPrecisionAt07",
    "globalRelationRecallAt07",
    "globalRelationF1At07",
    "globalRelationPrecisionAt08",
    "globalRelationRecallAt08",
    "globalRelationF1At08",
)
TOKEN_METRICS = (
    "inputTokens",
    "outputTokens",
    "reasoningTokens",
    "cachedInputTokens",
)
MEAN_TOKEN_METRICS = tuple(f"mean{metric[0].upper()}{metric[1:]}" for metric in TOKEN_METRICS)


def _mean(values: Iterable[float | int | None]) -> float | None:
    usable = [float(value) for value in values if value is not None]
    return fmean(usable) if usable else None


def _depth_tier(max_depth: int) -> str:
    if max_depth <= 1:
        return "low"
    if max_depth == 2:
        return "medium"
    return "high"


def _compact_global(result: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "threshold", "matchedTaskCount", "generatedTaskCount", "referenceTaskCount",
        "generatedRelationCount", "referenceRelationCount", "matchedRelationCount",
        "precision", "recall", "f1",
    )
    return {
        "protocol": result["protocol"],
        **{field: result[field] for field in fields},
        "sensitivity": [
            {field: item[field] for field in fields}
            for item in result.get("sensitivity", [])
        ],
    }


def _sensitivity_by_threshold(
    result: dict[str, Any], threshold: float,
) -> dict[str, Any]:
    for item in result.get("sensitivity", []):
        if abs(float(item.get("threshold", -1)) - threshold) < 1e-9:
            return item
    if abs(float(result.get("threshold", -1)) - threshold) < 1e-9:
        return result
    return {}


def _profiles(dataset: Path, samples: Iterable[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for sample in sorted(set(samples)):
        reference = bpmn_to_reference(
            (dataset / "bpmn" / f"{sample}.bpmn").read_text(encoding="utf-8")
        )
        profile = complexity_profile(reference)
        profile["depthTier"] = _depth_tier(int(profile["taskMaxNestingDepth"]))
        profile["eligibleTaskGt10"] = int(profile["tasks"]) > 10
        result[sample] = profile
    return result


def _metric_record(
    run: dict[str, Any],
    profile: dict[str, Any],
    reference: dict[str, Any],
    sound_override: bool | None = None,
) -> dict[str, Any]:
    quality = run.get("quality") or {}
    success = bool(run.get("success"))
    sound = sound_override if sound_override is not None else quality.get("sound")
    token_usage = run.get("tokenUsage") or {}
    record: dict[str, Any] = {
        "sample": str(run["sample"]),
        "repetition": int(run["repetition"]),
        "strategy": str(run["strategy"]),
        "success": success,
        "sound": sound is True,
        "qualityEvaluable": False,
        "totalTokens": float(token_usage.get("totalTokens") or 0),
        "tasks": int(profile["tasks"]),
        "taskMaxNestingDepth": int(profile["taskMaxNestingDepth"]),
        "depthTier": profile["depthTier"],
    }
    for metric in TOKEN_METRICS:
        record[metric] = float(token_usage.get(metric) or 0)
    for metric in QUALITY_METRICS:
        record[metric] = None
    if not success or not run.get("graph") or not quality.get("taskAlignment"):
        return record

    global_result = calculate_global_control_flow_relations(
        graph_to_pme(run["graph"]), reference, quality["taskAlignment"]
    )
    conditional = quality.get("conditionalControlFlowRelations") or quality.get(
        "controlFlowRelations", {}
    )
    task_alignment = quality["taskAlignment"]
    conditional_sensitivity = {
        threshold: _sensitivity_by_threshold(conditional, threshold)
        for threshold in (0.6, 0.7, 0.8)
    }
    global_sensitivity = {
        threshold: _sensitivity_by_threshold(global_result, threshold)
        for threshold in (0.6, 0.7, 0.8)
    }
    record.update({
        "qualityEvaluable": True,
        "pmoTaskNameDiceAt07": quality.get("pmoTaskNameDiceAt07"),
        "gatewayTypeRoleCountDice": quality.get("gatewayTypeRoleF1"),
        "globalRelation": _compact_global(global_result),
    })
    for suffix, threshold in (("06", 0.6), ("07", 0.7), ("08", 0.8)):
        conditional_item = conditional_sensitivity[threshold]
        global_item = global_sensitivity[threshold]
        for label, item in (
            ("conditionalRelation", conditional_item),
            ("globalRelation", global_item),
        ):
            record[f"{label}PrecisionAt{suffix}"] = item.get("precision")
            record[f"{label}RecallAt{suffix}"] = item.get("recall")
            record[f"{label}F1At{suffix}"] = item.get("f1")
        record[f"conditionalRelationCoverageAt{suffix}"] = conditional_item.get(
            "matchedTaskPairCoverage"
        )
    return record


def _flow_means(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[(record["sample"], record["strategy"])].append(record)
    rows: list[dict[str, Any]] = []
    for (sample, strategy), members in sorted(grouped.items()):
        anchor = members[0]
        successful = [row for row in members if row["success"]]
        evaluable = [row for row in members if row["qualityEvaluable"]]
        row: dict[str, Any] = {
            "sample": sample,
            "strategy": strategy,
            "depthTier": anchor["depthTier"],
            "tasks": anchor["tasks"],
            "taskMaxNestingDepth": anchor["taskMaxNestingDepth"],
            "plannedRuns": len(members),
            "successfulRuns": len(successful),
            "qualityEvaluableRuns": len(evaluable),
            "completionRate": len(successful) / len(members),
            "flowCompletionAllThree": float(len(successful) == len(members)),
            "soundnessRatePlanned": sum(row["sound"] for row in members) / len(members),
            "soundnessRateCompleted": (
                sum(row["sound"] for row in successful) / len(successful)
                if successful else None
            ),
            "flowSoundnessAllThree": float(
                len(successful) == len(members)
                and all(member["sound"] for member in members)
            ),
            "meanTotalTokens": _mean(row["totalTokens"] for row in members),
        }
        for metric, mean_metric in zip(TOKEN_METRICS, MEAN_TOKEN_METRICS):
            row[mean_metric] = _mean(member[metric] for member in members)
        for metric in QUALITY_METRICS:
            row[metric] = _mean(member[metric] for member in evaluable)
            row[f"{metric}Conservative"] = _mean(
                0.0 if member[metric] is None else member[metric]
                for member in members
            )
        rows.append(row)
    return rows


def _group_means(flow_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in flow_rows:
        grouped[(row["strategy"], row["depthTier"])].append(row)
    rows: list[dict[str, Any]] = []
    for strategy in STRATEGY_ORDER:
        for tier in DEPTH_ORDER:
            members = grouped.get((strategy, tier), [])
            if not members:
                continue
            row: dict[str, Any] = {
                "strategy": strategy,
                "depthTier": tier,
                "processes": len(members),
                "qualityEvaluableProcesses": sum(
                    int(item["qualityEvaluableRuns"] > 0) for item in members
                ),
                "samples": [item["sample"] for item in members],
                "plannedRuns": sum(item["plannedRuns"] for item in members),
                "successfulRuns": sum(item["successfulRuns"] for item in members),
                "qualityEvaluableRuns": sum(
                    item["qualityEvaluableRuns"] for item in members
                ),
            }
            for metric in (
                "completionRate", "flowCompletionAllThree", "soundnessRatePlanned",
                "soundnessRateCompleted", "flowSoundnessAllThree",
                "meanTotalTokens", *MEAN_TOKEN_METRICS, *QUALITY_METRICS,
            ):
                row[metric] = _mean(item[metric] for item in members)
            rows.append(row)
    return rows


def _paired_deltas(flow_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {(row["sample"], row["strategy"]): row for row in flow_rows}
    result: list[dict[str, Any]] = []
    for sample in sorted({row["sample"] for row in flow_rows}):
        semantic = by_key.get((sample, "semantic"))
        single = by_key.get((sample, "single_segment"))
        if not semantic or not single:
            continue
        row: dict[str, Any] = {
            "sample": sample,
            "depthTier": semantic["depthTier"],
            "tasks": semantic["tasks"],
            "taskMaxNestingDepth": semantic["taskMaxNestingDepth"],
        }
        for metric in (
            "flowCompletionAllThree", "flowSoundnessAllThree",
            "meanTotalTokens", *MEAN_TOKEN_METRICS, *QUALITY_METRICS,
        ):
            left, right = semantic.get(metric), single.get(metric)
            row[metric] = None if left is None or right is None else float(left) - float(right)
        result.append(row)
    return result


def _paired_group_means(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for tier in DEPTH_ORDER:
        members = [row for row in rows if row["depthTier"] == tier]
        row: dict[str, Any] = {
            "depthTier": tier,
            "processes": len(members),
            "samples": [member["sample"] for member in members],
        }
        for metric in (
            "flowCompletionAllThree", "flowSoundnessAllThree",
            "meanTotalTokens", *MEAN_TOKEN_METRICS, *QUALITY_METRICS,
        ):
            row[metric] = _mean(member[metric] for member in members)
        result.append(row)
    return result


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    scalar_rows = [
        {key: value for key, value in row.items() if not isinstance(value, (dict, list))}
        for row in rows
    ]
    fields = list(scalar_rows[0])
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(scalar_rows)


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    oneshot_report = json.loads(ONESHOT_INPUT.read_text(encoding="utf-8"))
    dataset = Path(oneshot_report["dataset"])
    all_samples = [str(sample) for sample in oneshot_report["selectedSamples"]]
    profiles = _profiles(dataset, all_samples)
    selected = sorted(
        sample for sample, profile in profiles.items() if profile["eligibleTaskGt10"]
    )
    references = {
        sample: bpmn_to_reference(
            (dataset / "bpmn" / f"{sample}.bpmn").read_text(encoding="utf-8")
        )
        for sample in selected
    }

    oneshot_records = [
        _metric_record(run, profiles[str(run["sample"])], references[str(run["sample"])])
        for run in oneshot_report["runs"]
        if str(run["sample"]) in references
    ]

    soundness_v4 = json.loads(
        (FORMAL_DATA / "soundness_v4_recomputed.json").read_text(encoding="utf-8")
    )
    sound_map = {
        (str(row["sample"]), int(row["repetition"]), str(row["strategy"])): row.get("v4_sound")
        for row in soundness_v4["rows"]
    }
    formal_records: list[dict[str, Any]] = []
    for batch_number in range(1, 6):
        report = json.loads(
            (FORMAL_DATA / f"batch{batch_number:02d}.json").read_text(
                encoding="utf-8"
            )
        )
        for run in report["runs"]:
            sample = str(run["sample"])
            if sample not in references:
                continue
            key = (sample, int(run["repetition"]), str(run["strategy"]))
            formal_records.append(
                _metric_record(
                    run, profiles[sample], references[sample], sound_map.get(key)
                )
            )

    oneshot_flows = _flow_means(oneshot_records)
    formal_flows = _flow_means(formal_records)
    oneshot_groups = _group_means(oneshot_flows)
    formal_groups = _group_means(formal_flows)
    paired = _paired_deltas(formal_flows)
    paired_groups = _paired_group_means(paired)
    manifest = {
        "schemaVersion": "depth31-descriptive-reanalysis-v4",
        "dataset": str(dataset),
        "selection": "reference Task count > 10",
        "stratification": {
            "low": "taskMaxNestingDepth 0-1",
            "medium": "taskMaxNestingDepth 2",
            "high": "taskMaxNestingDepth >=3",
        },
        "aggregation": (
            "Quality metrics average evaluable repetitions within each process and strategy, "
            "then average process means within each depth stratum. Primary Completion and "
            "Soundness are strict process-level binary metrics: Completion=1 only when all "
            "three repetitions complete; Soundness=1 only when all three repetitions complete "
            "and all three outputs are sound. Run-level rates are retained for audit only. "
            "Paired reliability differences are therefore restricted to -1, 0, or 1."
        ),
        "analysisScope": (
            "Descriptive comparison only. No confidence interval, significance test, "
            "equivalence margin, or population-level superiority claim is produced."
        ),
        "qualityMetrics": {
            "pmoTaskNameDiceAt07": "PMo-compatible greedy Task-name Dice at 0.7",
            "gatewayTypeRoleCountDice": "six-category gateway type-role multiset Dice",
            "conditionalRelationF1At07": "relations only among Task pairs aligned at 0.7",
            "globalRelationF1At07": "all classifiable relations; unmatched Task relations remain FP or FN",
            "thresholdSensitivity": "conditional relation and global relation P/R/F1 at Task-alignment thresholds 0.6, 0.7, and 0.8",
        },
        "tokenMetrics": {
            "meanInputTokens": "mean input tokens per run; includes cached input tokens",
            "meanOutputTokens": "mean output tokens per run; includes reasoning tokens when reported by the provider",
            "meanReasoningTokens": "mean reasoning-token subset of output tokens per run",
            "meanCachedInputTokens": "mean cached-token subset of input tokens per run",
            "meanTotalTokens": "mean total tokens per run, defined by the provider as input plus output tokens",
        },
        "selectedProcesses": len(selected),
        "selectedSamples": selected,
        "tierCounts": {
            tier: sum(profiles[sample]["depthTier"] == tier for sample in selected)
            for tier in DEPTH_ORDER
        },
        "profiles": [{"sample": sample, **profiles[sample]} for sample in selected],
        "inputs": {
            "oneshot": str(ONESHOT_INPUT),
            "formalBatches": [
                str(FORMAL_DATA / f"batch{number:02d}.json")
                for number in range(1, 6)
            ],
            "formalSoundnessOverride": str(
                FORMAL_DATA / "soundness_v4_recomputed.json"
            ),
        },
        "generationCallsMade": 0,
    }
    report = {
        "manifest": manifest,
        "integrity": {
            "oneshotRuns": len(oneshot_records),
            "oneshotExpectedRuns": len(selected) * 3,
            "formalRuns": len(formal_records),
            "formalExpectedRuns": len(selected) * 2 * 3,
            "oneshotUniqueKeys": len({
                (row["sample"], row["repetition"], row["strategy"])
                for row in oneshot_records
            }),
            "formalUniqueKeys": len({
                (row["sample"], row["repetition"], row["strategy"])
                for row in formal_records
            }),
        },
        "oneshot": {
            "runMetrics": oneshot_records,
            "flowMeans": oneshot_flows,
            "depthMeans": oneshot_groups,
        },
        "formal": {
            "runMetrics": formal_records,
            "flowMeans": formal_flows,
            "depthMeans": formal_groups,
            "pairedSemanticMinusSingle": paired,
            "pairedDepthMeans": paired_groups,
        },
    }

    (OUTPUT / "depth31_metrics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUTPUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (PREEXPERIMENT_DATA / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_csv(PREEXPERIMENT_DATA / "oneshot_run_metrics.csv", oneshot_records)
    _write_csv(PREEXPERIMENT_DATA / "oneshot_flow_means.csv", oneshot_flows)
    _write_csv(PREEXPERIMENT_DATA / "oneshot_depth_means.csv", oneshot_groups)
    _write_csv(OUTPUT / "formal_run_metrics.csv", formal_records)
    _write_csv(OUTPUT / "formal_flow_means.csv", formal_flows)
    _write_csv(OUTPUT / "formal_depth_means.csv", formal_groups)
    _write_csv(OUTPUT / "formal_paired_differences.csv", paired)
    _write_csv(OUTPUT / "formal_paired_depth_means.csv", paired_groups)
    assert len(selected) == 31
    assert len(oneshot_records) == len(selected) * 3
    assert len(formal_records) == len(selected) * 2 * 3
    assert report["integrity"]["oneshotUniqueKeys"] == len(oneshot_records)
    assert report["integrity"]["formalUniqueKeys"] == len(formal_records)
    print(f"selected={len(selected)} oneshot_runs={len(oneshot_records)} formal_runs={len(formal_records)}")
    print(OUTPUT.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
