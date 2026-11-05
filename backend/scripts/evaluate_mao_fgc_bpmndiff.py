from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from statistics import mean

from prepare_bpmndiffviz_bpmn import normalize_for_legacy_camunda

ALGORITHMS = {
    "Greedy": "greedy",
    "TabuSearch": "tabusearch",
    "Ants": "ants",
    "SimulatedAnnealing": "simulatedannealing",
}

PROCESS_NAMES = {
    "21": "Shipping",
    "22": "Subrogation",
    "23": "Credit Scoring",
    "24": "Restaurant",
}

# Table IV of the MAO paper. These are aggregates over human-created models,
# not the individual observations needed to reproduce Table V.
HUMAN_DISTANCE = {
    "21": {
        "Greedy": (75.7, 72.0, 6.0, 156.0),
        "TabuSearch": (74.4, 71.0, 7.0, 153.0),
        "Ants": (80.0, 78.0, 6.0, 159.0),
        "SimulatedAnnealing": (70.8, 71.0, 9.0, 151.0),
    },
    "22": {
        "Greedy": (65.0, 64.0, 22.0, 113.0),
        "TabuSearch": (62.8, 64.0, 20.0, 110.0),
        "Ants": (69.6, 67.0, 19.0, 115.0),
        "SimulatedAnnealing": (60.2, 61.0, 22.0, 106.0),
    },
    "23": {
        "Greedy": (77.6, 75.5, 24.0, 153.0),
        "TabuSearch": (75.0, 72.0, 22.0, 151.0),
        "Ants": (79.1, 80.0, 20.0, 156.0),
        "SimulatedAnnealing": (73.7, 73.0, 22.0, 151.0),
    },
    "24": {
        "Greedy": (99.9, 98.0, 23.0, 175.0),
        "TabuSearch": (95.5, 97.0, 23.0, 174.0),
        "Ants": (100.8, 103.0, 20.0, 178.0),
        "SimulatedAnnealing": (92.6, 95.0, 24.0, 171.0),
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate generated FG-C BPMN with the four BPMNDiffViz algorithms used by MAO."
    )
    parser.add_argument("--generated-dir", type=Path, required=True)
    parser.add_argument("--reference-dir", type=Path, required=True)
    parser.add_argument("--bpmndiffviz-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _penalty(install_dir: Path, algorithm: str, source: Path, target: Path) -> float:
    classpath = os.pathsep.join(
        [
            str(install_dir / "target" / "classes"),
            str(install_dir / "target" / "BPMNDiffViz" / "WEB-INF" / "lib" / "*"),
        ]
    )
    completed = subprocess.run(
        ["java", "-cp", classpath, "BpmnDiffCli", algorithm, str(source), str(target)],
        cwd=install_dir,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    for line in reversed(completed.stdout.splitlines()):
        try:
            return float(line.strip())
        except ValueError:
            continue
    raise RuntimeError(f"BPMNDiffViz returned no numeric penalty: {completed.stdout}")


def main() -> int:
    args = parse_args()
    generated = sorted(args.generated_dir.glob("*.bpmn"))
    pattern = re.compile(r"^(21|22|23|24)-r(\d+)-(.+)\.bpmn$")
    candidates = [(path, pattern.match(path.name)) for path in generated]
    candidates = [(path, match) for path, match in candidates if match]
    if not candidates:
        raise FileNotFoundError(
            f"No generated BPMN named <21..24>-r<repetition>-<strategy>.bpmn in {args.generated_dir}"
        )
    if not (args.bpmndiffviz_dir / "target" / "classes" / "BpmnDiffCli.class").exists():
        raise FileNotFoundError("BpmnDiffCli is not built; run setup_bpmndiffviz.ps1 first")

    rows: list[dict] = []
    compatibility_notes: dict[str, list[str]] = {}
    with tempfile.TemporaryDirectory(prefix="bpmndiffviz-fgc-") as temporary:
        staging = Path(temporary)
        normalized_references: dict[str, Path] = {}
        for sample in PROCESS_NAMES:
            source = args.reference_dir / f"{sample}.bpmn"
            destination = staging / f"reference-{sample}.bpmn"
            compatibility_notes[f"reference-{sample}"] = normalize_for_legacy_camunda(
                source, destination
            )
            normalized_references[sample] = destination

        for index, (path, match) in enumerate(candidates):
            assert match is not None
            sample, repetition, strategy = match.groups()
            normalized = staging / f"generated-{index}.bpmn"
            compatibility_notes[path.name] = normalize_for_legacy_camunda(path, normalized)
            for display_name, cli_name in ALGORITHMS.items():
                penalty = _penalty(
                    args.bpmndiffviz_dir, cli_name, normalized, normalized_references[sample]
                )
                human_mean, human_median, human_min, human_max = HUMAN_DISTANCE[sample][display_name]
                rows.append(
                    {
                        "sample": sample,
                        "process": PROCESS_NAMES[sample],
                        "repetition": int(repetition),
                        "strategy": strategy,
                        "algorithm": display_name,
                        "distance": penalty,
                        "humanMean": human_mean,
                        "humanMedian": human_median,
                        "humanMin": human_min,
                        "humanMax": human_max,
                        "belowHumanMean": penalty < human_mean,
                        "belowHumanMedian": penalty < human_median,
                    }
                )

    grouped: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for row in rows:
        grouped[(row["sample"], row["strategy"], row["algorithm"])].append(row["distance"])
    summary = [
        {
            "sample": sample,
            "process": PROCESS_NAMES[sample],
            "strategy": strategy,
            "algorithm": algorithm,
            "runs": len(values),
            "meanDistance": mean(values),
            "humanMean": HUMAN_DISTANCE[sample][algorithm][0],
            "belowHumanMean": mean(values) < HUMAN_DISTANCE[sample][algorithm][0],
        }
        for (sample, strategy, algorithm), values in sorted(grouped.items())
    ]

    payload = {
        "schemaVersion": "1.0",
        "metric": "BPMNDiffViz edit-distance penalty; lower is better",
        "rows": rows,
        "summary": summary,
        "compatibilityNormalization": compatibility_notes,
        "limitations": [
            "MAO does not report the BPMNDiffViz version, heuristic seeds, or preprocessing steps.",
            "PMo event children are reordered for the legacy Camunda parser without changing graph structure.",
            "An empty conditional event definition receives the condition 'true' for parser compatibility.",
            "Table V cannot be reconstructed from Table IV aggregates; individual human distances are required.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_path = args.output.with_suffix(".csv")
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"json={args.output.resolve()}")
    print(f"csv={csv_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
