"""Interactive sessions: a short-lived GitHub Actions job that behaves like a remote shell.

The runner connects OUT to this service and long-polls for work, so no inbound access to GitHub
is needed.  Commands are queued here, executed on the runner, and their output is streamed back
in ~1 s chunks into per-command log files that the caller can read incrementally.
Everything (workspace on the runner, logs/artifacts here) expires automatically.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, AsyncIterator

from .config import Settings, derive_session_token
from .db import Database
from .github import GitHub, GitHubError
from .jobs import (BuildError, UPLOAD_ID_RE, _clean_text, _iso, _safe_rel, validate_env, validate_git,
                   validate_toolchain, validate_workdir)
from .presets import PRESETS

SESSION_ID_RE = re.compile(r"rs-[a-f0-9]{16}")
CMD_ID_RE = re.compile(r"c\d{1,5}")
ACTIVE = ("opening", "ready")
MARKER = b"\n[output truncated: per-command log limit reached]\n"


def _clamp(value: int | None, default: int, lo: int, hi: int) -> int:
    try:
        v = int(value) if value is not None else default
    except (TypeError, ValueError):
        v = default
    return max(lo, min(v, hi))


class SessionService:
    def __init__(self, settings: Settings, db: Database, gh: GitHub | None, builds):
        self.s, self.db, self.gh, self.builds = settings, db, gh, builds
        self.root = settings.data_dir / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ utils
    @staticmethod
    def check_id(sid: str) -> str:
        if not isinstance(sid, str) or not SESSION_ID_RE.fullmatch(sid):
            raise BuildError("Invalid session_id (expected something like 'rs-0123456789abcdef')")
        return sid

    @staticmethod
    def check_cmd(cid: str) -> str:
        if not isinstance(cid, str) or not CMD_ID_RE.fullmatch(cid):
            raise BuildError("Invalid cmd_id (expected something like 'c3')")
        return cid

    def sdir(self, sid: str) -> Path:
        return self.root / sid

    def cmd_log(self, sid: str, cid: str) -> Path:
        return self.sdir(sid) / f"{cid}.log"

    def art_dir(self, sid: str) -> Path:
        return self.sdir(sid) / "artifacts"

    def _get(self, sid: str) -> dict:
        s = self.db.get_session(self.check_id(sid))
        if not s:
            raise BuildError("Unknown session (it may have expired and been deleted, or the service restarted)")
        return s

    def touch(self, sid: str) -> None:
        self.db.update_session(sid, last_activity=time.time())

    # -------------------------------------------------------------------- open
    async def open(self, *, upload_id: str | None = None, git_url: str | None = None, git_ref: str | None = None,
                   language: str = "generic", working_directory: str | None = None,
                   env: dict[str, str] | None = None, toolchain: dict[str, str] | None = None,
                   ttl_minutes: int | None = None, idle_minutes: int | None = None,
                   retain_minutes: int | None = None, label: str | None = None,
                   webhook_url: str | None = None) -> dict:
        problems = self.s.problems()
        if problems:
            raise BuildError("Service is not fully configured: " + "; ".join(problems))
        if self.gh is None:
            raise BuildError("GitHub is not configured")
        self.builds.check_setup()
        language = (language or "generic").strip().lower()
        if language not in PRESETS:
            raise BuildError(f"language must be one of: {', '.join(PRESETS)}")
        if bool(upload_id) == bool(git_url):
            raise BuildError("Provide exactly one source: upload_id (from POST /upload) or git_url (public https repo)")
        if upload_id:
            if not UPLOAD_ID_RE.fullmatch(upload_id):
                raise BuildError("Invalid upload_id")
            if not self.builds.upload_path(upload_id).exists() or not self.db.get_upload(upload_id):
                raise BuildError("Upload not found or expired; POST the source ZIP to /upload again")
            source: dict[str, Any] = {"type": "upload"}
        else:
            source = validate_git(git_url, git_ref)
        if self.db.count_active_sessions() >= self.s.max_concurrent_sessions:
            raise BuildError(f"{self.s.max_concurrent_sessions} sessions are already active; close one first")

        ttl = _clamp(ttl_minutes, 60, 5, self.s.max_session_minutes)
        idle = _clamp(idle_minutes, self.s.session_idle_minutes, 5, 240)
        retain = _clamp(retain_minutes, self.s.session_retain_minutes, 1, 1440)
        webhook = None
        if webhook_url:
            from urllib.parse import urlparse
            u = urlparse(webhook_url.strip())
            if u.scheme != "https" or not u.netloc or len(webhook_url) > 500:
                raise BuildError("webhook_url must be a short https URL")
            webhook = webhook_url.strip()
        sid = "rs-" + secrets.token_hex(8)
        spec = {
            "label": _clean_text(label or "", 80) or language, "language": language,
            "ttl_minutes": ttl, "idle_minutes": idle, "retain_minutes": retain,
            "workdir": validate_workdir(working_directory), "env": validate_env(env),
            "toolchain": validate_toolchain(toolchain), "source": source,
            "max_extract_mb": self.s.max_extract_mb, "max_artifact_mb": self.s.max_artifact_mb,
        }
        if webhook:
            spec["webhook_url"] = webhook
        self.db.create_session(sid, spec, upload_id, time.time() + ttl * 60)
        try:
            run_id = await self.gh.dispatch(self.s.github_ref, {"job_id": sid, "language": language, "mode": "session"})
            if run_id is None:
                for _ in range(5):
                    await asyncio.sleep(1.5)
                    run = await self.gh.find_run(sid)
                    if run:
                        run_id = run["id"]
                        break
            run_url = f"https://github.com/{self.s.github_repo}/actions/runs/{run_id}" if run_id else None
            self.db.update_session(sid, run_id=run_id, run_url=run_url)
        except Exception as exc:
            self.db.update_session(sid, state="error", note=str(exc)[:300], closed_at=time.time(),
                                   retain_until=time.time() + 60)
            raise
        return {
            "session_id": sid, "state": "opening", "language": language, "label": spec["label"],
            "expires_at": _iso(time.time() + ttl * 60), "idle_timeout_minutes": idle,
            "artifacts_kept_minutes_after_close": retain, "run_url": run_url,
            "next": "The runner needs ~30-90 s to start. You can call exec_command right away (it queues until the "
                    "runner connects) or wait_for_session first.",
        }

    # ------------------------------------------------------------------ status
    async def _check_opening(self, s: dict) -> dict:
        if s["state"] != "opening":
            return s
        age = time.time() - s["created_at"]
        if age > 75 and s["run_id"] and self.gh:
            try:
                run = await self.gh.get_run(s["run_id"])
            except GitHubError:
                return s
            if run.get("status") == "completed":
                self._finish(s, "error", "The GitHub run ended before the session started (a setup step failed). "
                                         "Open run_url to see which step.")
                return self.db.get_session(s["session_id"]) or s
        return s

    def _finish(self, s: dict, state: str, note: str) -> None:
        now = time.time()
        retain = int((s["spec"] or {}).get("retain_minutes", self.s.session_retain_minutes))
        self.db.update_session(s["session_id"], state=state, note=note, closed_at=s["closed_at"] or now,
                               retain_until=now + retain * 60)
        self.db.cancel_queued_commands(s["session_id"])
        url = (s.get("spec") or {}).get("webhook_url")
        if url:
            try:
                payload = {
                    "event": "session.finished",
                    "session_id": s["session_id"],
                    "state": state,
                    "note": note,
                    "label": (s.get("spec") or {}).get("label"),
                    "language": (s.get("spec") or {}).get("language"),
                }
                asyncio.get_event_loop().create_task(self._fire_webhook(url, payload))
            except RuntimeError:
                pass

    async def _fire_webhook(self, url: str, payload: dict) -> None:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.post(url, json=payload, headers={"User-Agent": "remote-build-mcp/webhook"})
        except Exception:
            pass

    async def status(self, sid: str) -> dict:
        s = await self._check_opening(self._get(sid))
        now = time.time()
        state = s["state"]
        if state == "ready" and s["runner_seen"] and now - s["runner_seen"] > 120:
            state = "lost"
        counts = self.db.command_counts(sid)
        recent = [{"cmd_id": c["cmd_id"], "kind": c["kind"], "state": c["state"], "exit_code": c["exit_code"],
                   "command": (c["payload"].get("command") or c["kind"])[:100]}
                  for c in self.db.list_commands(sid, 5)]
        spec = s["spec"] or {}
        out: dict[str, Any] = {
            "session_id": sid, "state": state, "label": spec.get("label"), "language": spec.get("language"),
            "run_url": s["run_url"], "created_at": _iso(s["created_at"]),
            "seconds_left": max(0, int(s["expires_at"] - now)) if state in (*ACTIVE, "lost") else 0,
            "commands": {"running": counts.get("running", 0), "queued": counts.get("queued", 0),
                         "finished": counts.get("done", 0) + counts.get("cancelled", 0)},
            "recent_commands": recent, "artifacts": self.artifacts(sid),
        }
        if state in ACTIVE:
            idle_left = spec.get("idle_minutes", 30) * 60 - (now - s["last_activity"])
            out["idle_seconds_left"] = max(0, int(idle_left)) if not counts.get("running") else None
        if s["note"]:
            out["note"] = s["note"]
        if s["retain_until"] and state not in ACTIVE:
            out["data_kept_until"] = _iso(s["retain_until"])
        return out

    async def wait_ready(self, sid: str, max_wait_seconds: int = 50) -> dict:
        end = time.time() + _clamp(max_wait_seconds, 50, 1, 55)
        while True:
            st = await self.status(sid)
            if st["state"] != "opening" or time.time() + 3 > end:
                st["ready"] = st["state"] == "ready"
                return st
            await asyncio.sleep(3)

    def list(self, limit: int = 20) -> list[dict]:
        out = []
        for s in self.db.list_sessions(_clamp(limit, 20, 1, 100)):
            spec = s["spec"] or {}
            out.append({"session_id": s["session_id"], "label": spec.get("label"), "language": spec.get("language"),
                        "state": s["state"], "created_at": _iso(s["created_at"]), "run_url": s["run_url"]})
        return out

    # ---------------------------------------------------------------- commands
    def _require_active(self, s: dict) -> None:
        if s["state"] not in ACTIVE:
            raise BuildError(f"Session is {s['state']}" + (f" ({s['note']})" if s["note"] else "")
                             + "; open a new session")

    def _enqueue(self, sid: str, kind: str, payload: dict) -> str:
        s = self._get(sid)
        self._require_active(s)
        cid = self.db.add_command(sid, kind, payload)
        if cid is None:
            raise BuildError("Too many commands in this session (limit 500); open a new session")
        return cid

    async def exec(self, sid: str, command: str, cwd: str | None = None, env: dict[str, str] | None = None,
                   timeout_seconds: int | None = 600, wait_seconds: int = 25, max_chars: int = 30000) -> dict:
        if not command or not command.strip():
            raise BuildError("command is empty")
        if len(command) > 20000:
            raise BuildError("command is too long (max 20000 characters)")
        s = self._get(sid)
        remaining = max(10, int(s["expires_at"] - time.time()))
        payload = {"command": command, "cwd": validate_workdir(cwd), "env": validate_env(env),
                   "timeout_seconds": _clamp(timeout_seconds, 600, 1, min(remaining, 3 * 3600))}
        cid = self._enqueue(sid, "exec", payload)
        return await self._after_enqueue(sid, cid, wait_seconds, max_chars)

    async def put(self, sid: str, upload_id: str, dest: str | None = None, mode: str = "overlay",
                  keep: list[str] | None = None, wait_seconds: int = 120) -> dict:
        if not UPLOAD_ID_RE.fullmatch(upload_id or ""):
            raise BuildError("Invalid upload_id")
        if not self.builds.upload_path(upload_id).exists():
            raise BuildError("Upload not found or expired; POST the ZIP to /upload again")
        if mode not in {"overlay", "replace"}:
            raise BuildError("mode must be 'overlay' (add/overwrite files) or 'replace' (wipe first, keeping `keep`)")
        keep_names = [k for k in (keep or ["node_modules", ".gradle", "build", "target", ".cache", ".venv", "dist"])
                      if re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", k)]
        cid = self._enqueue(sid, "put", {"upload_id": upload_id, "dest": validate_workdir(dest), "mode": mode,
                                         "keep": keep_names})
        return await self._after_enqueue(sid, cid, wait_seconds, 10000)

    async def collect(self, sid: str, paths: str, package: str = "auto", name: str | None = None,
                      cwd: str | None = None, wait_seconds: int = 120) -> dict:
        lines = [ln.strip() for ln in (paths or "").replace(",", "\n").splitlines() if ln.strip()]
        if not lines or len(lines) > 20 or any(len(ln) > 300 for ln in lines):
            raise BuildError("paths: 1-20 newline-separated globs relative to the project (supports **)")
        lines = [_safe_rel(ln, "paths entry") for ln in lines]
        if package not in {"auto", "zip"}:
            raise BuildError("package must be 'auto' or 'zip'")
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "-", name or "")[:60].strip("-.") or None
        cid = self._enqueue(sid, "collect", {"paths": lines, "package": package, "name": safe_name,
                                             "cwd": validate_workdir(cwd)})
        return await self._after_enqueue(sid, cid, wait_seconds, 10000)

    async def _after_enqueue(self, sid: str, cid: str, wait_seconds: int, max_chars: int) -> dict:
        view = await self.read_output(sid, cid, 0, max_chars=max_chars, wait_seconds=wait_seconds, tail=True)
        if not view["finished"]:
            view["hint"] = (f"Still {view['state']}. Call read_output(session_id, '{cid}', offset={view['next_offset']}, "
                            "wait_seconds=25) for more, or cancel_command.")
        return view

    async def read_output(self, sid: str, cid: str, offset: int = 0, max_chars: int = 30000,
                          wait_seconds: int = 0, tail: bool = False) -> dict:
        self._get(sid)
        self.check_cmd(cid)
        end = time.time() + _clamp(wait_seconds, 0, 0, 55)
        offset = max(0, int(offset or 0))
        path = self.cmd_log(sid, cid)
        while True:
            c = self.db.get_command(sid, cid)
            if not c:
                raise BuildError("Unknown cmd_id for this session")
            size = path.stat().st_size if path.exists() else 0
            finished = c["state"] in ("done", "cancelled")
            if size > offset or finished or time.time() >= end:
                break
            await asyncio.sleep(0.4)
        self.touch(sid)
        limit = _clamp(max_chars, 30000, 500, 200000)
        start, skipped = offset, False
        if tail and size - start > limit:
            start, skipped = size - limit, True
        data = b""
        if size > start:
            with path.open("rb") as fh:
                fh.seek(start)
                data = fh.read(limit)
        payload = c["payload"] or {}
        return {
            "session_id": sid, "cmd_id": cid, "kind": c["kind"],
            "command": (payload.get("command") or "")[:200] or None,
            "state": c["state"], "finished": finished, "exit_code": c["exit_code"], "timed_out": bool(c["timed_out"]),
            "output": data.decode("utf-8", "replace"), "from_offset": start, "next_offset": start + len(data),
            "total_bytes": size, "earlier_output_skipped": skipped,
        }

    async def cancel_command(self, sid: str, cid: str) -> dict:
        self._get(sid)
        c = self.db.get_command(sid, self.check_cmd(cid))
        if not c:
            raise BuildError("Unknown cmd_id for this session")
        if c["state"] in ("done", "cancelled"):
            return {"cmd_id": cid, "cancelled": False, "reason": f"already {c['state']}"}
        if c["state"] == "queued":
            self.db.update_command(sid, cid, state="cancelled", cancel=1, finished_at=time.time())
        else:
            self.db.update_command(sid, cid, cancel=1)  # runner is told on its next poll
        return {"cmd_id": cid, "cancelled": True}

    def transcript(self, sid: str, max_chars: int = 20000) -> dict:
        self._get(sid)
        parts: list[str] = []
        for c in self.db.list_commands(sid, 12):
            p = c["payload"] or {}
            title = p.get("command") or f"[{c['kind']}]"
            f = self.cmd_log(sid, c["cmd_id"])
            body = f.read_bytes()[-2500:].decode("utf-8", "replace") if f.exists() else ""
            parts.append(f"$ {title}\n{body.rstrip()}\n[{c['cmd_id']} {c['state']} exit={c['exit_code']}]")
        return {"session_id": sid, "transcript": "\n\n".join(parts)[-_clamp(max_chars, 20000, 1000, 100000):]}

    # --------------------------------------------------------------- artifacts
    def artifacts(self, sid: str) -> list[dict]:
        d = self.art_dir(sid)
        s = self.db.get_session(sid)
        if not d.exists() or not s:
            return []
        retain = int((s["spec"] or {}).get("retain_minutes", 30))
        until = s["retain_until"] or (s["expires_at"] + retain * 60)
        return [{"name": f.name, "size_bytes": f.stat().st_size, "download": f"/api/sessions/{sid}/artifacts/{f.name}",
                 "available_until_at_least": _iso(until)}
                for f in sorted(d.iterdir()) if f.is_file() and not f.name.endswith(".part")]

    def artifact_path(self, sid: str, name: str) -> Path | None:
        self.check_id(sid)
        safe = Path(name).name
        path = self.art_dir(sid) / safe
        return path if safe == name and path.is_file() and not safe.endswith(".part") else None

    # ------------------------------------------------------------ close/delete
    async def _cancel_run(self, s: dict) -> None:
        if s["run_id"] and self.gh:
            try:
                await self.gh.cancel_run(s["run_id"])
            except GitHubError:
                pass

    async def close(self, sid: str, retain_minutes: int | None = None, reason: str = "closed on request") -> dict:
        s = self._get(sid)
        if s["state"] in ACTIVE:
            if retain_minutes is not None:
                spec = s["spec"]
                spec["retain_minutes"] = _clamp(retain_minutes, 30, 1, 1440)
                self.db.update_session(sid, spec=spec)
                s = self.db.get_session(sid)
            await self._close(s, reason, cancel_now=s["state"] == "opening")
        return await self.status(sid)

    async def _close(self, s: dict, reason: str, cancel_now: bool = False) -> None:
        sid = s["session_id"]
        self._finish(s, "closed" if cancel_now else "closing", reason)
        if cancel_now:
            await self._cancel_run(s)

    async def delete(self, sid: str) -> dict:
        s = self._get(sid)
        if s["state"] in (*ACTIVE, "closing"):
            await self._cancel_run(s)
        self._wipe(sid)
        return {"session_id": sid, "deleted": True}

    def _wipe(self, sid: str) -> None:
        shutil.rmtree(self.sdir(sid), ignore_errors=True)
        self.db.delete_session(sid)

    async def sweep(self) -> None:
        now = time.time()
        for s in self.db.sessions_in_states(ACTIVE):
            sid, spec = s["session_id"], s["spec"] or {}
            running = self.db.command_counts(sid).get("running", 0)
            reason, cancel_now = None, False
            if now > s["expires_at"]:
                reason = "maximum session time reached"
            elif s["state"] == "ready" and s["runner_seen"] and now - s["runner_seen"] > 180:
                reason, cancel_now = "lost contact with the runner", True
            elif s["state"] == "opening" and now - s["created_at"] > 900:
                reason, cancel_now = "the runner never connected", True
            elif s["state"] == "ready" and not running and now - s["last_activity"] > spec.get("idle_minutes", 30) * 60:
                reason = f"idle for {spec.get('idle_minutes', 30)} minutes"
            if reason:
                await self._close(s, reason, cancel_now)
        for s in self.db.sessions_in_states(("closing",)):
            if now - (s["closed_at"] or now) > 45:  # the runner had its chance to exit cleanly
                await self._cancel_run(s)
                self.db.update_session(s["session_id"], state="closed")
        for s in self.db.sessions_in_states(("closing", "closed", "error")):
            if s["retain_until"] and now > s["retain_until"]:
                if s["state"] == "closing":
                    await self._cancel_run(s)
                self._wipe(s["session_id"])
        for d in self.root.glob("rs-*"):  # orphan directories (e.g. after the database was lost)
            if d.is_dir() and not self.db.get_session(d.name) and now - d.stat().st_mtime > 3600:
                shutil.rmtree(d, ignore_errors=True)

    # ----------------------------------------------------------- runner side
    def runner_spec(self, sid: str) -> dict | None:
        s = self.db.get_session(self.check_id(sid))
        if not s or s["state"] not in ACTIVE:
            return None
        return {"session_id": sid, **s["spec"], "session_token": derive_session_token(self.s.runner_token, sid)}

    def runner_source(self, sid: str) -> Path | None:
        s = self.db.get_session(self.check_id(sid))
        if not s or not s["upload_id"] or s["state"] not in ACTIVE:
            return None
        path = self.builds.upload_path(s["upload_id"])
        return path if path.exists() else None

    def runner_upload(self, sid: str, upload_id: str) -> Path | None:
        s = self.db.get_session(self.check_id(sid))
        if not s or s["state"] not in ACTIVE or not UPLOAD_ID_RE.fullmatch(upload_id):
            return None
        path = self.builds.upload_path(upload_id)
        return path if path.exists() else None

    async def runner_next(self, sid: str, wait: int) -> dict:
        end = time.time() + _clamp(wait, 25, 0, 30)
        while True:
            s = self.db.get_session(self.check_id(sid))
            if not s or s["state"] not in ACTIVE:
                return {"type": "close"}  # unknown/closing/expired: tell the runner to exit and stop burning minutes
            patch = {"runner_seen": time.time()}
            if s["state"] == "opening":
                patch["state"] = "ready"
            self.db.update_session(sid, **patch)
            cancels = self.db.pending_cancels(sid)
            if cancels:
                self.db.update_command(sid, cancels[0], cancel_sent=1)
                return {"type": "cancel", "cmd_id": cancels[0]}
            c = self.db.claim_next_command(sid)
            if c:
                return {"type": c["kind"], "cmd_id": c["cmd_id"], **c["payload"]}
            if time.time() >= end:
                return {"type": "idle"}
            await asyncio.sleep(0.4)

    def runner_output(self, sid: str, cid: str, offset: int, data: bytes) -> dict:
        self.check_cmd(cid)
        if not self.db.get_command(sid, cid):
            raise BuildError("Unknown command")
        path = self.cmd_log(sid, cid)
        path.parent.mkdir(parents=True, exist_ok=True)
        size = path.stat().st_size if path.exists() else 0
        cap = self.s.max_cmd_log_mb * 1024 * 1024
        if offset + len(data) <= size:
            return {"ok": True, "duplicate": True}
        piece = data[max(0, size - offset):]
        if size >= cap:
            return {"ok": True, "truncated": True}
        if size + len(piece) > cap:
            piece = piece[: cap - size] + MARKER
        with path.open("ab") as fh:
            fh.write(piece)
        return {"ok": True}

    def runner_done(self, sid: str, cid: str, payload: dict) -> dict:
        self.check_cmd(cid)
        c = self.db.get_command(sid, cid)
        if not c:
            raise BuildError("Unknown command")
        cancelled = bool(payload.get("cancelled"))
        code = payload.get("exit_code")
        self.db.update_command(sid, cid, state="cancelled" if cancelled else "done",
                               exit_code=int(code) if isinstance(code, int) else None,
                               timed_out=1 if payload.get("timed_out") else 0, finished_at=time.time())
        self.touch(sid)
        return {"ok": True}

    async def runner_store_artifact(self, sid: str, filename: str, chunks: AsyncIterator[bytes]) -> dict:
        self.check_id(sid)
        safe = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(filename or "artifact.bin").name)[:120].strip(". ") or "artifact.bin"
        d = self.art_dir(sid)
        d.mkdir(parents=True, exist_ok=True)
        existing = sum(f.stat().st_size for f in d.iterdir() if f.is_file() and f.name != safe)
        file_cap = self.s.max_artifact_mb * 1024 * 1024
        total_cap = self.s.max_session_artifact_mb * 1024 * 1024
        tmp = d / (safe + ".part")
        size, sha = 0, hashlib.sha256()
        try:
            with tmp.open("wb") as out:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > file_cap:
                        raise BuildError(f"Artifact exceeds the per-file limit of {self.s.max_artifact_mb} MB")
                    if existing + size > total_cap:
                        raise BuildError(f"Session artifacts exceed the total limit of {self.s.max_session_artifact_mb} MB")
                    sha.update(chunk)
                    out.write(chunk)
            tmp.replace(d / safe)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return {"filename": safe, "size_bytes": size, "sha256": sha.hexdigest()}
