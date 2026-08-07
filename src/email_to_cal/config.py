"""Runtime configuration: the portal's config.json, overridable by environment variables."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    JsonConfigSettingsSource,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# Written by the web portal; relative so it lands in the mounted volume in the container
# and in ./data locally, like every other runtime file.
CONFIG_FILE = Path("data/config.json")


class CategoryRule(BaseModel):
    """One (category, description, calendar) triple from the operator's config."""

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    calendar: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _normalise_name(cls, value: str) -> str:
        return value.strip().lower()


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

    imap_host: str = "imap.mail.me.com"
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    # iCloud drops long-lived idle sockets well before RFC 2177's 29-minute ceiling, and
    # imap-tools refuses anything past it outright. Zero would spin the loop at full CPU.
    imap_idle_seconds: int = Field(default=300, ge=10, le=1740)
    first_run_lookback_days: int = Field(default=0, ge=0)
    # Folders to catch up on periodically, for mail filed away before IDLE saw it. New
    # mail always lands in imap_folder first, so these need a sweep, not a second watcher.
    # NoDecode preserves the comma-separated form an environment override uses instead of
    # asking pydantic-settings to JSON-decode it before the validator below sees it.
    sweep_folders: Annotated[list[str], NoDecode] = []
    sweep_interval_minutes: int = Field(default=15, ge=1)

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    enable_vision: bool = True
    # A negative value would make every attachment look oversized and silently vanish.
    max_attachment_mb: float = Field(default=8.0, gt=0)
    min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)

    # The OAuth client from the Google Cloud console (Desktop app type): two strings,
    # no downloaded credentials file.
    google_client_id: str = ""
    google_client_secret: str = ""
    # Relative to the working directory, so one config works both locally and in the
    # container, where WORKDIR is /app and the volume mounts at /app/data.
    google_token_file: Path = Path("data/token.json")
    # Its own calendar by default, created on first run; "primary" writes to the
    # user's main calendar instead.
    default_calendar: str = "email-to-cal events"
    default_timezone: str = "UTC"

    state_db: Path = Path("data/state.sqlite")
    # Safe by default: a fresh install logs what it would create until switched off.
    dry_run: bool = True
    log_level: str = "INFO"

    categories: list[CategoryRule] = []

    @field_validator("categories", mode="before")
    @classmethod
    def _parse_categories(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            return json.loads(stripped) if stripped else []
        return value

    @field_validator("sweep_folders", mode="before")
    @classmethod
    def _parse_sweep_folders(cls, value: Any) -> Any:
        # Comma-separated, because iCloud folder names contain spaces ("Deleted Messages")
        # far more often than commas.
        if isinstance(value, str):
            return [name.strip() for name in value.split(",") if name.strip()]
        return value

    @field_validator("default_timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"DEFAULT_TIMEZONE {value!r} is not a known IANA zone") from exc
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        seen: set[str] = set()
        for rule in self.categories:
            if rule.name in seen:
                raise ValueError(f"duplicate category name {rule.name!r}")
            seen.add(rule.name)

        if self.imap_folder in self.sweep_folders:
            raise ValueError(
                f"swept folders must not repeat the watched folder ({self.imap_folder!r}); "
                "it is already watched continuously"
            )
        return self

    @property
    def max_attachment_bytes(self) -> int:
        return int(self.max_attachment_mb * 1024 * 1024)

    def calendar_for(self, category: str | None) -> str:
        """Map an extracted category name onto a calendar name, defaulting when unmatched."""
        if category:
            wanted = category.strip().lower()
            for rule in self.categories:
                if rule.name == wanted:
                    return rule.calendar
        return self.default_calendar
