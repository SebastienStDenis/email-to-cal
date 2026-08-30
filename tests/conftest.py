from __future__ import annotations

from pathlib import Path

import pytest

from email_to_cal import prefs
from email_to_cal.config import CREDENTIALS, Settings
from email_to_cal.prefs import Prefs

FIXTURES = Path(__file__).parent / "fixtures"

# Every credential named explicitly, so that a developer's own .env or data/secrets.env
# can never be what a test is really asserting against.
BLANK = dict.fromkeys(CREDENTIALS, "")


@pytest.fixture(autouse=True)
def _isolate_from_ambient_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's real data/ directory and shell out of the tests.

    Settings reads both by default, which is what makes the service configurable - and
    what would otherwise let a real credential silently change what the suite is
    testing. The chdir isolates the relative data paths.
    """
    for name in CREDENTIALS:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        **BLANK
        | {
            "icloud_email": "test@icloud.com",
            "icloud_app_password": "secret",
            "anthropic_api_key": "test-key",
        }
    )


@pytest.fixture
def unconfigured() -> Settings:
    """A deployment on its first boot, with nothing entered anywhere."""
    return Settings(**BLANK)


@pytest.fixture(autouse=True)
def preferences(monkeypatch: pytest.MonkeyPatch) -> Prefs:
    """A fully configured deployment, installed as the live preferences.

    Autouse because the portal reads the live row rather than being handed one, and a
    developer's own database must never be what a test asserts against.
    """
    configured = Prefs(calendar="Bookings", timezone="Europe/Zurich")
    monkeypatch.setattr(prefs, "_current", configured)
    return configured


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()
