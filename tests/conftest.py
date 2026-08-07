from __future__ import annotations

from pathlib import Path

import pytest

from email_to_cal.config import CategoryRule, Settings

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolate_from_ambient_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's real data/config.json and shell out of the tests.

    Settings reads both by default, which is what makes the service configurable — and
    what would otherwise let a local DRY_RUN=true or a real credential silently change
    what the suite is testing. The chdir isolates the relative config paths.
    """
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        imap_username="test@icloud.com",
        imap_password="secret",
        anthropic_api_key="test-key",
        dry_run=False,
        state_db=tmp_path / "state.sqlite",
        google_token_file=tmp_path / "token.json",
        default_timezone="Europe/Zurich",
        default_calendar="primary",
        categories=[
            CategoryRule(
                name="travel",
                description="Flights, trains, and hotel stays.",
                calendar="Sebastiens Travels",
            ),
            CategoryRule(name="music", description="Concerts and live music.", calendar="Music"),
        ],
    )


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()
