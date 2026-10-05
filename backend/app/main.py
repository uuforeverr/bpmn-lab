from __future__ import annotations

from threading import Lock

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .config import load_settings
from .pipeline import Pipeline
from .store import RunStore

settings = load_settings()
store = RunStore(settings.app.database_path)
store.fail_running("后端进程在模型调用期间中断，请重试当前步骤")
pipeline = Pipeline(settings)
active_runs: set[str] = set()
active_runs_lock = Lock()
app = FastAPI(title="BPMN Incremental Generation Lab", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=settings.app.cors_origins, allow_methods=["*"], allow_headers=["*"])


class CreateRun(BaseModel):
    inputText: str
    strategy: str = "semantic"


STAGE_ACTIVITY = {
    "INPUT_READY": "sentence_splitter",
    "SENTENCES_PREPARED": "semantic_resolver",
    "SEMANTIC_RESOLVED": "planner",
    "CONTEXT_BUILT": "generator",
    "SUBGRAPH_GENERATED": "pipeline",
    "STRUCTURE_VALIDATED": "reviewer",
    "SEMANTIC_REVIEWED": "pipeline",
    "GENERATOR_RECONSIDERING": "generator",
    "GENERATOR_DECIDED": "generator",
    "REPAIRING": "repair",
    "FINAL_VALIDATED": "bpmn_layout",
}


def claim_run(run_id: str):
    with active_runs_lock:
        if run_id in active_runs:
            raise HTTPException(409, "run is already executing")
        active_runs.add(run_id)


def release_run(run_id: str):
    with active_runs_lock:
        active_runs.discard(run_id)


def mark_running(run: dict):
    state = run["state"]
    state.pop("executionError", None)
    state["reviewerEnabled"] = settings.pipeline.reviewer_enabled
    if (run["status"] == "failed" and run["stage"] == "REPAIRING"
            and state.get("repairCount", 0) >= settings.pipeline.max_semantic_repairs):
        state["repairCount"] = 0
    active_agent = STAGE_ACTIVITY.get(run["stage"], "pipeline")
    if run["stage"] in {
        "STRUCTURE_VALIDATED", "SEMANTIC_REVIEWED",
        "GENERATOR_RECONSIDERING", "GENERATOR_DECIDED",
    } and not settings.pipeline.reviewer_enabled:
        active_agent = "pipeline"
    state["activeAgent"] = active_agent
    store.save(run["id"], "running", run["stage"], state, {"status": "running", "activeAgent": state["activeAgent"]})


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "modelConfigured": bool(settings.llm.model and settings.llm.resolved_key()),
        "thinkingModes": {
            agent: settings.llm.thinking_for(agent)
            for agent in ("semantic_resolver", "planner", "generator", "reviewer", "repair_plan", "repair")
        },
        "layoutConfigured": settings.layout.enabled,
        "layoutAvailable": bool(pipeline.layout and pipeline.layout.health()),
        "reviewerEnabled": settings.pipeline.reviewer_enabled,
    }


@app.get("/api/runs")
def list_runs(): return store.list()


@app.post("/api/runs")
def create_run(body: CreateRun):
    if body.strategy not in {"semantic", "fixed_length", "single_segment"}: raise HTTPException(400, "unsupported strategy")
    state = pipeline.initial_state(body.inputText, body.strategy)
    run_id = store.create(body.inputText, body.strategy, state)
    return store.get(run_id)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    run = store.get(run_id)
    if not run: raise HTTPException(404, "run not found")
    return run


@app.post("/api/runs/{run_id}/step")
def step_run(run_id: str):
    run = store.get(run_id)
    if not run: raise HTTPException(404, "run not found")
    if run["stage"] == "COMPLETED": return run
    claim_run(run_id)
    try:
        mark_running(run)
        stage, state = pipeline.step(run["stage"], run["state"], run["input_text"])
        state.pop("activeAgent", None)
        status = "completed" if stage == "COMPLETED" else "waiting"
        store.save(run_id, status, stage, state, {"status": status})
    except Exception as exc:
        run["state"].pop("activeAgent", None)
        store.save(run_id, "failed", run["stage"], run["state"], {"error": str(exc)})
        raise HTTPException(422, str(exc))
    finally:
        release_run(run_id)
    return store.get(run_id)


@app.post("/api/runs/{run_id}/continue")
def continue_run(run_id: str):
    claim_run(run_id)
    try:
        for _ in range(200):
            run = store.get(run_id)
            if not run: raise HTTPException(404, "run not found")
            if run["stage"] == "COMPLETED": return run
            mark_running(run)
            stage, state = pipeline.step(run["stage"], run["state"], run["input_text"])
            state.pop("activeAgent", None)
            store.save(run_id, "completed" if stage == "COMPLETED" else "running", stage, state)
        raise RuntimeError("pipeline exceeded step limit")
    except HTTPException:
        raise
    except Exception as exc:
        run["state"].pop("activeAgent", None)
        store.save(run_id, "failed", run["stage"], run["state"], {"error": str(exc)})
        raise HTTPException(422, str(exc))
    finally:
        release_run(run_id)
