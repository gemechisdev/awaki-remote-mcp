import base64
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
import requests

API_VERSION = "2022-11-28"

class GitHubError(RuntimeError):
    pass

class GitHub:
    def __init__(self, token: str, api_url: str = "https://api.github.com"):
        if not token:
            raise GitHubError("GITHUB_TOKEN is not configured")
        self.token = token
        self.api = api_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        })
        self._owner = None

    def request(self, method: str, path: str, **kwargs):
        response = self.session.request(method, self.api + path, timeout=60, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json().get("message", response.text)
            except Exception:
                detail = response.text
            raise GitHubError(f"GitHub API {method} {path} failed ({response.status_code}): {detail}")
        if response.status_code == 204 or not response.content:
            return None
        return response

    @property
    def owner(self) -> str:
        if self._owner is None:
            self._owner = self.request("GET", "/user").json()["login"]
        return self._owner

    def create_private_repo(self, name: str) -> dict:
        return self.request("POST", "/user/repos", json={
            "name": name,
            "private": True,
            "auto_init": False,
            "has_issues": False,
            "has_projects": False,
            "has_wiki": False,
        }).json()

    def push_source(self, repo_url: str, source_dir: Path, build_workflow: str):
        workflow_dir = source_dir / ".github" / "workflows"
        workflow_dir.mkdir(parents=True, exist_ok=True)
        (workflow_dir / "build.yml").write_text(build_workflow, encoding="utf-8")

        subprocess.run(["git", "init", "-b", "main"], cwd=source_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Remote Build Bot"], cwd=source_dir, check=True)
        subprocess.run(["git", "config", "user.email", "remote-build-bot@users.noreply.github.com"], cwd=source_dir, check=True)
        subprocess.run(["git", "add", "-A"], cwd=source_dir, check=True)
        subprocess.run(["git", "commit", "-m", "Temporary remote build source"], cwd=source_dir, check=True)

        askpass_dir = Path(tempfile.mkdtemp(prefix="gh-askpass-"))
        askpass = askpass_dir / "askpass.sh"
        askpass.write_text(
            '#!/bin/sh\ncase "$1" in *Username*) echo "x-access-token";; *) echo "$GITHUB_PUSH_TOKEN";; esac\n',
            encoding="utf-8",
        )
        askpass.chmod(0o700)
        env = os.environ.copy()
        env.update({
            "GIT_ASKPASS": str(askpass),
            "GIT_TERMINAL_PROMPT": "0",
            "GITHUB_PUSH_TOKEN": self.token,
        })
        try:
            subprocess.run(["git", "remote", "add", "origin", repo_url], cwd=source_dir, check=True, capture_output=True)
            subprocess.run(["git", "push", "-u", "origin", "main"], cwd=source_dir, check=True, capture_output=True, env=env, timeout=180)
        finally:
            shutil.rmtree(askpass_dir, ignore_errors=True)

    def dispatch(self, owner: str, repo: str, language: str, command: str, artifact_paths: str, timeout_minutes: int):
        payload = {
            "ref": "main",
            "inputs": {
                "language": language,
                "build_command": command,
                "artifact_paths": artifact_paths,
                "timeout_minutes": str(timeout_minutes),
            },
        }
        # GitHub may need a moment to index a newly-pushed workflow file.
        last_error = None
        for attempt in range(8):
            try:
                self.request("POST", f"/repos/{owner}/{repo}/actions/workflows/build.yml/dispatches", json=payload)
                return
            except GitHubError as exc:
                last_error = exc
                if "(404)" not in str(exc):
                    raise
                time.sleep(2)
        raise last_error

    def latest_run(self, owner: str, repo: str) -> dict | None:
        data = self.request("GET", f"/repos/{owner}/{repo}/actions/runs", params={
            "event": "workflow_dispatch", "per_page": 5
        }).json()
        return data["workflow_runs"][0] if data.get("workflow_runs") else None

    def run_logs(self, owner: str, repo: str, run_id: int) -> bytes:
        response = self.session.get(
            f"{self.api}/repos/{owner}/{repo}/actions/runs/{run_id}/logs",
            timeout=120,
            allow_redirects=True,
        )
        if response.status_code >= 400:
            raise GitHubError(f"Could not download build logs ({response.status_code})")
        return response.content

    def artifacts(self, owner: str, repo: str, run_id: int) -> list[dict]:
        data = self.request("GET", f"/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts").json()
        return [a for a in data.get("artifacts", []) if not a.get("expired")]

    def download_artifact(self, owner: str, repo: str, artifact_id: int) -> bytes:
        response = self.session.get(
            f"{self.api}/repos/{owner}/{repo}/actions/artifacts/{artifact_id}/zip",
            timeout=180,
            allow_redirects=True,
        )
        if response.status_code >= 400:
            raise GitHubError(f"Could not download artifact ({response.status_code})")
        return response.content

    def delete_repo(self, owner: str, repo: str):
        self.request("DELETE", f"/repos/{owner}/{repo}")
