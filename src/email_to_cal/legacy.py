"""One-time import of the configuration file earlier releases wrote.

Before the settings page split credentials from preferences, everything lived in
`data/config.json`. A deployment that still has one, and no `data/secrets.env` yet, gets
it carried across on its first boot: the credentials into the secrets file, the
preferences into the database, and the file renamed so it is never read again.

This module can be deleted once every deployment has booted on the new layout.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from . import prefs
from .config import DATA_DIR, SECRETS_FILE, write_secrets
from .store import Store

log = logging.getLogger(__name__)

LEGACY_FILE = DATA_DIR / "config.json"

# Old name to new, for the values that still have a home.
CREDENTIAL_KEYS = {
    "apple_id": "icloud_email",
    "apple_password": "icloud_app_password",
    "anthropic_api_key": "anthropic_api_key",
    "pushover_token": "pushover_token",
    "pushover_user": "pushover_user_key",
}
PREFERENCE_KEYS = {
    "flag_colour": "flag_colour",
    "default_calendar": "calendar",
    "default_timezone": "timezone",
    "categories": "categories",
    "log_level": "log_level",
}


def import_legacy_config(store: Store) -> bool:
    """Carry `data/config.json` across, once. Returns whether anything was imported."""
    if not LEGACY_FILE.exists() or SECRETS_FILE.exists():
        return False
    try:
        old: dict[str, Any] = json.loads(LEGACY_FILE.read_text())
    except (OSError, ValueError):
        log.warning("could not read %s; leaving it alone", LEGACY_FILE, exc_info=True)
        return False

    credentials = {
        new: str(old[name]).strip()
        for name, new in CREDENTIAL_KEYS.items()
        if isinstance(old.get(name), str) and str(old[name]).strip()
    }
    if credentials:
        write_secrets(credentials)

    # Each preference on its own, so one value the new model refuses - a flag colour
    # that is no longer offered, say - does not cost the ones it accepts.
    for name, new in PREFERENCE_KEYS.items():
        if name not in old:
            continue
        try:
            prefs.save(store, {new: old[name]})
        except ValidationError:
            log.warning("skipping %s=%r from %s", name, old[name], LEGACY_FILE)

    LEGACY_FILE.rename(LEGACY_FILE.with_suffix(".json.imported"))
    log.info(
        "imported %s: %s; the file is kept as %s",
        LEGACY_FILE,
        ", ".join(sorted(credentials)) or "no credentials",
        LEGACY_FILE.with_suffix(".json.imported"),
    )
    return True
