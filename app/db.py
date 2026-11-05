"""Tiny SQLite store for job records and staged uploads.

On free hosts the disk is ephemeral.  The design therefore never *depends*
on this store: the runner's completion callback is an upsert and the GitHub
run itself is always the fallback source of truth for status.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    state       TEXT NOT NULL,          -- created | dispatched | completed | cancelled | error
    upload_id   TEXT,
    run_id      INTEGER,
    run_url     TEXT,
    fetches     INTEGER NOT NULL DEFAULT 0,
    spec        TEXT NOT NULL DEFAULT '{}',
    result      TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created_at);
CREATE TABLE IF NOT EXISTS uploads (
    upload_id   TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    size_bytes  INTEGER NOT NULL,
    info        TEXT NOT NULL DEFAULT '{}'
);
"""

_JSON_COLS = {"spec", "result"}


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        out = dict(row)
        for col in _JSON_COLS:
            raw = out.get(col)
            out[col] = json.loads(raw) if raw else None
        return out

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, args)

    # -- jobs --------------------------------------------------------------
    def create_job(self, job_id: str, spec: dict, upload_id: str | None, state: str = "created") -> None:
        now = time.time()
        self._exec(
            "INSERT INTO jobs(job_id, created_at, updated_at, state, upload_id, spec) VALUES (?,?,?,?,?,?)",
            (job_id, now, now, state, upload_id, json.dumps(spec)),
        )

    def get_job(self, job_id: str) -> dict | None:
        return self._row(self._exec("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        for col in _JSON_COLS & fields.keys():
            fields[col] = json.dumps(fields[col]) if fields[col] is not None else None
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE jobs SET {cols} WHERE job_id=?", (*fields.values(), job_id))

    def upsert_completed(self, job_id: str, result: dict, spec: dict) -> None:
        now = time.time()
        with self._lock:
            if self.get_job(job_id):
                self.update_job(job_id, state="completed", result=result)
            else:
                self._exec(
                    "INSERT INTO jobs(job_id, created_at, updated_at, state, spec, result) VALUES (?,?,?,?,?,?)",
                    (job_id, now, now, "completed", json.dumps(spec), json.dumps(result)),
                )

    def list_jobs(self, limit: int = 20) -> list[dict]:
        rows = self._exec("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def active_count(self, max_age_s: float) -> int:
        cutoff = time.time() - max_age_s
        return self._exec(
            "SELECT COUNT(*) FROM jobs WHERE state IN ('created','dispatched') AND created_at > ?", (cutoff,)
        ).fetchone()[0]

    def jobs_using_upload(self, upload_id: str, exclude: str | None = None) -> int:
        return self._exec(
            "SELECT COUNT(*) FROM jobs WHERE upload_id=? AND state IN ('created','dispatched') AND job_id != ?",
            (upload_id, exclude or ""),
        ).fetchone()[0]

    def delete_job(self, job_id: str) -> None:
        self._exec("DELETE FROM jobs WHERE job_id=?", (job_id,))

    def jobs_older_than(self, seconds: float) -> list[str]:
        cutoff = time.time() - seconds
        return [r[0] for r in self._exec("SELECT job_id FROM jobs WHERE created_at < ?", (cutoff,)).fetchall()]

    def bump_fetch(self, job_id: str) -> int:
        self._exec("UPDATE jobs SET fetches = fetches + 1, updated_at=? WHERE job_id=?", (time.time(), job_id))
        job = self.get_job(job_id)
        return job["fetches"] if job else 0

    # -- uploads -----------------------------------------------------------
    def add_upload(self, upload_id: str, size: int, info: dict) -> None:
        self._exec(
            "INSERT INTO uploads(upload_id, created_at, size_bytes, info) VALUES (?,?,?,?)",
            (upload_id, time.time(), size, json.dumps(info)),
        )

    def get_upload(self, upload_id: str) -> dict | None:
        row = self._exec("SELECT * FROM uploads WHERE upload_id=?", (upload_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["info"] = json.loads(out["info"] or "{}")
        return out

    def delete_upload(self, upload_id: str) -> None:
        self._exec("DELETE FROM uploads WHERE upload_id=?", (upload_id,))

    def uploads_older_than(self, seconds: float) -> list[str]:
        cutoff = time.time() - seconds
        return [r[0] for r in self._exec("SELECT upload_id FROM uploads WHERE created_at < ?", (cutoff,)).fetchall()]
