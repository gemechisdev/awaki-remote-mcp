"""ASGI application: MCP (Streamable HTTP) + REST + runner callbacks.

Run with:  uvicorn --factory app.main:create_app --host 0.0.0.0 --port $PORT
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from typing import Any, AsyncIterator

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route

from . import __version__
from .bootstrap import doctor as run_doctor
from .bootstrap import run_bootstrap
from .config import Settings
from .db import Database
from .github import GitHub, GitHubError
from .jobs import BuildError, BuildService
from .presets import PRESETS
from .sessions import SessionService
from .sessions_http import register_session_tools, session_routes

log = logging.getLogger("remote-build")

INSTRUCTIONS = """\
Remote builds on free GitHub Actions runners (4 vCPU / 16 GB, Ubuntu, sudo). Reach for them when the work is heavier than your current environment — long compiles, Gradle/Android/Flutter, native builds, big test suites, or anything that needs more CPU/RAM/time.

Two modes:
• BUILD: source → submit_build → wait_for_build → download_artifact
• SESSION: open_session → exec_command (+ read_output) → put_files / collect_artifacts → close_session

Source (exactly one): public git_url (preferred) or upload ZIP (upload_source or POST /upload).
Secrets only via env= (masked). Prefer webhook_url over polling. Languages: android kotlin java rust go python node cpp flutter generic (get_build_presets for defaults).
Sessions auto-expire. Commands start in project root; cd/export do not persist — chain with &&.
"""


def _bad(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def create_app(settings: Settings | None = None) -> Starlette:
    s = settings or Settings.from_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db = Database(s.data_dir / "state.db")
    gh: GitHub | None = None
    if s.github_token and s.github_repo:
        gh = GitHub(s.github_token, s.github_repo, s.github_api_url)
    svc = BuildService(s, db, gh)
    sess = SessionService(s, db, gh, svc)
    auto = bool(gh is not None and s.sync_runner_repo)
    sync_state: dict[str, Any] = {"status": "pending" if auto else "skipped"}
    svc.setup_state = sync_state

    # ------------------------------------------------------------------ MCP
    mcp = FastMCP(
        "Remote Build",
        instructions=INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        # Host validation is for localhost-only servers; this service is public and bearer-protected.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def guard(coro):  # noqa: D401
        try:
            return await coro
        except (BuildError, GitHubError) as exc:
            raise ValueError(str(exc)) from None

    @mcp.tool()
    async def submit_build(
        language: str,
        upload_id: str | None = None,
        git_url: str | None = None,
        git_ref: str | None = None,
        build_command: str | None = None,
        artifact_paths: str | None = None,
        timeout_minutes: int = 30,
        working_directory: str | None = None,
        env: dict[str, str] | None = None,
        label: str | None = None,
        package: str = "auto",
        return_artifacts: bool = True,
        toolchain: dict[str, str] | None = None,
        webhook_url: str | None = None,
    ) -> dict:
        """Start a one-shot remote build on a 4 vCPU / 16 GB runner. Use for compiles, packaging, or tests that are slow or heavy locally.

        Source (exactly one): upload_id or public git_url (+ optional git_ref).
        language: android|kotlin|java|rust|go|python|node|cpp|flutter|generic (preset fills command + artifact globs).
        Override with build_command / artifact_paths. working_directory for monorepos. env for secrets (masked).
        package: 'auto'|'zip'. return_artifacts defaults true (set false for test-only). webhook_url gets a finish POST.
        Example: language='android', upload_id='…', build_command='./gradlew assembleDebug'
        Then wait_for_build → download_artifact.
        """
        return await guard(svc.submit(
            language=language, upload_id=upload_id, git_url=git_url, git_ref=git_ref,
            build_command=build_command, artifact_paths=artifact_paths, timeout_minutes=timeout_minutes,
            working_directory=working_directory, env=env, label=label, package=package,
            return_artifacts=return_artifacts, toolchain=toolchain, webhook_url=webhook_url))

    @mcp.tool()
    async def wait_for_build(job_id: str, max_wait_seconds: int = 50) -> dict:
        """Long-poll up to max_wait_seconds. Returns done=true + final state, or done=false (call again).
        States: queued | running | succeeded | build_failed | delivery_failed | failed | timed_out | cancelled.
        """
        return await guard(svc.wait(job_id, max_wait_seconds))

    @mcp.tool()
    async def get_build_status(job_id: str) -> dict:
        """Current build state. When finished, includes artifact {filename, size_bytes, download}."""
        return await guard(svc.status(job_id))

    @mcp.tool()
    async def get_build_logs(job_id: str, tail_lines: int = 200, errors_only: bool = False) -> dict:
        """Post-build log: error_excerpt (key failures) + log_tail. Prefer errors_only=true to save tokens."""
        return await guard(svc.logs(job_id, tail_lines=tail_lines, errors_only=errors_only))

    @mcp.tool()
    async def get_artifact_info(job_id: str) -> dict:
        """Download URL + metadata for a finished artifact (GET with the same Bearer token)."""
        info = svc.artifact_info(svc.check_job_id(job_id))
        if not info:
            raise ValueError("No artifact available (build not finished, failed, expired, or return_artifacts=false)")
        return {**info, "url": (s.public_url + info["download"]) if s.public_url else info["download"]}

    @mcp.tool()
    async def cancel_build(job_id: str) -> dict:
        """Cancel a queued or running build."""
        return await guard(svc.cancel(job_id))

    @mcp.tool()
    async def list_builds(limit: int = 20) -> list[dict]:
        """Recent builds (newest first)."""
        return svc.list(limit)

    @mcp.tool()
    async def delete_build(job_id: str) -> dict:
        """Delete a build's stored log/source from this service."""
        return await guard(asyncio.to_thread(svc.delete, job_id))

    @mcp.tool()
    async def service_doctor() -> dict:
        """Read-only health of runner repo, workflow files, secrets and config."""
        resp = await api_doctor(None)  # type: ignore[arg-type]
        return json.loads(resp.body)

    @mcp.tool()
    async def service_setup() -> dict:
        """Re-push workflow files and secrets to the runner repo. Safe to repeat."""
        resp = await api_bootstrap(None)  # type: ignore[arg-type]
        data = json.loads(resp.body)
        if "error" in data:
            raise ValueError(data["error"])
        return data

    @mcp.tool()
    async def get_build_presets() -> dict:
        """Default build_command, artifact_paths and notes for each language."""
        return {name: {"build_command": p.command, "artifact_paths": p.artifacts, "notes": p.description}
                for name, p in PRESETS.items()}

    @mcp.tool()
    async def get_upload_instructions() -> dict:
        """How to obtain an upload_id (or skip upload entirely with a public git_url)."""
        base = (s.public_url or "").rstrip("/") or "https://YOUR-SERVICE"
        upload_url = f"{base}/upload"
        return {
            "preferred": "public git_url → pass git_url= directly to submit_build / open_session (no upload).",
            "pure_mcp": "upload_source(zip_base64=...) → returns upload_id. Soft limit ~30 MB decoded.",
            "binary": {
                "url": upload_url,
                "auth": "Authorization: Bearer <MCP_API_TOKEN>",
                "raw": f"curl -sS -X POST '{upload_url}' -H 'Authorization: Bearer <TOKEN>' -H 'Content-Type: application/zip' --data-binary @project.zip",
                "multipart": f"curl -sS -X POST '{upload_url}' -H 'Authorization: Bearer <TOKEN>' -F 'file=@project.zip;type=application/zip'",
            },
            "limits": {
                "max_upload_mb": s.max_upload_mb,
                "max_base64_mb": min(30, s.max_upload_mb),
                "tip": "Zip source only; exclude .git, node_modules, build/, .gradle, target/, .venv, secrets.",
            },
            "next": "submit_build(upload_id=...) or open_session(upload_id=...). On success use download_artifact.",
        }

    @mcp.tool()
    async def upload_source(zip_base64: str) -> dict:
        """Upload a project ZIP as base64 (pure MCP). Returns upload_id (expires after ~2 h) for submit_build / open_session.

        Soft limit ~30 MB decoded; larger projects → git_url or binary POST /upload.
        Accepts optional data: URI prefix. Zip source only (exclude .git, node_modules, build caches).
        """
        # Strip optional data-URI prefix if an agent includes it
        raw = zip_base64.strip()
        if "," in raw and raw.lower().startswith("data:"):
            raw = raw.split(",", 1)[1]
        try:
            data = base64.b64decode(raw, validate=False)
        except Exception as exc:
            raise ValueError(f"Invalid base64: {exc}") from None
        if not data:
            raise ValueError("Decoded ZIP is empty")
        # Soft limit for base64 path (memory safety). Binary /upload still allows full max_upload_mb.
        max_b64_mb = min(30, s.max_upload_mb)
        if len(data) > max_b64_mb * 1024 * 1024:
            raise ValueError(
                f"Base64 upload exceeds {max_b64_mb} MB decoded. "
                "Use a public git_url or POST the ZIP as binary to /upload instead."
            )

        async def chunks() -> AsyncIterator[bytes]:
            # Stream in 1 MiB pieces so stage_upload size-checks work the same way
            view = memoryview(data)
            step = 1024 * 1024
            for i in range(0, len(view), step):
                yield bytes(view[i : i + step])

        return await guard(svc.stage_upload(chunks()))

    @mcp.tool()
    async def download_artifact(job_id: str, as_base64: bool = False) -> dict:
        """Get a finished artifact: always returns url + metadata. Optionally include base64 if ≤15 MB.

        Prefer the URL (GET with same Bearer token) for large files; as_base64 only when binary HTTP is unavailable.
        """
        info = svc.artifact_info(svc.check_job_id(job_id))
        if not info:
            raise ValueError(
                "No artifact available (build not finished, failed, expired, or return_artifacts=false)"
            )
        base = (s.public_url or "").rstrip("/")
        url = (base + info["download"]) if base else info["download"]
        result: dict[str, Any] = {
            **info,
            "url": url,
            "auth_header": "Authorization: Bearer <your-MCP_API_TOKEN>",
            "curl": (
                f"curl -sS -L -H 'Authorization: Bearer <your-MCP_API_TOKEN>' "
                f"-o '{info['filename']}' '{url}'"
            ),
        }
        if as_base64:
            path = svc.artifact_file(job_id)
            if path is None:
                raise ValueError("Artifact file disappeared (expired?)")
            size = path.stat().st_size
            max_b64 = 15 * 1024 * 1024
            if size > max_b64:
                result["base64_omitted"] = (
                    f"File is {size} bytes (> 15 MB). Download via the url/curl instead."
                )
            else:
                result["content_base64"] = base64.b64encode(path.read_bytes()).decode("ascii")
                result["encoding"] = "base64"
        return result

    register_session_tools(mcp, sess, guard, s.public_url)

    # ----------------------------------------------------------- plain routes
    async def health(request: Request) -> JSONResponse:
        problems = s.problems()
        body: dict[str, Any] = {"ok": True, "service": "remote-build-mcp", "version": __version__,
                                "configured": not problems, "setup": sync_state.get("status")}
        if request.scope.get("state", {}).get("authed"):
            body.update(problems=problems, setup_details=sync_state,
                        active_builds=db.active_count((s.max_build_minutes + 20) * 60),
                        active_sessions=db.count_active_sessions())
        return JSONResponse(body)

    async def upload(request: Request) -> JSONResponse:
        declared = int(request.headers.get("content-length") or 0)
        if declared > s.max_upload_mb * 1024 * 1024 + 4096:
            return _bad(f"Upload exceeds {s.max_upload_mb} MB", 413)
        ctype = request.headers.get("content-type", "")
        try:
            if ctype.startswith("multipart/form-data"):
                form = await request.form()
                file = form.get("file")
                if file is None or not hasattr(file, "read"):
                    return _bad("Send the ZIP in multipart field 'file' (or POST the raw ZIP body)")

                async def chunks():
                    while True:
                        piece = await file.read(1024 * 1024)
                        if not piece:
                            return
                        yield piece

                return JSONResponse(await svc.stage_upload(chunks()))
            return JSONResponse(await svc.stage_upload(request.stream()))
        except BuildError as exc:
            return _bad(str(exc), 413 if "exceeds" in str(exc) else 400)

    # ------------------------------------------------------------- REST mirror
    async def api_submit(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return _bad("JSON object body required")
        try:
            return JSONResponse(await svc.submit(**body))
        except TypeError as exc:
            return _bad(f"Bad arguments: {exc}")
        except (BuildError, GitHubError) as exc:
            return _bad(str(exc), 502 if isinstance(exc, GitHubError) else 400)

    async def api_list(request: Request) -> JSONResponse:
        return JSONResponse(svc.list(int(request.query_params.get("limit", 20))))

    async def api_status(request: Request) -> JSONResponse:
        job_id = request.path_params["job_id"]
        try:
            wait = int(request.query_params.get("wait", 0))
            return JSONResponse(await (svc.wait(job_id, wait) if wait else svc.status(job_id)))
        except (BuildError, GitHubError) as exc:
            return _bad(str(exc))

    async def api_logs(request: Request) -> JSONResponse:
        try:
            return JSONResponse(await svc.logs(request.path_params["job_id"],
                                               tail_lines=int(request.query_params.get("tail", 200)),
                                               errors_only=request.query_params.get("errors_only") == "1"))
        except (BuildError, GitHubError) as exc:
            return _bad(str(exc))

    async def api_artifact(request: Request):
        try:
            job_id = svc.check_job_id(request.path_params["job_id"])
        except BuildError as exc:
            return _bad(str(exc))
        path = svc.artifact_file(job_id)
        if path is None:
            return _bad("No artifact available (not finished, failed, expired, or never collected)", 404)
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")

    async def api_cancel(request: Request) -> JSONResponse:
        try:
            return JSONResponse(await svc.cancel(request.path_params["job_id"]))
        except (BuildError, GitHubError) as exc:
            return _bad(str(exc))

    async def api_delete(request: Request) -> JSONResponse:
        try:
            return JSONResponse(svc.delete(request.path_params["job_id"]))
        except BuildError as exc:
            return _bad(str(exc))

    async def api_doctor(request: Request) -> JSONResponse:
        if gh is None:
            return JSONResponse({"ok": False, "findings": [{"level": "FAIL", "message": m} for m in s.problems()]})
        try:
            findings = [{"level": lv, "message": msg} for lv, msg in await run_doctor(gh, s)]
        except GitHubError as exc:
            return _bad(str(exc), 502)
        return JSONResponse({"ok": not any(f["level"] == "FAIL" for f in findings), "setup": sync_state.get("status"),
                             "findings": findings})

    async def api_bootstrap(request: Request) -> JSONResponse:
        if gh is None:
            return _bad("; ".join(s.problems()))
        try:
            result = await run_bootstrap(gh, s)
        except GitHubError as exc:
            return _bad(str(exc), 502)
        sync_state.clear()
        sync_state.update(status="ok" if result["ok"] else "attention", detail=None, **result)
        return JSONResponse(result)

    async def api_presets(request: Request) -> JSONResponse:
        return JSONResponse({n: {"build_command": p.command, "artifact_paths": p.artifacts, "notes": p.description}
                             for n, p in PRESETS.items()})

    # ------------------------------------------------------ runner callbacks
    def _job(request: Request) -> str:
        return svc.check_job_id(request.path_params["job_id"])

    async def runner_spec(request: Request) -> JSONResponse:
        try:
            spec = svc.runner_spec(_job(request))
        except BuildError as exc:
            return _bad(str(exc))
        if spec is None:
            return _bad("Unknown or already finished job (was the service restarted? resubmit the build)", 404)
        return JSONResponse(spec)

    async def runner_source(request: Request):
        try:
            path = svc.runner_source(_job(request))
        except BuildError as exc:
            return _bad(str(exc))
        if path is None:
            return _bad("Source not available (expired, already finished, or fetched too many times)", 404)
        return FileResponse(path, media_type="application/zip", filename="source.zip")

    async def runner_artifact(request: Request) -> JSONResponse:
        try:
            job_id = _job(request)
            return JSONResponse(await svc.store_artifact(
                job_id, request.query_params.get("filename", "artifact.bin"), request.stream()))
        except BuildError as exc:
            return _bad(str(exc), 413 if "exceeds" in str(exc) else 400)

    async def runner_complete(request: Request) -> JSONResponse:
        try:
            job_id = _job(request)
        except BuildError as exc:
            return _bad(str(exc))
        form = await request.form()
        try:
            result = json.loads(str(form.get("result", "")))
            if not isinstance(result, dict):
                raise ValueError
        except ValueError:
            return _bad("Field 'result' must be a JSON object")
        log = b""
        upload_file = form.get("log")
        if upload_file is not None and hasattr(upload_file, "read"):
            log = await upload_file.read()
        svc.runner_complete(job_id, result, log)
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------- lifecycle
    async def bootstrap_loop() -> None:
        """Automatic setup: no shell needed. Runs at every start, retries until it works, re-verifies periodically."""
        if not auto:
            return
        delay = 5
        while True:
            try:
                result = await run_bootstrap(gh, s)
                sync_state.clear()
                sync_state.update(status="ok" if result["ok"] else "attention", detail=None,
                                  last_run=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **result)
                if result["files_updated"]:
                    log.info("Runner repo updated: %s", result["files_updated"])
                log.info("Automatic setup finished (status=%s, secrets set: %s)", sync_state["status"],
                         result["secrets_set"])
                delay = 5
                await asyncio.sleep(6 * 3600)  # cheap periodic self-heal (e.g. someone deleted a secret)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                blocked = bool(s.problems())
                sync_state.clear()
                sync_state.update(status="blocked" if blocked else "failed", detail=str(exc)[:500],
                                  last_run=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                log.warning("Automatic setup failed (retrying in %ss): %s", delay, exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)

    async def background() -> None:
        while True:
            try:
                removed = await asyncio.to_thread(svc.sweep)
                if any(removed.values()):
                    log.info("Sweep removed %s", removed)
            except Exception:
                log.exception("sweep failed")
            await asyncio.sleep(600)

    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/upload", upload, methods=["POST", "PUT"]),
        Route("/api/presets", api_presets, methods=["GET"]),
        Route("/api/doctor", api_doctor, methods=["GET"]),
        Route("/api/bootstrap", api_bootstrap, methods=["POST"]),
        Route("/api/builds", api_submit, methods=["POST"]),
        Route("/api/builds", api_list, methods=["GET"]),
        Route("/api/builds/{job_id}", api_status, methods=["GET"]),
        Route("/api/builds/{job_id}", api_delete, methods=["DELETE"]),
        Route("/api/builds/{job_id}/logs", api_logs, methods=["GET"]),
        Route("/api/builds/{job_id}/artifact", api_artifact, methods=["GET"]),
        Route("/api/builds/{job_id}/cancel", api_cancel, methods=["POST"]),
        Route("/runner/{job_id}/spec", runner_spec, methods=["GET"]),
        Route("/runner/{job_id}/source", runner_source, methods=["GET"]),
        Route("/runner/{job_id}/artifact", runner_artifact, methods=["PUT", "POST"]),
        Route("/runner/{job_id}/complete", runner_complete, methods=["POST"]),
        *session_routes(sess),
        Mount("/", app=mcp.streamable_http_app()),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        problems = s.problems()
        if problems:
            log.warning("Configuration problems: %s", "; ".join(problems))
        async def session_loop() -> None:
            while True:
                try:
                    await sess.sweep()
                except Exception:
                    log.exception("session sweep failed")
                await asyncio.sleep(20)

        task = asyncio.create_task(background())
        task2 = asyncio.create_task(session_loop())
        task3 = asyncio.create_task(bootstrap_loop())
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            task.cancel()
            task2.cancel()
            task3.cancel()
            if gh is not None:
                await gh.aclose()

    from .security import AuthMiddleware
    from starlette.middleware import Middleware

    return Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(AuthMiddleware, settings=s)])
