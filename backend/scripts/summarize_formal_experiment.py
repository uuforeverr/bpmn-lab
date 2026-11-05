from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable


EXPERIMENT_ID = "pmo_full_50_seed20261017"
SIZE_ORDER = ("S", "M", "L", "XL")
STRATEGIES = ("semantic", "single_segment")
THRESHOLDS = (0.6, 0.7, 0.8)


def _mean(values: Iterable[float | int | None]) -> float | None:
    usable = [float(value) for value in values if value is not None]
    return fmean(usable) if usable else None


def _threshold_row(rows: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    return next(row for row in rows if abs(float(row["threshold"]) - threshold) < 1e-9)


def _flatten_flow_mean(flow: dict[str, Any]) -> dict[str, Any]:
    quality = flow["qualityMeans"]
    complexity = flow["complexity"]
    row: dict[str, Any] = {
        "sample": flow["sample"],
        "strategy": flow["strategy"],
        "planned_runs": flow["plannedRuns"],
        "successful_runs": flow["successfulRuns"],
        "completion_rate": flow["completionRate"],
        "size_bucket": flow["sizeBucket"],
        "tasks": complexity["tasks"],
        "events": complexity["events"],
        "gateways": complexity["gateways"],
        "sequence_flows": complexity["sequenceFlows"],
        "total_nodes": complexity["totalNodes"],
        "nesting_depth": complexity["nestingDepth"],
        "cfc": complexity["controlFlowComplexity"],
        "task_dice_07": quality["pmoTaskNameDiceAt07"],
        "task_alignment": quality["taskSemanticAlignmentScore"],
        "gateway_precision": quality["gatewayTypeRolePrecision"],
        "gateway_recall": quality["gatewayTypeRoleRecall"],
        "gateway_f1": quality["gatewayTypeRoleF1"],
        "gateway_abs_error": quality["gatewayTypeRoleAbsoluteCountError"],
        "mean_tokens": flow["meanEndToEndTokens"],
        "mean_seconds": flow["meanDurationSeconds"],
    }
    for threshold in THRESHOLDS:
        suffix = str(threshold).replace(".", "")
        task = _threshold_row(flow["taskThresholdMeans"], threshold)
        relation = _threshold_row(flow["controlFlowRelationThresholdMeans"], threshold)
        row.update({
            f"task_precision_{suffix}": task["precision"],
            f"task_recall_{suffix}": task["recall"],
            f"task_f1_{suffix}": task["f1"],
            f"relation_precision_{suffix}": relation["precision"],
            f"relation_recall_{suffix}": relation["recall"],
            f"relation_f1_{suffix}": relation["f1"],
            f"pair_coverage_{suffix}": relation["matchedTaskPairCoverage"],
        })
    return row


def _aggregate_flows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    metric_fields = [
        key for key in rows[0]
        if key not in {
            "sample", "strategy", "size_bucket", "planned_runs", "successful_runs",
            "tasks", "events", "gateways", "sequence_flows", "total_nodes",
            "nesting_depth", "cfc",
        }
    ]
    return {
        "flowCount": len(rows),
        "plannedRuns": sum(int(row["planned_runs"]) for row in rows),
        "successfulRuns": sum(int(row["successful_runs"]) for row in rows),
        **{field: _mean(row[field] for row in rows) for field in metric_fields},
    }


def _paired_differences(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {(row["sample"], row["strategy"]): row for row in rows}
    metric_fields = [
        key for key in rows[0]
        if key not in {
            "sample", "strategy", "size_bucket", "planned_runs", "successful_runs",
            "completion_rate", "tasks", "events", "gateways", "sequence_flows",
            "total_nodes", "nesting_depth", "cfc",
        }
    ]
    paired: list[dict[str, Any]] = []
    for sample in sorted({row["sample"] for row in rows}):
        semantic = by_key[(sample, "semantic")]
        single = by_key[(sample, "single_segment")]
        paired.append({
            "sample": sample,
            "size_bucket": semantic["size_bucket"],
            **{
                field: (
                    None
                    if semantic[field] is None or single[field] is None
                    else float(semantic[field]) - float(single[field])
                )
                for field in metric_fields
            },
        })
    return paired


def _fmt(value: float | None, digits: int = 3) -> str:
    return "NA" if value is None else f"{value:.{digits}f}"


def main() -> int:
    backend = Path(__file__).resolve().parents[1]
    root = backend / "results" / EXPERIMENT_ID
    output = root / "combined"
    output.mkdir(parents=True, exist_ok=True)

    reports: list[dict[str, Any]] = []
    all_runs: list[dict[str, Any]] = []
    flow_means: list[dict[str, Any]] = []
    samples_seen: set[str] = set()
    validation: list[dict[str, Any]] = []

    for batch_number in range(1, 6):
        path = root / f"batch{batch_number:02d}" / f"batch{batch_number:02d}.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        reports.append(report)
        runs = report["runs"]
        schedule = report["schedule"]
        run_keys = {(row["sample"], row["repetition"], row["strategy"]) for row in runs}
        schedule_keys = {(row["sample"], row["repetition"], row["strategy"]) for row in schedule}
        selected = set(report["selectedSamples"])
        validation.append({
            "batch": batch_number,
            "plannedRuns": len(schedule),
            "recordedRuns": len(runs),
            "uniqueRunKeys": len(run_keys),
            "missingRunKeys": len(schedule_keys - run_keys),
            "extraRunKeys": len(run_keys - schedule_keys),
            "selectedSamples": sorted(selected),
        })
        if len(schedule) != 60 or len(runs) != 60 or run_keys != schedule_keys:
            raise RuntimeError(f"Batch {batch_number:02d} failed completeness validation")
        if samples_seen & selected:
            raise RuntimeError(f"Batch {batch_number:02d} duplicates samples from an earlier batch")
        samples_seen |= selected
        all_runs.extend(runs)
        flow_means.extend(_flatten_flow_mean(row) for row in report["summary"]["flowMeans"])

    if len(samples_seen) != 50 or len(all_runs) != 300 or len(flow_means) != 100:
        raise RuntimeError("Combined experiment cardinality is invalid")

    flow_fields = list(flow_means[0])
    with (output / "flow_means.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=flow_fields)
        writer.writeheader()
        writer.writerows(sorted(flow_means, key=lambda row: (row["sample"], row["strategy"])))

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in flow_means:
        grouped[(row["size_bucket"], row["strategy"])].append(row)
    complexity_groups: list[dict[str, Any]] = []
    for size in SIZE_ORDER:
        for strategy in STRATEGIES:
            aggregate = _aggregate_flows(grouped[(size, strategy)])
            complexity_groups.append({"size_bucket": size, "strategy": strategy, **aggregate})
    group_fields = list(complexity_groups[0])
    with (output / "complexity_group_means.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=group_fields)
        writer.writeheader()
        writer.writerows(complexity_groups)

    paired = _paired_differences(flow_means)
    paired_by_size: dict[str, dict[str, Any]] = {}
    paired_metrics = [key for key in paired[0] if key not in {"sample", "size_bucket"}]
    for size in (*SIZE_ORDER, "ALL"):
        rows = paired if size == "ALL" else [row for row in paired if row["size_bucket"] == size]
        paired_by_size[size] = {
            "flowCount": len(rows),
            **{metric: _mean(row[metric] for row in rows) for metric in paired_metrics},
        }

    sample_bucket = {
        row["sample"]: row["size_bucket"] for row in flow_means if row["strategy"] == "semantic"
    }
    run_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for run in all_runs:
        run_groups[(sample_bucket[run["sample"]], run["strategy"])].append(run)

    strategy_summaries: dict[str, Any] = {}
    for strategy in STRATEGIES:
        strategy_runs = [run for run in all_runs if run["strategy"] == strategy]
        successful = [run for run in strategy_runs if run["success"]]
        sound = [
            run for run in successful
            if run["quality"]["soundness"]["calculationSucceeded"]
            and run["quality"]["soundness"]["sound"]
        ]
        strategy_flows = [row for row in flow_means if row["strategy"] == strategy]
        strategy_summaries[strategy] = {
            "plannedRuns": len(strategy_runs),
            "successfulRuns": len(successful),
            "completionRate": len(successful) / len(strategy_runs),
            "soundRuns": len(sound),
            "soundnessRatePlanned": len(sound) / len(strategy_runs),
            "soundnessRateSuccessful": len(sound) / len(successful),
            "llmCalls": sum(run["llmCalls"] for run in strategy_runs),
            "totalTokens": sum(run["tokenUsage"]["totalTokens"] for run in strategy_runs),
            "durationSeconds": sum(run["durationSeconds"] for run in strategy_runs),
            "flowWeightedMetrics": _aggregate_flows(strategy_flows),
        }

    size_run_summary: list[dict[str, Any]] = []
    for size in SIZE_ORDER:
        for strategy in STRATEGIES:
            rows = run_groups[(size, strategy)]
            successful = [row for row in rows if row["success"]]
            sound = [
                row for row in successful
                if row["quality"]["soundness"]["calculationSucceeded"]
                and row["quality"]["soundness"]["sound"]
            ]
            size_run_summary.append({
                "sizeBucket": size,
                "strategy": strategy,
                "plannedRuns": len(rows),
                "successfulRuns": len(successful),
                "completionRate": len(successful) / len(rows),
                "soundRuns": len(sound),
                "soundnessRatePlanned": len(sound) / len(rows),
                "soundnessRateSuccessful": len(sound) / len(successful) if successful else None,
            })

    failures = [
        {
            "sample": run["sample"],
            "repetition": run["repetition"],
            "strategy": run["strategy"],
            "stage": run["stage"],
            "errorType": run.get("errorType"),
            "error": run.get("error"),
            "llmCalls": run["llmCalls"],
            "totalTokens": run["tokenUsage"]["totalTokens"],
        }
        for run in all_runs if not run["success"]
    ]
    sound_count = sum(
        run["success"]
        and run["quality"]["soundness"]["calculationSucceeded"]
        and run["quality"]["soundness"]["sound"]
        for run in all_runs
    )

    audit_path = root / "batch03" / "infrastructure_retry_audit_final.json"
    retry_audit = json.loads(audit_path.read_text(encoding="utf-8"))
    discarded = retry_audit["discardedAttempts"]
    discarded_by_strategy = {
        strategy: {
            "llmCalls": sum(row["llmCalls"] for row in discarded if row["strategy"] == strategy),
            "totalTokens": sum(row["totalTokens"] for row in discarded if row["strategy"] == strategy),
        }
        for strategy in STRATEGIES
    }
    audited_cost_lower_bounds = {
        strategy: {
            "llmCalls": strategy_summaries[strategy]["llmCalls"] + discarded_by_strategy[strategy]["llmCalls"],
            "totalTokens": strategy_summaries[strategy]["totalTokens"] + discarded_by_strategy[strategy]["totalTokens"],
        }
        for strategy in STRATEGIES
    }

    combined = {
        "schemaVersion": "1.0",
        "experimentId": EXPERIMENT_ID,
        "sourceBatches": [f"batch{number:02d}/batch{number:02d}.json" for number in range(1, 6)],
        "validation": {
            "batches": validation,
            "sampleCount": len(samples_seen),
            "plannedRuns": 300,
            "recordedRuns": len(all_runs),
            "uniqueRunKeys": len({(run["sample"], run["repetition"], run["strategy"]) for run in all_runs}),
            "successfulRuns": sum(run["success"] for run in all_runs),
            "failedRuns": len(failures),
            "qualityCompleteSuccessfulRuns": sum(
                run["success"] and run.get("qualityMetricsComplete", False) for run in all_runs
            ),
            "soundRuns": sound_count,
            "soundnessRatePlanned": sound_count / 300,
            "soundnessRateSuccessful": sound_count / sum(run["success"] for run in all_runs),
        },
        "strategySummaries": strategy_summaries,
        "sizeRunSummaries": size_run_summary,
        "complexityGroupFlowMeans": complexity_groups,
        "pairedSemanticMinusSingleBySize": paired_by_size,
        "flowMeans": flow_means,
        "failures": failures,
        "costAudit": {
            "canonical": {
                strategy: {
                    "llmCalls": strategy_summaries[strategy]["llmCalls"],
                    "totalTokens": strategy_summaries[strategy]["totalTokens"],
                }
                for strategy in STRATEGIES
            },
            "discardedInfrastructureAttempts": discarded_by_strategy,
            "auditedLowerBounds": audited_cost_lower_bounds,
            "uncheckpointedAttempt": retry_audit["uncheckpointedAttempt"],
        },
    }
    (output / "combined_report.json").write_text(
        json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# PMo 50 正式实验汇总",
        "",
        "## 完整性",
        "",
        f"- 流程：{len(samples_seen)}/50",
        f"- Trial：{len(all_runs)}/300，唯一键 {combined['validation']['uniqueRunKeys']}/300",
        f"- 成功：{combined['validation']['successfulRuns']}/300 = {combined['validation']['successfulRuns']/300:.2%}",
        f"- Sound：{sound_count}/300 = {sound_count/300:.2%}（按成功生成：{sound_count}/{combined['validation']['successfulRuns']} = {sound_count/combined['validation']['successfulRuns']:.2%}）",
        f"- 成功 trial 的质量指标完整：{combined['validation']['qualityCompleteSuccessfulRuns']}/{combined['validation']['successfulRuns']}",
        "",
        "## 按策略总体结果",
        "",
        "指标按冻结口径先在每个流程内平均三次重复，再在 50 个流程间平均；Completion 与 Soundness 使用 trial 计数。",
        "",
        "| 策略 | Completion | Soundness/计划 | Task Dice@0.7 | Task Alignment | Task F1@0.7 | Gateway F1 | Relation F1@0.7 | Pair Coverage@0.7 | Mean tokens/flow-run |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for strategy in STRATEGIES:
        summary = strategy_summaries[strategy]
        metrics = summary["flowWeightedMetrics"]
        lines.append(
            f"| {strategy} | {summary['completionRate']:.2%} | {summary['soundnessRatePlanned']:.2%} | "
            f"{_fmt(metrics['task_dice_07'])} | {_fmt(metrics['task_alignment'])} | "
            f"{_fmt(metrics['task_f1_07'])} | {_fmt(metrics['gateway_f1'])} | "
            f"{_fmt(metrics['relation_f1_07'])} | {_fmt(metrics['pair_coverage_07'])} | "
            f"{metrics['mean_tokens']:.0f} |"
        )
    lines.extend([
        "",
        "## 复杂度分档差值",
        "",
        "下表为每个复杂度档内的流程均值差 `semantic - single_segment`。正值表示语义分段更高；token 正值表示语义分段成本更高。",
        "",
        "| 档位 | 流程数 | Task Dice@0.7 Δ | Task Alignment Δ | Task F1@0.7 Δ | Gateway F1 Δ | Relation F1@0.7 Δ | Pair Coverage@0.7 Δ | Mean token Δ |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for size in SIZE_ORDER:
        row = paired_by_size[size]
        lines.append(
            f"| {size} | {row['flowCount']} | {_fmt(row['task_dice_07'])} | "
            f"{_fmt(row['task_alignment'])} | {_fmt(row['task_f1_07'])} | "
            f"{_fmt(row['gateway_f1'])} | {_fmt(row['relation_f1_07'])} | "
            f"{_fmt(row['pair_coverage_07'])} | {row['mean_tokens']:.0f} |"
        )
    lines.extend([
        "",
        "## Token 成本审计",
        "",
        "| 策略 | 规范 calls | 规范 tokens | 被替换基础设施 calls | 被替换基础设施 tokens | 审计 calls 下界 | 审计 tokens 下界 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for strategy in STRATEGIES:
        canonical = combined["costAudit"]["canonical"][strategy]
        extra = discarded_by_strategy[strategy]
        lower = audited_cost_lower_bounds[strategy]
        lines.append(
            f"| {strategy} | {canonical['llmCalls']:,} | {canonical['totalTokens']:,} | "
            f"{extra['llmCalls']:,} | {extra['totalTokens']:,} | "
            f"{lower['llmCalls']:,} | {lower['totalTokens']:,} |"
        )
    lines.extend([
        "",
        "Batch 03 另有一次在 checkpoint 前被命令执行器终止的重试，usage 未返回；其消耗未知且未按零处理。因此审计值是下界。",
        "",
        "## 失败项",
        "",
        f"共 {len(failures)} 个失败 trial；均保留在原始 batch JSON 中。完整错误见 `combined_report.json`。",
    ])
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(combined["validation"], ensure_ascii=False, indent=2))
    print(f"output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
