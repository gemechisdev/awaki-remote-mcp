"""HTTP surface of the session feature: REST API, runner callbacks and MCP tools."""
from __future__ import annotations

import functools
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from .github import GitHubError
from .jobs import BuildError
from .sessions import SessionService


def _bad(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _api(handler):
    @functools.wraps(handler)
    async def wrapper(request: Request):
        try:
            return await handler(request)
        except GitHubError as exc:
            return _bad(str(exc), 502)
        except BuildError as exc:
            message = str(exc)
            return _bad(message, 404 if message.startswith("Unknown") else 413 if "exceeds" in message else 400)
        except TypeError as exc:
            return _bad(f"Bad arguments: {exc}")
    return wrapper


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise BuildError("JSON object body required") from None
    if not isinstance(body, dict):
        raise BuildError("JSON object body required")
    return body


def _int(request: Request, name: str, default: int) -> int:
    try:
        return int(request.query_params.get(name, default))
    except ValueError:
        return default


def session_routes(svc: SessionService) -> list[Route]:
    # ------------------------------------------------------------ REST (caller)
    @_api
    async def open_(request: Request):
        return JSONResponse(await svc.open(**await _json(request)))

    @_api
    async def list_(request: Request):
        return JSONResponse(svc.list(_int(request, "limit", 20)))

    @_api
    async def status(request: Request):
        sid = request.path_params["sid"]
        wait = _int(request, "wait", 0)
        return JSONResponse(await (svc.wait_ready(sid, wait) if wait else svc.status(sid)))

    @_api
    async def exec_(request: Request):
        body = await _json(request)
        return JSONResponse(await svc.exec(request.path_params["sid"], **body))

    @_api
    async def output(request: Request):
        q = request.query_params
        return JSONResponse(await svc.read_output(
            request.path_params["sid"], request.path_params["cid"], offset=_int(request, "offset", 0),
            max_chars=_int(request, "max_chars", 30000), wait_seconds=_int(request, "wait", 0),
            tail=q.get("tail") == "1"))

    @_api
    async def cancel_cmd(request: Request):
        return JSONResponse(await svc.cancel_command(request.path_params["sid"], request.path_params["cid"]))

    @_api
    async def put(request: Request):
        return JSONResponse(await svc.put(request.path_params["sid"], **await _json(request)))

    @_api
    async def collect(request: Request):
        return JSONResponse(await svc.collect(request.path_params["sid"], **await _json(request)))

    @_api
    async def artifacts(request: Request):
        sid = svc.check_id(request.path_params["sid"])
        return JSONResponse({"session_id": sid, "artifacts": svc.artifacts(sid)})

    @_api
    async def artifact(request: Request):
        sid = request.path_params["sid"]
        path = svc.artifact_path(sid, request.path_params["name"])
        if path is None:
            return _bad("Unknown artifact (not collected, or expired)", 404)
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")

    @_api
    async def transcript(request: Request):
        return JSONResponse(svc.transcript(request.path_params["sid"], _int(request, "max_chars", 20000)))

    @_api
    async def close(request: Request):
        retain = request.query_params.get("retain_minutes")
        return JSONResponse(await svc.close(request.path_params["sid"], int(retain) if retain else None))

    @_api
    async def delete(request: Request):
        return JSONResponse(await svc.delete(request.path_params["sid"]))

    # ----------------------------------------------------------- runner callbacks
    @_api
    async def r_spec(request: Request):
        spec = svc.runner_spec(request.path_params["sid"])
        return JSONResponse(spec) if spec else _bad("Session is not open", 404)

    @_api
    async def r_source(request: Request):
        path = svc.runner_source(request.path_params["sid"])
        return FileResponse(path, media_type="application/zip") if path else _bad("Source not available", 404)

    @_api
    async def r_upload(request: Request):
        path = svc.runner_upload(request.path_params["sid"], request.path_params["upload_id"])
        return FileResponse(path, media_type="application/zip") if path else _bad("Upload not available", 404)

    @_api
    async def r_next(request: Request):
        return JSONResponse(await svc.runner_next(request.path_params["sid"], _int(request, "wait", 25)))

    @_api
    async def r_output(request: Request):
        data = await request.body()
        if len(data) > 2 * 1024 * 1024:
            return _bad("chunk too large", 413)
        return JSONResponse(svc.runner_output(request.path_params["sid"], request.path_params["cid"],
                                              _int(request, "offset", 0), data))

    @_api
    async def r_done(request: Request):
        return JSONResponse(svc.runner_done(request.path_params["sid"], request.path_params["cid"],
                                            await _json(request)))

    @_api
    async def r_artifact(request: Request):
        return JSONResponse(await svc.runner_store_artifact(
            request.path_params["sid"], request.query_params.get("filename", "artifact.bin"), request.stream()))

    return [
        Route("/api/sessions", open_, methods=["POST"]),
        Route("/api/sessions", list_, methods=["GET"]),
        Route("/api/sessions/{sid}", status, methods=["GET"]),
        Route("/api/sessions/{sid}", delete, methods=["DELETE"]),
        Route("/api/sessions/{sid}/exec", exec_, methods=["POST"]),
        Route("/api/sessions/{sid}/commands/{cid}/output", output, methods=["GET"]),
        Route("/api/sessions/{sid}/commands/{cid}/cancel", cancel_cmd, methods=["POST"]),
        Route("/api/sessions/{sid}/put", put, methods=["POST"]),
        Route("/api/sessions/{sid}/collect", collect, methods=["POST"]),
        Route("/api/sessions/{sid}/artifacts", artifacts, methods=["GET"]),
        Route("/api/sessions/{sid}/artifacts/{name}", artifact, methods=["GET"]),
        Route("/api/sessions/{sid}/transcript", transcript, methods=["GET"]),
        Route("/api/sessions/{sid}/close", close, methods=["POST"]),
        Route("/runner/s/{sid}/spec", r_spec, methods=["GET"]),
        Route("/runner/s/{sid}/source", r_source, methods=["GET"]),
        Route("/srunner/{sid}/next", r_next, methods=["GET"]),
        Route("/srunner/{sid}/upload/{upload_id}", r_upload, methods=["GET"]),
        Route("/srunner/{sid}/cmd/{cid}/output", r_output, methods=["POST"]),
        Route("/srunner/{sid}/cmd/{cid}/done", r_done, methods=["POST"]),
        Route("/srunner/{sid}/artifact", r_artifact, methods=["PUT", "POST"]),
    ]


def register_session_tools(mcp, svc: SessionService, guard, public_url: str) -> None:
    base = public_url or "https://YOUR-SERVICE"

    @mcp.tool()
    async def open_session(
        upload_id: str | None = None,
        git_url: str | None = None,
        git_ref: str | None = None,
        language: str = "generic",
        working_directory: str | None = None,
        env: dict[str, str] | None = None,
        toolchain: dict[str, str] | None = None,
        ttl_minutes: int = 60,
        idle_minutes: int = 30,
        retain_minutes: int = 30,
        label: str | None = None,
        webhook_url: str | None = None,
    ) -> dict:
        """Open a temporary remote shell (4 vCPU / 16 GB, Ubuntu, sudo) with the project loaded.

        Use when the work is heavier than your current environment: Gradle/Android/Flutter, native compiles,
        large test suites, slow npm/cargo installs, or multi-step debugging.
        Source: upload_id or public git_url. language: android|kotlin|java|rust|go|python|node|cpp|flutter|generic.
        ttl/idle/retain control lifetime. env for secrets. webhook_url gets a finish POST. Auto-expires.
        """
        return await guard(svc.open(
            upload_id=upload_id, git_url=git_url, git_ref=git_ref, language=language,
            working_directory=working_directory, env=env, toolchain=toolchain, ttl_minutes=ttl_minutes,
            idle_minutes=idle_minutes, retain_minutes=retain_minutes, label=label,
            webhook_url=webhook_url))

    @mcp.tool()
    async def wait_for_session(session_id: str, max_wait_seconds: int = 50) -> dict:
        """Wait until the runner is connected (ready=true) or max_wait_seconds elapses."""
        return await guard(svc.wait_ready(session_id, max_wait_seconds))

    @mcp.tool()
    async def session_status(session_id: str) -> dict:
        """State, time left, running/recent commands, and collected artifacts."""
        return await guard(svc.status(session_id))

    @mcp.tool()
    async def exec_command(
        session_id: str,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 600,
        wait_seconds: int = 25,
    ) -> dict:
        """Run a bash command on the remote machine. Returns output; if still running, state='running' + next_offset for read_output.

        Starts in project root (or cwd). cd/export do not persist — chain with && or pass cwd/env.
        Installed packages, build outputs and caches persist for the whole session. Concurrent commands allowed.
        Raise timeout_seconds for long jobs (e.g. full Gradle assemble or cargo build).
        """
        return await guard(svc.exec(session_id, command, cwd, env, timeout_seconds, wait_seconds))

    @mcp.tool()
    async def read_output(session_id: str, cmd_id: str, offset: int = 0, wait_seconds: int = 25,
                          max_chars: int = 30000, tail: bool = False) -> dict:
        """Follow command output (pass next_offset from previous call). Like tail -f with optional wait."""
        return await guard(svc.read_output(session_id, cmd_id, offset, max_chars, wait_seconds, tail))

    @mcp.tool()
    async def cancel_command(session_id: str, cmd_id: str) -> dict:
        """Kill a running command or drop a queued one."""
        return await guard(svc.cancel_command(session_id, cmd_id))

    @mcp.tool()
    async def put_files(session_id: str, upload_id: str, dest: str | None = None, mode: str = "overlay",
                        keep: list[str] | None = None, wait_seconds: int = 120) -> dict:
        """Push a ZIP (already uploaded) into the session. overlay = add/overwrite; replace = wipe first (keeps caches)."""
        return await guard(svc.put(session_id, upload_id, dest, mode, keep, wait_seconds))

    @mcp.tool()
    async def collect_artifacts(session_id: str, paths: str, package: str = "auto", name: str | None = None,
                                cwd: str | None = None, wait_seconds: int = 120) -> dict:
        """Copy matching files (newline globs, supports **) from the session so they can be downloaded by URL.

        package: 'auto' (single→as-is, several→zip) | 'zip' | 'named' (each file kept separate with its own URL).
        """
        return await guard(svc.collect(session_id, paths, package, name, cwd, wait_seconds))

    @mcp.tool()
    async def list_session_artifacts(session_id: str) -> dict:
        """Artifacts collected so far, with download paths."""
        sid = svc.check_id(session_id)
        return {"session_id": sid, "base_url": base, "artifacts": svc.artifacts(sid)}

    @mcp.tool()
    async def get_session_log(session_id: str, max_chars: int = 20000) -> dict:
        """Condensed transcript of recent commands (command, output tail, exit code)."""
        return await guard(_ret(svc.transcript(session_id, max_chars)))

    @mcp.tool()
    async def close_session(session_id: str, retain_minutes: int | None = None) -> dict:
        """End the session (VM destroyed). Logs/artifacts remain for retain_minutes then delete."""
        return await guard(svc.close(session_id, retain_minutes))

    @mcp.tool()
    async def list_sessions(limit: int = 20) -> list[dict]:
        """Recent sessions."""
        return svc.list(limit)

    @mcp.tool()
    async def delete_session(session_id: str) -> dict:
        """Force-close and immediately wipe all logs and artifacts."""
        return await guard(svc.delete(session_id))


async def _ret(value: Any) -> Any:
    return value
