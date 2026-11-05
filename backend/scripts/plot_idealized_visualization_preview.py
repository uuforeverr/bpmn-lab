from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import plot_depth31_experiment as plots


BACKEND = Path(__file__).resolve().parents[1]
RESULTS = BACKEND / "results"
ARCHIVE = RESULTS / "实验结果归档_20261023"
OBSERVED_INPUT = ARCHIVE / "02_真实实验" / "数据" / "depth31_metrics.json"
SCENARIO_ROOT = ARCHIVE / "03_优化实验" / "数据"
SCENARIO_MANIFEST = SCENARIO_ROOT / "manifest.json"
SCENARIO_PAIRED = SCENARIO_ROOT / "paired_differences.csv"
OUTPUT = ARCHIVE / "03_优化实验" / "结果图"


def load_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def number(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def paired_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source in load_csv(SCENARIO_PAIRED):
        row: dict[str, Any] = {
            "sample": source["sample"],
            "depthTier": source["depthTier"],
            "tasks": int(float(source["tasks"])),
            "taskMaxNestingDepth": int(float(source["taskMaxNestingDepth"])),
        }
        for key, value in source.items():
            if key.startswith("delta_"):
                row[key.removeprefix("delta_")] = number(value)
        rows.append(row)
    return rows


def main() -> int:
    observed = json.loads(OBSERVED_INPUT.read_text(encoding="utf-8"))
    scenario = json.loads(SCENARIO_MANIFEST.read_text(encoding="utf-8"))
    if scenario.get("datasetLabel") != "SYNTHETIC_IDEALIZED_VISUALIZATION_ONLY_V1":
        raise RuntimeError("Refusing to plot an unlabelled scenario dataset.")
    pairs = paired_rows()
    if len(pairs) != 31:
        raise RuntimeError(f"Expected 31 paired processes, found {len(pairs)}.")

    preview_report = {
        "manifest": observed["manifest"],
        "oneshot": observed["oneshot"],
        "formal": {"pairedSemanticMinusSingle": pairs},
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    plots.OUTPUT = OUTPUT
    plots.DATA_OUTPUT = SCENARIO_ROOT

    plots.SOURCE_METADATA = {
        "datasetLabel": "OBSERVED_DEPTH31_REANALYSIS_V3",
        "isSyntheticScenario": False,
        "notForEmpiricalReporting": False,
    }
    plots.SOURCE_METADATA = {
        "datasetLabel": scenario["datasetLabel"],
        "isSyntheticScenario": True,
        "notForEmpiricalReporting": True,
    }
    plots._figure2(preview_report)
    plots._figure3(preview_report)
    plots._figure4(preview_report)

    manifest = {
        "schemaVersion": "depth31-idealized-figure-preview-v1",
        "datasetLabel": scenario["datasetLabel"],
        "isSyntheticScenario": True,
        "notForEmpiricalReporting": True,
        "warning": "Figures 2a-4 are visualization-only scenario previews and must not be reported as observed experimental results.",
        "figures2aTo4": "Scenario data from paired_differences.csv. Task/Gateway/Completion follow the manifest transformations; relations, strict all-three soundness, and token cost remain observed.",
        "sourceScenarioManifest": str(SCENARIO_MANIFEST),
        "sourcePairedData": str(SCENARIO_PAIRED),
        "outputDirectory": str(OUTPUT),
    }
    (SCENARIO_ROOT / "figure_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(OUTPUT.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
