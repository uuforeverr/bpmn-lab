from __future__ import annotations

import csv
import hashlib
import math
from pathlib import Path
from typing import Any

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.ticker import FixedLocator, FuncFormatter
from scipy import stats as scipy_stats

from audit_panel_alignment import require_matplotlib_panel_alignment


EXPERIMENT_ID = "pmo_full_50_seed20261017"
SIZE_ORDER = ("S", "M", "L", "XL")
SIZE_N = {"S": 8, "M": 25, "L": 13, "XL": 4}
BOOTSTRAP_SAMPLES = 20_000

BLUE = "#2878B5"
ORANGE = "#E07B39"
GREEN = "#3B9B7A"
PURPLE = "#7A5AA6"
GREY = "#6B7280"


mpl.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans", "sans-serif"],
    "font.size": 7,
    "axes.titlesize": 8,
    "axes.labelsize": 7,
    "xtick.labelsize": 6.5,
    "ytick.labelsize": 6.5,
    "legend.fontsize": 6.2,
    "axes.linewidth": 0.7,
    "axes.spines.right": False,
    "axes.spines.top": False,
    "legend.frameon": False,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "svg.fonttype": "none",
})


def _deterministic_jitter(count: int, label: str, width: float) -> np.ndarray:
    """Spread every observation without stochastic row sampling or data simulation."""
    if count <= 1:
        return np.zeros(count)
    positions = np.linspace(-width, width, count)
    order = sorted(
        range(count),
        key=lambda index: hashlib.sha256(f"{label}:{index}".encode("utf-8")).digest(),
    )
    jitter = np.empty(count)
    jitter[order] = positions
    return jitter


def _bootstrap_geometric_mean_ci(values: np.ndarray, label: str) -> tuple[float, float, float]:
    if np.any(values <= 0):
        raise ValueError(f"Token ratios must be strictly positive for the log scale: {label}")
    logs = np.log(values)
    result = scipy_stats.bootstrap(
        (logs,),
        np.mean,
        n_resamples=BOOTSTRAP_SAMPLES,
        confidence_level=0.95,
        method="percentile",
        random_state=20261017,
    )
    return (
        float(np.exp(logs.mean())),
        float(np.exp(result.confidence_interval.low)),
        float(np.exp(result.confidence_interval.high)),
    )


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


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


def _base_axis(ax: plt.Axes) -> None:
    ax.axhline(0, color="#9CA3AF", linewidth=0.7, linestyle=(0, (3, 2)), zorder=0)
    ax.set_xticks(range(4), [f"{size}\nn={SIZE_N[size]}" for size in SIZE_ORDER])
    ax.grid(axis="y", color="#E5E7EB", linewidth=0.45, zorder=0)
    ax.tick_params(length=2.5, width=0.6)


def _draw_single_delta(
    ax: plt.Axes,
    paired: pd.DataFrame,
    stats: pd.DataFrame,
    metric: str,
    color: str,
) -> None:
    for x, size in enumerate(SIZE_ORDER):
        series = paired.loc[paired["size_bucket"] == size, f"delta__{metric}"]
        n_before = len(series)
        n_after = int(series.notna().sum())
        if n_before != n_after:
            raise ValueError(f"Unexpected missing plotted values for {metric}/{size}: {n_before-n_after}")
        values = series.to_numpy()
        jitter = _deterministic_jitter(len(values), f"scatter:{metric}:{size}", 0.13)
        ax.scatter(
            x + jitter,
            values,
            s=10,
            color=color,
            alpha=0.28,
            edgecolors="none",
            zorder=2,
        )
        row = stats[(stats["metric"] == metric) & (stats["size_bucket"] == size)].iloc[0]
        mean = row["mean_delta"]
        errors = np.array([[mean - row["bootstrap_ci95_low"]], [row["bootstrap_ci95_high"] - mean]])
        ax.errorbar(
            x,
            mean,
            yerr=errors,
            fmt="o",
            markersize=4.3,
            color=color,
            markeredgecolor="white",
            markeredgewidth=0.6,
            capsize=2.4,
            elinewidth=1.1,
            zorder=4,
        )


def _draw_multi_delta(
    ax: plt.Axes,
    paired: pd.DataFrame,
    stats: pd.DataFrame,
    definitions: list[tuple[str, str, str, str]],
) -> None:
    offsets = np.linspace(-0.18, 0.18, len(definitions))
    for offset, (metric, label, color, marker) in zip(offsets, definitions):
        for x, size in enumerate(SIZE_ORDER):
            series = paired.loc[paired["size_bucket"] == size, f"delta__{metric}"]
            n_before = len(series)
            n_after = int(series.notna().sum())
            if n_before != n_after:
                raise ValueError(f"Unexpected missing plotted values for {metric}/{size}: {n_before-n_after}")
            values = series.to_numpy()
            jitter = _deterministic_jitter(len(values), f"scatter:{metric}:{size}", 0.035)
            ax.scatter(
                x + offset + jitter,
                values,
                s=7,
                color=color,
                alpha=0.13,
                edgecolors="none",
                zorder=1,
            )
            row = stats[(stats["metric"] == metric) & (stats["size_bucket"] == size)].iloc[0]
            mean = row["mean_delta"]
            errors = np.array([[mean - row["bootstrap_ci95_low"]], [row["bootstrap_ci95_high"] - mean]])
            ax.errorbar(
                x + offset,
                mean,
                yerr=errors,
                fmt=marker,
                markersize=3.8,
                color=color,
                markeredgecolor="white",
                markeredgewidth=0.5,
                capsize=2,
                elinewidth=0.9,
                label=label if x == 0 else None,
                zorder=4,
            )


def _source_tables(
    paired: pd.DataFrame, stats: pd.DataFrame, output: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source_rows: list[dict[str, Any]] = []
    metric_panels = {
        "task_dice_07": "a",
        "task_alignment": "b",
        "gateway_f1": "b",
        "pair_coverage_07": "b",
        "completion_rate": "c",
        "soundness_rate_planned": "c",
    }
    for _, row in paired.iterrows():
        for metric, panel in metric_panels.items():
            source_rows.append({
                "panel": panel,
                "sample": f"{int(row['sample']):02d}",
                "size_bucket": row["size_bucket"],
                "metric": metric,
                "semantic": row[f"semantic__{metric}"],
                "single_segment": row[f"single_segment__{metric}"],
                "plotted_value": row[f"delta__{metric}"],
                "value_definition": "semantic - single_segment",
            })
        ratio = row["semantic__mean_tokens"] / row["single_segment__mean_tokens"]
        source_rows.append({
            "panel": "d",
            "sample": f"{int(row['sample']):02d}",
            "size_bucket": row["size_bucket"],
            "metric": "token_ratio",
            "semantic": row["semantic__mean_tokens"],
            "single_segment": row["single_segment__mean_tokens"],
            "plotted_value": ratio,
            "value_definition": "semantic / single_segment",
        })

    summary_rows: list[dict[str, Any]] = []
    for metric, panel in metric_panels.items():
        for size in SIZE_ORDER:
            row = stats[(stats["metric"] == metric) & (stats["size_bucket"] == size)].iloc[0]
            summary_rows.append({
                "panel": panel,
                "size_bucket": size,
                "metric": metric,
                "estimate": row["mean_delta"],
                "ci95_low": row["bootstrap_ci95_low"],
                "ci95_high": row["bootstrap_ci95_high"],
                "n_flows": int(row["n_flows"]),
                "summary_definition": "arithmetic mean paired difference; process bootstrap CI",
            })
    for size in SIZE_ORDER:
        values = (
            paired.loc[paired["size_bucket"] == size, "semantic__mean_tokens"].to_numpy()
            / paired.loc[paired["size_bucket"] == size, "single_segment__mean_tokens"].to_numpy()
        )
        estimate, low, high = _bootstrap_geometric_mean_ci(values, size)
        summary_rows.append({
            "panel": "d",
            "size_bucket": size,
            "metric": "token_ratio",
            "estimate": estimate,
            "ci95_low": low,
            "ci95_high": high,
            "n_flows": len(values),
            "summary_definition": "geometric mean semantic/single token ratio; process bootstrap CI",
        })
    _write_csv(output / "formal_experiment_figure_source.csv", source_rows)
    _write_csv(output / "formal_experiment_figure_summary.csv", summary_rows)
    return source_rows, summary_rows


def main() -> int:
    backend = Path(__file__).resolve().parents[1]
    combined = backend / "results" / EXPERIMENT_ID / "combined"
    output = combined / "figures"
    output.mkdir(parents=True, exist_ok=True)
    paired = pd.read_csv(combined / "paired_flow_differences.csv", dtype={"sample": str})
    stats_table = pd.read_csv(combined / "complexity_paired_statistics.csv")
    _, summary_rows = _source_tables(paired, stats_table, output)
    figure_summary = pd.DataFrame(summary_rows)

    fig_width_mm = 180
    width_inches = fig_width_mm / 25.4
    fig, axes = plt.subplots(2, 2, figsize=(width_inches, width_inches * 0.73))
    fig.subplots_adjust(left=0.105, right=0.985, bottom=0.10, top=0.94, wspace=0.30, hspace=0.43)

    ax = axes[0, 0]
    _base_axis(ax)
    _draw_single_delta(ax, paired, stats_table, "task_dice_07", BLUE)
    ax.set_title("Task-name agreement", loc="left", fontweight="bold", pad=5)
    ax.set_ylabel("Paired difference\n(Semantic − single)")
    ax.set_ylim(-0.34, 0.36)
    _panel_label(ax, "a")

    ax = axes[0, 1]
    _base_axis(ax)
    _draw_multi_delta(ax, paired, stats_table, [
        ("task_alignment", "Task alignment", BLUE, "o"),
        ("gateway_f1", "Gateway F1", ORANGE, "s"),
        ("pair_coverage_07", "Pair coverage", GREEN, "^"),
    ])
    ax.set_title("Semantic and structural quality", loc="left", fontweight="bold", pad=5)
    ax.set_ylabel("Paired difference\n(Semantic − single)")
    ax.set_ylim(-0.48, 0.52)
    ax.legend(loc="lower left", ncol=1, handletextpad=0.4, borderaxespad=0.2)
    _panel_label(ax, "b")

    ax = axes[1, 0]
    _base_axis(ax)
    _draw_multi_delta(ax, paired, stats_table, [
        ("completion_rate", "Completion", PURPLE, "o"),
        ("soundness_rate_planned", "Soundness", ORANGE, "s"),
    ])
    ax.set_title("Pipeline reliability", loc="left", fontweight="bold", pad=5)
    ax.set_ylabel("Rate difference\n(Semantic − single)")
    ax.set_ylim(-0.72, 0.28)
    ax.legend(loc="lower left", handletextpad=0.4, borderaxespad=0.2)
    _panel_label(ax, "c")

    ax = axes[1, 1]
    ax.axhline(1, color="#9CA3AF", linewidth=0.7, linestyle=(0, (3, 2)), zorder=0)
    for x, size in enumerate(SIZE_ORDER):
        subset = paired[paired["size_bucket"] == size]
        values = (subset["semantic__mean_tokens"] / subset["single_segment__mean_tokens"]).to_numpy()
        if np.any(values <= 0):
            raise ValueError(f"Token ratios must be strictly positive for the log scale: {size}")
        jitter = _deterministic_jitter(len(values), f"token:{size}", 0.13)
        ax.scatter(x + jitter, values, s=10, color=GREY, alpha=0.28, edgecolors="none", zorder=2)
        row = figure_summary[
            (figure_summary["metric"] == "token_ratio")
            & (figure_summary["size_bucket"] == size)
        ].iloc[0]
        mean = row["estimate"]
        errors = np.array([[mean - row["ci95_low"]], [row["ci95_high"] - mean]])
        ax.errorbar(
            x,
            mean,
            yerr=errors,
            fmt="D",
            markersize=4.0,
            color=PURPLE,
            markeredgecolor="white",
            markeredgewidth=0.5,
            capsize=2.4,
            elinewidth=1.1,
            zorder=4,
        )
    ax.set_yscale("log")
    ax.yaxis.set_major_locator(FixedLocator([1, 2, 4, 8]))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:g}×"))
    ax.set_ylim(0.75, 12)
    ax.set_xticks(range(4), [f"{size}\nn={SIZE_N[size]}" for size in SIZE_ORDER])
    ax.grid(axis="y", which="major", color="#E5E7EB", linewidth=0.45, zorder=0)
    ax.tick_params(length=2.5, width=0.6)
    ax.set_title("End-to-end token cost", loc="left", fontweight="bold", pad=5)
    ax.set_ylabel("Token ratio\n(Semantic / single)")
    _panel_label(ax, "d")

    base = output / "formal_experiment_tradeoffs"
    fig.canvas.draw()
    require_matplotlib_panel_alignment(
        fig,
        json_out=f"{base}.alignment.json",
        overlay_svg=f"{base}.alignment.svg",
        tolerance_pt=1.5,
        gutter_tolerance_pt=1.5,
        require_panel_labels=True,
        strict=True,
    )
    fig.savefig(f"{base}.pdf")
    fig.savefig(f"{base}.svg")
    fig.savefig(f"{base}.png", dpi=600)
    fig.savefig(f"{base}.tiff", dpi=600, pil_kwargs={"compression": "tiff_lzw"})
    plt.close(fig)

    caption = """# Figure contract and caption

## Figure-level claim

Semantic segmentation yields small, non-monotonic quality differences across complexity groups, while consistently increasing token cost and sometimes reducing completion or soundness.

## Caption

**Quality, reliability and cost differences between semantic-segmented and single-pass BPMN generation.** Each point represents one PMo process after averaging the three planned repetitions within a strategy (S, n=8; M, n=25; L, n=13; XL, n=4). **a**, Paired difference in PMo-compatible Task-name Dice@0.7. **b**, Paired differences in Task Semantic Alignment, Gateway type-role count F1 and Matched Task-pair Coverage@0.7. **c**, Paired differences in completion and soundness rates, both using the three planned runs per process as denominator. In a–c, large symbols show the mean paired difference and error bars show process-level bootstrap 95% confidence intervals (20,000 resamples); the dashed line denotes no difference. **d**, End-to-end token ratio; large diamonds show the geometric mean ratio and process-level bootstrap 95% confidence intervals, and the dashed line denotes equal cost. Quality scores are averaged only over successful outputs; failed trials remain represented through completion and soundness. Relation F1 is omitted from the figure because its interpretation is conditional on matched-pair coverage and is reported together with coverage in the statistical tables. XL estimates are exploratory because only four processes fall in that group.

## Data provenance

Input: `../paired_flow_differences.csv` and `../complexity_paired_statistics.csv`. Plot-level source and summary values are preserved as `formal_experiment_figure_source.csv` and `formal_experiment_figure_summary.csv`.
"""
    (output / "formal_experiment_tradeoffs_caption.md").write_text(caption, encoding="utf-8")
    print(base)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
