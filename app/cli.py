"""Command line helper:  python -m app.cli {bootstrap|sync|secrets|doctor}"""
from __future__ import annotations

import argparse
import asyncio
import sys

from .bootstrap import doctor, push_secrets, sync_files
from .config import Settings
from .github import GitHub, GitHubError


async def _run(cmd: str) -> int:
    s = Settings.from_env()
    if not s.github_token or not s.github_repo:
        print("Set GITHUB_TOKEN and GITHUB_REPO (owner/name) first.", file=sys.stderr)
        return 2
    gh = GitHub(s.github_token, s.github_repo, s.github_api_url)
    try:
        if cmd in ("bootstrap", "sync"):
            changed = await sync_files(gh, s.github_ref)
            print("Pushed:" if changed else "Runner files already up to date.", *changed)
        if cmd in ("bootstrap", "secrets"):
            if len(s.mcp_api_token) < 24 or not s.public_url:
                print("Set MCP_API_TOKEN (>=24 chars) and PUBLIC_URL before pushing secrets.", file=sys.stderr)
                return 2
            print("Set repository secrets:", ", ".join(await push_secrets(gh, s)))
        if cmd in ("bootstrap", "doctor"):
            findings = await doctor(gh, s)
            for level, message in findings:
                print(f"[{level:4}] {message}")
            return 1 if any(level == "FAIL" for level, _ in findings) else 0
        return 0
    except GitHubError as exc:
        print(f"GitHub error: {exc}", file=sys.stderr)
        return 1
    finally:
        await gh.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__)
    parser.add_argument("command", choices=["bootstrap", "sync", "secrets", "doctor"])
    sys.exit(asyncio.run(_run(parser.parse_args().command)))


if __name__ == "__main__":
    main()
