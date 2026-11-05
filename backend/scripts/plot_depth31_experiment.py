from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np

from audit_panel_alignment import require_matplotlib_panel_alignment


BACKEND = Path(__file__).resolve().parents[1]
ARCHIVE = BACKEND / "results" / "实验结果归档_20261023"
PREEXPERIMENT_DATA = ARCHIVE / "01_前置实验" / "数据"
PREEXPERIMENT_FIGURES = ARCHIVE / "01_前置实验" / "结果图"
FORMAL_DATA = ARCHIVE / "02_真实实验" / "数据"
FORMAL_FIGURES = ARCHIVE / "02_真实实验" / "结果图"
INPUT = FORMAL_DATA / "depth31_metrics.json"
OUTPUT = FORMAL_FIGURES
DATA_OUTPUT = FORMAL_DATA
QA_OUTPUT = BACKEND / ".figure_qa"
DEPTH_ORDER = ("low", "medium", "high")
DEPTH_LABELS = {"low": "Low", "medium": "Medium", "high": "High"}
TIER_COLORS = {"low": "#4C78A8", "medium": "#E39C37", "high": "#B24A5A"}
METRIC_COLORS = ("#356CA5", "#D47A32", "#3E8B6D", "#735AA8")
SOURCE_METADATA: dict[str, Any] = {}


mpl.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans", "sans-serif"],
    "font.size": 7,
    "axes.titlesize": 8,
    "axes.labelsize": 7,
    "xtick.labelsize": 6.5,
    "ytick.labelsize": 6.5,
    "legend.fontsize": 6.3,
    "axes.linewidth": 0.7,
    "axes.spines.right": False,
    "axes.spines.top": False,
    "legend.frameon": False,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "svg.fonttype": "none",
})


def _usable(values: Iterable[Any]) -> list[float]:
    return [float(value) for value in values if value is not None]


def _mean(values: Iterable[Any]) -> float | None:
    usable = _usable(values)
    return fmean(usable) if usable else None


def _jitter(count: int, label: str, width: float = 0.13) -> np.ndarray:
    if count <= 1:
        return np.zeros(count)
    positions = np.linspace(-width, width, count)
    order = sorted(
        range(count),
        key=lambda index: hashlib.sha256(f"{label}:{index}".encode()).digest(),
    )
    result = np.empty(count)
    result[order] = positions
    return result


def _panel_label(ax: plt.Axes, label: str) -> None:
    ax.annotate(
        label,
        xy=(0, 1),
        xycoords="axes fraction",
        xytext=(-25, 11),
        textcoords="offset points",
        ha="left",
        va="top",
        fontsize=9,
        fontweight="bold",
        annotation_clip=False,
    )


def _style_axis(ax: plt.Axes, zero_line: bool = False) -> None:
    if zero_line:
        ax.axhline(0, color="#858B93", linewidth=0.8, linestyle=(0, (3, 2)), zorder=0)
    ax.grid(axis="y", color="#E1E5EA", linewidth=0.45, zorder=0)
    ax.tick_params(length=2.5, width=0.6)


def _xlabels(rows: list[dict[str, Any]], metric: str) -> list[str]:
    del rows, metric
    return [DEPTH_LABELS[tier] for tier in DEPTH_ORDER]


def _draw_tier_points(
    ax: plt.Axes,
    rows: list[dict[str, Any]],
    metric: str,
    color: str,
    absolute: bool,
) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for x, tier in enumerate(DEPTH_ORDER):
        values = _usable(
            row.get(metric) for row in rows if row["depthTier"] == tier
        )
        if not values:
            continue
        jitter = _jitter(len(values), f"{metric}:{tier}")
        ax.scatter(
            x + jitter,
            values,
            s=15,
            color=color,
            alpha=0.36,
            edgecolors="none",
            zorder=2,
        )
        center = fmean(values)
        ax.scatter(
            [x],
            [center],
            s=35,
            marker="D",
            color=color,
            edgecolors="white",
            linewidths=0.65,
            zorder=4,
        )
        summary.append({
            "depthTier": tier,
            "metric": metric,
            "evaluableFlows": len(values),
            "mean": center,
            "valueDefinition": "process mean" if absolute else "semantic minus single-segment",
        })
    ax.set_xticks(range(3), _xlabels(rows, metric))
    return summary


def _save_figure(fig: plt.Figure, base: Path, panel_count: int) -> None:
    base.parent.mkdir(parents=True, exist_ok=True)
    QA_OUTPUT.mkdir(parents=True, exist_ok=True)
    qa_base = QA_OUTPUT / base.name
    fig.canvas.draw()
    require_matplotlib_panel_alignment(
        fig,
        json_out=f"{qa_base}.alignment.json",
        overlay_svg=f"{qa_base}.alignment.svg",
        tolerance_pt=1.5,
        gutter_tolerance_pt=1.5,
        require_panel_labels=panel_count > 1,
        strict=True,
    )
    fig.savefig(f"{base}.pdf", bbox_inches="tight")
    fig.savefig(f"{base}.svg", bbox_inches="tight")
    fig.savefig(f"{base}.png", dpi=600, bbox_inches="tight")
    plt.close(fig)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    rows = [{**SOURCE_METADATA, **row} for row in rows]
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _single_metric_figure(
    rows: list[dict[str, Any]],
    metric: str,
    ylabel: str,
    color: str,
    base_name: str,
    *,
    absolute: bool,
    ylim: tuple[float, float],
) -> None:
    source: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    fig, ax = plt.subplots(figsize=(112 / 25.4, 76 / 25.4))
    fig.subplots_adjust(left=0.18, right=0.985, bottom=0.18, top=0.985)
    _style_axis(ax, zero_line=not absolute)
    summaries.extend(_draw_tier_points(ax, rows, metric, color, absolute=absolute))
    ax.set_ylim(*ylim)
    ax.set_ylabel(ylabel)
    for row in rows:
        source.append({
            "sample": row["sample"],
            "depthTier": row["depthTier"],
            "taskMaxNestingDepth": row.get("taskMaxNestingDepth"),
            "metric": metric,
            "value" if absolute else "delta": row.get(metric),
            "valueDefinition": "process mean" if absolute else "semantic minus single-segment",
            "plannedRuns": row.get("plannedRuns"),
            "qualityEvaluableRuns": row.get("qualityEvaluableRuns"),
        })
    base = OUTPUT / base_name
    _save_figure(fig, base, 1)
    _write_csv(DATA_OUTPUT / f"{base.name}_source.csv", source)
    _write_csv(DATA_OUTPUT / f"{base.name}_summary.csv", summaries)


def _figure1(report: dict[str, Any]) -> None:
    rows = report["oneshot"]["flowMeans"]
    definitions = [
        ("pmoTaskNameDiceAt07", "Task Dice@0.7", METRIC_COLORS[0], "figure1a_oneshot_task_by_depth"),
        ("gatewayTypeRoleCountDice", "Gateway Dice", METRIC_COLORS[1], "figure1b_oneshot_gateway_by_depth"),
        ("globalRelationF1At07", "Global Relation F1@0.7", METRIC_COLORS[2], "figure1c_oneshot_global_relation_by_depth"),
        ("flowSoundnessAllThree", "Soundness", METRIC_COLORS[3], "figure1d_oneshot_soundness_by_depth"),
    ]
    for metric, ylabel, color, base_name in definitions:
        _single_metric_figure(
            rows, metric, ylabel, color, base_name,
            absolute=True, ylim=(-0.03, 1.03),
        )


def _figure2(report: dict[str, Any]) -> None:
    rows = report["formal"]["pairedSemanticMinusSingle"]
    definitions = [
        ("pmoTaskNameDiceAt07", "Δ Task Dice@0.7", METRIC_COLORS[0], "figure2a_paired_task_delta"),
        ("gatewayTypeRoleCountDice", "Δ Gateway Dice", METRIC_COLORS[1], "figure2b_paired_gateway_delta"),
        ("globalRelationF1At07", "Δ Global Relation F1@0.7", METRIC_COLORS[2], "figure2c_paired_global_relation_delta"),
    ]
    for metric, ylabel, color, base_name in definitions:
        _single_metric_figure(
            rows, metric, ylabel, color, base_name,
            absolute=False, ylim=(-0.65, 0.65),
        )


def _figure3(report: dict[str, Any]) -> None:
    rows = report["formal"]["pairedSemanticMinusSingle"]
    definitions = [
        ("conditionalRelationF1At07", "Relations after Task matching", "Δ Conditional Relation F1@0.7", 1.0),
        ("flowCompletionAllThree", "End-to-end completion", "Δ Completion", 1.0),
        ("flowSoundnessAllThree", "All-three soundness", "Δ Soundness", 1.0),
        ("meanTotalTokens", "End-to-end token cost", "Δ Token cost (×10³)", 0.001),
    ]
    source: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    fig, axes = plt.subplots(2, 2, figsize=(180 / 25.4, 120 / 25.4))
    fig.subplots_adjust(left=0.10, right=0.985, bottom=0.12, top=0.94, wspace=0.30, hspace=0.48)
    for index, (ax, (metric, title, ylabel, scale), color) in enumerate(
        zip(axes.flat, definitions, ("#356CA5", "#735AA8", "#D47A32", "#616872"))
    ):
        _style_axis(ax, zero_line=True)
        scaled = [{**row, metric: None if row.get(metric) is None else row[metric] * scale} for row in rows]
        metric_summaries = _draw_tier_points(ax, scaled, metric, color, absolute=False)
        for item in metric_summaries:
            item["originalMetric"] = metric
            item["displayScale"] = scale
        summaries.extend(metric_summaries)
        if metric != "meanTotalTokens":
            ax.set_ylim(-1.05, 1.05)
        ax.set_ylabel(ylabel)
        _panel_label(ax, chr(ord("a") + index))
        for row in rows:
            source.append({
                "panel": chr(ord("a") + index),
                "sample": row["sample"],
                "depthTier": row["depthTier"],
                "metric": metric,
                "delta": row.get(metric),
                "displayValue": None if row.get(metric) is None else row[metric] * scale,
                "valueDefinition": "semantic minus single-segment",
            })
    base = OUTPUT / "figure3_relation_reliability_cost"
    _save_figure(fig, base, 4)
    _write_csv(DATA_OUTPUT / f"{base.name}_source.csv", source)
    _write_csv(DATA_OUTPUT / f"{base.name}_summary.csv", summaries)


def _figure4(report: dict[str, Any]) -> None:
    rows = report["formal"]["pairedSemanticMinusSingle"]
    definitions = [
        ("conditionalRelationF1At", "Conditional relations", "Δ Conditional Relation F1"),
        ("globalRelationF1At", "Global relations", "Δ Global Relation F1"),
    ]
    source: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    fig, axes = plt.subplots(1, 2, figsize=(135 / 25.4, 66 / 25.4))
    fig.subplots_adjust(left=0.11, right=0.985, bottom=0.22, top=0.88, wspace=0.35)
    thresholds = (("06", 0.6), ("07", 0.7), ("08", 0.8))
    markers = {"low": "o", "medium": "s", "high": "D"}
    for panel_index, (ax, (prefix, title, ylabel)) in enumerate(zip(axes, definitions)):
        _style_axis(ax, zero_line=True)
        for tier in DEPTH_ORDER:
            means: list[float] = []
            for suffix, threshold in thresholds:
                metric = f"{prefix}{suffix}"
                values = _usable(
                    row.get(metric) for row in rows if row["depthTier"] == tier
                )
                mean_value = fmean(values)
                means.append(mean_value)
                summaries.append({
                    "panel": chr(ord("a") + panel_index),
                    "depthTier": tier,
                    "threshold": threshold,
                    "metric": metric,
                    "evaluableFlows": len(values),
                    "meanDelta": mean_value,
                })
                for row in rows:
                    if row["depthTier"] == tier:
                        source.append({
                            "panel": chr(ord("a") + panel_index),
                            "sample": row["sample"],
                            "depthTier": tier,
                            "threshold": threshold,
                            "metric": metric,
                            "delta": row.get(metric),
                        })
            ax.plot(
                [0.6, 0.7, 0.8],
                means,
                marker=markers[tier],
                markersize=4.2,
                linewidth=1.25,
                color=TIER_COLORS[tier],
                label=tier.capitalize(),
                zorder=3,
            )
            if panel_index == len(definitions) - 1:
                label_offset = {"low": -0.025, "medium": 0.020, "high": 0.030}[tier]
                ax.text(
                    0.850,
                    means[-1] + label_offset,
                    tier.capitalize(),
                    color=TIER_COLORS[tier],
                    fontsize=6.3,
                    va="center",
                    ha="left",
                )
        ax.set_xticks([0.6, 0.7, 0.8])
        ax.set_xlim(0.585, 0.910)
        ax.set_xlabel("Task similarity threshold")
        ax.set_ylabel(ylabel)
        ax.set_ylim(-0.22, 0.22)
        _panel_label(ax, chr(ord("a") + panel_index))
    base = OUTPUT / "figure4_threshold_sensitivity"
    _save_figure(fig, base, 2)
    _write_csv(DATA_OUTPUT / f"{base.name}_source.csv", source)
    _write_csv(DATA_OUTPUT / f"{base.name}_summary.csv", summaries)


def _coverage_report(report: dict[str, Any]) -> None:
    rows: list[dict[str, Any]] = []
    selected_counts = report["manifest"]["tierCounts"]
    for experiment, section in (("oneshot", report["oneshot"]), ("formal", report["formal"])):
        strategies = sorted({row["strategy"] for row in section["flowMeans"]})
        for strategy in strategies:
            for tier in DEPTH_ORDER:
                members = [
                    row for row in section["flowMeans"]
                    if row["strategy"] == strategy and row["depthTier"] == tier
                ]
                rows.append({
                    "experiment": experiment,
                    "strategy": strategy,
                    "depthTier": tier,
                    "flowCount": int(selected_counts[tier]),
                    "qualityEvaluableFlowCount": sum(row["qualityEvaluableRuns"] > 0 for row in members),
                    "plannedRunCount": sum(row["plannedRuns"] for row in members),
                    "completedRunCount": sum(row["successfulRuns"] for row in members),
                    "qualityEvaluableRunCount": sum(row["qualityEvaluableRuns"] for row in members),
                })
    _write_csv(FORMAL_DATA / "evaluation_coverage_counts.csv", rows)


def main() -> int:
    global OUTPUT, DATA_OUTPUT
    report = json.loads(INPUT.read_text(encoding="utf-8"))
    if report["manifest"]["schemaVersion"] != "depth31-descriptive-reanalysis-v4":
        raise RuntimeError("Run recompute_depth31_metrics.py before plotting.")
    OUTPUT = PREEXPERIMENT_FIGURES
    DATA_OUTPUT = PREEXPERIMENT_DATA
    _figure1(report)
    OUTPUT = FORMAL_FIGURES
    DATA_OUTPUT = FORMAL_DATA
    _figure2(report)
    _figure3(report)
    _figure4(report)
    _coverage_report(report)
    print(OUTPUT.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
