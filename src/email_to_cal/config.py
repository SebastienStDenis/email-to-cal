"""Runtime configuration: the portal's config.json, overridable by environment variables."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    JsonConfigSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# Written by the web portal; relative so it lands in the mounted volume in the container
# and in ./data locally, like every other runtime file.
CONFIG_FILE = Path("data/config.json")


class CategoryRule(BaseModel):
    """One (category, description, calendar) triple from the operator's config.

    The description is what the model reads, so it says what belongs in the category
    rather than merely naming it.
    """

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

    # One Apple ID and one app-specific password reach both the mail and the calendars.
    apple_id: str = ""
    apple_password: str = ""
    imap_host: str = "imap.mail.me.com"
    imap_port: int = 993
    # Which colour of flag asks for an event. Only this one is processed, so the others
    # keep whatever meaning they already have - red is left alone by default because it
    # is the colour every Mail client reaches for first.
    flag_colour: Literal["red", "orange", "yellow", "green", "blue", "purple", "grey"] = "blue"
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
    # Where events land when they match no category. Every calendar named here or in a
    # category has to exist already; the service never creates one.
    default_calendar: str = ""
    default_timezone: str = "UTC"
    categories: list[CategoryRule] = []

    # Both outcomes are pushed to the phone, so this is the only place a failure is
    # reported. A no-op until both keys are configured.
    pushover_user: str = ""
    pushover_token: str = ""

    state_db: Path = Path("data/state.sqlite")
    log_level: str = "INFO"

    @field_validator("categories", mode="before")
    @classmethod
    def _parse_categories(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            return json.loads(stripped) if stripped else []
        return value

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

    @model_validator(mode="after")
    def _unique_category_names(self) -> Settings:
        seen: set[str] = set()
        for rule in self.categories:
            if rule.name in seen:
                raise ValueError(f"duplicate category name {rule.name!r}")
            seen.add(rule.name)
        return self

    @property
    def max_attachment_bytes(self) -> int:
        return int(self.max_attachment_mb * 1024 * 1024)

    @property
    def calendars(self) -> set[str]:
        """Every calendar the service writes to, which all have to exist up front."""
        return {self.default_calendar} | {rule.calendar for rule in self.categories}

    def calendar_for(self, category: str | None) -> str:
        """Map an extracted category name onto a calendar, defaulting when unmatched."""
        if category:
            wanted = category.strip().lower()
            for rule in self.categories:
                if rule.name == wanted:
                    return rule.calendar
        return self.default_calendar
