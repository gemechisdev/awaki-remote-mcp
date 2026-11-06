# Remote Build MCP

Two modes on one free setup: **one-shot builds** and **temporary remote-shell sessions**.

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

### 3. Deploy this service (that's the whole setup: no shell needed)
Render (Docker) or Koyeb free tier. Set these environment variables in the host's dashboard:

| Variable | Value |
|---|---|
| `MCP_API_TOKEN` | a random string, 32+ characters (Render can generate one: see `render.yaml`) |
| `GITHUB_TOKEN` | the token from step 2 |
| `GITHUB_REPO` | `you/my-build-runner` |
| `PUBLIC_URL` | only if auto-detection fails (Render and Koyeb URLs are detected automatically) |

**On every start the service sets itself up automatically**, in the background, with retries:
1. pushes `build.yml` and `runner.py` to the runner repo (only when they changed),
2. sets the two repository secrets the runner needs (`RB_SERVICE_URL`, `RB_RUNNER_TOKEN`, encrypted),
3. verifies everything (public repo, files, secrets) and re-checks every 6 hours.

If the token is missing a permission or the repo has no `main` branch, setup reports the exact problem and keeps
retrying every few minutes, so fixing the token in GitHub is enough (no redeploy). Until setup has succeeded, builds
and sessions are refused with that message instead of failing mysteriously.

### 4. Check that it worked (optional, no shell)
- Open `https://YOUR-SERVICE/health`: `"setup":"ok"` means the runner repo is ready (`pending` for the first seconds).
- Details, from any computer or phone: `curl -H "Authorization: Bearer <MCP_API_TOKEN>" https://YOUR-SERVICE/api/doctor`
  (or have your agent call the `service_doctor` MCP tool). `POST /api/bootstrap` (tool: `service_setup`) re-runs setup on demand.
- The `python -m app.cli bootstrap|doctor` command still exists for people who do have a shell, but is never required.

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

(One-shot build tools. Session tools are described in the next section.)

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


## Session mode: a temporary remote shell (run tests, npm, gradlew, go build, ...)

For "run these commands on a better machine for a while": open a session, run as many commands as you like with
**live output**, push updated code, pull outputs, close. Your own server stays light; the work happens on a
GitHub VM (4 vCPU / 16 GB, Ubuntu, sudo).

```
agent ──ZIP──▶ service ──dispatch──▶ GitHub job (the "session")
agent ──exec "gradlew assembleDebug"──▶ service ◀──long-poll for commands── runner (executes, streams output)
agent ◀──read_output (live, ~1 s chunks)── service ◀──────────────────────── runner
agent ◀──download URL── service ◀──collected artifacts / logs ───────────── runner
```

The runner connects **out** to your service (GitHub runners accept no inbound traffic) and polls for work, so no tunnels
or SSH are involved. While a session is open the traffic also keeps a free-tier host awake.

### From a shell (Claude Code, Cursor, your own scripts) - `client/rb.py`
```bash
export RB_URL=https://YOUR-SERVICE RB_TOKEN=<MCP_API_TOKEN>
python client/rb.py open --dir ./my-app --language android --include-env   # zips + uploads, starts the VM (~1 min)
python client/rb.py exec "./gradlew assembleDebug"                          # output streams live; exit code is passed through
python client/rb.py exec "npm ci && npm test"
python client/rb.py put --dir ./my-app --mode replace                       # push edited code, keep node_modules/.gradle caches
python client/rb.py pull "app/build/outputs/apk/**/*.apk" --out ./out       # collect + download
python client/rb.py close
```
`--include-env` also uploads `.env` files and keystores (excluded by default). The session id is stored in `./.rb-session`.

### From an MCP client
`open_session` -> `exec_command` (returns output; if still running, follow it with `read_output(offset=next_offset)`,
like `tail -f`) -> `put_files` / `collect_artifacts` -> `close_session`. Also `wait_for_session`, `session_status`,
`cancel_command`, `get_session_log`, `list_session_artifacts`, `list_sessions`, `delete_session`.
Collected files are downloaded with `GET /api/sessions/<id>/artifacts/<name>` (bearer token). The agent uploads the
ZIP with `POST /upload` first (same as builds) or uses `git_url` for a public repo.

### How it behaves
- Each command starts in the project root (or `cwd`) in a fresh `bash`: `cd` and `export` do **not** persist, so chain with
  `&&` or pass `cwd`/`env`. Installed packages, build outputs and caches **do** persist for the whole session.
- Commands can run concurrently (e.g. a dev server in one, tests in another); for servers redirect output to a file and
  use a large `timeout_seconds`. Cancel any command with `cancel_command`.
- Not a TTY: interactive programs (vim, REPL prompts) are not supported.
- **Everything is temporary.** Hard lifetime `ttl_minutes` (default 60, max 180), auto-close after `idle_minutes` (default 30)
  without commands, then logs and collected artifacts stay for `retain_minutes` (default 30) and are deleted
  automatically; the GitHub job is cancelled and the VM disappears. `delete_session` wipes it all immediately.
- Updating code: `put_files` with `mode=overlay` (add/overwrite) or `replace` (wipe first, keeping
  `node_modules, .gradle, build, target, .cache, .venv, dist` unless you pass `keep`).
- Limits (env vars): `MAX_SESSION_MINUTES`, `MAX_CONCURRENT_SESSIONS` (2), `MAX_CMD_LOG_MB` (20 per command),
  `MAX_SESSION_ARTIFACT_MB` (500), `MAX_ARTIFACT_MB` (200 per file).

### Session security
- Your code and command output never go into the public repo or its log; commands arrive from your service.
- The global runner token exists only in the first (fetch) step. During the session the runner holds a **per-session
  token** that works for that one session only and dies with it. Commands you run *can* read that token (and have
  `sudo`), which is why it is scoped so narrowly. Do not put real production secrets in sessions you don't trust.
- `.env` and other env values are sent only if you include them.

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
app/        service (MCP + REST + runner endpoints), builds + sessions, GitHub client, bootstrap/doctor CLI
runner/     runner.py  - executed on GitHub (fetch / build / deliver / session), stdlib only
workflow/   build.yml  - the generic workflow pushed to the runner repo
client/     rb.py      - agent-side helper (builds and live sessions)
```

Run locally: `uvicorn --factory app.main:create_app --port 8000` (note `--factory`).
