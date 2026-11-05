from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.process_quality import assess_soundness


EXPERIMENT_ID = "pmo_full_50_seed20261017"
STRATEGIES = ("semantic", "single_segment")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    backend = Path(__file__).resolve().parents[1]
    root = backend / "results" / EXPERIMENT_ID
    output = root / "combined"
    evaluator = backend / "app" / "process_quality.py"
    rows: list[dict[str, Any]] = []

    for batch_number in range(1, 6):
        path = root / f"batch{batch_number:02d}" / f"batch{batch_number:02d}.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        for run in report["runs"]:
            original = ((run.get("quality") or {}).get("soundness") or {})
            recomputed = (
                assess_soundness(run["bpmnXml"])
                if run.get("success") and run.get("bpmnXml")
                else None
            )
            conversion = (recomputed or {}).get("conversion") or {}
            diagnostics = (recomputed or {}).get("diagnostics") or {}
            rows.append({
                "batch": batch_number,
                "sample": run["sample"],
                "repetition": run["repetition"],
                "strategy": run["strategy"],
                "pipeline_success": bool(run.get("success")),
                "original_protocol": original.get("protocol"),
                "original_calculation_succeeded": original.get("calculationSucceeded"),
                "original_sound": original.get("sound"),
                "v4_protocol": (recomputed or {}).get("protocol"),
                "v4_calculation_succeeded": (recomputed or {}).get("calculationSucceeded"),
                "v4_sound": (recomputed or {}).get("sound"),
                "sound_changed": (
                    recomputed is not None and original.get("sound") != recomputed.get("sound")
                ),
                "normalized_start_event_count": len(conversion.get("normalizedStartEvents", [])),
                "normalized_intermediate_event_count": len(
                    conversion.get("normalizedIntermediateEvents", [])
                ),
                "reason": (recomputed or {}).get("reason"),
                "diagnostic_messages": diagnostics.get("Outputs.DIAGNOSTIC_MESSAGES"),
            })

    summaries: dict[str, dict[str, Any]] = {}
    for strategy in STRATEGIES:
        selected = [row for row in rows if row["strategy"] == strategy]
        successful = [row for row in selected if row["pipeline_success"]]
        assessable = [row for row in successful if row["v4_calculation_succeeded"]]
        sound = [row for row in assessable if row["v4_sound"]]
        summaries[strategy] = {
            "plannedRuns": len(selected),
            "successfulRuns": len(successful),
            "assessableRuns": len(assessable),
            "soundRuns": len(sound),
            "completionRate": len(successful) / len(selected),
            "soundnessRatePlanned": len(sound) / len(selected),
            "soundnessRateSuccessful": len(sound) / len(successful),
        }

    changed = [row for row in rows if row["sound_changed"]]
    result = {
        "schemaVersion": "1.0",
        "experimentId": EXPERIMENT_ID,
        "analysis": "versioned offline Soundness recomputation; original batch JSON is unchanged",
        "protocol": "pm4py_workflow_net_soundness_with_event_abstraction_v4",
        "evaluatorSha256": hashlib.sha256(evaluator.read_bytes()).hexdigest(),
        "rows": rows,
        "strategySummaries": summaries,
        "changedRows": changed,
    }
    (output / "soundness_v4_recomputed.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_csv(output / "soundness_v4_recomputed.csv", rows)

    lines = [
        "# Soundness v4 离线重算",
        "",
        "本文件只从五批原始 BPMN 产物离线重算 Soundness，不覆盖原始 batch JSON。v4 将多出边 Start Event 显式转换为 Start 后接静默 Parallel Split。",
        "",
        "| 策略 | Completion | Assessable/成功 | Sound/计划 | Sound/成功 |",
        "|---|---:|---:|---:|---:|",
    ]
    for strategy in STRATEGIES:
        row = summaries[strategy]
        lines.append(
            f"| {strategy} | {row['successfulRuns']}/{row['plannedRuns']} = {row['completionRate']:.2%} | "
            f"{row['assessableRuns']}/{row['successfulRuns']} | "
            f"{row['soundRuns']}/{row['plannedRuns']} = {row['soundnessRatePlanned']:.2%} | "
            f"{row['soundRuns']}/{row['successfulRuns']} = {row['soundnessRateSuccessful']:.2%} |"
        )
    lines.extend([
        "",
        f"Sound 结果发生变化的 trial：{len(changed)} 个。",
        "",
        "| Batch | 流程 | 重复 | 策略 | 原结果 | v4 | 归一化 Start 数 |",
        "|---:|---:|---:|---|---:|---:|---:|",
    ])
    for row in changed:
        lines.append(
            f"| {row['batch']:02d} | {row['sample']} | {row['repetition']} | "
            f"{row['strategy']} | {row['original_sound']} | {row['v4_sound']} | "
            f"{row['normalized_start_event_count']} |"
        )
    (output / "soundness_v4_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({
        "rows": len(rows),
        "changedRows": len(changed),
        "strategySummaries": summaries,
        "output": str(output),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
