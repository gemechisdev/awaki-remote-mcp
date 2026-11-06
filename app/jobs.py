"""Core build orchestration: validate -> dispatch -> track -> report."""
from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator

from .config import Settings
from .db import Database
from .github import GitHub, GitHubError, parse_rb_result
from .presets import PRESETS, TOOLCHAIN_DEFAULTS, TOOLCHAIN_KEYS
from .security import ArchiveError, inspect_zip

JOB_ID_RE = re.compile(r"rb-[a-f0-9]{16}")
UPLOAD_ID_RE = re.compile(r"[a-f0-9]{16}")
TERMINAL = {"succeeded", "build_failed", "delivery_failed", "failed", "cancelled", "timed_out"}
_VERSION_RE = re.compile(r"[0-9A-Za-z._*+-]{1,24}")
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_RESERVED_ENV = ("RB_", "GITHUB_", "RUNNER_", "ACTIONS_")
_GIT_URL_RE = re.compile(r"https://[A-Za-z0-9.-]+(?::\d+)?/[A-Za-z0-9._~/-]+")
_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
_WORKDIR_RE = re.compile(r"[A-Za-z0-9_. /-]{1,200}")


class BuildError(ValueError):
    """A problem the caller (the AI agent) can fix; the message is shown to it."""


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def _parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _clean_text(value: str, limit: int) -> str:
    return "".join(ch for ch in value if ch.isprintable() or ch in "\n\t").strip()[:limit]


def _safe_rel(path: str, what: str) -> str:
    p = PurePosixPath(path.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts:
        raise BuildError(f"{what} must be a relative path without '..': {path!r}")
    return str(p)


def validate_env(env: dict[str, str] | None) -> dict[str, str]:
    clean: dict[str, str] = {}
    for key, value in (env or {}).items():
        if not _ENV_KEY_RE.fullmatch(str(key)) or str(key).upper().startswith(_RESERVED_ENV):
            raise BuildError(f"Invalid or reserved environment variable name: {key!r}")
        if len(str(value)) > 4096:
            raise BuildError(f"Environment variable {key} is too long")
        clean[str(key)] = str(value)
    if len(clean) > 50:
        raise BuildError("At most 50 environment variables")
    return clean


def validate_toolchain(toolchain: dict[str, str] | None) -> dict[str, str]:
    tc = dict(TOOLCHAIN_DEFAULTS)
    for key, value in (toolchain or {}).items():
        if key not in TOOLCHAIN_KEYS or not _VERSION_RE.fullmatch(str(value)):
            raise BuildError(f"toolchain supports only {sorted(TOOLCHAIN_KEYS)} with simple version strings")
        tc[key] = str(value)
    return tc


def validate_workdir(working_directory: str | None) -> str:
    if working_directory and working_directory.strip("./ "):
        if not _WORKDIR_RE.fullmatch(working_directory):
            raise BuildError("working_directory contains unsupported characters")
        return _safe_rel(working_directory.strip(), "working_directory")
    return ""


def validate_git(git_url: str | None, git_ref: str | None) -> dict:
    if not _GIT_URL_RE.fullmatch(git_url or ""):
        raise BuildError("git_url must be a plain https URL without credentials or query string")
    if git_ref and not _REF_RE.fullmatch(git_ref):
        raise BuildError("Invalid git_ref")
    return {"type": "git", "url": git_url, "ref": git_ref or ""}


def result_state(res: dict) -> tuple[str, str]:
    """Map the runner's result into (state, human detail)."""
    if res.get("ok"):
        return "succeeded", res.get("reason") or "Build finished; the artifact is ready to download."
    if res.get("timed_out"):
        return "timed_out", "The build command exceeded its time limit."
    code = res.get("exit_code")
    if code not in (0, None):
        return "build_failed", f"Build command exited with code {code}."
    if code == 0 and res.get("delivery_error"):
        return "delivery_failed", f"Build succeeded but sending the artifact to the service failed: {res['delivery_error']}"
    return "failed", res.get("reason") or "The runner stopped before the build command finished."


def extract_errors(text: str, max_lines: int = 60) -> str:
    """Pull the most useful failure lines out of a build log (Gradle, Kotlin, Rust, Go, C, Python, npm)."""
    lines = text.splitlines()
    picked: list[str] = []
    seen: set[str] = set()

    def add(line: str) -> None:
        key = line.strip()
        if key and key not in seen and len(picked) < max_lines:
            seen.add(key)
            picked.append(line.rstrip()[:400])

    for i, line in enumerate(lines):
        if line.startswith("* What went wrong"):
            for follow in lines[i: i + 12]:
                add(follow)
    pattern = re.compile(
        r"(?i)(^e: |\berror\b[:\[ ]|\bFAILED\b|\bfatal\b|panic:|undefined reference|"
        r"Traceback \(most recent|ModuleNotFoundError|SyntaxError|npm ERR!|cannot find|"
        r"No such file or directory|Execution failed for task|BUILD FAILED)"
    )
    for line in lines:
        if pattern.search(line):
            add(line)
    return "\n".join(picked)


class BuildService:
    def __init__(self, settings: Settings, db: Database, gh: GitHub | None):
        self.s = settings
        self.db = db
        self.gh = gh
        self.uploads_dir = settings.data_dir / "uploads"
        self.logs_dir = settings.data_dir / "logs"
        self.artifacts_dir = settings.data_dir / "artifacts"
        for d in (self.uploads_dir, self.logs_dir, self.artifacts_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._run_cache: dict[int, tuple[float, dict]] = {}
        self.setup_state: dict[str, Any] = {"status": "ok"}  # replaced by the app's auto-bootstrap state

    def check_setup(self) -> None:
        st = self.setup_state
        if st.get("status") == "pending":
            raise BuildError("Automatic setup is still running (usually a few seconds after a deploy); retry shortly.")
        if st.get("status") in ("failed", "blocked"):
            raise BuildError(f"Automatic setup has not succeeded: {st.get('detail')}. It retries by itself every few "
                             "minutes; call service_doctor (or GET /api/doctor) for details.")

    # ------------------------------------------------------------------ utils
    def _github(self) -> GitHub:
        if self.gh is None:
            raise BuildError("; ".join(self.s.problems()) or "GitHub is not configured")
        return self.gh

    @staticmethod
    def check_job_id(job_id: str) -> str:
        if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
            raise BuildError("Invalid job_id (expected something like 'rb-0123456789abcdef')")
        return job_id

    def upload_path(self, upload_id: str) -> Path:
        return self.uploads_dir / f"{upload_id}.zip"

    def log_path(self, job_id: str) -> Path:
        return self.logs_dir / f"{job_id}.log"

    def artifact_dir(self, job_id: str) -> Path:
        return self.artifacts_dir / job_id

    # ---------------------------------------------------------------- uploads
    async def stage_upload(self, chunks: AsyncIterator[bytes]) -> dict:
        upload_id = uuid.uuid4().hex[:16]
        target = self.uploads_dir / f"{upload_id}.part"
        limit = self.s.max_upload_mb * 1024 * 1024
        size = 0
        try:
            with target.open("wb") as out:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > limit:
                        raise BuildError(f"Upload exceeds {self.s.max_upload_mb} MB")
                    out.write(chunk)
            try:
                info = await asyncio.to_thread(inspect_zip, target, self.s.max_extract_mb)
            except ArchiveError as exc:
                raise BuildError(str(exc)) from exc
            target.rename(self.upload_path(upload_id))
        finally:
            target.unlink(missing_ok=True)
        self.db.add_upload(upload_id, size, info)
        return {"upload_id": upload_id, "size_bytes": size, "expires_after_minutes": self.s.source_ttl_minutes, **info}

    # ----------------------------------------------------------------- submit
    async def submit(self, *, language: str, upload_id: str | None = None, git_url: str | None = None,
                     git_ref: str | None = None, build_command: str | None = None,
                     artifact_paths: str | None = None, timeout_minutes: int | None = None,
                     working_directory: str | None = None, env: dict[str, str] | None = None,
                     label: str | None = None, package: str = "auto", return_artifacts: bool = True,
                     toolchain: dict[str, str] | None = None) -> dict:
        problems = self.s.problems()
        if problems:
            raise BuildError("Service is not fully configured: " + "; ".join(problems))
        gh = self._github()
        self.check_setup()

        language = (language or "").strip().lower()
        if language not in PRESETS:
            raise BuildError(f"language must be one of: {', '.join(PRESETS)}")
        preset = PRESETS[language]

        if bool(upload_id) == bool(git_url):
            raise BuildError("Provide exactly one source: upload_id (from POST /upload) or git_url (public https repo)")
        source: dict[str, Any]
        if upload_id:
            if not UPLOAD_ID_RE.fullmatch(upload_id):
                raise BuildError("Invalid upload_id")
            if not self.upload_path(upload_id).exists() or not self.db.get_upload(upload_id):
                raise BuildError("Upload not found or expired; POST the source ZIP to /upload again")
            source = {"type": "upload"}
        else:
            if not _GIT_URL_RE.fullmatch(git_url or ""):
                raise BuildError("git_url must be a plain https URL without credentials or query string")
            if git_ref and not _REF_RE.fullmatch(git_ref):
                raise BuildError("Invalid git_ref")
            source = {"type": "git", "url": git_url, "ref": git_ref or ""}

        command = (build_command or preset.command or "").strip()
        if not command:
            raise BuildError(f"build_command is required for language '{language}'")
        if len(command) > 8000:
            raise BuildError("build_command is too long (max 8000 characters)")
        artifacts = (artifact_paths or preset.artifacts or "").strip()
        if return_artifacts and not artifacts:
            raise BuildError(f"artifact_paths is required for language '{language}' (e.g. 'build/myapp')")
        art_lines = [ln.strip() for ln in artifacts.splitlines() if ln.strip()]
        if len(art_lines) > 20 or any(len(ln) > 300 for ln in art_lines):
            raise BuildError("artifact_paths: at most 20 lines of 300 characters")
        art_lines = [_safe_rel(ln, "artifact_paths entry") for ln in art_lines]

        if package not in {"auto", "zip"}:
            raise BuildError("package must be 'auto' (single file as-is, several files zipped) or 'zip'")

        workdir = ""
        if working_directory and working_directory.strip("./ "):
            if not _WORKDIR_RE.fullmatch(working_directory):
                raise BuildError("working_directory contains unsupported characters")
            workdir = _safe_rel(working_directory.strip(), "working_directory")

        clean_env: dict[str, str] = {}
        for key, value in (env or {}).items():
            if not _ENV_KEY_RE.fullmatch(str(key)) or str(key).upper().startswith(_RESERVED_ENV):
                raise BuildError(f"Invalid or reserved environment variable name: {key!r}")
            if len(str(value)) > 4096:
                raise BuildError(f"Environment variable {key} is too long")
            clean_env[str(key)] = str(value)
        if len(clean_env) > 50:
            raise BuildError("At most 50 environment variables")

        tc = dict(TOOLCHAIN_DEFAULTS)
        for key, value in (toolchain or {}).items():
            if key not in TOOLCHAIN_KEYS or not _VERSION_RE.fullmatch(str(value)):
                raise BuildError(f"toolchain supports only {sorted(TOOLCHAIN_KEYS)} with simple version strings")
            tc[key] = str(value)

        timeout = max(1, min(int(timeout_minutes or 30), self.s.max_build_minutes))

        if self.db.active_count(max_age_s=(self.s.max_build_minutes + 20) * 60) >= self.s.max_concurrent_builds:
            raise BuildError(f"{self.s.max_concurrent_builds} builds are already active; wait for one to finish")

        job_id = "rb-" + secrets.token_hex(8)
        spec = {
            "label": _clean_text(label or "", 80) or language,
            "language": language,
            "command": command,
            "artifacts": art_lines,
            "timeout_minutes": timeout,
            "workdir": workdir,
            "env": clean_env,
            "package": package,
            "return_artifacts": bool(return_artifacts),
            "max_artifact_mb": self.s.max_artifact_mb,
            "toolchain": tc,
            "source": source,
            "max_extract_mb": self.s.max_extract_mb,
        }
        self.db.create_job(job_id, spec, upload_id)
        try:
            run_id = await gh.dispatch(self.s.github_ref, {"job_id": job_id, "language": language, "mode": "build"})
            run_url = None
            if run_id is None:  # older API behaviour: locate the run by its run-name
                for _ in range(5):
                    await asyncio.sleep(1.5)
                    run = await gh.find_run(job_id)
                    if run:
                        run_id, run_url = run["id"], run.get("html_url")
                        break
            if run_id and not run_url:
                run_url = f"https://github.com/{self.s.github_repo}/actions/runs/{run_id}"
            self.db.update_job(job_id, state="dispatched", run_id=run_id, run_url=run_url)
        except Exception as exc:
            self.db.update_job(job_id, state="error", error=str(exc)[:500])
            self._release_source(job_id)
            raise
        return {
            "job_id": job_id, "state": "queued", "language": language, "label": spec["label"],
            "build_command": command, "artifact_paths": art_lines, "timeout_minutes": timeout,
            "run_url": run_url,
            "next": "Call wait_for_build(job_id) until done=true; on success download the file with "
                    "GET /api/builds/{job_id}/artifact (or get_artifact_info); on failure call get_build_logs(job_id).",
        }

    # ----------------------------------------------------------------- status
    async def _load_run(self, job_id: str, job: dict | None) -> dict | None:
        gh = self._github()
        run_id = job["run_id"] if job else None
        if run_id:
            cached = self._run_cache.get(run_id)
            if cached and time.time() - cached[0] < 3:
                return cached[1]
            run = await gh.get_run(run_id)
        else:
            run = await gh.find_run(job_id)
            if run and job:
                self.db.update_job(job_id, run_id=run["id"], run_url=run.get("html_url"), state="dispatched")
        if run:
            self._run_cache[run["id"]] = (time.time(), run)
        return run

    async def status(self, job_id: str) -> dict:
        self.check_job_id(job_id)
        job = self.db.get_job(job_id)
        if job and (job["result"] or job["state"] in ("cancelled", "error")):
            return self._view(job_id, job, None)
        run = await self._load_run(job_id, job)
        job = self.db.get_job(job_id)
        if run and run.get("status") == "completed" and not (job and job["result"]):
            age = time.time() - (_parse_iso(run.get("updated_at")) or time.time())
            if age > 45:  # the callback never arrived (service slept/restarted): recover from the public log
                try:
                    recovered = parse_rb_result(await self._github().run_log_text(run["id"]))
                except GitHubError:
                    recovered = None
                if recovered:
                    recovered["recovered"] = True
                    self.db.upsert_completed(job_id, recovered, {})
                    job = self.db.get_job(job_id)
        return self._view(job_id, job, run)

    def _view(self, job_id: str, job: dict | None, run: dict | None) -> dict:
        spec = (job or {}).get("spec") or {}
        res = (job or {}).get("result")
        out: dict[str, Any] = {"job_id": job_id, "label": spec.get("label"), "language": spec.get("language")}
        if job:
            out["created_at"] = _iso(job["created_at"])
        out["run_url"] = (job or {}).get("run_url") or (run or {}).get("html_url")
        if res:
            state, detail = result_state(res)
            out.update(state=state, detail=detail, exit_code=res.get("exit_code"),
                       build_seconds=res.get("build_seconds"), matched_files=res.get("artifacts", []))
            art = self.artifact_info(job_id)
            if art:
                out["artifact"] = art
            if res.get("recovered"):
                out["note"] = "Recovered from the GitHub run after the service restarted; full logs may be unavailable."
            if state in ("build_failed", "timed_out", "failed"):
                out["hint"] = "Call get_build_logs(job_id) for the error excerpt and log tail."
            return out
        if job and job["state"] == "cancelled":
            out.update(state="cancelled", detail="Cancelled on request.")
            return out
        if job and job["state"] == "error":
            out.update(state="failed", detail=job.get("error") or "Dispatch failed.")
            return out
        if not run:
            out.update(state="queued", detail="Waiting for GitHub to start the workflow run.")
            if not job:
                out["state"] = "unknown"
                out["detail"] = "No record of this job and no matching GitHub run (it may have been deleted)."
            return out
        gh_status, conclusion = run.get("status"), run.get("conclusion")
        if gh_status in ("queued", "waiting", "requested", "pending"):
            out.update(state="queued", detail="Queued on GitHub, waiting for a runner.")
        elif gh_status == "in_progress":
            out.update(state="running", detail="Build is running on GitHub Actions.")
        elif conclusion == "cancelled":
            out.update(state="cancelled", detail="The GitHub run was cancelled.")
        elif conclusion == "timed_out":
            out.update(state="timed_out", detail="The GitHub run timed out.")
        elif conclusion == "success":
            out.update(state="running", detail="Build finished; waiting for the runner's final report.")
        else:
            out.update(state="failed", detail=f"GitHub run ended with '{conclusion}' before reporting a result "
                                              "(setup step failed or the service was unreachable).",
                       hint="Open run_url to see which setup step failed.")
        return out

    async def wait(self, job_id: str, max_wait_seconds: int = 50) -> dict:
        deadline = time.time() + max(1, min(int(max_wait_seconds), 55))
        while True:
            view = await self.status(job_id)
            if view["state"] in TERMINAL or view["state"] == "unknown":
                view["done"] = True
                return view
            if time.time() + 4 > deadline:
                view["done"] = False
                return view
            await asyncio.sleep(4)

    # ------------------------------------------------------------------- logs
    async def logs(self, job_id: str, tail_lines: int = 200, max_chars: int = 20000, errors_only: bool = False) -> dict:
        self.check_job_id(job_id)
        view = await self.status(job_id)
        path = self.log_path(job_id)
        out: dict[str, Any] = {"job_id": job_id, "state": view["state"]}
        if not path.exists():
            out["log_available"] = False
            out["message"] = ("The build has not finished yet; logs are uploaded when the build ends."
                              if view["state"] in ("queued", "running", "unknown")
                              else "No log is stored (service restarted, or the build never started). "
                                   "Check run_url for the setup steps.")
            out["run_url"] = view.get("run_url")
            return out
        text = await asyncio.to_thread(path.read_text, "utf-8", "replace")
        out["log_available"] = True
        out["total_lines"] = text.count("\n") + 1
        errors = extract_errors(text)
        if errors:
            out["error_excerpt"] = errors
        if not errors_only:
            tail = "\n".join(text.splitlines()[-max(1, min(tail_lines, 2000)):])
            out["log_tail"] = tail[-max(500, min(max_chars, 60000)):]
        return out

    # ------------------------------------------------------------ cancel/list
    async def cancel(self, job_id: str) -> dict:
        self.check_job_id(job_id)
        view = await self.status(job_id)
        if view["state"] in TERMINAL:
            return {"job_id": job_id, "cancelled": False, "reason": f"Job already {view['state']}"}
        job = self.db.get_job(job_id)
        run_id = job["run_id"] if job else None
        if not run_id:
            run = await self._github().find_run(job_id)
            run_id = run["id"] if run else None
        if not run_id:
            raise BuildError("GitHub has not created the run yet; try again in a few seconds")
        await self._github().cancel_run(run_id)
        if job:
            self.db.update_job(job_id, state="cancelled")
            self._release_source(job_id)
        return {"job_id": job_id, "cancelled": True}

    def list(self, limit: int = 20) -> list[dict]:
        out = []
        for job in self.db.list_jobs(max(1, min(limit, 100))):
            spec = job["spec"] or {}
            state = result_state(job["result"])[0] if job["result"] else job["state"]
            out.append({"job_id": job["job_id"], "label": spec.get("label"), "language": spec.get("language"),
                        "state": state, "created_at": _iso(job["created_at"]), "run_url": job["run_url"]})
        return out

    def delete(self, job_id: str) -> dict:
        self.check_job_id(job_id)
        self._release_source(job_id, force=True)
        self.log_path(job_id).unlink(missing_ok=True)
        shutil.rmtree(self.artifact_dir(job_id), ignore_errors=True)
        self.db.delete_job(job_id)
        return {"job_id": job_id, "deleted": True}

    # -------------------------------------------------------------- artifacts
    def artifact_file(self, job_id: str) -> Path | None:
        d = self.artifact_dir(self.check_job_id(job_id))
        files = [f for f in d.glob("*") if f.is_file() and not f.name.endswith(".part")] if d.exists() else []
        return files[0] if files else None

    def artifact_info(self, job_id: str) -> dict | None:
        f = self.artifact_file(job_id)
        if not f:
            return None
        expires = f.stat().st_mtime + self.s.artifact_ttl_minutes * 60
        return {"filename": f.name, "size_bytes": f.stat().st_size,
                "download": f"/api/builds/{job_id}/artifact", "expires_at": _iso(expires),
                "note": "GET the download path with 'Authorization: Bearer <token>'. "
                        "Files are kept only temporarily; download promptly."}

    async def store_artifact(self, job_id: str, filename: str, chunks: AsyncIterator[bytes]) -> dict:
        """Receive the runner's packaged artifact (streamed to disk, size-capped)."""
        safe = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(filename or "artifact.bin").name)[:120].strip(". ") or "artifact.bin"
        d = self.artifact_dir(job_id)
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / (safe + ".part")
        limit = self.s.max_artifact_mb * 1024 * 1024
        size, sha = 0, hashlib.sha256()
        try:
            with tmp.open("wb") as out:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > limit:
                        raise BuildError(f"Artifact exceeds the service limit of {self.s.max_artifact_mb} MB")
                    sha.update(chunk)
                    out.write(chunk)
            tmp.rename(d / safe)
        except BaseException:
            shutil.rmtree(d, ignore_errors=True)
            raise
        return {"filename": safe, "size_bytes": size, "sha256": sha.hexdigest()}

    # -------------------------------------------------------- runner callbacks
    def runner_spec(self, job_id: str) -> dict | None:
        job = self.db.get_job(job_id)
        if not job or job["state"] not in ("created", "dispatched"):
            return None
        return {"job_id": job_id, **job["spec"]}

    def runner_source(self, job_id: str) -> Path | None:
        job = self.db.get_job(job_id)
        if not job or not job["upload_id"] or job["state"] not in ("created", "dispatched"):
            return None
        if self.db.bump_fetch(job_id) > 5:
            return None
        path = self.upload_path(job["upload_id"])
        return path if path.exists() else None

    _RESULT_KEYS = {"exit_code", "timed_out", "build_seconds", "artifacts", "package", "artifact",
                    "delivery_error", "ok", "reason", "label", "language"}

    def runner_complete(self, job_id: str, result: dict, log: bytes) -> None:
        clean = {k: v for k, v in result.items() if k in self._RESULT_KEYS}
        spec = {"label": clean.get("label"), "language": clean.get("language")}
        cap = self.s.max_log_mb * 1024 * 1024
        if len(log) > cap:
            log = b"[... earlier output truncated by the service ...]\n" + log[-cap:]
        if log:
            self.log_path(job_id).write_bytes(log)
        self.db.upsert_completed(job_id, clean, spec)
        self._release_source(job_id)

    # --------------------------------------------------------------- cleanup
    def _release_source(self, job_id: str, force: bool = False) -> None:
        job = self.db.get_job(job_id)
        upload_id = job["upload_id"] if job else None
        if not upload_id:
            return
        if force or self.db.jobs_using_upload(upload_id, exclude=job_id) == 0:
            self.upload_path(upload_id).unlink(missing_ok=True)
            self.db.delete_upload(upload_id)

    def sweep(self) -> dict:
        """Delete expired uploads, old jobs and orphan files. Safe to call any time."""
        removed = {"uploads": 0, "jobs": 0}
        for upload_id in self.db.uploads_older_than(self.s.source_ttl_minutes * 60):
            self.upload_path(upload_id).unlink(missing_ok=True)
            self.db.delete_upload(upload_id)
            removed["uploads"] += 1
        for job_id in self.db.jobs_older_than(self.s.job_retention_hours * 3600):
            self.log_path(job_id).unlink(missing_ok=True)
            shutil.rmtree(self.artifact_dir(job_id), ignore_errors=True)
            self.db.delete_job(job_id)
            removed["jobs"] += 1
        art_cutoff = time.time() - self.s.artifact_ttl_minutes * 60
        for d in self.artifacts_dir.glob("rb-*"):
            if d.is_dir() and d.stat().st_mtime < art_cutoff:
                shutil.rmtree(d, ignore_errors=True)
        cutoff = time.time() - 3600
        for stray in list(self.uploads_dir.glob("*.part")) + list(self.uploads_dir.glob("*.zip")):
            known = stray.suffix == ".zip" and self.db.get_upload(stray.stem)
            if not known and stray.stat().st_mtime < cutoff:
                stray.unlink(missing_ok=True)
        return removed
