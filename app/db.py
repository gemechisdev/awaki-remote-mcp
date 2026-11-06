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
    error       TEXT,
    build_hash  TEXT
);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created_at);
CREATE INDEX IF NOT EXISTS jobs_build_hash ON jobs(build_hash);
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    state        TEXT NOT NULL,         -- opening | ready | closing | closed | error
    upload_id    TEXT,
    run_id       INTEGER,
    run_url      TEXT,
    spec         TEXT NOT NULL DEFAULT '{}',
    expires_at   REAL NOT NULL,
    last_activity REAL NOT NULL,
    runner_seen  REAL,
    closed_at    REAL,
    retain_until REAL,
    note         TEXT,
    next_cmd     INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS commands (
    session_id   TEXT NOT NULL,
    cmd_id       TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    kind         TEXT NOT NULL,         -- exec | put | collect
    payload      TEXT NOT NULL DEFAULT '{}',
    state        TEXT NOT NULL,         -- queued | running | done | cancelled
    exit_code    INTEGER,
    timed_out    INTEGER NOT NULL DEFAULT 0,
    cancel       INTEGER NOT NULL DEFAULT 0,
    cancel_sent  INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL,
    started_at   REAL,
    finished_at  REAL,
    PRIMARY KEY (session_id, cmd_id)
);
CREATE TABLE IF NOT EXISTS uploads (
    upload_id   TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    size_bytes  INTEGER NOT NULL,
    info        TEXT NOT NULL DEFAULT '{}'
);
"""

_JSON_COLS = {"spec", "result", "payload"}


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        # Soft migration for existing DBs (ephemeral hosts usually start empty)
        try:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN build_hash TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            self._conn.execute("CREATE INDEX IF NOT EXISTS jobs_build_hash ON jobs(build_hash)")
        except sqlite3.OperationalError:
            pass

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
    def create_job(self, job_id: str, spec: dict, upload_id: str | None, state: str = "created",
                   build_hash: str | None = None) -> None:
        now = time.time()
        self._exec(
            "INSERT INTO jobs(job_id, created_at, updated_at, state, upload_id, spec, build_hash) "
            "VALUES (?,?,?,?,?,?,?)",
            (job_id, now, now, state, upload_id, json.dumps(spec), build_hash),
        )

    def find_successful_by_hash(self, build_hash: str, max_age_s: float) -> dict | None:
        """Most recent successful job with this build_hash whose artifact may still exist."""
        cutoff = time.time() - max_age_s
        row = self._exec(
            "SELECT * FROM jobs WHERE build_hash=? AND state='completed' AND created_at > ? "
            "ORDER BY created_at DESC LIMIT 1",
            (build_hash, cutoff),
        ).fetchone()
        return self._row(row)

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

    # -- sessions ----------------------------------------------------------
    def create_session(self, sid: str, spec: dict, upload_id: str | None, expires_at: float) -> None:
        now = time.time()
        self._exec(
            "INSERT INTO sessions(session_id, created_at, updated_at, state, upload_id, spec, expires_at, last_activity)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (sid, now, now, "opening", upload_id, json.dumps(spec), expires_at, now),
        )

    def get_session(self, sid: str) -> dict | None:
        return self._row(self._exec("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone())

    def update_session(self, sid: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        if "spec" in fields:
            fields["spec"] = json.dumps(fields["spec"])
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE sessions SET {cols} WHERE session_id=?", (*fields.values(), sid))

    def sessions_in_states(self, states: tuple[str, ...]) -> list[dict]:
        marks = ",".join("?" * len(states))
        rows = self._exec(f"SELECT * FROM sessions WHERE state IN ({marks})", states).fetchall()
        return [self._row(r) for r in rows]

    def list_sessions(self, limit: int = 20) -> list[dict]:
        rows = self._exec("SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def count_active_sessions(self) -> int:
        return self._exec("SELECT COUNT(*) FROM sessions WHERE state IN ('opening','ready')").fetchone()[0]

    def delete_session(self, sid: str) -> None:
        with self._lock:
            self._exec("DELETE FROM commands WHERE session_id=?", (sid,))
            self._exec("DELETE FROM sessions WHERE session_id=?", (sid,))

    def add_command(self, sid: str, kind: str, payload: dict, max_commands: int = 500) -> str | None:
        with self._lock:
            row = self._exec("SELECT next_cmd FROM sessions WHERE session_id=?", (sid,)).fetchone()
            if not row or row[0] > max_commands:
                return None
            n = row[0]
            cid = f"c{n}"
            self._exec("UPDATE sessions SET next_cmd=?, last_activity=?, updated_at=? WHERE session_id=?",
                       (n + 1, time.time(), time.time(), sid))
            self._exec(
                "INSERT INTO commands(session_id, cmd_id, seq, kind, payload, state, created_at) VALUES (?,?,?,?,?,?,?)",
                (sid, cid, n, kind, json.dumps(payload), "queued", time.time()),
            )
            return cid

    def get_command(self, sid: str, cid: str) -> dict | None:
        return self._row(self._exec("SELECT * FROM commands WHERE session_id=? AND cmd_id=?", (sid, cid)).fetchone())

    def list_commands(self, sid: str, limit: int = 200) -> list[dict]:
        rows = self._exec("SELECT * FROM commands WHERE session_id=? ORDER BY seq DESC LIMIT ?", (sid, limit)).fetchall()
        return [self._row(r) for r in reversed(rows)]

    def update_command(self, sid: str, cid: str, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE commands SET {cols} WHERE session_id=? AND cmd_id=?", (*fields.values(), sid, cid))

    def claim_next_command(self, sid: str) -> dict | None:
        with self._lock:
            row = self._exec(
                "SELECT cmd_id FROM commands WHERE session_id=? AND state='queued' AND cancel=0 ORDER BY seq LIMIT 1",
                (sid,)).fetchone()
            if not row:
                return None
            self.update_command(sid, row[0], state="running", started_at=time.time())
            return self.get_command(sid, row[0])

    def pending_cancels(self, sid: str) -> list[str]:
        rows = self._exec(
            "SELECT cmd_id FROM commands WHERE session_id=? AND state='running' AND cancel=1 AND cancel_sent=0",
            (sid,)).fetchall()
        return [r[0] for r in rows]

    def command_counts(self, sid: str) -> dict[str, int]:
        rows = self._exec("SELECT state, COUNT(*) FROM commands WHERE session_id=? GROUP BY state", (sid,)).fetchall()
        return {r[0]: r[1] for r in rows}

    def cancel_queued_commands(self, sid: str) -> None:
        self._exec("UPDATE commands SET state='cancelled', finished_at=? WHERE session_id=? AND state='queued'",
                   (time.time(), sid))

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
