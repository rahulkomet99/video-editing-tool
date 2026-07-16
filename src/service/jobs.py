"""SQLite-backed job store.

Jobs are the unit of work the API hands to the worker pool. State lives in
SQLite so it survives restarts and is queryable (status, per-job token usage,
per-key history). A single connection guarded by a lock keeps it thread-safe
across the worker threads — fine for a single node; swap for Postgres when you
scale out.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

JobStatus = Literal["queued", "running", "succeeded", "failed"]


@dataclass
class Job:
    id: str
    api_key_id: str
    status: JobStatus = "queued"
    topic: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    output_path: str | None = None
    title: str | None = None
    error: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0

    @staticmethod
    def new(api_key_id: str, topic: str | None) -> Job:
        return Job(id=uuid.uuid4().hex, api_key_id=api_key_id, topic=topic)


_COLUMNS = list(Job.__dataclass_fields__.keys())


class JobStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        cols = ", ".join(
            f"{c} {'REAL' if c in ('created_at', 'started_at', 'finished_at', 'cost') else ('INTEGER' if c in ('tokens_in', 'tokens_out') else 'TEXT')}"
            for c in _COLUMNS
        )
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS jobs ({cols}, PRIMARY KEY (id))"
        )
        self._conn.commit()

    def create(self, job: Job) -> Job:
        d = asdict(job)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO jobs ({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
                [d[c] for c in _COLUMNS],
            )
            self._conn.commit()
        return job

    def update(self, job_id: str, **fields) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET {sets} WHERE id=?", [*fields.values(), job_id]
            )
            self._conn.commit()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        return Job(**dict(row)) if row else None

    def list(self, api_key_id: str | None = None, limit: int = 50) -> list[Job]:
        q = "SELECT * FROM jobs"
        args: list = []
        if api_key_id is not None:
            q += " WHERE api_key_id=?"
            args.append(api_key_id)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [Job(**dict(r)) for r in rows]

    def usage_summary(self, api_key_id: str | None = None) -> dict:
        q = (
            "SELECT COUNT(*) n, COALESCE(SUM(tokens_in),0) ti, "
            "COALESCE(SUM(tokens_out),0) to_, COALESCE(SUM(cost),0.0) c FROM jobs"
        )
        args: list = []
        if api_key_id is not None:
            q += " WHERE api_key_id=?"
            args.append(api_key_id)
        with self._lock:
            r = self._conn.execute(q, args).fetchone()
        return {
            "jobs": r["n"],
            "tokens_in": r["ti"],
            "tokens_out": r["to_"],
            "cost_estimate": round(r["c"], 4),
        }
