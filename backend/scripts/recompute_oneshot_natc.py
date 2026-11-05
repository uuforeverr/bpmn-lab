from __future__ import annotations

import argparse
import json
import random
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path
from statistics import mean
from typing import Any, Callable

from app.evaluation import (
    bpmn_to_reference,
    calculate_task_nesting_complexity,
    complexity_profile,
    natc_tertile_thresholds,
    natc_tier,
    summarize_runs,
)


METRICS: list[tuple[str, Callable[[dict[str, Any]], float]]] = [
    ("completionRate", lambda run: float(bool(run.get("success")))),
    ("soundnessRate", lambda run: float(run.get("quality", {}).get("sound") is True)),
    ("taskF1At07", lambda run: float(run["quality"]["taskF1"])),
    (
        "taskSemanticAlignmentScore",
        lambda run: float(run["quality"]["taskSemanticAlignmentScore"]),
    ),
    (
        "controlFlowRelationF1At07",
        lambda run: float(run["quality"]["controlFlowRelationF1"]),
    ),
    (
        "evaluableRelationCoverageAt07",
        lambda run: float(run["quality"]["controlFlowRelationMatchedTaskPairCoverage"]),
    ),
    ("gatewayTypeRoleF1", lambda run: float(run["quality"]["gatewayTypeRoleF1"])),
    ("totalTokens", lambda run: float(run["tokenUsage"]["totalTokens"])),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reclassify a stored one-shot report with NATC; makes no model calls."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--bootstrap-samples", type=int, default=20_000)
    parser.add_argument("--seed", type=int, default=20261020)
    return parser.parse_args()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def bootstrap_interval(
    values: list[float], samples: int, rng: random.Random,
) -> tuple[float, float]:
    if not values:
        return float("nan"), float("nan")
    size = len(values)
    estimates = sorted(
        mean(rng.choice(values) for _ in range(size))
        for _ in range(samples)
    )
    lower = estimates[int(0.025 * samples)]
    upper = estimates[min(samples - 1, int(0.975 * samples))]
    return lower, upper


def process_means(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        grouped[str(run["sample"])].append(run)
    rows: list[dict[str, Any]] = []
    for sample, sample_runs in sorted(grouped.items()):
        anchor = sample_runs[0]
        row: dict[str, Any] = {
            "sample": sample,
            "runs": len(sample_runs),
            "complexity": anchor["complexity"],
        }
        for name, getter in METRICS:
            row[name] = mean(getter(run) for run in sample_runs)
        rows.append(row)
    return rows


def grouped_statistics(
    rows: list[dict[str, Any]], tier_field: str, bootstrap_samples: int, seed: int,
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    result: list[dict[str, Any]] = []
    for tier in ("low", "medium", "high"):
        members = [row for row in rows if row["complexity"][tier_field] == tier]
        group: dict[str, Any] = {
            "tier": tier,
            "processes": len(members),
            "samples": [row["sample"] for row in members],
        }
        for name, _ in METRICS:
            values = [float(row[name]) for row in members]
            lower, upper = bootstrap_interval(values, bootstrap_samples, rng)
            group[name] = mean(values)
            group[f"{name}Ci95"] = [lower, upper]
        result.append(group)
    return result


def high_low_differences(
    rows: list[dict[str, Any]], tier_field: str, bootstrap_samples: int, seed: int,
) -> dict[str, dict[str, Any]]:
    rng = random.Random(seed)
    low = [row for row in rows if row["complexity"][tier_field] == "low"]
    high = [row for row in rows if row["complexity"][tier_field] == "high"]
    result: dict[str, dict[str, Any]] = {}
    for metric_name, _ in METRICS:
        observed = mean(row[metric_name] for row in high) - mean(
            row[metric_name] for row in low
        )
        estimates = sorted(
            mean(rng.choice(high)[metric_name] for _ in range(len(high)))
            - mean(rng.choice(low)[metric_name] for _ in range(len(low)))
            for _ in range(bootstrap_samples)
        )
        result[metric_name] = {
            "highMinusLow": observed,
            "ci95": [
                estimates[int(0.025 * bootstrap_samples)],
                estimates[min(bootstrap_samples - 1, int(0.975 * bootstrap_samples))],
            ],
        }
    return result


def fmt(value: float) -> str:
    return f"{value:.3f}"


def make_table(groups: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| Tier | Processes | Completion | Soundness | Task F1@0.7 | Task semantic alignment | Relation F1@0.7 | Evaluable relation coverage@0.7 | Mean tokens |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for group in groups:
        lines.append(
            "| {tier} | {processes} | {completion} | {soundness} | {task_f1} | "
            "{semantic} | {relation} | {coverage} | {tokens:,.0f} |".format(
                tier=group["tier"].capitalize(),
                processes=group["processes"],
                completion=fmt(group["completionRate"]),
                soundness=fmt(group["soundnessRate"]),
                task_f1=fmt(group["taskF1At07"]),
                semantic=fmt(group["taskSemanticAlignmentScore"]),
                relation=fmt(group["controlFlowRelationF1At07"]),
                coverage=fmt(group["evaluableRelationCoverageAt07"]),
                tokens=group["totalTokens"],
            )
        )
    return lines


def main() -> int:
    args = parse_args()
    original = json.loads(args.input.read_text(encoding="utf-8"))
    report = deepcopy(original)
    runs = report["runs"]
    dataset = Path(report["dataset"])
    samples = sorted({str(run["sample"]) for run in runs})

    profiles: dict[str, dict[str, Any]] = {}
    details: dict[str, dict[str, Any]] = {}
    for sample in samples:
        reference_path = dataset / "bpmn" / f"{sample}.bpmn"
        reference = bpmn_to_reference(reference_path.read_text(encoding="utf-8"))
        profiles[sample] = complexity_profile(reference)
        details[sample] = calculate_task_nesting_complexity(reference)

    natc1_thresholds = natc_tertile_thresholds(profiles.values(), "natc1")
    natc2_thresholds = natc_tertile_thresholds(profiles.values(), "natc2")
    for sample, profile in profiles.items():
        profile["natc1Tier"] = natc_tier(float(profile["natc1"]), natc1_thresholds)
        profile["natc2Tier"] = natc_tier(float(profile["natc2"]), natc2_thresholds)
    for run in runs:
        run["complexity"] = deepcopy(profiles[str(run["sample"])])

    report["summary"] = summarize_runs(runs)
    report["complexityRecomputation"] = {
        "source": str(args.input.resolve()),
        "generationCallsMade": 0,
        "protocol": "task_control_context_nesting_v1",
        "primaryScore": "NATC1=sum(1+taskDepth)",
        "sensitivityScore": "NATC2=sum((1+taskDepth)^2)",
        "natc1InclusiveTertileThresholds": list(natc1_thresholds),
        "natc2InclusiveTertileThresholds": list(natc2_thresholds),
        "aggregation": "mean three repetitions per process, then mean process values per tier",
    }

    manifest = {
        "schemaVersion": "natc-profile-v1",
        "dataset": str(dataset),
        "processes": len(samples),
        "natc1InclusiveTertileThresholds": list(natc1_thresholds),
        "natc2InclusiveTertileThresholds": list(natc2_thresholds),
        "tierCounts": {
            "natc1": dict(Counter(profile["natc1Tier"] for profile in profiles.values())),
            "natc2": dict(Counter(profile["natc2Tier"] for profile in profiles.values())),
        },
        "profiles": [
            {
                "sample": sample,
                **profiles[sample],
                "taskDepths": details[sample]["taskDepths"],
                "splitRegions": details[sample]["regions"],
            }
            for sample in samples
        ],
    }

    rows = process_means(runs)
    natc1_groups = grouped_statistics(
        rows, "natc1Tier", args.bootstrap_samples, args.seed,
    )
    natc2_groups = grouped_statistics(
        rows, "natc2Tier", args.bootstrap_samples, args.seed + 1,
    )
    natc1_differences = high_low_differences(
        rows, "natc1Tier", args.bootstrap_samples, args.seed + 2,
    )
    natc2_differences = high_low_differences(
        rows, "natc2Tier", args.bootstrap_samples, args.seed + 3,
    )

    try:
        from scipy.stats import spearmanr  # type: ignore
    except ImportError:
        correlations: list[dict[str, Any]] = []
    else:
        correlations = []
        for score in ("natc1", "natc2"):
            for metric_name, _ in METRICS:
                score_values = [row["complexity"][score] for row in rows]
                metric_values = [row[metric_name] for row in rows]
                if len(set(score_values)) < 2 or len(set(metric_values)) < 2:
                    continue
                statistic = spearmanr(
                    score_values,
                    metric_values,
                )
                correlations.append({
                    "score": score,
                    "metric": metric_name,
                    "rho": float(statistic.statistic),
                    "pValue": float(statistic.pvalue),
                })

    changed = [
        row["sample"] for row in rows
        if row["complexity"]["natc1Tier"] != row["complexity"]["natc2Tier"]
    ]
    analysis = {
        "aggregation": report["complexityRecomputation"]["aggregation"],
        "bootstrapSamples": args.bootstrap_samples,
        "natc1Groups": natc1_groups,
        "natc2Groups": natc2_groups,
        "natc1HighMinusLow": natc1_differences,
        "natc2HighMinusLow": natc2_differences,
        "spearman": correlations,
        "tierChangedUnderNatc2": changed,
    }
    report["natcAnalysis"] = analysis

    lines = [
        "# One-shot 150-run results reclassified by NATC",
        "",
        "This is an offline reclassification of stored results. It made no model calls and did not recompute or modify quality scores or token usage.",
        "",
        "## Frozen complexity definition",
        "",
        "- NATC1 = sum over reference Tasks of `(1 + Task nesting depth)`.",
        "- NATC2 = sum over reference Tasks of `(1 + Task nesting depth)^2`; it is a depth-emphasised sensitivity analysis.",
        f"- NATC1 inclusive tertile thresholds: {natc1_thresholds[0]:.3f}, {natc1_thresholds[1]:.3f}.",
        f"- NATC2 inclusive tertile thresholds: {natc2_thresholds[0]:.3f}, {natc2_thresholds[1]:.3f}.",
        "- Aggregation: average the three repetitions of each process first, then average process means within each tier.",
        "",
        "## Primary NATC1 stratification",
        "",
        *make_table(natc1_groups),
        "",
        "## Depth-emphasised NATC2 sensitivity stratification",
        "",
        *make_table(natc2_groups),
        "",
        "## High-minus-low process-level differences",
        "",
        "| Metric | NATC1 difference [95% bootstrap CI] | NATC2 difference [95% bootstrap CI] |",
        "|---|---:|---:|",
    ]
    for metric_name, _ in METRICS:
        natc1_row = natc1_differences[metric_name]
        natc2_row = natc2_differences[metric_name]
        lines.append(
            "| {metric} | {d1:+.3f} [{l1:+.3f}, {u1:+.3f}] | "
            "{d2:+.3f} [{l2:+.3f}, {u2:+.3f}] |".format(
                metric=metric_name,
                d1=natc1_row["highMinusLow"],
                l1=natc1_row["ci95"][0],
                u1=natc1_row["ci95"][1],
                d2=natc2_row["highMinusLow"],
                l2=natc2_row["ci95"][0],
                u2=natc2_row["ci95"][1],
            )
        )
    lines.extend([
        "",
        "## Membership sensitivity",
        "",
        f"{len(changed)} of 50 processes change tier between NATC1 and NATC2: "
        + (", ".join(changed) if changed else "none")
        + ".",
        "",
        "## Process membership",
        "",
    ])
    for tier in ("low", "medium", "high"):
        members = [
            row["sample"] for row in rows if row["complexity"]["natc1Tier"] == tier
        ]
        lines.append(f"- NATC1 {tier}: {', '.join(members)}")

    atomic_write(args.output, json.dumps(report, ensure_ascii=False, indent=2))
    atomic_write(args.profiles, json.dumps(manifest, ensure_ascii=False, indent=2))
    atomic_write(args.summary, "\n".join(lines) + "\n")
    print(f"report={args.output.resolve()}")
    print(f"profiles={args.profiles.resolve()}")
    print(f"summary={args.summary.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
