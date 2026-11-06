"""Environment-driven configuration.

Everything is read once by ``Settings.from_env()``.  Secrets only ever come
from environment variables.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path


def _int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(lo, min(value, hi))


def derive_runner_token(mcp_api_token: str) -> str:
    """Derive the runner secret from the MCP token (one-way).

    You only have to manage ONE secret.  If the runner token ever leaks it
    does not reveal the MCP token.
    """
    return hmac.new(mcp_api_token.encode(), b"remote-build-mcp/runner/v1", hashlib.sha256).hexdigest()


def derive_session_token(runner_token: str, session_id: str) -> str:
    """Per-session token (stateless: recomputed on every request, survives service restarts).

    Only this value is available to the interactive session on the runner; the global runner
    token is used just once, by the fetch step, and is not present while your commands run.
    """
    return hmac.new(runner_token.encode(), f"session:{session_id}".encode(), hashlib.sha256).hexdigest()


def make_download_token(mcp_api_token: str, kind: str, resource_id: str, name: str,
                        ttl_seconds: int = 3600) -> tuple[str, int]:
    """Short-lived signed download token. Agents use the resulting URL without the MCP token.

    Returns (token, expires_unix). kind is 'build' or 'session'.
    """
    expires = int(time.time()) + max(60, min(ttl_seconds, 86400))
    payload = f"dl:v1:{kind}:{resource_id}:{name}:{expires}"
    sig = hmac.new(mcp_api_token.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{expires}.{sig}", expires


def verify_download_token(mcp_api_token: str, kind: str, resource_id: str, name: str, token: str) -> bool:
    """Validate a temporary download token. Constant-time where practical."""
    if not token or not mcp_api_token or "." not in token:
        return False
    try:
        exp_s, sig = token.split(".", 1)
        expires = int(exp_s)
    except ValueError:
        return False
    if expires < int(time.time()):
        return False
    payload = f"dl:v1:{kind}:{resource_id}:{name}:{expires}"
    expected = hmac.new(mcp_api_token.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, expected)


# Client access tokens: minted by authenticated MCP tool; used for HTTP/WS by the agent
_CLIENT_SCOPES = frozenset({"upload", "download", "logs", "api"})


def make_client_token(mcp_api_token: str, scopes: list[str] | None = None,
                      ttl_seconds: int = 3600) -> tuple[str, int, list[str]]:
    """Issue a temporary client token. Stateless (HMAC); valid until expiry across restarts.

    Returns (token, expires_unix, scopes). Default scopes cover upload, download, logs, api.
    """
    allowed = sorted(_CLIENT_SCOPES if not scopes else (set(scopes) & _CLIENT_SCOPES))
    if not allowed:
        allowed = sorted(_CLIENT_SCOPES)
    expires = int(time.time()) + max(60, min(int(ttl_seconds), 86400))
    scope_s = ",".join(allowed)
    payload = f"client:v1:{scope_s}:{expires}"
    sig = hmac.new(mcp_api_token.encode(), payload.encode(), hashlib.sha256).hexdigest()[:40]
    return f"ct.{expires}.{scope_s.replace(',', '+')}.{sig}", expires, allowed


def verify_client_token(mcp_api_token: str, token: str, need_scope: str | None = None) -> bool:
    """Validate a client access token; optionally require a scope (upload|download|logs|api)."""
    if not token or not mcp_api_token or not token.startswith("ct."):
        return False
    parts = token.split(".")
    if len(parts) != 4:
        return False
    _, exp_s, scope_enc, sig = parts
    try:
        expires = int(exp_s)
    except ValueError:
        return False
    if expires < int(time.time()):
        return False
    scope_s = scope_enc.replace("+", ",")
    payload = f"client:v1:{scope_s}:{expires}"
    expected = hmac.new(mcp_api_token.encode(), payload.encode(), hashlib.sha256).hexdigest()[:40]
    if not hmac.compare_digest(sig, expected):
        return False
    if need_scope:
        return need_scope in scope_s.split(",")
    return True


def _public_url() -> str:
    url = os.getenv("PUBLIC_URL", "").strip() or os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if not url and os.getenv("KOYEB_PUBLIC_DOMAIN"):
        url = "https://" + os.environ["KOYEB_PUBLIC_DOMAIN"].strip()
    return url.rstrip("/")


@dataclass(frozen=True)
class Settings:
    # GitHub
    github_token: str = ""
    github_repo: str = ""          # "owner/name" of the PUBLIC runner repo
    github_ref: str = "main"
    github_api_url: str = "https://api.github.com"
    # Auth
    mcp_api_token: str = ""
    runner_token: str = ""
    public_url: str = ""
    # Storage / limits
    data_dir: Path = Path("./data")
    max_upload_mb: int = 100
    max_extract_mb: int = 1000
    max_build_minutes: int = 60
    max_concurrent_builds: int = 3
    source_ttl_minutes: int = 120
    job_retention_hours: int = 72
    max_log_mb: int = 8
    max_artifact_mb: int = 200
    artifact_ttl_minutes: int = 180
    # Sessions (interactive "remote shell" mode)
    max_session_minutes: int = 180
    max_concurrent_sessions: int = 2
    session_idle_minutes: int = 30
    session_retain_minutes: int = 30
    max_cmd_log_mb: int = 20
    max_session_artifact_mb: int = 500
    # Behaviour
    sync_runner_repo: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        mcp_token = os.getenv("MCP_API_TOKEN", "").strip()
        return cls(
            github_token=os.getenv("GITHUB_TOKEN", "").strip(),
            github_repo=os.getenv("GITHUB_REPO", "").strip().strip("/"),
            github_ref=os.getenv("GITHUB_REF_NAME", "main").strip() or "main",
            github_api_url=os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
            mcp_api_token=mcp_token,
            runner_token=os.getenv("RUNNER_TOKEN", "").strip()
            or (derive_runner_token(mcp_token) if mcp_token else ""),
            public_url=_public_url(),
            data_dir=Path(os.getenv("DATA_DIR", "./data")),
            max_upload_mb=_int("MAX_UPLOAD_MB", 100, 1, 2000),
            max_extract_mb=_int("MAX_EXTRACT_MB", 1000, 10, 5000),
            max_build_minutes=_int("MAX_BUILD_MINUTES", 60, 1, 350),
            max_concurrent_builds=_int("MAX_CONCURRENT_BUILDS", 3, 1, 20),
            source_ttl_minutes=_int("SOURCE_TTL_MINUTES", 120, 5, 1440),
            job_retention_hours=_int("JOB_RETENTION_HOURS", 72, 1, 24 * 30),
            max_log_mb=_int("MAX_LOG_MB", 8, 1, 50),
            max_artifact_mb=_int("MAX_ARTIFACT_MB", 200, 1, 2000),
            artifact_ttl_minutes=_int("ARTIFACT_TTL_MINUTES", 180, 5, 4320),
            max_session_minutes=_int("MAX_SESSION_MINUTES", 180, 10, 345),
            max_concurrent_sessions=_int("MAX_CONCURRENT_SESSIONS", 2, 1, 10),
            session_idle_minutes=_int("SESSION_IDLE_MINUTES", 30, 5, 240),
            session_retain_minutes=_int("SESSION_RETAIN_MINUTES", 30, 1, 1440),
            max_cmd_log_mb=_int("MAX_CMD_LOG_MB", 20, 1, 200),
            max_session_artifact_mb=_int("MAX_SESSION_ARTIFACT_MB", 500, 10, 5000),
            sync_runner_repo=(os.getenv("AUTO_BOOTSTRAP") or os.getenv("SYNC_RUNNER_REPO") or "true")
            .strip().lower() not in {"0", "false", "no"},
        )

    @property
    def owner_repo(self) -> tuple[str, str]:
        owner, _, repo = self.github_repo.partition("/")
        return owner, repo

    def problems(self) -> list[str]:
        """Human-readable configuration problems (empty list == healthy)."""
        out: list[str] = []
        if len(self.mcp_api_token) < 24:
            out.append("MCP_API_TOKEN must be set to a random string of at least 24 characters")
        if not self.github_token:
            out.append("GITHUB_TOKEN is not set")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.github_repo):
            out.append("GITHUB_REPO must look like 'owner/name' (your PUBLIC runner repository)")
        if not self.public_url.startswith(("https://", "http://")):
            out.append("PUBLIC_URL is not set (the GitHub runner must be able to reach this service)")
        return out
