"""One-time (and on-deploy) setup of the PUBLIC runner repository.

The repository only ever contains two generic, secret-free files:
  .github/workflows/build.yml          (the workflow)
  .github/remote-build/runner.py       (the step scripts)
Your source code and artifacts never touch it.
"""
from __future__ import annotations

from pathlib import Path

from .config import Settings
from .github import GitHub, GitHubError

ROOT = Path(__file__).resolve().parent.parent
MANAGED_FILES: dict[str, Path] = {
    ".github/workflows/build.yml": ROOT / "workflow" / "build.yml",
    ".github/remote-build/runner.py": ROOT / "runner" / "runner.py",
}


async def sync_files(gh: GitHub, ref: str) -> list[str]:
    """Create/update the managed files. Returns the repo paths that changed."""
    changed: list[str] = []
    for repo_path, local in MANAGED_FILES.items():
        content = local.read_bytes()
        existing = await gh.get_file(repo_path, ref)
        if existing and existing[1] == content:
            continue
        await gh.put_file(repo_path, content, f"Update {repo_path} (remote-build-mcp)", ref,
                          existing[0] if existing else None)
        changed.append(repo_path)
    return changed


def expected_secrets(s: Settings) -> dict[str, str]:
    return {"RB_SERVICE_URL": s.public_url, "RB_RUNNER_TOKEN": s.runner_token}


async def push_secrets(gh: GitHub, s: Settings) -> list[str]:
    names = []
    for name, value in expected_secrets(s).items():
        await gh.set_secret(name, value)
        names.append(name)
    return names


async def run_bootstrap(gh: GitHub, s: Settings) -> dict:
    """Everything `python -m app.cli bootstrap` does, callable at startup. Idempotent and cheap:
    pushes the workflow/runner files if they changed, (re)sets the two repository secrets, then runs the checks."""
    problems = s.problems()
    if problems:
        raise GitHubError("Configuration incomplete: " + "; ".join(problems))
    try:
        changed = await sync_files(gh, s.github_ref)
        secrets = await push_secrets(gh, s)
    except GitHubError as exc:
        hint = ""
        if exc.status in (403, 404):
            hint = (" The token needs access to ONLY the runner repo with: Actions R/W, Contents R/W, "
                    "Secrets R/W, Metadata R. Also check GITHUB_REPO and that the repo has a 'main' branch "
                    "(create it with a README).")
        elif exc.status in (409, 422):
            hint = " The repo may be empty: add a README on the 'main' branch first."
        raise GitHubError(str(exc) + hint, exc.status) from exc
    findings = await doctor(gh, s)
    return {"files_updated": changed, "secrets_set": secrets,
            "findings": [{"level": lv, "message": msg} for lv, msg in findings],
            "ok": not any(lv == "FAIL" for lv, _ in findings)}


async def doctor(gh: GitHub, s: Settings) -> list[tuple[str, str]]:
    """Return (level, message) findings; level is OK / WARN / FAIL."""
    out: list[tuple[str, str]] = []
    for problem in s.problems():
        out.append(("FAIL", problem))
    try:
        info = await gh.repo_info()
    except GitHubError as exc:
        out.append(("FAIL", str(exc)))
        return out
    if info.get("private"):
        out.append(("WARN", "Runner repo is PRIVATE: builds count against the 2,000 free minutes/month. "
                            "Make it public for unlimited free standard-runner minutes (it holds no source or secrets)."))
    else:
        out.append(("OK", "Runner repo is public (free, unlimited standard-runner minutes)."))
    if info.get("default_branch") != s.github_ref:
        out.append(("WARN", f"Default branch is '{info.get('default_branch')}' but GITHUB_REF_NAME is '{s.github_ref}'."))
    if info.get("archived"):
        out.append(("FAIL", "Runner repo is archived."))
    for repo_path, local in MANAGED_FILES.items():
        existing = await gh.get_file(repo_path, s.github_ref)
        if existing is None:
            out.append(("FAIL", f"{repo_path} is missing in the runner repo (run bootstrap)."))
        elif existing[1] != local.read_bytes():
            out.append(("WARN", f"{repo_path} differs from this release (run sync, or enable SYNC_RUNNER_REPO)."))
        else:
            out.append(("OK", f"{repo_path} is up to date."))
    try:
        present = await gh.list_secret_names()
        missing = sorted(set(expected_secrets(s)) - present)
        out.append(("FAIL", f"Missing repository secrets: {', '.join(missing)} (run bootstrap)") if missing
                   else ("OK", "All required repository secrets exist."))
    except GitHubError as exc:
        out.append(("WARN", f"Could not list secrets: {exc}"))
    return out
