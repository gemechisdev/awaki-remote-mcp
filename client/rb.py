#!/usr/bin/env python3
"""Agent-side helper (standard library only): zip -> upload -> build -> wait -> download.

    export RB_URL=https://your-service.onrender.com
    export RB_TOKEN=<MCP_API_TOKEN>
    python rb.py build --dir ./my-app --language android --out ./out
    python rb.py build --dir . --language rust --label "my tool" --out ./out
    python rb.py status rb-0123456789abcdef
    python rb.py logs   rb-0123456789abcdef
    python rb.py cancel rb-0123456789abcdef

Exit codes: 0 success, 1 build failed, 2 usage/service error.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

EXCLUDE = [".git", ".gradle", ".idea", "node_modules", "build", "dist", "target", ".venv", "venv",
           "__pycache__", ".dart_tool", ".next", "local.properties", "*.keystore", "*.jks", ".env", ".env.*",
           "*.pem", "*.p12", "*.apk", "*.aab", "*.zip"]


def base() -> tuple[str, dict]:
    url, token = os.environ.get("RB_URL", "").rstrip("/"), os.environ.get("RB_TOKEN", "")
    if not url or not token:
        sys.exit("Set RB_URL and RB_TOKEN")
    return url, {"Authorization": f"Bearer {token}"}


def call(method: str, path: str, body=None, headers=None, raw: bool = False, timeout: int = 120):
    url, auth = base()
    data = body if isinstance(body, (bytes, type(None))) or hasattr(body, "read") else json.dumps(body).encode()
    req = urllib.request.Request(url + path, data=data, method=method, headers={**auth, **(headers or {})})
    if isinstance(body, dict):
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp if raw else json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        try:
            detail = json.loads(detail).get("error", detail)
        except ValueError:
            pass
        sys.exit(f"HTTP {exc.code} from service: {detail}")
    except urllib.error.URLError as exc:
        sys.exit(f"Cannot reach service: {exc.reason}")


def excluded(rel: Path) -> bool:
    return any(fnmatch.fnmatch(part, pat) for part in rel.parts for pat in EXCLUDE)


def make_zip(src: Path, dest: Path) -> int:
    count = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src.rglob("*")):
            rel = path.relative_to(src)
            if path.is_file() and not path.is_symlink() and not excluded(rel):
                info = zipfile.ZipInfo.from_file(path, rel.as_posix())
                info.compress_type = zipfile.ZIP_DEFLATED
                with open(path, "rb") as fh:
                    zf.writestr(info, fh.read())
                count += 1
    return count


def cmd_build(a: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "source.zip"
        n = make_zip(Path(a.dir).resolve(), archive)
        print(f"Zipped {n} files ({archive.stat().st_size // 1024} KiB); uploading...", file=sys.stderr)
        with open(archive, "rb") as fh:
            up = call("POST", "/upload", fh, {"Content-Type": "application/zip",
                                              "Content-Length": str(archive.stat().st_size)}, timeout=600)
    print(f"Detected: {', '.join(up.get('detected', [])) or 'unknown'}", file=sys.stderr)
    payload = {"upload_id": up["upload_id"], "language": a.language, "timeout_minutes": a.timeout}
    for key in ("build_command", "artifact_paths", "working_directory", "label"):
        if getattr(a, key):
            payload[key] = getattr(a, key)
    if a.no_artifact:
        payload["return_artifacts"] = False
    job = call("POST", "/api/builds", payload)
    job_id = job["job_id"]
    print(f"Submitted {job_id}  {job.get('run_url') or ''}", file=sys.stderr)
    last = None
    while True:
        st = call("GET", f"/api/builds/{job_id}?wait=50", timeout=120)
        if st["state"] != last:
            print(f"state: {st['state']} - {st.get('detail', '')}", file=sys.stderr)
            last = st["state"]
        if st.get("done"):
            break
        time.sleep(1)
    if st["state"] != "succeeded":
        logs = call("GET", f"/api/builds/{job_id}/logs?tail=80")
        print(logs.get("error_excerpt") or logs.get("log_tail") or logs.get("message", ""))
        print(json.dumps(st, indent=2))
        return 1
    art = st.get("artifact")
    if art:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        target = out / art["filename"]
        with call("GET", f"/api/builds/{job_id}/artifact", raw=True, timeout=1800) as resp, open(target, "wb") as fh:
            while chunk := resp.read(1 << 20):
                fh.write(chunk)
        print(f"Saved {target} ({target.stat().st_size} bytes, sha256 {art['sha256']})", file=sys.stderr)
        print(str(target))
    print(json.dumps(st, indent=2), file=sys.stderr)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--dir", default=".")
    b.add_argument("--language", required=True)
    b.add_argument("--build-command", dest="build_command")
    b.add_argument("--artifact-paths", dest="artifact_paths", help="newline-separated globs")
    b.add_argument("--working-directory", dest="working_directory")
    b.add_argument("--label")
    b.add_argument("--timeout", type=int, default=30)
    b.add_argument("--out", default="./build-output")
    b.add_argument("--no-artifact", action="store_true")
    for name in ("status", "logs", "cancel"):
        sub.add_parser(name).add_argument("job_id")
    a = p.parse_args()
    if a.cmd == "build":
        sys.exit(cmd_build(a))
    path = {"status": "GET /api/builds/{}", "logs": "GET /api/builds/{}/logs?tail=200",
            "cancel": "POST /api/builds/{}/cancel"}[a.cmd]
    method, route = path.split(" ", 1)
    print(json.dumps(call(method, route.format(urllib.parse.quote(a.job_id)), b"" if method == "POST" else None), indent=2))


if __name__ == "__main__":
    main()
