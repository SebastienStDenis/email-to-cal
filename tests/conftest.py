from __future__ import annotations

from pathlib import Path

import pytest

from email_to_cal.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolate_from_ambient_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's real data/config.json and shell out of the tests.

    Settings reads both by default, which is what makes the service configurable - and
    what would otherwise let a real credential silently change what the suite is
    testing. The chdir isolates the relative config paths.
    """
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        apple_id="test@icloud.com",
        apple_password="secret",
        anthropic_api_key="test-key",
        state_db=tmp_path / "state.sqlite",
        default_timezone="Europe/Zurich",
        default_calendar="Bookings",
    )


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()
