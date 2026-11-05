from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any


BACKEND = Path(__file__).resolve().parents[1]
RESULTS = BACKEND / "results"
INPUT = RESULTS / "depth31_reanalysis_20261020" / "depth31_metrics.json"
FORMAL_ROOT = RESULTS / "pmo_full_50_seed20261017"
OUTPUT_JSON = RESULTS / "depth31_reanalysis_20261020" / "global_relation_diagnosis.json"
OUTPUT_CSV = RESULTS / "depth31_reanalysis_20261020" / "global_relation_diagnosis_by_group.csv"
OUTPUT_MD = RESULTS / "depth31_reanalysis_20261020" / "global_relation_diagnosis.md"


def mean(rows: list[dict[str, Any]], field: str) -> float | None:
    values = [float(row[field]) for row in rows if row.get(field) is not None]
    return fmean(values) if values else None


def pearson(rows: list[dict[str, Any]], left: str, right: str) -> float | None:
    pairs = [
        (float(row[left]), float(row[right]))
        for row in rows
        if row.get(left) is not None and row.get(right) is not None
    ]
    if len(pairs) < 2:
        return None
    xs, ys = zip(*pairs)
    mx, my = fmean(xs), fmean(ys)
    numerator = sum((x - mx) * (y - my) for x, y in pairs)
    denominator = math.sqrt(
        sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)
    )
    return numerator / denominator if denominator else None


def task_protocol_differences(selected: set[str]) -> dict[str, Any]:
    differences: list[float] = []
    rows_compared = 0
    for batch_number in range(1, 6):
        path = FORMAL_ROOT / f"batch{batch_number:02d}" / f"batch{batch_number:02d}.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        for run in report["runs"]:
            if str(run["sample"]) not in selected or not run.get("success"):
                continue
            quality = run.get("quality") or {}
            pmo = quality.get("pmoTaskNameDiceAt07")
            alignment = quality.get("taskAlignment") or {}
            threshold_f1 = alignment.get("f1")
            if pmo is None or threshold_f1 is None:
                continue
            rows_compared += 1
            differences.append(abs(float(pmo) - float(threshold_f1)))
    return {
        "runsCompared": rows_compared,
        "maximumAbsoluteDifference": max(differences) if differences else None,
        "nonzeroDifferenceRunsAt1e12": sum(value > 1e-12 for value in differences),
    }


def main() -> int:
    report = json.loads(INPUT.read_text(encoding="utf-8"))
    run_rows = [row for row in report["formal"]["runMetrics"] if row["qualityEvaluable"]]
    selected = {str(row["sample"]) for row in run_rows}
    for row in run_rows:
        global_result = row.get("globalRelation") or {}
        matched = int(global_result.get("matchedTaskCount") or 0)
        reference = int(global_result.get("referenceTaskCount") or 0)
        generated = int(global_result.get("generatedTaskCount") or 0)
        row["matchedReferenceTaskFractionAt07"] = matched / reference if reference else 1.0
        row["matchedGeneratedTaskFractionAt07"] = matched / generated if generated else 1.0

    group_rows: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in run_rows:
        grouped[(row["strategy"], row["depthTier"])].append(row)
    for (strategy, tier), members in sorted(grouped.items()):
        group_rows.append({
            "strategy": strategy,
            "depthTier": tier,
            "evaluableRuns": len(members),
            "meanPmoTaskNameDiceAt07": mean(members, "pmoTaskNameDiceAt07"),
            "meanMatchedReferenceTaskFractionAt07": mean(members, "matchedReferenceTaskFractionAt07"),
            "meanMatchedTaskPairCoverageAt07": mean(members, "conditionalRelationCoverageAt07"),
            "meanConditionalRelationF1At07": mean(members, "conditionalRelationF1At07"),
            "meanGlobalRelationPrecisionAt07": mean(members, "globalRelationPrecisionAt07"),
            "meanGlobalRelationRecallAt07": mean(members, "globalRelationRecallAt07"),
            "meanGlobalRelationF1At07": mean(members, "globalRelationF1At07"),
        })

    correlations = {
        "globalF1_vs_pmoTaskDice": pearson(run_rows, "globalRelationF1At07", "pmoTaskNameDiceAt07"),
        "globalF1_vs_matchedReferenceTaskFraction": pearson(
            run_rows, "globalRelationF1At07", "matchedReferenceTaskFractionAt07"
        ),
        "globalF1_vs_matchedTaskPairCoverage": pearson(
            run_rows, "globalRelationF1At07", "conditionalRelationCoverageAt07"
        ),
        "globalF1_vs_conditionalRelationF1": pearson(
            run_rows, "globalRelationF1At07", "conditionalRelationF1At07"
        ),
    }
    protocol_check = task_protocol_differences(selected)
    result = {
        "purpose": "diagnostic only; does not define a new paper metric",
        "input": str(INPUT),
        "implementationConclusion": (
            "The implementation intentionally scores all classifiable generated and reference Task-pair relations. "
            "A true positive requires both endpoints to be aligned and the relation label to match."
        ),
        "evaluableFormalRuns": len(run_rows),
        "correlations": correlations,
        "taskProtocolCheck": protocol_check,
        "groupMeans": group_rows,
    }
    OUTPUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    with OUTPUT_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(group_rows[0]))
        writer.writeheader()
        writer.writerows(group_rows)

    def fmt(value: float | None) -> str:
        return "NA" if value is None else f"{value:.3f}"

    lines = [
        "# Global Relation F1 diagnosis",
        "",
        "This is a diagnostic audit, not a new evaluation metric.",
        "",
        "## Group means",
        "",
        "| Strategy | Tier | Runs | PMo Dice@0.7 | Matched reference Tasks | Matched Task-pair coverage | Conditional F1 | Global P | Global R | Global F1 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in group_rows:
        lines.append(
            f"| {row['strategy']} | {row['depthTier']} | {row['evaluableRuns']} | "
            f"{fmt(row['meanPmoTaskNameDiceAt07'])} | {fmt(row['meanMatchedReferenceTaskFractionAt07'])} | "
            f"{fmt(row['meanMatchedTaskPairCoverageAt07'])} | {fmt(row['meanConditionalRelationF1At07'])} | "
            f"{fmt(row['meanGlobalRelationPrecisionAt07'])} | {fmt(row['meanGlobalRelationRecallAt07'])} | "
            f"{fmt(row['meanGlobalRelationF1At07'])} |"
        )
    lines.extend([
        "",
        "## Correlations across evaluable formal runs",
        "",
        f"- Global F1 vs PMo Task-name Dice@0.7: r={fmt(correlations['globalF1_vs_pmoTaskDice'])}.",
        f"- Global F1 vs matched reference-Task fraction: r={fmt(correlations['globalF1_vs_matchedReferenceTaskFraction'])}.",
        f"- Global F1 vs matched Task-pair coverage: r={fmt(correlations['globalF1_vs_matchedTaskPairCoverage'])}.",
        f"- Global F1 vs Conditional Relation F1: r={fmt(correlations['globalF1_vs_conditionalRelationF1'])}.",
        "",
        "## Protocol audit",
        "",
        f"PMo Dice and threshold-alignment F1 were both present in {protocol_check['runsCompared']} runs. "
        f"Their maximum absolute difference was {protocol_check['maximumAbsoluteDifference']:.3g}; "
        f"{protocol_check['nonzeroDifferenceRunsAt1e12']} runs differed by more than 1e-12.",
        "",
        "Global Relation does not directly reuse the PMo greedy pairs. It uses the maximum-cardinality, then maximum-similarity Task alignment at the same 0.7 threshold. In this dataset the two Task-level scores are numerically identical, but their pairing protocols remain conceptually distinct.",
    ])
    OUTPUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(OUTPUT_MD.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
