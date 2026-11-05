# Remote Build MCP

A remote build service for an AI agent running on a small Koyeb/Render instance.

The agent continues editing code on its existing server. When compilation is needed, it uploads a ZIP of the workspace to this service, then calls the MCP `submit_build` tool. This service creates a **temporary private GitHub repository per build**, pushes the source and a generated GitHub Actions workflow, dispatches the workflow, and uses a GitHub-hosted Ubuntu runner to compile it. After successful artifact delivery to Telegram, the temporary repository is deleted.

## Important free-tier facts

- GitHub Free currently includes **2,000 Actions minutes/month** for private repositories and 500 MB of combined artifact/package storage. Standard hosted runners in public repositories are free, but this project intentionally uses private ephemeral repositories to avoid publishing source. Check current billing limits before relying on this for high-volume use.
- The monthly private-repository allowance is shared by all private Actions workflows owned by the account. Once exhausted, private workflows may stop running unless paid usage is enabled. Set a **$0 Actions spending budget** in GitHub Billing to prevent accidental overage; this may cause runs to be blocked when the free quota is exhausted.
- GitHub-hosted `ubuntu-latest` standard runners are substantially more capable than a 0.10 CPU web-service instance, but exact hardware can change. This is not an unlimited free compute service.
- Telegram's standard Bot API `sendDocument` upload limit is 50 MB. The service leaves the GitHub repository intact if the compressed artifact is too large or delivery fails. Use the authenticated `/download/{job_id}` endpoint while the repository still exists.
- This project does not use Colab. Free Colab is not designed to be an unattended remote build worker.

## Architecture

```text
AI Agent (Koyeb / Render)
   |
   | 1. ZIP current workspace
   | 2. POST /upload (Bearer MCP_API_TOKEN)
   | 3. MCP submit_build(upload_id, language, command, artifact_paths)
   v
Remote Build MCP (small API service)
   |
   | create private repo + git push source + workflow
   | dispatch workflow via GitHub REST API
   v
GitHub Actions (Ubuntu runner)
   |
   | compile / test / package
   | upload build-output artifact (1-day retention)
   v
Remote Build MCP
   |
   | download artifact ZIP
   | sendDocument to Telegram channel
   | delete temporary private repository
   v
Telegram Channel
```

## 1. Create GitHub credentials (no credit card required)

1. Create/sign in to a GitHub account.
2. Create a classic Personal Access Token at `https://github.com/settings/tokens`.
3. Give it the `repo` and `delete_repo` scopes. These are broad permissions because the service creates and deletes private repositories. Keep this token only in the service environment; never send it to the AI model or a build runner.
4. In GitHub Billing, configure a **$0 Actions spending budget** if available for your account. Private repositories on GitHub Free include a monthly Actions allowance; this project cannot guarantee unlimited builds.

The service calls `GET /user`, `POST /user/repos`, `POST /actions/workflows/.../dispatches`, reads run logs/artifacts, and deletes each temporary repo after Telegram delivery.

## 2. Configure Telegram

1. Create a bot with `@BotFather`.
2. Add the bot to your Telegram channel as an administrator with permission to post messages.
3. Set `TELEGRAM_BOT_TOKEN` to the bot token.
4. Set `TELEGRAM_CHAT_ID` to the channel username (for example `@my_builds`) or numeric channel ID.

The artifact is uploaded as a ZIP document. Extract it after downloading from Telegram.

## 3. Deploy the MCP service

### Render

1. Put this project in a Git repository you control (this is the MCP service source, not your application source).
2. Create a Render Web Service from it, or use the included `render.yaml`.
3. Add environment variables:
   - `GITHUB_TOKEN`
   - `MCP_API_TOKEN` (generate a long random value)
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. Build command: `pip install -r requirements.txt`
5. Start command: `uvicorn app.service:app --host 0.0.0.0 --port $PORT`
6. Set health check path to `/health`.

### Koyeb

1. Deploy the Dockerfile as a web service.
2. Expose the port from `$PORT` (default 8000).
3. Add the same environment variables.
4. Use `/health` as the health check.

The service needs only modest resources. It handles ZIP transfer and GitHub API calls; compilation happens on GitHub Actions.

**Free-host caveat:** Render/Koyeb free instances may sleep, restart, or have ephemeral disks. Upload a ZIP and submit its build promptly. If the MCP service restarts between upload and submit, upload again. For more reliability, keep the service awake if the provider permits it or use the agent's existing server as the upload endpoint. The actual build continues on GitHub even if the MCP service sleeps after dispatch.

## 4. Connect your agent

Remote MCP endpoint:

```text
https://YOUR-SERVICE-DOMAIN/mcp
```

Send this header on MCP requests:

```http
Authorization: Bearer YOUR_MCP_API_TOKEN
```

For a generic MCP client, configure a Streamable HTTP server with that URL and header. Example Claude Code configuration:

```bash
claude mcp add --transport http remote-build \
  https://YOUR-SERVICE-DOMAIN/mcp \
  --header "Authorization: Bearer YOUR_MCP_API_TOKEN"
```

If your agent framework supports remote MCP Streamable HTTP, configure the same endpoint and bearer header there.

## 5. Upload a workspace and build it

The MCP server exposes a small authenticated REST upload endpoint because large source ZIPs should not be embedded inside an MCP JSON tool call.

### Create a source ZIP on the agent server

Exclude `.git`, caches, virtual environments, build outputs and secrets. Example:

```bash
cd /path/to/project
zip -r /tmp/project-source.zip . \
  -x '.git/*' '*/.git/*' '.venv/*' '*/.venv/*' \
     'node_modules/*' '*/node_modules/*' \
     'build/*' '*/build/*' '.env' '*/.env'
```

Do not include credentials, signing keys, keystores, `.env` files, or private certificates in the source ZIP. If a build needs a secret, prefer a secure signing workflow; this starter intentionally does not pass secrets to the runner.

### Upload the ZIP

```bash
curl -X POST "https://YOUR-SERVICE-DOMAIN/upload" \
  -H "Authorization: Bearer YOUR_MCP_API_TOKEN" \
  -F "file=@/tmp/project-source.zip"
```

Response:

```json
{
  "upload_id": "0123456789abcdef",
  "size_bytes": 123456,
  "expires_after_minutes": 60
}
```

### Call MCP tool `submit_build`

Android example:

```json
{
  "upload_id": "0123456789abcdef",
  "language": "android",
  "build_command": "chmod +x ./gradlew && ./gradlew --no-daemon assembleDebug",
  "artifact_paths": "app/build/outputs/apk/debug/*.apk",
  "timeout_minutes": 30
}
```

Rust example:

```json
{
  "upload_id": "0123456789abcdef",
  "language": "rust",
  "build_command": "cargo build --release",
  "artifact_paths": "target/release/my-binary",
  "timeout_minutes": 30
}
```

Go example:

```json
{
  "upload_id": "0123456789abcdef",
  "language": "go",
  "build_command": "go build -o dist/my-app ./...",
  "artifact_paths": "dist/my-app",
  "timeout_minutes": 20
}
```

Python wheel example:

```json
{
  "upload_id": "0123456789abcdef",
  "language": "python",
  "build_command": "python -m pip install --upgrade build && python -m build",
  "artifact_paths": "dist/*",
  "timeout_minutes": 20
}
```

The tool returns a `job_id` immediately. Poll `get_build_status(job_id)` until `status` is `completed`. If `conclusion` is `failure`, call `get_build_logs(job_id)` and feed the useful error lines back to your coding agent.

On success, call `deliver_build_artifact(job_id)`. It sends the artifact ZIP to Telegram and deletes the temporary GitHub repository after successful delivery.

## 6. MCP tools

| Tool | Purpose |
|---|---|
| `submit_build` | Start a build from an uploaded source ZIP |
| `get_build_status` | Get queued/in-progress/completed status |
| `get_build_logs` | Retrieve recent logs for AI debugging |
| `deliver_build_artifact` | Send build-output ZIP to Telegram; delete repo after success |
| `delete_build` | Manually delete a temporary repo |

REST endpoints:

- `GET /health` — health check (no auth)
- `POST /upload` — multipart ZIP upload (Bearer auth)
- `GET /download/{job_id}` — stream artifact ZIP (Bearer auth; use if Telegram limit is exceeded)

## 7. Build environment matrix

| language | Toolchain |
|---|---|
| `android`, `kotlin` | Temurin JDK 17; Gradle wrapper should be in the project |
| `rust` | Stable Rust toolchain |
| `go` | Stable Go |
| `python` | Python 3.12 |
| `node` | Node.js 22 |
| `cpp` | build-essential, CMake, Ninja |
| `flutter` | Flutter stable + Java 17 |
| `generic` | Ubuntu runner's preinstalled tools |

Use `generic` for other build stacks, or extend `.github/workflows/build.yml` with another setup step.

## 8. Security and operational notes

- This tool executes build commands supplied by your AI agent. Treat the agent and source as trusted. Do not expose this MCP server publicly without a strong `MCP_API_TOKEN`.
- The build runner receives no GitHub PAT or Telegram token. The generated workflow grants read-only repository contents/actions permissions.
- Source code is pushed to a private repository and is deleted after successful Telegram delivery. Failed builds remain available for log inspection; call `delete_build` after debugging.
- GitHub may retain workflow logs/artifacts briefly according to platform retention and deletion behavior. The workflow sets artifact retention to one day.
- The service's local upload staging uses temporary disk. Configure `MAX_UPLOAD_MB` and `MAX_EXTRACT_MB` to fit the host's disk limits.
- Keep the service's GitHub token in environment secrets. Never place it in a build command, source archive, log, or model prompt.
- If your Android project requires signing, avoid committing keystores/passwords into the temporary source. Add a dedicated encrypted-secret signing design before producing release APKs.
- The build command is executed on a Linux Ubuntu runner. Windows-only build steps will not work as-is.
- GitHub Actions is quota-limited for private repositories. It is the best no-card starting point, not an unlimited free CI provider.

## 9. Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Set secrets in .env, then:
set -a; source .env; set +a
uvicorn app.service:app --reload --port 8000
```

Check `http://localhost:8000/health`. Connect MCP Inspector to `http://localhost:8000/mcp` with the bearer header.

## License

MIT
