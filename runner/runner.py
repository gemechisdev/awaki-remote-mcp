#!/usr/bin/env python3
"""Step scripts executed on the GitHub-hosted runner:  runner.py {fetch|build|deliver}

Design rules (this repository is PUBLIC, so the workflow log is public):
  * Source code, build command, build output and artifacts never appear in the workflow log.
    The job spec and source are pulled from your service; build output goes to a private file
    that is uploaded to your service when the build ends.
  * Secrets are only present in the `fetch` and `deliver` steps - never in the `build` step.
  * Standard library only; networking via curl (retries survive a free-tier service waking up).
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import zipfile
from pathlib import Path, PurePosixPath

WS = Path(os.environ.get("GITHUB_WORKSPACE", ".")).resolve()
STATE = WS / "_rb_state"
SRC = WS / "src"
URL = os.environ.get("RB_SERVICE_URL", "").rstrip("/")
TOKEN = os.environ.get("RB_RUNNER_TOKEN", "")
JOB = os.environ.get("RB_JOB_ID", "")

HEAD_LOG_BYTES = 16 * 1024 * 1024
TAIL_LOG_LINES = 20000


def say(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str) -> "None":
    print(f"::error::{msg}", flush=True)
    sys.exit(1)


# --------------------------------------------------------------------------- curl
def curl(endpoint: str, *args: str, max_time: int = 900, query: str = "") -> subprocess.CompletedProcess:
    """Call the service. Token/URL travel via stdin config so they never show in `ps`."""
    config = f'url = "{URL}/runner/{JOB}/{endpoint}{query}"\nheader = "Authorization: Bearer {TOKEN}"\n'
    cmd = ["curl", "-sS", "-f", "-L", "--connect-timeout", "30", "--max-time", str(max_time),
           "--retry", "12", "--retry-delay", "10", "--retry-connrefused", "--retry-max-time", "420",
           "-K", "-", *args]
    return subprocess.run(cmd, input=config, text=True, capture_output=True)


def load(name: str) -> dict:
    return json.loads((STATE / name).read_text())


def set_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{key}={value}\n")


# ---------------------------------------------------------------------------- fetch
def safe_extract(zip_path: Path, dest: Path, max_mb: int) -> None:
    limit, total = max_mb * 1024 * 1024, 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            p = PurePosixPath(info.filename.replace("\\", "/"))
            if p.is_absolute() or ".." in p.parts:
                die("Unsafe path in source archive")
            if ".git" in p.parts:
                continue
            mode = info.external_attr >> 16
            if (mode & 0o170000) == 0o120000:  # skip symlinks
                continue
            total += info.file_size
            if total > limit:
                die(f"Source expands beyond {max_mb} MB")
            target = dest.joinpath(*p.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            if mode & 0o111:  # preserve executable bit (gradlew, scripts)
                target.chmod(0o755)


def cmd_fetch() -> None:
    if not (URL and TOKEN and JOB):
        die("Missing RB_SERVICE_URL / RB_RUNNER_TOKEN / job id (run `python -m app.cli bootstrap`)")
    STATE.mkdir(parents=True, exist_ok=True)
    spec_path = STATE / "spec.json"
    res = curl("spec", "-o", str(spec_path))
    if res.returncode != 0:
        die(f"Could not fetch the job spec from the service: {res.stderr.strip()[-300:]}")
    spec = json.loads(spec_path.read_text())

    for value in (spec.get("env") or {}).values():  # mask before anything can print them
        for line in str(value).splitlines():
            if len(line.strip()) >= 3:
                print(f"::add-mask::{line.strip()}")

    shutil.rmtree(SRC, ignore_errors=True)
    staging = WS / "_rb_staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    source = spec["source"]
    if source["type"] == "upload":
        zpath = STATE / "source.zip"
        res = curl("source", "-o", str(zpath))
        if res.returncode != 0:
            die(f"Could not download the source: {res.stderr.strip()[-300:]}")
        safe_extract(zpath, staging, int(spec.get("max_extract_mb", 1000)))
        zpath.unlink(missing_ok=True)
    else:
        clone = ["git", "clone", "--depth", "1"]
        if source.get("ref"):
            clone += ["--branch", source["ref"]]
        out = subprocess.run([*clone, "--", source["url"], str(staging / "repo")], capture_output=True, text=True)
        if out.returncode != 0:
            die("git clone failed (is the repository public and the ref valid?)")
        shutil.rmtree(staging / "repo" / ".git", ignore_errors=True)
        for item in (staging / "repo").iterdir():
            shutil.move(str(item), str(staging / item.name))
        shutil.rmtree(staging / "repo")

    entries = [e for e in staging.iterdir()]
    root = entries[0] if len(entries) == 1 and entries[0].is_dir() else staging  # strip single wrapper dir
    shutil.move(str(root), str(SRC))
    shutil.rmtree(staging, ignore_errors=True)

    workdir = (SRC / spec.get("workdir", "")).resolve()
    if SRC.resolve() not in [workdir, *workdir.parents] or not workdir.is_dir():
        die("working_directory does not exist inside the source")

    tc = spec.get("toolchain", {})
    set_output("java_version", tc.get("java_version", "17"))
    set_output("node_version", tc.get("node_version", "22"))
    set_output("python_version", tc.get("python_version", "3.12"))
    set_output("go_version", tc.get("go_version", "stable"))
    set_output("workdir_rel", str(workdir.relative_to(WS)))
    set_output("step_timeout", str(int(spec.get("timeout_minutes", 30)) + 3))
    say("Source ready.")


# ---------------------------------------------------------------------------- build
def cmd_build() -> None:
    spec = load("spec.json")
    workdir = (SRC / spec.get("workdir", "")).resolve()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RB_", "INPUT_"))}
    env.update({"CI": "true", "TERM": "dumb"})
    env.update({k: str(v) for k, v in (spec.get("env") or {}).items()})
    timeout_s = int(spec.get("timeout_minutes", 30)) * 60

    say("Build started.")
    started = time.time()
    proc = subprocess.Popen(["bash", "-eo", "pipefail", "-c", spec["command"]], cwd=workdir, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    log_path = STATE / "build.log"
    tail: collections.deque[bytes] = collections.deque(maxlen=TAIL_LOG_LINES)
    state = {"written": 0, "truncated": False}

    def pump() -> None:
        with open(log_path, "wb") as fh:
            for line in proc.stdout:  # type: ignore[union-attr]
                if state["written"] < HEAD_LOG_BYTES:
                    fh.write(line)
                    state["written"] += len(line)
                else:
                    state["truncated"] = True
                    tail.append(line)
            if state["truncated"]:
                fh.write(b"\n[... middle of the log omitted (too large); last lines follow ...]\n")
                fh.writelines(tail)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    timed_out = False
    try:
        code = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                proc.wait(timeout=15)
                break
            except subprocess.TimeoutExpired:
                continue
        code = 124
    reader.join(timeout=30)
    seconds = round(time.time() - started)
    (STATE / "build.json").write_text(json.dumps({"exit_code": code, "timed_out": timed_out, "seconds": seconds}))
    say(f"Build finished: exit={code} timed_out={timed_out} seconds={seconds}")  # no build output in the public log


# -------------------------------------------------------------------------- deliver
def collect(patterns: list[str], workdir: Path) -> list[Path]:
    import glob

    found: dict[str, Path] = {}
    for pattern in patterns:
        for match in glob.glob(pattern, root_dir=str(workdir), recursive=True):
            path = (workdir / match).resolve()
            if workdir not in [path, *path.parents]:
                continue
            candidates = [p for p in path.rglob("*") if p.is_file()] if path.is_dir() else [path]
            for p in candidates:
                if p.is_file() and not p.is_symlink():
                    found[str(p)] = p
    files = sorted(found.values())
    if len(files) > 5000:
        raise RuntimeError("artifact_paths matched more than 5000 files")
    return files


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def package(files: list[Path], workdir: Path, mode: str, label: str) -> Path:
    if len(files) == 1 and mode == "auto":
        return files[0]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-")[:40] or "build"
    out = STATE / f"{slug}-{JOB}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in files:
            zf.write(f, f.relative_to(workdir).as_posix())
    return out


def cmd_deliver() -> None:
    spec = load("spec.json")
    build_file = STATE / "build.json"
    result: dict = {"label": spec.get("label"), "language": spec.get("language"),
                    "exit_code": None, "timed_out": False, "artifacts": [], "ok": False}
    if not build_file.exists():
        result["reason"] = "A setup step failed before the build command ran (open the run page to see which)."
    else:
        build = json.loads(build_file.read_text())
        result.update(exit_code=build["exit_code"], timed_out=build["timed_out"], build_seconds=build["seconds"])
        if build["exit_code"] == 0 and not build["timed_out"]:
            if not spec.get("return_artifacts", True):
                result.update(ok=True, reason="Build succeeded (no artifact requested).")
            else:
                try:
                    workdir = (SRC / spec.get("workdir", "")).resolve()
                    files = collect(spec["artifacts"], workdir)
                    if not files:
                        raise RuntimeError("artifact_paths matched no files (check the glob; paths are relative to the project/working_directory)")
                    result["artifacts"] = [{"path": f.relative_to(workdir).as_posix(), "size": f.stat().st_size}
                                           for f in files[:200]]
                    pkg = package(files, workdir, spec.get("package", "auto"), spec.get("label") or "build")
                    size = pkg.stat().st_size
                    if size > int(spec.get("max_artifact_mb", 200)) * 1024 * 1024:
                        raise RuntimeError(f"artifact is {size // 1048576} MB, above the service limit of {spec.get('max_artifact_mb')} MB")
                    digest = sha256(pkg)
                    up = curl("artifact", "-X", "PUT", "-T", str(pkg), "-o", str(STATE / "upload.json"),
                              max_time=1800, query="?filename=" + urllib.parse.quote(pkg.name))
                    if up.returncode != 0:
                        raise RuntimeError(f"upload to the service failed: {up.stderr.strip()[-200:]}")
                    reply = json.loads((STATE / "upload.json").read_text())
                    if reply.get("sha256") != digest:
                        raise RuntimeError("artifact checksum mismatch after upload")
                    result.update(artifact={"filename": reply["filename"], "size_bytes": size, "sha256": digest,
                                            "kind": "file" if pkg in files else "zip"},
                                  package="file" if pkg in files else "zip", ok=True,
                                  reason="Build finished; the artifact is ready to download.")
                except Exception as exc:  # noqa: BLE001
                    result["delivery_error"] = str(exc)[:300]
        elif build["timed_out"]:
            result["reason"] = "Build timed out."

    (STATE / "result.json").write_text(json.dumps(result))
    args = ["-X", "POST", "-F", f"result=<{STATE / 'result.json'}"]
    if (STATE / "build.log").exists():
        args += ["-F", f"log=@{STATE / 'build.log'}"]
    post = curl("complete", *args, max_time=300)
    if post.returncode != 0:
        say(f"::warning::Could not report to the service: {post.stderr.strip()[-200:]}")
    # Minimal, non-sensitive summary so the result can be recovered from the run log if the service restarted.
    public = {"ok": result["ok"], "exit_code": result["exit_code"], "timed_out": result["timed_out"],
              "build_seconds": result.get("build_seconds"), "language": result["language"],
              "delivery_error": "artifact step failed" if result.get("delivery_error") else None}
    say("RB_RESULT " + json.dumps(public, separators=(",", ":")))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    commands = {"fetch": cmd_fetch, "build": cmd_build, "deliver": cmd_deliver}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        die("usage: runner.py fetch|build|deliver")
    commands[sys.argv[1]]()
