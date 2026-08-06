"""Runtime configuration, sourced entirely from the environment."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    imap_host: str = "imap.mail.me.com"
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    # iCloud drops long-lived idle sockets well before RFC 2177's 29-minute ceiling, and
    # imap-tools refuses anything past it outright. Zero would spin the loop at full CPU.
    imap_idle_seconds: int = Field(default=300, ge=10, le=1740)
    first_run_lookback_days: int = Field(default=0, ge=0)

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    enable_vision: bool = True
    enable_web_search: bool = False
    # A negative value would make every attachment look oversized and silently vanish.
    max_attachment_mb: float = Field(default=8.0, gt=0)
    min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)

    google_credentials_file: Path = Path("/data/credentials.json")
    google_token_file: Path = Path("/data/token.json")
    default_calendar: str = "primary"
    default_timezone: str = "UTC"

    state_db: Path = Path("/data/state.sqlite")
    dry_run: bool = False
    log_level: str = "INFO"

    categories: list[CategoryRule] = []
    categories_file: Path | None = None

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
            raise ValueError(f"DEFAULT_TIMEZONE {value!r} is not a known IANA zone") from exc
        return value

    @model_validator(mode="after")
    def _finalise_categories(self) -> Settings:
        if self.categories_file is not None:
            # pydantic only turns ValueError into a ValidationError, so a typo'd path
            # would otherwise reach the operator as a bare traceback.
            try:
                raw = self.categories_file.read_text()
            except OSError as exc:
                raise ValueError(
                    f"cannot read CATEGORIES_FILE {self.categories_file}: {exc}"
                ) from exc
            loaded = yaml.safe_load(raw) or []
            if not isinstance(loaded, list):
                raise ValueError(
                    f"CATEGORIES_FILE {self.categories_file} must contain a list of "
                    f"{{name, description, calendar}} entries, got {type(loaded).__name__}"
                )
            self.categories = [CategoryRule.model_validate(item) for item in loaded]

        seen: set[str] = set()
        for rule in self.categories:
            if rule.name in seen:
                raise ValueError(f"duplicate category name {rule.name!r}")
            seen.add(rule.name)
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
