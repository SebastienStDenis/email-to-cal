from __future__ import annotations

import json
from pathlib import Path

from email_to_cal import prefs
from email_to_cal.config import SECRETS_FILE, Settings
from email_to_cal.legacy import LEGACY_FILE, import_legacy_config
from email_to_cal.store import Store


def write_legacy(values: dict[str, object]) -> None:
    LEGACY_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEGACY_FILE.write_text(json.dumps(values))


def test_the_old_file_is_carried_across_and_put_aside(tmp_path: Path) -> None:
    write_legacy(
        {
            "apple_id": "me@icloud.com",
            "apple_password": "xxxx-xxxx",
            "anthropic_api_key": "sk-ant",
            "pushover_user": "usr",
            "pushover_token": "tok",
            "flag_colour": "purple",
            "default_calendar": "Bookings",
            "default_timezone": "America/New_York",
            "log_level": "DEBUG",
            "categories": [{"name": "Travel", "description": "Flights.", "calendar": "Trips"}],
            "imap_host": "imap.mail.me.com",
            "provider": "ollama",
        }
    )

    with Store(tmp_path / "state.sqlite") as store:
        assert import_legacy_config(store)
        loaded = prefs.load(store)

    settings = Settings()
    assert (settings.icloud_email, settings.icloud_app_password) == ("me@icloud.com", "xxxx-xxxx")
    assert settings.anthropic_api_key == "sk-ant"
    assert (settings.pushover_user_key, settings.pushover_token) == ("usr", "tok")
    assert (loaded.flag_colour, loaded.calendar, loaded.timezone) == (
        "purple",
        "Bookings",
        "America/New_York",
    )
    assert loaded.log_level == "DEBUG"
    assert [(c.name, c.calendar) for c in loaded.categories] == [("travel", "Trips")]
    # Never read twice: the file is kept for reference under another name.
    assert not LEGACY_FILE.exists()
    assert LEGACY_FILE.with_suffix(".json.imported").exists()


def test_a_value_the_new_model_refuses_does_not_cost_the_rest(tmp_path: Path) -> None:
    # Red is no longer a flag colour; the calendar beside it is still worth keeping.
    write_legacy({"flag_colour": "red", "default_calendar": "Bookings"})

    with Store(tmp_path / "state.sqlite") as store:
        import_legacy_config(store)
        loaded = prefs.load(store)

    assert loaded.flag_colour == "blue"
    assert loaded.calendar == "Bookings"


def test_empty_credentials_are_not_written(tmp_path: Path) -> None:
    write_legacy({"apple_id": "", "anthropic_api_key": "sk-ant"})

    with Store(tmp_path / "state.sqlite") as store:
        import_legacy_config(store)

    assert "ICLOUD_EMAIL" not in SECRETS_FILE.read_text()
    assert Settings().anthropic_api_key == "sk-ant"


def test_nothing_happens_once_the_new_layout_exists(tmp_path: Path) -> None:
    write_legacy({"anthropic_api_key": "old"})
    SECRETS_FILE.write_text("ANTHROPIC_API_KEY=new\n")

    with Store(tmp_path / "state.sqlite") as store:
        assert not import_legacy_config(store)

    # What was typed on the settings page is newer than the file, and wins.
    assert Settings().anthropic_api_key == "new"
    assert LEGACY_FILE.exists()


def test_nothing_happens_without_the_old_file(tmp_path: Path) -> None:
    with Store(tmp_path / "state.sqlite") as store:
        assert not import_legacy_config(store)
