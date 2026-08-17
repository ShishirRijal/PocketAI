"""App settings, loaded from env / .env via pydantic-settings."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="POCKET_",
        extra="ignore",
    )

    env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    log_json: bool = False

    # storage
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'pocket.db'}"
    redis_url: str | None = None

    # public URL the app is reachable at (used for signature checks + export links)
    public_url: str = "http://localhost:8080"
    admin_token: str | None = None

    # defaults for newly created users
    default_base_currency: str = "EUR"
    default_timezone: str = "Europe/Tallinn"

    # bootstrap allowlist: these identities get mapped to the owner user on startup
    owner_whatsapp: str | None = None  # "whatsapp:+3725xxxxxxx"
    owner_telegram: str | None = None  # telegram chat id
    owner_discord: str | None = None  # discord user id
    owner_cli: str = "cli:local"

    # twilio / whatsapp
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_whatsapp_from: str | None = None  # "whatsapp:+14155238886"

    # telegram
    telegram_bot_token: str | None = None
    telegram_webhook_secret: str | None = None

    # discord
    discord_public_key: str | None = None
    discord_bot_token: str | None = None
    discord_application_id: str | None = None

    verify_signatures: bool = True

    # llm
    llm_config_path: Path = PROJECT_ROOT / "config" / "llm.yaml"
    llm_daily_cost_cap_usd: float = 1.0
    llm_timeout_s: float = 10.0

    # queue: "inline" = asyncio task in the api process, "redis" = redis streams + worker
    queue_backend: Literal["inline", "redis"] = "inline"

    # behaviour knobs
    undo_window_minutes: int = 5
    pending_ttl_minutes: int = 15
    session_ttl_minutes: int = 15
    duplicate_window_minutes: int = 5
    rate_limit_per_min: int = 30
    query_rate_limit_per_min: int = 5
    confidence_commit: float = 0.85
    confidence_ask: float = 0.6

    # fx
    fx_cache_hours: int = 12

    # jobs
    scheduler_enabled: bool = True
    digest_weekday: int = 6  # sunday
    digest_hour: int = 20
    backup_dir: Path = PROJECT_ROOT / "data" / "backups"
    backup_keep: int = 14
    backup_azure_sas_url: str | None = Field(
        default=None, description="container SAS url, backups are PUT as block blobs"
    )
    export_dir: Path = PROJECT_ROOT / "data" / "exports"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    # provider keys (OPENAI_API_KEY, GEMINI_API_KEY, ...) are read by litellm straight
    # from os.environ, so push .env in there too, not just into Settings
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    return Settings()
