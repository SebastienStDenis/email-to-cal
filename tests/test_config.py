from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from email_to_cal.config import Settings


def test_bad_default_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not a recognised time zone"):
        Settings(default_timezone="Middle/Earth")


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(provider="gpt")


def test_config_json_is_read_but_the_environment_overrides_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The portal writes data/config.json; a real environment variable must still beat
    it for one-off debugging."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "config.json").write_text('{"apple_id": "from-config"}')

    assert Settings().apple_id == "from-config"

    monkeypatch.setenv("APPLE_ID", "from-env")
    assert Settings().apple_id == "from-env"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        # Polling faster than a minute is what makes iCloud start refusing connections.
        ("poll_interval_seconds", 30),
        ("poll_interval_seconds", 0),
        # Negative sizes made every attachment look oversized and vanish silently.
        ("max_attachment_mb", -1.0),
        ("max_attachment_mb", 0.0),
        # Too small a context window truncates the prompt rather than the email.
        ("ollama_num_ctx", 512),
        ("ollama_timeout_seconds", 0.0),
    ],
)
def test_out_of_range_numeric_settings_are_rejected(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: value})
