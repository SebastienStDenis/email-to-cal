from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from email_to_cal.config import Settings


def test_categories_parse_from_a_json_env_string() -> None:
    raw = '[{"name":"Travel","description":"Flights.","calendar":"Sebastiens Travels"}]'
    settings = Settings(categories=raw)  # type: ignore[arg-type]
    assert len(settings.categories) == 1
    assert settings.categories[0].name == "travel"
    assert settings.categories[0].calendar == "Sebastiens Travels"


def test_duplicate_category_names_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate category name"):
        Settings(
            categories=[
                {"name": "travel", "description": "a", "calendar": "A"},
                {"name": "Travel", "description": "b", "calendar": "B"},
            ],  # type: ignore[arg-type]
        )


def test_empty_category_description_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(categories=[{"name": "travel", "description": "", "calendar": "A"}])  # type: ignore[arg-type]


def test_bad_default_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not a recognised time zone"):
        Settings(default_timezone="Europe/Atlantis")


def test_empty_categories_env_string() -> None:
    assert Settings(categories="").categories == []  # type: ignore[arg-type]


def test_sweep_folders_parse_from_a_comma_separated_env_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWEEP_FOLDERS", "Archive,Sent")
    assert Settings().sweep_folders == ["Archive", "Sent"]


def test_config_json_is_read_but_the_environment_overrides_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The portal writes data/config.json; a real environment variable must still beat
    it for one-off debugging."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "config.json").write_text('{"imap_username": "from-config"}')

    assert Settings().imap_username == "from-config"

    monkeypatch.setenv("IMAP_USERNAME", "from-env")
    assert Settings().imap_username == "from-env"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        # Zero would spin the IMAP loop at full CPU; past 29 minutes imap-tools refuses.
        ("imap_idle_seconds", 0),
        ("imap_idle_seconds", 3600),
        # Negative sizes made every attachment look oversized and vanish silently.
        ("max_attachment_mb", -1.0),
        ("max_attachment_mb", 0.0),
        # Out-of-range thresholds would silently discard every event.
        ("min_confidence", 1.5),
        ("min_confidence", -0.1),
        ("first_run_lookback_days", -1),
    ],
)
def test_out_of_range_numeric_settings_are_rejected(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: value})
