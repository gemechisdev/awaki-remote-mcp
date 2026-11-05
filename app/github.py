"""Minimal async GitHub REST client (only what this project needs)."""
from __future__ import annotations

import asyncio
import base64
import io
import re
import zipfile
from typing import Any

import httpx

API_VERSION = "2022-11-28"
WORKFLOW_FILE = "build.yml"


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class GitHub:
    def __init__(self, token: str, repo: str, api_url: str = "https://api.github.com",
                 client: httpx.AsyncClient | None = None):
        if not token:
            raise GitHubError("GITHUB_TOKEN is not configured")
        self.repo = repo
        self.api = api_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=15.0),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "remote-build-mcp",
            },
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- plumbing ----------------------------------------------------------
    async def _req(self, method: str, path: str, *, ok: tuple[int, ...] = (200, 201, 202, 204),
                   retries: int = 3, **kw: Any) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = await self._client.request(method, f"{self.api}{path}", **kw)
            except httpx.TransportError as exc:  # network blip
                last = GitHubError(f"GitHub unreachable: {exc}")
                await asyncio.sleep(min(2 ** attempt, 8))
                continue
            if resp.status_code in ok:
                return resp
            if resp.status_code in (502, 503, 504) and attempt < retries:
                await asyncio.sleep(min(2 ** attempt, 8))
                continue
            raise GitHubError(self._explain(method, path, resp), resp.status_code)
        raise last or GitHubError("GitHub request failed")

    @staticmethod
    def _explain(method: str, path: str, resp: httpx.Response) -> str:
        try:
            detail = resp.json().get("message", resp.text[:300])
        except Exception:
            detail = resp.text[:300]
        hint = ""
        if resp.status_code in (403, 429) and resp.headers.get("x-ratelimit-remaining") == "0":
            hint = f" (rate limited; resets at unix time {resp.headers.get('x-ratelimit-reset')})"
        elif resp.status_code == 401:
            hint = " (GITHUB_TOKEN is invalid or expired)"
        elif resp.status_code == 404:
            hint = " (repo/workflow not found, or the token cannot access it; did you run `python -m app.cli bootstrap`?)"
        return f"GitHub API {method} {path} failed ({resp.status_code}): {detail}{hint}"

    # -- repo ----------------------------------------------------------------
    async def repo_info(self) -> dict:
        return (await self._req("GET", f"/repos/{self.repo}")).json()

    # -- workflow runs -------------------------------------------------------
    async def dispatch(self, ref: str, inputs: dict[str, str]) -> int | None:
        """Trigger the build workflow. Returns the run id when GitHub provides it."""
        path = f"/repos/{self.repo}/actions/workflows/{WORKFLOW_FILE}/dispatches"
        body = {"ref": ref, "inputs": inputs, "return_run_details": True}
        try:
            resp = await self._req("POST", path, json=body)
        except GitHubError as exc:
            if exc.status in (400, 422) and "return_run_details" in str(exc):
                resp = await self._req("POST", path, json={"ref": ref, "inputs": inputs})
            elif exc.status == 422 and "workflow_dispatch" in str(exc):
                raise GitHubError(
                    f"{exc}. The runner repo has no up-to-date workflow; run `python -m app.cli bootstrap`.",
                    422,
                ) from exc
            else:
                raise
        if resp.status_code == 200 and resp.content:
            try:
                return int(resp.json().get("workflow_run_id"))
            except (TypeError, ValueError):
                return None
        return None

    async def find_run(self, job_id: str) -> dict | None:
        """Find the run whose ``run-name`` equals the job id."""
        resp = await self._req("GET", f"/repos/{self.repo}/actions/workflows/{WORKFLOW_FILE}/runs",
                               params={"event": "workflow_dispatch", "per_page": 50})
        for run in resp.json().get("workflow_runs", []):
            if run.get("display_title") == job_id or run.get("name") == job_id:
                return run
        return None

    async def get_run(self, run_id: int) -> dict:
        return (await self._req("GET", f"/repos/{self.repo}/actions/runs/{run_id}")).json()

    async def cancel_run(self, run_id: int) -> None:
        await self._req("POST", f"/repos/{self.repo}/actions/runs/{run_id}/cancel", ok=(202, 409))

    async def run_log_text(self, run_id: int, max_chars: int = 400_000) -> str:
        resp = await self._req("GET", f"/repos/{self.repo}/actions/runs/{run_id}/logs", timeout=120)
        try:
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                parts = [zf.read(n).decode("utf-8", "replace") for n in sorted(zf.namelist()) if n.endswith(".txt")]
        except zipfile.BadZipFile:
            return ""
        return "\n".join(parts)[-max_chars:]

    # -- repo contents (used by bootstrap / sync) ------------------------------
    async def get_file(self, path: str, ref: str) -> tuple[str, bytes] | None:
        try:
            data = (await self._req("GET", f"/repos/{self.repo}/contents/{path}", params={"ref": ref})).json()
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        return data["sha"], base64.b64decode(data.get("content", ""))

    async def put_file(self, path: str, content: bytes, message: str, branch: str, sha: str | None) -> None:
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content).decode(),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        await self._req("PUT", f"/repos/{self.repo}/contents/{path}", json=body)

    # -- secrets ---------------------------------------------------------------
    async def list_secret_names(self) -> set[str]:
        data = (await self._req("GET", f"/repos/{self.repo}/actions/secrets", params={"per_page": 100})).json()
        return {item["name"] for item in data.get("secrets", [])}

    async def set_secret(self, name: str, value: str) -> None:
        from nacl import encoding, public  # lazy: only bootstrap needs it

        key = (await self._req("GET", f"/repos/{self.repo}/actions/secrets/public-key")).json()
        box = public.SealedBox(public.PublicKey(key["key"].encode(), encoding.Base64Encoder()))
        encrypted = base64.b64encode(box.encrypt(value.encode())).decode()
        await self._req("PUT", f"/repos/{self.repo}/actions/secrets/{name}",
                        json={"encrypted_value": encrypted, "key_id": key["key_id"]})


_RB_RESULT = re.compile(r"RB_RESULT (\{.*\})")


def parse_rb_result(log_text: str) -> dict | None:
    """Recover the (non-sensitive) result line the runner prints in the public log."""
    import json

    found = None
    for match in _RB_RESULT.finditer(log_text):
        try:
            found = json.loads(match.group(1))
        except ValueError:
            continue
    return found
