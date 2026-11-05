import os
from dataclasses import dataclass

@dataclass(frozen=True)
class Settings:
    github_token: str = os.getenv("GITHUB_TOKEN", "")
    github_api_url: str = os.getenv("GITHUB_API_URL", "https://api.github.com")
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    mcp_api_token: str = os.getenv("MCP_API_TOKEN", "")
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "100"))
    max_extract_mb: int = int(os.getenv("MAX_EXTRACT_MB", "500"))
    max_build_minutes: int = int(os.getenv("MAX_BUILD_MINUTES", "45"))
    github_repo_prefix: str = os.getenv("GITHUB_REPO_PREFIX", "agent-build")
    temp_dir: str = os.getenv("TEMP_DIR", "/tmp/remote-build-mcp")

settings = Settings()
