from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any


BACKEND = Path(__file__).resolve().parents[1]
RESULTS = BACKEND / "results"
ARCHIVE = RESULTS / "实验结果归档_20261023"
FORMAL_DATA = ARCHIVE / "02_真实实验" / "数据"
OUTPUT = ARCHIVE / "03_优化实验" / "数据"
SOURCE = FORMAL_DATA / "depth31_metrics.json"
TIERS = ("low", "medium", "high")
STRATEGIES = ("single_segment", "semantic")
EQUIVALENT_ADDITIONS = {"low": 0.2, "medium": 0.8, "high": 1.5}
TARGET_SEMANTIC_UNSOUND_COUNTS = {"low": 0, "medium": 1, "high": 2}
DISPLAY_METRICS = (
    "flowCompletionAllThree",
    "flowSoundnessAllThree",
    "pmoTaskNameDiceAt07",
    "gatewayTypeRoleCountDice",
    "conditionalRelationF1At06",
    "conditionalRelationF1At07",
    "conditionalRelationF1At08",
    "globalRelationF1At06",
    "globalRelationF1At07",
    "globalRelationF1At08",
    "meanInputTokens",
    "meanOutputTokens",
    "meanReasoningTokens",
    "meanCachedInputTokens",
    "meanTotalTokens",
)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields: list[str] = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def load_denominators(selected: set[str]) -> dict[tuple[str, str], dict[str, float]]:
    values: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(
        lambda: {"task": [], "gateway": []}
    )
    for batch_number in range(1, 6):
        path = FORMAL_DATA / f"batch{batch_number:02d}.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        for run in report["runs"]:
            sample = str(run["sample"])
            strategy = str(run["strategy"])
            quality = run.get("quality") or {}
            if sample not in selected or strategy not in STRATEGIES or not run.get("success"):
                continue
            pmo = quality.get("pmoTaskNameDice") or {}
            gateway = quality.get("gatewayTypeRoleCounts") or {}
            task_denominator = pmo.get("denominator")
            gateway_denominator = (
                float(gateway.get("generatedCount", 0))
                + float(gateway.get("referenceCount", 0))
            )
            if task_denominator:
                values[(sample, strategy)]["task"].append(float(task_denominator))
            if gateway_denominator:
                values[(sample, strategy)]["gateway"].append(gateway_denominator)
    return {
        key: {
            "meanTaskDiceDenominator": fmean(item["task"]) if item["task"] else 0.0,
            "meanGatewayDiceDenominator": fmean(item["gateway"]) if item["gateway"] else 0.0,
        }
        for key, item in values.items()
    }


def improve_dice(observed: float | None, equivalent_addition: float, denominator: float) -> float | None:
    if observed is None:
        return None
    if denominator <= 0:
        return float(observed)
    return min(1.0, float(observed) + 2.0 * equivalent_addition / denominator)


def mean(values: list[float | None]) -> float | None:
    usable = [float(value) for value in values if value is not None]
    return fmean(usable) if usable else None


def select_semantic_unsound_samples(
    source_rows: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """Select the least reliable observed semantic flows for the scenario.

    Selection is deterministic and restricted to flows that already failed the
    strict observed all-three Soundness rule. Lower planned-run Soundness is
    ranked first; ties use lower completed-output Soundness and then sample ID.
    """
    selected: dict[str, list[str]] = {}
    semantic_rows = [row for row in source_rows if row["strategy"] == "semantic"]
    for tier in TIERS:
        target = TARGET_SEMANTIC_UNSOUND_COUNTS[tier]
        candidates = [
            row for row in semantic_rows
            if row["depthTier"] == tier and row["flowSoundnessAllThree"] == 0.0
        ]
        candidates.sort(key=lambda row: (
            float(row["soundnessRatePlanned"]),
            float(row["soundnessRateCompleted"] or 0.0),
            str(row["sample"]),
        ))
        if len(candidates) < target:
            raise RuntimeError(
                f"Tier {tier} needs {target} observed unsound candidates, "
                f"but only {len(candidates)} are available."
            )
        selected[tier] = [str(row["sample"]) for row in candidates[:target]]
    return selected


def main() -> int:
    report = json.loads(SOURCE.read_text(encoding="utf-8"))
    if report["manifest"]["schemaVersion"] != "depth31-descriptive-reanalysis-v4":
        raise RuntimeError("Run recompute_depth31_metrics.py before building the visualization-only dataset.")

    source_rows = [
        row for row in report["formal"]["flowMeans"] if row["strategy"] in STRATEGIES
    ]
    selected = {str(row["sample"]) for row in source_rows}
    selected_unsound_by_tier = select_semantic_unsound_samples(source_rows)
    selected_unsound = {
        sample for samples in selected_unsound_by_tier.values() for sample in samples
    }
    denominators = load_denominators(selected)
    rows: list[dict[str, Any]] = []

    for source in source_rows:
        strategy = str(source["strategy"])
        tier = str(source["depthTier"])
        sample = str(source["sample"])
        denom = denominators.get((sample, strategy), {})
        row: dict[str, Any] = {
            "datasetLabel": "SYNTHETIC_IDEALIZED_VISUALIZATION_ONLY_V1",
            "isSyntheticScenario": True,
            "notForEmpiricalReporting": True,
            "sample": sample,
            "strategy": strategy,
            "depthTier": tier,
            "tasks": source["tasks"],
            "taskMaxNestingDepth": source["taskMaxNestingDepth"],
            "plannedRuns": source["plannedRuns"],
            "successfulRunsObserved": source["successfulRuns"],
            "qualityEvaluableRunsObserved": source["qualityEvaluableRuns"],
            "runCompletionRateObserved": source["completionRate"],
            "flowCompletionAllThreeObserved": source["flowCompletionAllThree"],
            "soundnessRatePlannedObserved": source["soundnessRatePlanned"],
            "soundnessRateCompletedObserved": source["soundnessRateCompleted"],
            "flowSoundnessAllThreeObserved": source["flowSoundnessAllThree"],
            "pmoTaskNameDiceAt07Observed": source["pmoTaskNameDiceAt07"],
            "gatewayTypeRoleCountDiceObserved": source["gatewayTypeRoleCountDice"],
            "conditionalRelationF1At07Observed": source["conditionalRelationF1At07"],
            "globalRelationF1At07Observed": source["globalRelationF1At07"],
            "meanTotalTokensObserved": source["meanTotalTokens"],
            "meanInputTokensObserved": source["meanInputTokens"],
            "meanOutputTokensObserved": source["meanOutputTokens"],
            "meanReasoningTokensObserved": source["meanReasoningTokens"],
            "meanCachedInputTokensObserved": source["meanCachedInputTokens"],
            "meanTaskDiceDenominator": denom.get("meanTaskDiceDenominator"),
            "meanGatewayDiceDenominator": denom.get("meanGatewayDiceDenominator"),
            "equivalentMatchedTaskAddition": 0.0,
            "equivalentMatchedGatewayAddition": 0.0,
            "valueProvenance": "observed_unchanged",
        }

        if strategy == "semantic":
            q = EQUIVALENT_ADDITIONS[tier]
            row.update({
                "flowCompletionAllThree": 1.0,
                "flowSoundnessAllThree": float(sample not in selected_unsound),
                "pmoTaskNameDiceAt07": improve_dice(
                    source["pmoTaskNameDiceAt07"], q, float(denom.get("meanTaskDiceDenominator") or 0)
                ),
                "gatewayTypeRoleCountDice": improve_dice(
                    source["gatewayTypeRoleCountDice"], q, float(denom.get("meanGatewayDiceDenominator") or 0)
                ),
                "conditionalRelationF1At07": source["conditionalRelationF1At07"],
                "globalRelationF1At07": source["globalRelationF1At07"],
                "meanTotalTokens": source["meanTotalTokens"],
                "meanInputTokens": source["meanInputTokens"],
                "meanOutputTokens": source["meanOutputTokens"],
                "meanReasoningTokens": source["meanReasoningTokens"],
                "meanCachedInputTokens": source["meanCachedInputTokens"],
                "equivalentMatchedTaskAddition": q,
                "equivalentMatchedGatewayAddition": q,
                "valueProvenance": "idealized_semantic_completion_soundness_and_count_dice",
            })
            for metric in (
                "conditionalRelationF1At06",
                "conditionalRelationF1At08",
                "globalRelationF1At06",
                "globalRelationF1At08",
            ):
                row[metric] = source[metric]
        else:
            for metric in DISPLAY_METRICS:
                row[metric] = source[metric]
        rows.append(row)

    by_key = {(row["sample"], row["strategy"]): row for row in rows}
    paired: list[dict[str, Any]] = []
    for sample in sorted(selected):
        single = by_key[(sample, "single_segment")]
        semantic = by_key[(sample, "semantic")]
        item: dict[str, Any] = {
            "datasetLabel": "SYNTHETIC_IDEALIZED_VISUALIZATION_ONLY_V1",
            "isSyntheticScenario": True,
            "notForEmpiricalReporting": True,
            "sample": sample,
            "depthTier": semantic["depthTier"],
            "tasks": semantic["tasks"],
            "taskMaxNestingDepth": semantic["taskMaxNestingDepth"],
        }
        for metric in DISPLAY_METRICS:
            left, right = semantic.get(metric), single.get(metric)
            item[f"single_{metric}"] = right
            item[f"semanticScenario_{metric}"] = left
            item[f"delta_{metric}"] = None if left is None or right is None else float(left) - float(right)
        paired.append(item)

    summaries: list[dict[str, Any]] = []
    for strategy in STRATEGIES:
        for tier in TIERS:
            members = [row for row in rows if row["strategy"] == strategy and row["depthTier"] == tier]
            summary: dict[str, Any] = {
                "datasetLabel": "SYNTHETIC_IDEALIZED_VISUALIZATION_ONLY_V1",
                "isSyntheticScenario": True,
                "notForEmpiricalReporting": True,
                "strategy": strategy,
                "depthTier": tier,
                "processes": len(members),
            }
            for metric in DISPLAY_METRICS:
                summary[metric] = mean([member.get(metric) for member in members])
            summaries.append(summary)

    manifest = {
        "schemaVersion": "depth31-idealized-visualization-only-v2",
        "datasetLabel": "SYNTHETIC_IDEALIZED_VISUALIZATION_ONLY_V1",
        "isSyntheticScenario": True,
        "notForEmpiricalReporting": True,
        "warning": "These values are a hypothetical visualization preview, not experimental observations and not valid evidence for the paper.",
        "createdAtUtc": datetime.now(timezone.utc).isoformat(),
        "baseObservedDataset": str(SOURCE),
        "selection": report["manifest"]["selection"],
        "stratification": report["manifest"]["stratification"],
        "metricsIncluded": list(DISPLAY_METRICS),
        "metricsExcluded": ["taskSemanticAlignmentScore", "Task P/R/F1"],
        "rules": {
            "singleSegment": "All displayed values remain observed.",
            "semanticCompletion": "Set strict process-level all-three Completion to 1 for visualization only.",
            "semanticSoundness": (
                "Set low/medium/high semantic unsound process counts to 0/1/2. "
                "Within each tier, retain the observed strict-unsound processes with "
                "the lowest planned-run Soundness; break ties by completed-output "
                "Soundness and sample ID."
            ),
            "semanticUnsoundTargetsByTier": TARGET_SEMANTIC_UNSOUND_COUNTS,
            "semanticUnsoundSamplesByTier": selected_unsound_by_tier,
            "semanticTaskDice": "Start from observed semantic PMo Task-name Dice@0.7 and add 2q/(mean generated+reference Task count), capped at 1.",
            "semanticGatewayDice": "Start from observed semantic Gateway type-role count Dice and add 2q/(mean generated+reference Gateway role count), capped at 1.",
            "equivalentAdditionsByTier": EQUIVALENT_ADDITIONS,
            "relationsAndTokens": "Keep observed semantic values unchanged.",
        },
        "rowCounts": {"flowMetrics": len(rows), "pairedDifferences": len(paired), "tierSummary": len(summaries)},
    }

    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(OUTPUT / "flow_metrics.csv", rows)
    write_csv(OUTPUT / "paired_differences.csv", paired)
    write_csv(OUTPUT / "tier_summary.csv", summaries)
    assert len(rows) == 62
    assert len(paired) == 31
    assert all(row["flowCompletionAllThree"] == 1.0 for row in rows if row["strategy"] == "semantic")
    for tier, expected in TARGET_SEMANTIC_UNSOUND_COUNTS.items():
        actual = sum(
            row["strategy"] == "semantic"
            and row["depthTier"] == tier
            and row["flowSoundnessAllThree"] == 0.0
            for row in rows
        )
        assert actual == expected
    assert all("taskSemanticAlignmentScore" not in row for row in rows)
    print(OUTPUT.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
