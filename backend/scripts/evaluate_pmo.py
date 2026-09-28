from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from app.config import load_settings
from app.pipeline import Pipeline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a small, reproducible PMo generation batch.")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--samples", nargs="+", default=["01", "05", "09"])
    parser.add_argument("--strategy", choices=["semantic", "fixed_length", "single_segment"], default="semantic")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-steps", type=int, default=200)
    return parser.parse_args()


def ground_truth_stats(dataset: Path, sample_id: str) -> dict[str, int]:
    path = dataset / "pme" / f"{sample_id}.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        "tasks": len(payload.get("tasks", [])),
        "events": len(payload.get("events", [])),
        "gateways": len(payload.get("gateways", [])),
        "sequenceFlows": len(payload.get("sequenceFlows", [])),
    }


def run_sample(pipeline: Pipeline, dataset: Path, sample_id: str, strategy: str, max_steps: int) -> dict[str, Any]:
    text_path = dataset / "descriptions" / f"{sample_id}.txt"
    text = text_path.read_text(encoding="utf-8")
    state = pipeline.initial_state(text, strategy)
    stage = "INPUT_READY"
    started = time.perf_counter()
    print(f"[{sample_id}] start strategy={strategy}", flush=True)
    try:
        for step_number in range(max_steps):
            if stage == "COMPLETED":
                break
            print(f"[{sample_id}] step={step_number} stage={stage}", flush=True)
            stage, state = pipeline.step(stage, state, text)
        if stage != "COMPLETED":
            raise RuntimeError(f"pipeline exceeded {max_steps} steps at {stage}")
        graph = state["graph"]
        generated_stats = {
            "tasks": sum(node["kind"] == "task" for node in graph["nodes"]),
            "events": sum(node["kind"] == "event" for node in graph["nodes"]),
            "gateways": sum(node["kind"] == "gateway" for node in graph["nodes"]),
            "sequenceFlows": len(graph["edges"]),
        }
        result = {
            "sample": sample_id,
            "success": True,
            "stage": stage,
            "durationSeconds": round(time.perf_counter() - started, 3),
            "segments": len(state["segments"]),
            "nodes": len(graph["nodes"]),
            "edges": len(graph["edges"]),
            "repairs": len(state["repairHistory"]),
            "llmCalls": len(state["llmCalls"]),
            "llmUsage": state["llmCalls"],
            "generated": generated_stats,
            "groundTruth": ground_truth_stats(dataset, sample_id),
            "reviewWarnings": state.get("reviewWarnings", []),
            "agentFailures": state.get("agentFailures", []),
            "segmentPlan": state["segments"],
            "graph": graph,
        }
    except Exception as exc:
        result = {
            "sample": sample_id,
            "success": False,
            "stage": stage,
            "durationSeconds": round(time.perf_counter() - started, 3),
            "errorType": type(exc).__name__,
            "error": str(exc),
            "segments": len(state.get("segments", [])),
            "repairs": len(state.get("repairHistory", [])),
            "llmCalls": len(state.get("llmCalls", [])),
            "llmUsage": state.get("llmCalls", []),
            "issues": state.get("issues", []),
            "edits": state.get("edits", []),
            "repairHistory": state.get("repairHistory", []),
            "agentFailures": state.get("agentFailures", []),
            "groundTruth": ground_truth_stats(dataset, sample_id),
        }
    print(
        f"[{sample_id}] success={result['success']} stage={result['stage']} "
        f"calls={result['llmCalls']} repairs={result['repairs']} "
        f"seconds={result['durationSeconds']}",
        flush=True,
    )
    if not result["success"]:
        print(f"[{sample_id}] error={result['errorType']}: {result['error']}", flush=True)
    return result


def main() -> int:
    args = parse_args()
    dataset = args.dataset.resolve()
    pipeline = Pipeline(load_settings())
    results = [run_sample(pipeline, dataset, sample_id, args.strategy, args.max_steps) for sample_id in args.samples]
    report = {
        "strategy": args.strategy,
        "dataset": str(dataset),
        "summary": {
            "total": len(results),
            "successful": sum(item["success"] for item in results),
            "failed": sum(not item["success"] for item in results),
        },
        "results": results,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"report={args.output.resolve()}", flush=True)
    else:
        print(rendered)
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
