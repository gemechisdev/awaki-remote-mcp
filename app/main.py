"""ASGI application: MCP (Streamable HTTP) + REST + runner callbacks.

Run with:  uvicorn --factory app.main:create_app --host 0.0.0.0 --port $PORT
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route

from . import __version__
from .bootstrap import sync_files
from .config import Settings
from .db import Database
from .github import GitHub, GitHubError
from .jobs import BuildError, BuildService
from .presets import PRESETS

log = logging.getLogger("remote-build")

INSTRUCTIONS = """\
Remote build service: compiles projects on GitHub Actions (4 vCPU / 16 GB Linux runners) and
sends the finished artifact (APK, binary, wheel, ...) back to YOU, the server that called it.

Workflow:
1. Zip the project (exclude .git, node_modules, build outputs, secrets) and POST the ZIP to
   {upload_url} with header 'Authorization: Bearer <token>' (raw body or multipart field 'file').
   The reply contains upload_id. Alternatively pass git_url for a PUBLIC https repository.
2. Call submit_build(upload_id, language, ...). Presets supply a sensible build_command and
   artifact_paths for android, kotlin, java, rust, go, python, node, cpp, flutter, generic.
3. Call wait_for_build(job_id) repeatedly until done=true. Builds take minutes; do other work meanwhile.
4. On failure call get_build_logs(job_id): it returns an error_excerpt and the log tail. Fix the code, resubmit.
5. On success download the artifact: GET {base}/api/builds/<job_id>/artifact with the same bearer token
   (one file as-is, or a ZIP when several files matched). It is kept for a few hours only.
Do not put secrets in build_command; use the env parameter instead (values are masked in logs).
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
    sync_state: dict[str, Any] = {"status": "pending"}

    # ------------------------------------------------------------------ MCP
    mcp = FastMCP(
        "Remote Build",
        instructions=INSTRUCTIONS.format(upload_url=(s.public_url or "https://YOUR-SERVICE") + "/upload",
                                       base=s.public_url or "https://YOUR-SERVICE"),
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        # Host validation is for localhost-only servers; this service is public and bearer-protected.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def guard(coro):
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
    ) -> dict:
        """Start a remote build and return immediately with a job_id.

        language: android | kotlin | java | rust | go | python | node | cpp | flutter | generic.
        upload_id: from POST /upload (source ZIP). OR git_url (+git_ref): a public https repository.
        build_command: optional for most languages (preset used); runs with bash in the project root.
        artifact_paths: newline-separated globs relative to the project (supports **). Preset default per language.
        working_directory: sub-folder to build in (monorepos). Artifact globs are relative to it.
        env: extra environment variables for the build (values are masked in logs; use for signing passwords).
        label: short name for this build, e.g. 'MyApp debug'.
        package: 'auto' (a single matching file is returned as-is, several are zipped) or 'zip'.
        return_artifacts: false = build/test only, no artifact is collected (artifact_paths not needed).
        toolchain: optional {java_version, node_version, python_version, go_version}.
        """
        return await guard(svc.submit(
            language=language, upload_id=upload_id, git_url=git_url, git_ref=git_ref,
            build_command=build_command, artifact_paths=artifact_paths, timeout_minutes=timeout_minutes,
            working_directory=working_directory, env=env, label=label, package=package,
            return_artifacts=return_artifacts, toolchain=toolchain))

    @mcp.tool()
    async def wait_for_build(job_id: str, max_wait_seconds: int = 50) -> dict:
        """Wait up to ~50 s for a build to finish. Returns done=true with the final state, or done=false (call again).

        States: queued, running, succeeded, build_failed, delivery_failed, failed, timed_out, cancelled.
        """
        return await guard(svc.wait(job_id, max_wait_seconds))

    @mcp.tool()
    async def get_build_status(job_id: str) -> dict:
        """Current state of a build; when finished, includes artifact {filename, size_bytes, download}."""
        return await guard(svc.status(job_id))

    @mcp.tool()
    async def get_build_logs(job_id: str, tail_lines: int = 200, errors_only: bool = False) -> dict:
        """Build output for debugging: error_excerpt (key failure lines) and log_tail (last N lines).

        Logs exist once the build has ended. Set errors_only=true to save tokens.
        """
        return await guard(svc.logs(job_id, tail_lines=tail_lines, errors_only=errors_only))

    @mcp.tool()
    async def get_artifact_info(job_id: str) -> dict:
        """Where to download a finished build's artifact (HTTP GET with the same bearer token)."""
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
        """Recent builds known to this service (newest first)."""
        return svc.list(limit)

    @mcp.tool()
    async def delete_build(job_id: str) -> dict:
        """Forget a build and delete its stored log/source from this service (GitHub holds no source)."""
        return await guard(asyncio.to_thread(svc.delete, job_id))

    @mcp.tool()
    async def get_build_presets() -> dict:
        """Supported languages with their default command, artifact globs and notes."""
        return {name: {"build_command": p.command, "artifact_paths": p.artifacts, "notes": p.description}
                for name, p in PRESETS.items()}

    # ----------------------------------------------------------- plain routes
    async def health(request: Request) -> JSONResponse:
        problems = s.problems()
        body: dict[str, Any] = {"ok": True, "service": "remote-build-mcp", "version": __version__,
                                "configured": not problems}
        if request.scope.get("state", {}).get("authed"):
            body.update(problems=problems, runner_repo_sync=sync_state,
                        active_builds=db.active_count((s.max_build_minutes + 20) * 60))
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
    async def background() -> None:
        if gh is not None and s.sync_runner_repo and not s.problems():
            try:
                changed = await sync_files(gh, s.github_ref)
                sync_state.update(status="ok", updated=changed)
                if changed:
                    log.info("Updated runner repo files: %s", changed)
            except Exception as exc:  # never block startup on this
                sync_state.update(status="failed", error=str(exc)[:300])
                log.warning("Runner repo sync failed: %s", exc)
        else:
            sync_state["status"] = "skipped"
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
        Mount("/", app=mcp.streamable_http_app()),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        problems = s.problems()
        if problems:
            log.warning("Configuration problems: %s", "; ".join(problems))
        task = asyncio.create_task(background())
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            task.cancel()
            if gh is not None:
                await gh.aclose()

    from .security import AuthMiddleware
    from starlette.middleware import Middleware

    return Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(AuthMiddleware, settings=s)])
