"""Authentication, rate limiting and archive validation."""
from __future__ import annotations

import hashlib
import hmac
import time
import zipfile
from collections import defaultdict, deque
from pathlib import Path, PurePosixPath

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Settings, derive_session_token, verify_download_token
from urllib.parse import parse_qs


def tokens_equal(supplied: str, expected: str) -> bool:
    """Constant-time comparison that also hides the expected length."""
    if not expected:
        return False
    a = hashlib.sha256(supplied.encode()).digest()
    b = hashlib.sha256(expected.encode()).digest()
    return hmac.compare_digest(a, b)


def bearer(headers: dict[str, str]) -> str:
    value = headers.get("authorization", "")
    return value[7:].strip() if value.lower().startswith("bearer ") else ""


class AuthMiddleware:
    """Pure-ASGI auth (BaseHTTPMiddleware would buffer MCP streaming responses).

    * ``/health``            open (details only for authenticated callers)
    * ``/runner/*``          runner token (given to GitHub as an encrypted secret)
    * ``/srunner/<sid>/*``   per-session token derived from the runner token
    * everything else        MCP_API_TOKEN
    Repeated failures from one client IP are throttled.
    """

    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.settings = settings
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def _client(self, scope: Scope, headers: dict[str, str]) -> str:
        fwd = headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
        client = scope.get("client")
        return client[0] if client else "unknown"

    def _blocked(self, who: str) -> bool:
        q = self._fails[who]
        now = time.time()
        while q and now - q[0] > 300:
            q.popleft()
        return len(q) >= 20

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        headers = {k.decode().lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if path == "/health":
            scope["state"] = {**scope.get("state", {}), "authed": tokens_equal(bearer(headers), self.settings.mcp_api_token)}
            await self.app(scope, receive, send)
            return
        who = self._client(scope, headers)
        if self._blocked(who):
            resp = JSONResponse({"error": "Too many failed authentication attempts"}, status_code=429,
                                headers={"Retry-After": "300"})
            await resp(scope, receive, send)
            return
        # Temporary download tokens: agents get a signed URL without the long-lived MCP token
        if self._allow_download_token(path, scope.get("query_string", b"")):
            await self.app(scope, receive, send)
            return
        if path.startswith("/srunner/"):  # interactive session runner: per-session token
            parts = path.split("/")
            sid = parts[2] if len(parts) > 2 else ""
            expected = derive_session_token(self.settings.runner_token, sid) if sid and self.settings.runner_token else ""
        elif path.startswith("/runner/"):
            expected = self.settings.runner_token
        else:
            expected = self.settings.mcp_api_token
        if not tokens_equal(bearer(headers), expected):
            self._fails[who].append(time.time())
            resp = JSONResponse({"error": "Unauthorized"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _allow_download_token(self, path: str, query_string: bytes) -> bool:
        """Allow GET of build/session artifacts when a valid short-lived ?token= is present."""
        if not path.startswith("/api/builds/") and not path.startswith("/api/sessions/"):
            return False
        if "/artifact" not in path:
            return False
        qs = parse_qs(query_string.decode("latin-1", "replace"))
        token = (qs.get("token") or [""])[0]
        if not token:
            return False
        parts = path.strip("/").split("/")
        # /api/builds/{job_id}/artifact[/{name}]
        # /api/sessions/{sid}/artifacts/{name}
        try:
            if parts[1] == "builds" and len(parts) >= 4 and parts[3] == "artifact":
                job_id, name = parts[2], parts[4] if len(parts) > 4 else ""
                # When no name in path, token is bound to empty name or the single file
                return verify_download_token(self.settings.mcp_api_token, "build", job_id, name or "*", token)
            if parts[1] == "sessions" and len(parts) >= 5 and parts[3] == "artifacts":
                sid, name = parts[2], parts[4]
                return verify_download_token(self.settings.mcp_api_token, "session", sid, name, token)
        except Exception:
            return False
        return False


# ---------------------------------------------------------------------------
# Archive validation (runs at upload time so bad archives fail fast)
# ---------------------------------------------------------------------------
class ArchiveError(ValueError):
    pass


_MARKERS = {
    "AndroidManifest.xml": "android",
    "gradlew": "gradle",
    "build.gradle": "gradle",
    "build.gradle.kts": "gradle",
    "pom.xml": "java",
    "Cargo.toml": "rust",
    "go.mod": "go",
    "pyproject.toml": "python",
    "setup.py": "python",
    "package.json": "node",
    "CMakeLists.txt": "cpp",
    "pubspec.yaml": "flutter",
}


def inspect_zip(path: Path, max_extract_mb: int) -> dict:
    """Validate a source archive and describe it. Raises ArchiveError."""
    if not zipfile.is_zipfile(path):
        raise ArchiveError("The upload is not a valid ZIP archive")
    limit = max_extract_mb * 1024 * 1024
    total = 0
    count = 0
    markers: set[str] = set()
    roots: set[str] = set()
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            raw = info.filename.replace("\\", "/")
            p = PurePosixPath(raw)
            if p.is_absolute() or ".." in p.parts:
                raise ArchiveError(f"Unsafe path in archive: {raw}")
            if p.parts and (len(p.parts) > 1 or info.is_dir()):
                roots.add(p.parts[0])
            if len(p.parts) == 1 and not info.is_dir():
                roots.add("")  # a loose file at the top level: not a single wrapper directory
            if info.is_dir():
                continue
            count += 1
            total += info.file_size
            if total > limit:
                raise ArchiveError(f"Archive expands to more than {max_extract_mb} MB")
            if len(p.parts) <= 3 and p.name in _MARKERS:
                markers.add(_MARKERS[p.name])
    if count == 0:
        raise ArchiveError("The archive contains no files")
    detected = sorted(markers)
    if "android" in markers:
        detected = ["android"] + [m for m in detected if m not in {"android", "gradle"}]
    elif "gradle" in markers:
        detected = ["kotlin/java (gradle)"] + [m for m in detected if m != "gradle"]
    return {"files": count, "uncompressed_mb": round(total / 1048576, 1), "detected": detected,
            "single_root_dir": len(roots) == 1 and "" not in roots}
