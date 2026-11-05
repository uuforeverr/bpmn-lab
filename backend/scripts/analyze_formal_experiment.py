from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from scipy import stats


EXPERIMENT_ID = "pmo_full_50_seed20261017"
SEED = 20261017
SIZE_ORDER = ("S", "M", "L", "XL")
STRATEGIES = ("semantic", "single_segment")
BOOTSTRAP_SAMPLES = 20_000
MONTE_CARLO_SAMPLES = 100_000

METRICS = (
    "completion_rate",
    "soundness_rate_planned",
    "task_dice_07",
    "task_alignment",
    "task_precision_06", "task_recall_06", "task_f1_06",
    "task_precision_07", "task_recall_07", "task_f1_07",
    "task_precision_08", "task_recall_08", "task_f1_08",
    "gateway_precision", "gateway_recall", "gateway_f1", "gateway_abs_error",
    "relation_precision_06", "relation_recall_06", "relation_f1_06", "pair_coverage_06",
    "relation_precision_07", "relation_recall_07", "relation_f1_07", "pair_coverage_07",
    "relation_precision_08", "relation_recall_08", "relation_f1_08", "pair_coverage_08",
    "mean_tokens", "mean_seconds",
)

PRIMARY_METRICS = (
    "completion_rate",
    "soundness_rate_planned",
    "task_dice_07",
    "task_alignment",
    "gateway_f1",
    "gateway_abs_error",
    "relation_f1_07",
    "pair_coverage_07",
    "mean_tokens",
)

METRIC_LABELS = {
    "completion_rate": "Completion rate",
    "soundness_rate_planned": "Soundness rate (planned denominator)",
    "task_dice_07": "PMo-compatible Task-name Dice@0.7",
    "task_alignment": "Task Semantic Alignment Score",
    "gateway_f1": "Gateway type-role count F1",
    "gateway_abs_error": "Gateway absolute count error",
    "relation_f1_07": "Control-flow Relation F1@0.7",
    "pair_coverage_07": "Matched Task-pair Coverage@0.7",
    "mean_tokens": "End-to-end tokens",
}


def _stable_rng(label: str) -> np.random.Generator:
    digest = hashlib.sha256(f"{SEED}:{label}".encode("utf-8")).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "little"))


def _mean(values: Iterable[float]) -> float:
    values = list(values)
    return float(np.mean(values)) if values else math.nan


def _bootstrap_mean_ci(values: np.ndarray, label: str) -> tuple[float, float]:
    if len(values) == 1:
        return float(values[0]), float(values[0])
    rng = _stable_rng(f"bootstrap:{label}")
    draws = rng.choice(values, size=(BOOTSTRAP_SAMPLES, len(values)), replace=True)
    means = draws.mean(axis=1)
    low, high = np.quantile(means, [0.025, 0.975])
    return float(low), float(high)


def _sign_flip_pvalue(values: np.ndarray, label: str) -> tuple[float, str, int]:
    observed = abs(float(values.mean()))
    n = len(values)
    tolerance = 1e-15
    if n <= 20:
        patterns = np.arange(1 << n, dtype=np.uint32)[:, None]
        bits = (patterns >> np.arange(n, dtype=np.uint32)) & 1
        signs = bits.astype(np.int8) * 2 - 1
        permuted = np.abs((signs * values).mean(axis=1))
        return float(np.mean(permuted >= observed - tolerance)), "exact", len(permuted)
    rng = _stable_rng(f"sign-flip:{label}")
    exceed = 0
    remaining = MONTE_CARLO_SAMPLES
    chunk = 10_000
    while remaining:
        current = min(chunk, remaining)
        signs = rng.choice(np.array([-1.0, 1.0]), size=(current, n))
        permuted = np.abs((signs * values).mean(axis=1))
        exceed += int(np.count_nonzero(permuted >= observed - tolerance))
        remaining -= current
    return (exceed + 1) / (MONTE_CARLO_SAMPLES + 1), "monte_carlo", MONTE_CARLO_SAMPLES


def _holm_adjust(pvalues: list[float]) -> list[float]:
    order = np.argsort(pvalues)
    adjusted = np.empty(len(pvalues), dtype=float)
    running = 0.0
    m = len(pvalues)
    for rank, index in enumerate(order):
        running = max(running, (m - rank) * pvalues[index])
        adjusted[index] = min(1.0, running)
    return adjusted.tolist()


def _sound(run: dict[str, Any]) -> bool:
    if not run.get("success"):
        return False
    soundness = (run.get("quality") or {}).get("soundness") or {}
    return bool(soundness.get("calculationSucceeded") and soundness.get("sound"))


def _load_data(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    combined_path = root / "combined" / "combined_report.json"
    combined = json.loads(combined_path.read_text(encoding="utf-8"))
    rows = [dict(row) for row in combined["flowMeans"]]
    runs: list[dict[str, Any]] = []
    for batch_number in range(1, 6):
        path = root / f"batch{batch_number:02d}" / f"batch{batch_number:02d}.json"
        runs.extend(json.loads(path.read_text(encoding="utf-8"))["runs"])
    if len(rows) != 100 or len(runs) != 300:
        raise RuntimeError("Expected 100 flow-strategy rows and 300 trials")
    return rows, runs


def _enrich_flow_rows(
    rows: list[dict[str, Any]], runs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    complexity: dict[str, dict[str, Any]] = {}
    for run in runs:
        grouped[(run["sample"], run["strategy"])].append(run)
        complexity.setdefault(run["sample"], run["complexity"])
    enriched: list[dict[str, Any]] = []
    for row in rows:
        trials = grouped[(row["sample"], row["strategy"])]
        if len(trials) != 3:
            raise RuntimeError(f"{row['sample']} {row['strategy']} has {len(trials)} trials")
        c = complexity[row["sample"]]
        sound_runs = sum(_sound(run) for run in trials)
        new = {
            **row,
            "weighted_score": c["weightedScore"],
            "weighted_level": c["weightedLevel"],
            "sound_runs": sound_runs,
            "soundness_rate_planned": sound_runs / len(trials),
            "soundness_rate_successful": (
                sound_runs / row["successful_runs"] if row["successful_runs"] else None
            ),
            "mean_llm_calls": _mean(run["llmCalls"] for run in trials),
        }
        enriched.append(new)
    return sorted(enriched, key=lambda row: (row["sample"], row["strategy"]))


def _pair_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed = {(row["sample"], row["strategy"]): row for row in rows}
    paired: list[dict[str, Any]] = []
    for sample in sorted({row["sample"] for row in rows}):
        semantic = indexed[(sample, "semantic")]
        single = indexed[(sample, "single_segment")]
        row: dict[str, Any] = {
            "sample": sample,
            "size_bucket": semantic["size_bucket"],
            "weighted_score": semantic["weighted_score"],
            "tasks": semantic["tasks"],
            "gateways": semantic["gateways"],
            "nesting_depth": semantic["nesting_depth"],
            "cfc": semantic["cfc"],
        }
        for metric in METRICS:
            semantic_value = semantic.get(metric)
            single_value = single.get(metric)
            row[f"semantic__{metric}"] = semantic_value
            row[f"single_segment__{metric}"] = single_value
            row[f"delta__{metric}"] = (
                None
                if semantic_value is None or single_value is None
                else float(semantic_value) - float(single_value)
            )
        paired.append(row)
    if len(paired) != 50:
        raise RuntimeError(f"Expected 50 paired processes, got {len(paired)}")
    return paired


def _group_statistics(paired: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for metric in METRICS:
        for size in (*SIZE_ORDER, "ALL"):
            selected = paired if size == "ALL" else [r for r in paired if r["size_bucket"] == size]
            complete = [r for r in selected if r[f"delta__{metric}"] is not None]
            values = np.asarray([r[f"delta__{metric}"] for r in complete], dtype=float)
            semantic = np.asarray([r[f"semantic__{metric}"] for r in complete], dtype=float)
            single = np.asarray([r[f"single_segment__{metric}"] for r in complete], dtype=float)
            if not len(values):
                continue
            ci_low, ci_high = _bootstrap_mean_ci(values, f"{metric}:{size}")
            pvalue, method, permutations = _sign_flip_pvalue(values, f"{metric}:{size}")
            sd = float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
            output.append({
                "metric": metric,
                "metric_label": METRIC_LABELS.get(metric, metric),
                "size_bucket": size,
                "n_flows": len(values),
                "semantic_mean": float(semantic.mean()),
                "single_segment_mean": float(single.mean()),
                "mean_delta": float(values.mean()),
                "sd_delta": sd,
                "se_delta": sd / math.sqrt(len(values)) if len(values) else None,
                "bootstrap_ci95_low": ci_low,
                "bootstrap_ci95_high": ci_high,
                "paired_effect_dz": float(values.mean() / sd) if sd > 0 else None,
                "fraction_delta_gt_zero": float(np.mean(values > 0)),
                "fraction_delta_eq_zero": float(np.mean(values == 0)),
                "p_value_sign_flip": pvalue,
                "sign_flip_method": method,
                "sign_flip_samples": permutations,
                "p_holm_four_complexity_groups": None,
            })
    for metric in METRICS:
        family = [
            row for row in output
            if row["metric"] == metric and row["size_bucket"] in SIZE_ORDER
        ]
        adjusted = _holm_adjust([row["p_value_sign_flip"] for row in family])
        for row, pvalue in zip(family, adjusted):
            row["p_holm_four_complexity_groups"] = pvalue
    return output


def _hc3_interaction(values: np.ndarray, scores: np.ndarray) -> dict[str, Any]:
    z = (scores - scores.mean()) / scores.std(ddof=1)
    x = np.column_stack([np.ones(len(z)), z])
    inv_xtx = np.linalg.inv(x.T @ x)
    beta = inv_xtx @ x.T @ values
    residual = values - x @ beta
    leverage = np.einsum("ij,jk,ik->i", x, inv_xtx, x)
    scaled = residual / np.maximum(1.0 - leverage, 1e-12)
    meat = x.T @ (x * scaled[:, None] ** 2)
    covariance = inv_xtx @ meat @ inv_xtx
    se = math.sqrt(float(covariance[1, 1]))
    dof = len(values) - x.shape[1]
    t_value = float(beta[1] / se) if se > 0 else math.nan
    p_value = float(2 * stats.t.sf(abs(t_value), dof)) if se > 0 else math.nan
    critical = float(stats.t.ppf(0.975, dof))
    rho, rho_p = stats.spearmanr(scores, values)
    return {
        "n_flows": len(values),
        "complexity_mean": float(scores.mean()),
        "complexity_sd": float(scores.std(ddof=1)),
        "intercept_at_mean_complexity": float(beta[0]),
        "slope_per_complexity_sd": float(beta[1]),
        "hc3_se": se,
        "ci95_low": float(beta[1] - critical * se),
        "ci95_high": float(beta[1] + critical * se),
        "t_value": t_value,
        "degrees_of_freedom": dof,
        "p_value_hc3": p_value,
        "spearman_rho": float(rho),
        "spearman_p_value": float(rho_p),
    }


def _interaction_statistics(paired: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for metric in METRICS:
        complete = [row for row in paired if row[f"delta__{metric}"] is not None]
        values = np.asarray([row[f"delta__{metric}"] for row in complete], dtype=float)
        scores = np.asarray([row["weighted_score"] for row in complete], dtype=float)
        output.append({
            "metric": metric,
            "metric_label": METRIC_LABELS.get(metric, metric),
            **_hc3_interaction(values, scores),
            "p_holm_across_metrics": None,
        })
    adjusted = _holm_adjust([row["p_value_hc3"] for row in output])
    for row, pvalue in zip(output, adjusted):
        row["p_holm_across_metrics"] = pvalue
    return output


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _fmt(value: float | None, digits: int = 3) -> str:
    if value is None or not math.isfinite(value):
        return "NA"
    return f"{value:.{digits}f}"


def _write_summary(
    path: Path,
    grouped: list[dict[str, Any]],
    interactions: list[dict[str, Any]],
) -> None:
    by_key = {(row["metric"], row["size_bucket"]): row for row in grouped}
    interaction_by_metric = {row["metric"]: row for row in interactions}
    lines = [
        "# PMo 50 正式实验：配对统计分析",
        "",
        "分析单位是流程：先在每个流程、每种策略内平均 3 次重复，再计算同一流程的配对差 `semantic - single_segment`。质量指标仅平均成功产物；Completion 与 Soundness 使用 3 次计划运行作为分母。",
        "",
        f"分档差值的 95% 区间为流程级配对 bootstrap（{BOOTSTRAP_SAMPLES:,} 次）；双侧 P 值来自配对符号翻转检验（小样本精确枚举，较大样本 {MONTE_CARLO_SAMPLES:,} 次 Monte Carlo）。同一指标的 S/M/L/XL 四次检验使用 Holm 校正。",
        "",
        "## 主要指标的复杂度分档结果",
        "",
        "`Δ > 0` 通常表示语义分段更优；Gateway absolute count error 与 token/time 为成本或误差，方向相反。XL 仅 4 个流程，区间只作探索性描述。",
        "",
        "| 指标 | 档位 | n | Semantic | Single | Δ mean | Δ SD | 95% bootstrap CI | Holm P |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for metric in PRIMARY_METRICS:
        for size in SIZE_ORDER:
            row = by_key[(metric, size)]
            lines.append(
                f"| {METRIC_LABELS[metric]} | {size} | {row['n_flows']} | "
                f"{_fmt(row['semantic_mean'])} | {_fmt(row['single_segment_mean'])} | "
                f"{_fmt(row['mean_delta'])} | {_fmt(row['sd_delta'])} | "
                f"[{_fmt(row['bootstrap_ci95_low'])}, {_fmt(row['bootstrap_ci95_high'])}] | "
                f"{_fmt(row['p_holm_four_complexity_groups'])} |"
            )
    lines.extend([
        "",
        "## 连续复杂度辅助交互检验",
        "",
        "回归因变量为同一流程的策略差值，解释变量为标准化后的参考 BPMN weighted complexity score。斜率即 Strategy × Complexity 的配对形式；标准误使用 HC3。该分析用于判断差值趋势，不替代四档描述统计。",
        "",
        "| 指标 | n | 每 1 SD 复杂度的 Δ 变化 | 95% HC3 CI | P | Holm P（跨全部指标） | Spearman ρ |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for metric in PRIMARY_METRICS:
        row = interaction_by_metric[metric]
        lines.append(
            f"| {METRIC_LABELS[metric]} | {row['n_flows']} | "
            f"{_fmt(row['slope_per_complexity_sd'])} | "
            f"[{_fmt(row['ci95_low'])}, {_fmt(row['ci95_high'])}] | "
            f"{_fmt(row['p_value_hc3'])} | {_fmt(row['p_holm_across_metrics'])} | "
            f"{_fmt(row['spearman_rho'])} |"
        )
    dice_all = by_key[("task_dice_07", "ALL")]
    alignment_all = by_key[("task_alignment", "ALL")]
    coverage_all = by_key[("pair_coverage_07", "ALL")]
    gateway_all = by_key[("gateway_f1", "ALL")]
    relation_all = by_key[("relation_f1_07", "ALL")]
    completion_all = by_key[("completion_rate", "ALL")]
    soundness_all = by_key[("soundness_rate_planned", "ALL")]
    lines.extend([
        "",
        "## 结果判读",
        "",
        f"- 合并 50 个流程后，语义分段在 Task Dice@0.7、Task Alignment 和 Pair Coverage@0.7 上的平均差分别为 {_fmt(dice_all['mean_delta'])}、{_fmt(alignment_all['mean_delta'])} 和 {_fmt(coverage_all['mean_delta'])}；对应 95% bootstrap 区间分别为 [{_fmt(dice_all['bootstrap_ci95_low'])}, {_fmt(dice_all['bootstrap_ci95_high'])}]、[{_fmt(alignment_all['bootstrap_ci95_low'])}, {_fmt(alignment_all['bootstrap_ci95_high'])}] 和 [{_fmt(coverage_all['bootstrap_ci95_low'])}, {_fmt(coverage_all['bootstrap_ci95_high'])}]。这些是跨指标校正前的总体配对结果，说明存在小幅总体语义收益。",
        f"- 但是四个复杂度档内的质量差异经 Holm 校正后均未达到显著，连续复杂度交互的质量指标斜率区间也都跨 0。因此当前数据不支持“语义分段优势随复杂度上升而系统增强”，只能支持“小幅总体收益但无单调复杂度趋势”。",
        f"- Gateway F1 和 Relation F1@0.7 的总体平均差分别为 {_fmt(gateway_all['mean_delta'])} 和 {_fmt(relation_all['mean_delta'])}，区间均跨 0；结构收益没有稳定证据。Relation F1 仍须与 Pair Coverage 联合解释。",
        f"- 可靠性存在代价：Completion 总体差为 {_fmt(completion_all['mean_delta'])}，Soundness 总体差为 {_fmt(soundness_all['mean_delta'])}；其 95% 区间分别为 [{_fmt(completion_all['bootstrap_ci95_low'])}, {_fmt(completion_all['bootstrap_ci95_high'])}] 和 [{_fmt(soundness_all['bootstrap_ci95_low'])}, {_fmt(soundness_all['bootstrap_ci95_high'])}]。",
        "- Token 成本在所有复杂度档均明显更高；成本随连续复杂度增加的斜率为正，但跨全部指标 Holm 校正后不显著。",
    ])
    lines.extend([
        "",
        "## 解释边界",
        "",
        "- Relation F1 必须与 Matched Task-pair Coverage 同读；高 Relation F1 不能弥补低覆盖。",
        "- 失败运行不被伪造为质量分数 0；它们通过 Completion/Soundness 体现可靠性代价。",
        "- Bootstrap 以流程为重采样单位，3 次重复不被误当作独立样本。",
        "- XL 的 n=4，不据此声称语义分段优势随复杂度单调增加。",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    backend = Path(__file__).resolve().parents[1]
    root = backend / "results" / EXPERIMENT_ID
    output = root / "combined"
    flow_rows, runs = _load_data(root)
    enriched = _enrich_flow_rows(flow_rows, runs)
    paired = _pair_rows(enriched)
    grouped = _group_statistics(paired)
    interactions = _interaction_statistics(paired)

    _write_csv(output / "flow_strategy_means_enriched.csv", enriched)
    _write_csv(output / "paired_flow_differences.csv", paired)
    _write_csv(output / "complexity_paired_statistics.csv", grouped)
    _write_csv(output / "continuous_complexity_interaction.csv", interactions)
    result = {
        "schemaVersion": "1.0",
        "experimentId": EXPERIMENT_ID,
        "analysisUnit": "paired process-level strategy means",
        "deltaDefinition": "semantic - single_segment",
        "randomSeed": SEED,
        "bootstrapSamples": BOOTSTRAP_SAMPLES,
        "monteCarloSignFlipSamples": MONTE_CARLO_SAMPLES,
        "qualityMissingnessRule": "quality metrics average successful outputs only; failures affect completion and soundness",
        "groupStatistics": grouped,
        "continuousComplexityInteraction": interactions,
    }
    (output / "statistical_analysis.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_summary(output / "statistical_summary.md", grouped, interactions)
    print(json.dumps({
        "flowStrategyRows": len(enriched),
        "pairedFlows": len(paired),
        "groupStatistics": len(grouped),
        "interactionTests": len(interactions),
        "output": str(output),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
