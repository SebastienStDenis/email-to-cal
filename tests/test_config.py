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
