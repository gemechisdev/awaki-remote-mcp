#!/usr/bin/env python3
"""Agent-side helper (standard library only): zip -> upload -> build -> wait -> download.

    export RB_URL=https://your-service.onrender.com
    export RB_TOKEN=<MCP_API_TOKEN>
    python rb.py build --dir ./my-app --language android --out ./out
    python rb.py build --dir . --language rust --label "my tool" --out ./out
    python rb.py status rb-0123456789abcdef
    python rb.py logs   rb-0123456789abcdef
    python rb.py cancel rb-0123456789abcdef

Interactive session (a temporary remote shell on GitHub Actions; output streams live):
    python rb.py open  --dir ./my-app --language node --include-env     # saves the id in ./.rb-session
    python rb.py exec  "npm ci && npm test"                             # streams output, exits with its exit code
    python rb.py exec  --cwd server "go build -o ../bin/app ."
    python rb.py put   --dir ./my-app --mode replace                    # push updated code (caches are kept)
    python rb.py pull  "app/build/outputs/apk/**/*.apk" --out ./out     # collect + download artifacts
    python rb.py close                                                  # data auto-expires shortly after

Exit codes: 0 success, 1 build/command failed, 2 usage/service error.
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
        if raw:  # caller streams and closes the response (`with call(..., raw=True) as resp`)
            return urllib.request.urlopen(req, timeout=timeout)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        try:
            detail = json.loads(detail).get("error", detail)
        except ValueError:
            pass
        sys.exit(f"HTTP {exc.code} from service: {detail}")
    except urllib.error.URLError as exc:
        sys.exit(f"Cannot reach service: {exc.reason}")


def excluded(rel: Path, patterns: list[str] | None = None) -> bool:
    return any(fnmatch.fnmatch(part, pat) for part in rel.parts for pat in (patterns or EXCLUDE))


SECRET_PATTERNS = {".env", ".env.*", "*.keystore", "*.jks"}


def make_zip(src: Path, dest: Path, include_secrets: bool = False) -> int:
    count = 0
    skip = [p for p in EXCLUDE if include_secrets is False or p not in SECRET_PATTERNS]
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src.rglob("*")):
            rel = path.relative_to(src)
            if path.is_file() and not path.is_symlink() and not excluded(rel, skip):
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


# ----------------------------------------------------------------------------- sessions
SESSION_FILE = Path(".rb-session")


def upload_dir(directory: str, include_env: bool) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "source.zip"
        n = make_zip(Path(directory).resolve(), archive, include_secrets=include_env)
        print(f"Zipped {n} files ({archive.stat().st_size // 1024} KiB); uploading...", file=sys.stderr)
        with open(archive, "rb") as fh:
            return call("POST", "/upload", fh, {"Content-Type": "application/zip",
                                                "Content-Length": str(archive.stat().st_size)}, timeout=600)


def session_id(a: argparse.Namespace) -> str:
    sid = getattr(a, "session", None) or os.environ.get("RB_SESSION") or (
        SESSION_FILE.read_text().strip() if SESSION_FILE.exists() else "")
    if not sid:
        sys.exit("No session: run `rb.py open ...` first (or pass --session / set RB_SESSION)")
    return sid


def cmd_open(a: argparse.Namespace) -> int:
    up = upload_dir(a.dir, a.include_env)
    payload = {"upload_id": up["upload_id"], "language": a.language, "ttl_minutes": a.ttl,
               "idle_minutes": a.idle, "retain_minutes": a.retain}
    for key in ("working_directory", "label"):
        if getattr(a, key):
            payload[key] = getattr(a, key)
    sess = call("POST", "/api/sessions", payload)
    sid = sess["session_id"]
    SESSION_FILE.write_text(sid + "\n")
    print(f"Opened {sid} (expires {sess['expires_at']}); starting the runner...", file=sys.stderr)
    while True:
        st = call("GET", f"/api/sessions/{sid}?wait=50", timeout=120)
        if st.get("ready") or st["state"] not in ("opening", "ready"):
            break
    print(f"state: {st['state']}  {st.get('note') or ''}", file=sys.stderr)
    print(sid)
    return 0 if st["state"] == "ready" else 1


def stream(sid: str, cid: str) -> int:
    offset = 0
    while True:
        out = call("GET", f"/api/sessions/{sid}/commands/{cid}/output?offset={offset}&wait=25", timeout=120)
        if out["output"]:
            sys.stdout.write(out["output"])
            sys.stdout.flush()
        offset = out["next_offset"]
        if out["finished"] and out["next_offset"] >= out["total_bytes"]:
            if out["timed_out"]:
                print("\n[command timed out]", file=sys.stderr)
            return int(out["exit_code"]) if out["exit_code"] is not None else 1


def cmd_exec(a: argparse.Namespace) -> int:
    sid = session_id(a)
    body = {"command": a.command, "timeout_seconds": a.timeout, "wait_seconds": 0}
    if a.cwd:
        body["cwd"] = a.cwd
    first = call("POST", f"/api/sessions/{sid}/exec", body)
    return stream(sid, first["cmd_id"])


def cmd_put(a: argparse.Namespace) -> int:
    sid = session_id(a)
    up = upload_dir(a.dir, a.include_env)
    res = call("POST", f"/api/sessions/{sid}/put", {"upload_id": up["upload_id"], "mode": a.mode,
                                                     "dest": a.dest or "", "wait_seconds": 120}, timeout=180)
    print(res["output"], end="")
    return 0 if res.get("exit_code") == 0 else 1


def cmd_pull(a: argparse.Namespace) -> int:
    sid = session_id(a)
    before = {x["name"] for x in call("GET", f"/api/sessions/{sid}/artifacts")["artifacts"]}
    body = {"paths": a.paths, "wait_seconds": 120}
    if a.name:
        body["name"] = a.name
    res = call("POST", f"/api/sessions/{sid}/collect", body, timeout=180)
    print(res["output"], end="", file=sys.stderr)
    if res.get("exit_code") != 0:
        return 1
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for art in call("GET", f"/api/sessions/{sid}/artifacts")["artifacts"]:
        if art["name"] in before and art["name"] != (a.name or art["name"]):
            continue
        target = out / art["name"]
        with call("GET", art["download"], raw=True, timeout=1800) as resp, open(target, "wb") as fh:
            while chunk := resp.read(1 << 20):
                fh.write(chunk)
        print(str(target))
    return 0


def cmd_close(a: argparse.Namespace) -> int:
    sid = session_id(a)
    path = f"/api/sessions/{sid}/close" + (f"?retain_minutes={a.retain}" if a.retain else "")
    st = call("POST", path, b"")
    print(f"{sid}: {st['state']}  (data kept until {st.get('data_kept_until')})", file=sys.stderr)
    if SESSION_FILE.exists() and SESSION_FILE.read_text().strip() == sid:
        SESSION_FILE.unlink()
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="one-shot build")
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

    o = sub.add_parser("open", help="open an interactive session")
    o.add_argument("--dir", default=".")
    o.add_argument("--language", default="generic")
    o.add_argument("--working-directory", dest="working_directory")
    o.add_argument("--label")
    o.add_argument("--ttl", type=int, default=60, help="max session minutes (default 60)")
    o.add_argument("--idle", type=int, default=30, help="auto-close after N idle minutes")
    o.add_argument("--retain", type=int, default=30, help="keep logs/artifacts N minutes after close")
    o.add_argument("--include-env", action="store_true", help="include .env files and keystores in the upload")
    e = sub.add_parser("exec", help="run a command in the session (streams output)")
    e.add_argument("command")
    e.add_argument("--cwd")
    e.add_argument("--timeout", type=int, default=1800)
    e.add_argument("--session")
    u = sub.add_parser("put", help="push updated files into the session")
    u.add_argument("--dir", default=".")
    u.add_argument("--dest")
    u.add_argument("--mode", choices=["overlay", "replace"], default="overlay")
    u.add_argument("--include-env", action="store_true")
    u.add_argument("--session")
    pl = sub.add_parser("pull", help="collect artifacts (globs) and download them")
    pl.add_argument("paths", help="newline-separated globs relative to the project")
    pl.add_argument("--out", default="./build-output")
    pl.add_argument("--name")
    pl.add_argument("--session")
    c = sub.add_parser("close", help="end the session")
    c.add_argument("--retain", type=int)
    c.add_argument("--session")
    sub.add_parser("sstatus", help="session status").add_argument("--session")
    a = p.parse_args()

    if a.cmd == "build":
        sys.exit(cmd_build(a))
    handlers = {"open": cmd_open, "exec": cmd_exec, "put": cmd_put, "pull": cmd_pull, "close": cmd_close}
    if a.cmd in handlers:
        sys.exit(handlers[a.cmd](a))
    if a.cmd == "sstatus":
        print(json.dumps(call("GET", f"/api/sessions/{session_id(a)}"), indent=2))
        return
    path = {"status": "GET /api/builds/{}", "logs": "GET /api/builds/{}/logs?tail=200",
            "cancel": "POST /api/builds/{}/cancel"}[a.cmd]
    method, route = path.split(" ", 1)
    print(json.dumps(call(method, route.format(urllib.parse.quote(a.job_id)), b"" if method == "POST" else None), indent=2))


if __name__ == "__main__":
    main()
