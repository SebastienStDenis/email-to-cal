"""Credentials, and the one file they live in.

Configuration splits in two, and no value lives in both halves. A *credential* is a
secret handed to another service: it is entered on the settings page, is never handed
back out by it, and is stored in `data/secrets.env` beside the database. Everything else
is a *preference*: it has a working default, it is edited on the same page, and the
database is the only place it lives - see `prefs`.

`data/secrets.env` outranks both the process environment and `.env`, which stay as an
optional way to seed a deployment. A fresh container needs neither, and a credential
typed into the settings page survives a restart of a container whose environment still
carries the old one.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# Relative on purpose: the image's WORKDIR is /app and the volume is mounted at
# /app/data, so the same default is the volume in Docker and ./data in a checkout.
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
SECRETS_FILE = DATA_DIR / "secrets.env"
STATE_FILE = DATA_DIR / "state.sqlite"


class Service(NamedTuple):
    """One account the settings page connects to, and the credentials it needs."""

    key: str
    name: str
    fields: tuple[str, ...]


# What the settings page offers, in the order it offers it.
SERVICES = (
    Service("icloud", "iCloud", ("icloud_email", "icloud_app_password")),
    Service("anthropic", "Anthropic", ("anthropic_api_key",)),
    Service("pushover", "Pushover", ("pushover_token", "pushover_user_key")),
    Service("watchtower", "Watchtower", ("watchtower_url", "watchtower_token")),
)

CREDENTIALS = tuple(name for service in SERVICES for name in service.fields)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # The settings page writes data/secrets.env, so it outranks the environment and
        # .env rather than being overridden by them: what somebody typed into the UI is
        # newer than anything the container was started with, and an empty value there
        # is how a credential is cleared for good.
        return (
            init_settings,
            DotEnvSettingsSource(settings_cls, env_file=SECRETS_FILE, env_file_encoding="utf-8"),
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )

    # --- iCloud, the mailbox we watch and the calendars we write ----------------------
    # One app-specific password from appleid.apple.com covers both IMAP and CalDAV, and
    # neither accepts the Apple ID password once two-factor authentication is on. The
    # address is the account's name rather than a secret, so it is the one credential
    # the settings page shows back.
    icloud_email: str = ""
    icloud_app_password: str = Field(default="", repr=False)

    # --- Anthropic, what reads the email ------------------------------------------------
    anthropic_api_key: str = Field(default="", repr=False)

    # --- Pushover, the phone ----------------------------------------------------------
    # The token belongs to the application registered at pushover.net; the user key
    # identifies the account every device of yours is signed in to.
    pushover_token: str = Field(default="", repr=False)
    pushover_user_key: str = Field(default="", repr=False)

    # --- Watchtower, the updater --------------------------------------------------------
    # The address is a name on the compose network and no more a secret than a hostname,
    # so like the Apple ID it is the one half the settings page shows back; the token is
    # whatever WATCHTOWER_HTTP_API_TOKEN was set to on that container.
    watchtower_url: str = ""
    watchtower_token: str = Field(default="", repr=False)

    @property
    def icloud_configured(self) -> bool:
        return bool(self.icloud_email and self.icloud_app_password)

    @property
    def anthropic_configured(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def pushover_configured(self) -> bool:
        return bool(self.pushover_token and self.pushover_user_key)

    @property
    def watchtower_configured(self) -> bool:
        return bool(self.watchtower_url and self.watchtower_token)


def write_secrets(values: Mapping[str, str]) -> Settings:
    """Persist credentials to `data/secrets.env`.

    The file is rewritten whole rather than appended to, so a replaced value leaves one
    line rather than two and a parser choosing between them. An empty value is kept as an
    empty line rather than dropped: that is what clears a credential the environment or
    `.env` would otherwise go on supplying.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lines = {}
    if SECRETS_FILE.exists():
        for line in SECRETS_FILE.read_text().splitlines():
            name, _, existing = line.partition("=")
            if name.strip():
                lines[name.strip()] = existing
    lines.update({name.upper(): value for name, value in values.items()})
    SECRETS_FILE.write_text("".join(f"{name}={value}\n" for name, value in sorted(lines.items())))
    SECRETS_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return Settings()
