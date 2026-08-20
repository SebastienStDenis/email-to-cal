"""Runtime configuration: the portal's config.json, overridable by environment variables."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import (
    BaseSettings,
    JsonConfigSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# Written by the web portal; relative so it lands in the mounted volume in the container
# and in ./data locally, like every other runtime file.
CONFIG_FILE = Path("data/config.json")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(json_file=CONFIG_FILE, extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # config.json is what the portal writes; environment variables override it for
        # one-off debugging (LOG_LEVEL=DEBUG email-to-cal run).
        return (
            init_settings,
            env_settings,
            JsonConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    # One Apple ID and one app-specific password reach both the mail and the calendars.
    apple_id: str = ""
    apple_password: str = ""
    imap_host: str = "imap.mail.me.com"
    imap_port: int = 993
    # Every folder is searched on every pass, so this is the delay between a flag going
    # on and the event appearing. Below a minute iCloud starts refusing connections.
    poll_interval_seconds: int = Field(default=60, ge=60)

    provider: Literal["anthropic", "ollama"] = "anthropic"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "gpt-oss:20b"
    # Long emails would overflow Ollama's small default context, which silently
    # truncates the top of the prompt - the extraction instructions - first.
    ollama_num_ctx: int = Field(default=16384, ge=2048)
    # CPU inference is slow; a short read timeout would abandon work about to finish.
    ollama_timeout_seconds: float = Field(default=600.0, gt=0)
    # Keep the model resident between emails so a burst of mail loads it once.
    ollama_keep_alive: str = "30m"

    enable_vision: bool = True
    # A negative value would make every attachment look oversized and silently vanish.
    max_attachment_mb: float = Field(default=8.0, gt=0)

    caldav_url: str = "https://caldav.icloud.com"
    calendar_name: str = ""
    default_timezone: str = "UTC"

    # Both outcomes are pushed to the phone, so this is the only place a failure is
    # reported. A no-op until both keys are configured.
    pushover_user: str = ""
    pushover_token: str = ""

    state_db: Path = Path("data/state.sqlite")
    log_level: str = "INFO"

    @field_validator("default_timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"{value!r} is not a recognised time zone; use a name like Europe/Zurich"
            ) from exc
        return value

    @property
    def max_attachment_bytes(self) -> int:
        return int(self.max_attachment_mb * 1024 * 1024)
