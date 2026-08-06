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


def test_categories_load_from_a_yaml_file(tmp_path: Path) -> None:
    path = tmp_path / "categories.yaml"
    path.write_text(
        "- name: music\n"
        "  description: Concerts and gigs.\n"
        "  calendar: Music\n"
        "- name: travel\n"
        "  description: Flights and hotels.\n"
        "  calendar: Sebastiens Travels\n"
    )
    settings = Settings(categories_file=path)
    assert [r.name for r in settings.categories] == ["music", "travel"]


def test_duplicate_category_names_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate category name"):
        Settings(
            categories=[
                {"name": "travel", "description": "a", "calendar": "A"},
                {"name": "Travel", "description": "b", "calendar": "B"},
            ]  # type: ignore[arg-type]
        )


def test_empty_category_description_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(categories=[{"name": "travel", "description": "", "calendar": "A"}])  # type: ignore[arg-type]


def test_bad_default_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not a known IANA zone"):
        Settings(default_timezone="Europe/Atlantis")


def test_empty_categories_env_string() -> None:
    assert Settings(categories="").categories == []  # type: ignore[arg-type]


def test_missing_categories_file_is_a_config_error_not_a_traceback(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="cannot read CATEGORIES_FILE"):
        Settings(categories_file=tmp_path / "nope.yaml")


def test_categories_file_must_hold_a_list(tmp_path: Path) -> None:
    path = tmp_path / "categories.yaml"
    path.write_text("music:\n  calendar: Music\n")
    with pytest.raises(ValidationError, match="must contain a list"):
        Settings(categories_file=path)


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
