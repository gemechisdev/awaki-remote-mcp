import io
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import requests
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route, Mount
from starlette.requests import Request
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware

from mcp.server.fastmcp import FastMCP

from .config import settings
from .github import GitHub, GitHubError
from .workflow import make_workflow

LANGUAGES = {"android", "kotlin", "rust", "go", "python", "node", "cpp", "flutter", "generic"}
mcp = FastMCP("Remote Build MCP", stateless_http=True, json_response=True)

def gh() -> GitHub:
    return GitHub(settings.github_token, settings.github_api_url)

def job_repo(job_id: str) -> str:
    if not re.fullmatch(r"rb-[a-f0-9]{16}", job_id):
        raise ValueError("Invalid job_id")
    return job_id

def safe_extract(zip_path: Path, destination: Path):
    max_total = settings.max_extract_mb * 1024 * 1024
    total = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            raw = info.filename.replace("\\", "/")
            p = PurePosixPath(raw)
            if p.is_absolute() or ".." in p.parts:
                raise ValueError(f"Unsafe path in source archive: {raw}")
            # Ignore VCS metadata and workflow files supplied by the caller.
            if ".git" in p.parts or ".github" in p.parts:
                continue
            mode = info.external_attr >> 16
            if (mode & 0o170000) == 0o120000:
                continue
            total += info.file_size
            if total > max_total:
                raise ValueError(f"Expanded source exceeds {settings.max_extract_mb} MB limit")
            if info.is_dir():
                continue
            target = destination.joinpath(*p.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)

def auth_ok(request: Request) -> bool:
    if not settings.mcp_api_token:
        return False
    return request.headers.get("authorization", "") == f"Bearer {settings.mcp_api_token}"

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        if not auth_ok(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)

async def health(request: Request):
    return JSONResponse({"ok": True, "service": "remote-build-mcp"})

async def upload_source(request: Request):
    content_length = int(request.headers.get("content-length", "0") or 0)
    if content_length > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"Upload exceeds {settings.max_upload_mb} MB")
    form = await request.form()
    upload = form.get("file")
    if upload is None or not getattr(upload, "filename", None):
        raise HTTPException(400, "Send a ZIP archive in multipart field 'file'")
    if not upload.filename.lower().endswith(".zip"):
        raise HTTPException(400, "Only .zip source archives are accepted")
    base = Path(settings.temp_dir) / "uploads"
    base.mkdir(parents=True, exist_ok=True)
    # Remove stale staging archives so temporary source files do not accumulate.
    for stale in base.glob("*.zip"):
        if time.time() - stale.stat().st_mtime > 3600:
            stale.unlink(missing_ok=True)
    upload_id = uuid.uuid4().hex[:16]
    target = base / f"{upload_id}.zip"
    size = 0
    with target.open("wb") as out:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > settings.max_upload_mb * 1024 * 1024:
                target.unlink(missing_ok=True)
                raise HTTPException(413, f"Upload exceeds {settings.max_upload_mb} MB")
            out.write(chunk)
    return JSONResponse({"upload_id": upload_id, "size_bytes": size, "expires_after_minutes": 60})

async def download_artifact_route(request: Request):
    job_id = job_repo(request.path_params["job_id"])
    client = gh()
    owner = client.owner
    run = client.latest_run(owner, job_id)
    if not run or run["status"] != "completed" or run["conclusion"] != "success":
        raise HTTPException(409, "Build is not successfully completed")
    artifacts = client.artifacts(owner, job_id, run["id"])
    artifact = next((a for a in artifacts if a["name"] == "build-output"), None)
    if not artifact:
        raise HTTPException(404, "Build artifact not found")
    content = client.download_artifact(owner, job_id, artifact["id"])
    return StreamingResponse(io.BytesIO(content), media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{job_id}-build-output.zip"'
    })

async def run_submit(upload_id: str, language: str, build_command: str, artifact_paths: str, timeout_minutes: int):
    if language not in LANGUAGES:
        raise ValueError(f"language must be one of: {', '.join(sorted(LANGUAGES))}")
    if not build_command.strip():
        raise ValueError("build_command is required")
    if not artifact_paths.strip():
        raise ValueError("artifact_paths is required")
    if not re.fullmatch(r"[a-f0-9]{16}", upload_id):
        raise ValueError("Invalid upload_id")
    timeout_minutes = max(1, min(int(timeout_minutes), settings.max_build_minutes))
    archive = Path(settings.temp_dir) / "uploads" / f"{upload_id}.zip"
    if not archive.exists():
        raise ValueError("Upload not found or expired; upload the source ZIP again")
    if time.time() - archive.stat().st_mtime > 3600:
        archive.unlink(missing_ok=True)
        raise ValueError("Upload expired after 60 minutes; upload the source ZIP again")
    work = Path(tempfile.mkdtemp(prefix="remote-build-"))
    repo = f"rb-{uuid.uuid4().hex[:16]}"
    try:
        safe_extract(archive, work)
        archive.unlink(missing_ok=True)
        client = gh()
        created = client.create_private_repo(repo)
        owner = created["owner"]["login"]
        repo_url = created["clone_url"]
        try:
            client.push_source(repo_url, work, make_workflow())
            client.dispatch(owner, repo, language, build_command, artifact_paths, timeout_minutes)
        except Exception:
            try:
                client.delete_repo(owner, repo)
            except Exception:
                pass
            raise
        return {"job_id": repo, "repository": f"{owner}/{repo}", "status": "queued",
                "language": language, "timeout_minutes": timeout_minutes,
                "note": "Source is in a temporary private repository and will be deleted after artifact delivery/cleanup."}
    finally:
        shutil.rmtree(work, ignore_errors=True)

@mcp.tool()
async def submit_build(upload_id: str, language: str, build_command: str,
                       artifact_paths: str = "app/build/outputs/**/*.apk",
                       timeout_minutes: int = 30) -> dict:
    """Start a remote build from a source ZIP previously uploaded to /upload.

    language: android, kotlin, rust, go, python, node, cpp, flutter, or generic.
    artifact_paths: newline-separated GitHub Actions upload-artifact globs.
    Returns immediately with a job_id; poll get_build_status.
    """
    import asyncio
    return await asyncio.to_thread(run_submit, upload_id, language, build_command, artifact_paths, timeout_minutes)

@mcp.tool()
def get_build_status(job_id: str) -> dict:
    """Get the latest status and conclusion for a remote build job."""
    client = gh()
    owner = client.owner
    repo = job_repo(job_id)
    run = client.latest_run(owner, repo)
    if not run:
        return {"job_id": job_id, "status": "queued", "detail": "Workflow run has not appeared yet."}
    return {"job_id": job_id, "status": run["status"], "conclusion": run.get("conclusion"),
            "run_number": run.get("run_number"), "html_url": run.get("html_url"),
            "created_at": run.get("created_at"), "updated_at": run.get("updated_at")}

@mcp.tool()
def get_build_logs(job_id: str) -> dict:
    """Retrieve the most recent build log text for a job."""
    import zipfile
    client = gh()
    owner = client.owner
    repo = job_repo(job_id)
    run = client.latest_run(owner, repo)
    if not run:
        return {"job_id": job_id, "status": "queued", "logs": "Workflow run not available yet."}
    data = client.run_logs(owner, repo, run["id"])
    snippets = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                if name.endswith(".txt"):
                    snippets.append(f"--- {name} ---\n{zf.read(name).decode('utf-8', errors='replace')}")
    except zipfile.BadZipFile:
        snippets.append("Could not parse GitHub log archive.")
    return {"job_id": job_id, "status": run["status"], "conclusion": run.get("conclusion"),
            "logs": "\n\n".join(snippets)[-30000:]}

@mcp.tool()
def deliver_build_artifact(job_id: str) -> dict:
    """Send the successful build-output ZIP to the configured Telegram channel, then delete its temporary GitHub repo.

    If the artifact is too large for Telegram's standard Bot API upload limit, the repository is kept for manual retrieval.
    """
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise ValueError("Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID before artifact delivery.")
    client = gh()
    owner = client.owner
    repo = job_repo(job_id)
    run = client.latest_run(owner, repo)
    if not run or run["status"] != "completed" or run["conclusion"] != "success":
        raise ValueError("Build has not completed successfully.")
    artifacts = client.artifacts(owner, repo, run["id"])
    artifact = next((a for a in artifacts if a["name"] == "build-output"), None)
    if not artifact:
        raise ValueError("No build-output artifact was produced. Check build logs and artifact_paths.")
    data = client.download_artifact(owner, repo, artifact["id"])
    if len(data) > 49 * 1024 * 1024:
        return {"delivered": False, "reason": "Artifact ZIP exceeds Telegram Bot API's standard upload limit.",
                "size_bytes": len(data), "download_endpoint": f"/download/{job_id}",
                "repository_kept": True, "workflow_url": run["html_url"]}
    filename = f"{job_id}-build-output.zip"
    response = requests.post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendDocument",
        data={"chat_id": settings.telegram_chat_id, "caption": f"Build successful: {job_id}"},
        files={"document": (filename, data, "application/zip")},
        timeout=180,
    )
    payload = response.json()
    if not response.ok or not payload.get("ok"):
        raise RuntimeError(f"Telegram upload failed: {payload.get('description', response.status_code)}")
    client.delete_repo(owner, repo)
    return {"delivered": True, "telegram_message_id": payload["result"]["message_id"],
            "filename": filename, "size_bytes": len(data), "temporary_repository_deleted": True}

@mcp.tool()
def delete_build(job_id: str) -> dict:
    """Delete a temporary build repository manually after retrieving logs/artifacts."""
    client = gh()
    owner = client.owner
    repo = job_repo(job_id)
    client.delete_repo(owner, repo)
    return {"deleted": True, "job_id": job_id}

routes = [
    Route("/health", health, methods=["GET"]),
    Route("/upload", upload_source, methods=["POST"]),
    Route("/download/{job_id}", download_artifact_route, methods=["GET"]),
    Mount("/", app=mcp.streamable_http_app()),
]

async def lifespan(app):
    async with mcp.session_manager.run():
        yield

app = Starlette(routes=routes, middleware=[Middleware(AuthMiddleware)], lifespan=lifespan)
