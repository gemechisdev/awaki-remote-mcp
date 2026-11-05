# Remote Build MCP

Give your AI agent a big build machine for free. Your agent (on a tiny Koyeb/Render box) uploads
its project, this service runs the build on **GitHub Actions** (4 vCPU / 16 GB), and the finished
file (APK, binary, wheel, ZIP) **comes back to your server** that called it.

```
 AI agent server ──ZIP──▶  this service  ──dispatch──▶  GitHub Actions (public repo)
        ▲                      ▲   │                         │
        │                      │   └─ spec + source ◀────────┤  (runner pulls them)
        └── download artifact ─┴──── artifact + log ◀────────┘  (runner pushes them)
```

## Why a public repo is safe here

A public repo gives unlimited free minutes on standard runners, but its workflow logs are public.
So this design makes sure there is nothing to leak:

| What | Where it lives | In the public repo / log? |
|---|---|---|
| Your source code | Only on this service, fetched once by the runner | **Never** (the repo holds just a generic workflow + `runner.py`) |
| Build command, env vars, artifact globs | Job spec on this service, fetched by the runner | **No** (dispatch inputs are only `job_id` + language) |
| Build output | Private file, uploaded to this service when the build ends | **No** (the `build` step prints two status lines) |
| Artifact | Uploaded from the runner to this service | **No** (no GitHub artifacts, no releases) |
| Secrets | GitHub encrypted repo secrets (service URL + runner token) | Masked; **not available** to the build step |

Only people with write access can start workflow runs, so strangers cannot trigger builds.
Job IDs are random; knowing one gives no access to anything without the tokens.

## Setup (about 10 minutes)

### 1. Create the runner repo
Create a new **public** GitHub repository (any name, e.g. `my-build-runner`) with a README so the
`main` branch exists. Turn off Issues/Wiki. In *Settings → Actions → General* set
"Fork pull request workflows" to require approval.

### 2. Create a fine-grained token (limited to that one repo)
GitHub → Settings → Developer settings → Fine-grained tokens → *Generate*:
- Repository access: **Only select repositories** → your runner repo
- Permissions: **Actions: Read and write**, **Contents: Read and write**, **Secrets: Read and write**, Metadata: Read
- No `delete_repo`, no access to your other repos.

### 3. Deploy this service
Render (Docker) or Koyeb. Set the environment variables (see `.env.example`):

| Variable | Value |
|---|---|
| `MCP_API_TOKEN` | `openssl rand -hex 32` |
| `GITHUB_TOKEN` | the token from step 2 |
| `GITHUB_REPO` | `you/my-build-runner` |
| `PUBLIC_URL` | your service URL (auto-detected on Render/Koyeb) |

On startup the service pushes `build.yml` and `runner.py` to the repo, and keeps them in sync after
every deploy (`SYNC_RUNNER_REPO=true`).

### 4. Push the secrets once and verify
Run locally (or in the host's shell) with the same env vars:

```bash
pip install -r requirements.txt
python -m app.cli bootstrap   # pushes workflow files + sets RB_SERVICE_URL / RB_RUNNER_TOKEN secrets, then runs doctor
python -m app.cli doctor      # re-check any time
```
`doctor` tells you in plain words what is missing (private repo, stale workflow, missing secrets...).

### 5. Connect your agent
- **MCP (Streamable HTTP):** URL `https://YOUR-SERVICE/mcp`, header `Authorization: Bearer <MCP_API_TOKEN>`.
- **Shell/REST:** `client/rb.py` does zip → upload → build → wait → download in one command.

```bash
export RB_URL=https://YOUR-SERVICE RB_TOKEN=<MCP_API_TOKEN>
python client/rb.py build --dir ./my-android-app --language android --out ./out
```

## MCP tools

| Tool | Purpose |
|---|---|
| `submit_build` | Start a build; returns `job_id` immediately |
| `wait_for_build` | Long-poll (≤50 s). Returns `done=true` with final state |
| `get_build_status` | State + artifact info |
| `get_build_logs` | `error_excerpt` (key failure lines, incl. Gradle "What went wrong") + log tail |
| `get_artifact_info` | Download URL, size, expiry |
| `cancel_build`, `list_builds`, `delete_build`, `get_build_presets` | housekeeping |

The agent uploads source with `POST /upload` (raw ZIP body or multipart `file`), then passes `upload_id`.
Alternatively `git_url` clones a **public** https repo on the runner.
Download the result with `GET /api/builds/<job_id>/artifact` (same bearer token).

States: `queued → running → succeeded | build_failed | delivery_failed | timed_out | failed | cancelled`.
A single matching file is returned as-is (e.g. `app-debug.apk`); several are zipped.

### Languages (presets fill in the command and artifact globs)
`android`, `kotlin`, `java`, `rust`, `go`, `python`, `node`, `cpp`, `flutter`, `generic`.
See `get_build_presets`. Override anything with `build_command`, `artifact_paths`, `working_directory`
(monorepos), `toolchain` (`java_version`, `node_version`, `python_version`, `go_version`), `env`
(masked; use it for signing passwords) and `timeout_minutes`.
Gradle, Cargo, Go, pip and npm caches persist between builds.

## Reliability notes

- **Free-tier sleep:** Render's free plan sleeps after ~15 min idle and has an ephemeral disk. The runner retries
  while the service wakes. If the service restarted mid-build, the final report still arrives (it recreates the
  job record) and status falls back to the GitHub run. Only a log/artifact from a *previous* instance can be lost.
  Download artifacts promptly (default retention 3 h). An always-on host (or the same box as your agent) avoids all of this.
- **Artifact size:** capped by `MAX_ARTIFACT_MB` (default 200) and your host's disk/RAM.
- **Concurrency:** `MAX_CONCURRENT_BUILDS` (default 3) protects you from runaway agents.
- **Limits of GitHub:** public repos get free standard runners subject to GitHub's terms; a job runs up to 6 h
  (this service caps builds at `MAX_BUILD_MINUTES`, default 60). Heavy abuse can get an account flagged, so keep it
  to your own projects.
- **Trust model:** the agent supplies the build command, which runs on a disposable GitHub VM with no
  access to your tokens. Don't paste real secrets into `build_command`; use `env`.

## Security summary

Constant-time bearer checks, separate runner token (derived one-way from `MCP_API_TOKEN`), failed-auth throttling,
ZIP validation (zip-slip, size and zip-bomb limits) at upload and again on the runner, strict input validation for
every build parameter, secrets scoped to only the two steps that need them, minimal-scope GitHub token.

## Layout

```
app/        service (MCP + REST + runner endpoints), GitHub client, bootstrap/doctor CLI
runner/     runner.py  - executed on GitHub (fetch / build / deliver), stdlib only
workflow/   build.yml  - the generic workflow pushed to the runner repo
client/     rb.py      - agent-side helper (zip, upload, wait, download)
```

Run locally: `uvicorn --factory app.main:create_app --port 8000` (note `--factory`).
