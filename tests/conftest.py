from __future__ import annotations

from pathlib import Path

import pytest

from email_to_cal.config import CategoryRule, Settings

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolate_from_ambient_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's real .env and shell out of the tests.

    Settings reads .env and the environment by default, which is what makes the service
    configurable — and what would otherwise let a local DRY_RUN=true or a real credential
    silently change what the suite is testing.
    """
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        imap_username="test@icloud.com",
        imap_password="secret",
        anthropic_api_key="test-key",
        state_db=tmp_path / "state.sqlite",
        google_token_file=tmp_path / "token.json",
        google_credentials_file=tmp_path / "credentials.json",
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
