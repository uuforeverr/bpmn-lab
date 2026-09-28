from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class RunStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init(self):
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              status TEXT NOT NULL, stage TEXT NOT NULL, strategy TEXT NOT NULL,
              input_text TEXT NOT NULL, state_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
              created_at TEXT NOT NULL, stage TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id, id);
            """)
            db.execute("PRAGMA optimize")

    def create(self, input_text: str, strategy: str, state: dict) -> str:
        run_id = uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (run_id, now, now, "ready", "INPUT_READY", strategy, input_text, json.dumps(state, ensure_ascii=False)))
        return run_id

    def get(self, run_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row: return None
            result = dict(row); result["state"] = json.loads(result.pop("state_json")); return result

    def save(self, run_id: str, status: str, stage: str, state: dict, event: dict | None = None):
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute("UPDATE runs SET updated_at=?, status=?, stage=?, state_json=? WHERE id=?",
                       (now, status, stage, json.dumps(state, ensure_ascii=False), run_id))
            if event is not None:
                db.execute("INSERT INTO events(run_id,created_at,stage,payload_json) VALUES(?,?,?,?)",
                           (run_id, now, stage, json.dumps(event, ensure_ascii=False)))

    def list(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT id,created_at,updated_at,status,stage,strategy,input_text FROM runs ORDER BY created_at DESC").fetchall()
            return [dict(row) for row in rows]

    def fail_running(self, reason: str) -> int:
        """Mark requests interrupted by a previous backend process as failed."""
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            rows = db.execute("SELECT id,stage,state_json FROM runs WHERE status='running'").fetchall()
            for row in rows:
                state = json.loads(row["state_json"])
                state.pop("activeAgent", None)
                state["executionError"] = reason
                db.execute(
                    "UPDATE runs SET updated_at=?,status='failed',state_json=? WHERE id=?",
                    (now, json.dumps(state, ensure_ascii=False), row["id"]),
                )
                db.execute(
                    "INSERT INTO events(run_id,created_at,stage,payload_json) VALUES(?,?,?,?)",
                    (row["id"], now, row["stage"], json.dumps({"error": reason}, ensure_ascii=False)),
                )
            return len(rows)
