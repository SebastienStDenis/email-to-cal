from __future__ import annotations

import stat

import pytest

from email_to_cal.config import SECRETS_FILE, Settings, write_secrets


def test_a_typed_credential_outranks_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The environment seeds a deployment; what was saved on the settings page wins."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    assert Settings().anthropic_api_key == "from-env"

    write_secrets({"anthropic_api_key": "typed-in"})
    assert Settings().anthropic_api_key == "typed-in"


def test_clearing_a_credential_beats_an_environment_still_carrying_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    write_secrets({"anthropic_api_key": ""})
    # A container restarted with the old value in its environment must not undo Forget.
    assert Settings().anthropic_api_key == ""


def test_writing_one_credential_leaves_the_others_alone() -> None:
    write_secrets({"icloud_email": "me@icloud.com", "icloud_app_password": "xxxx"})
    write_secrets({"pushover_token": "t"})

    settings = Settings()
    assert settings.icloud_email == "me@icloud.com"
    assert settings.icloud_app_password == "xxxx"
    assert settings.pushover_token == "t"


def test_the_secrets_file_is_private_to_the_owner() -> None:
    write_secrets({"icloud_app_password": "xxxx-xxxx-xxxx-xxxx"})
    assert stat.S_IMODE(SECRETS_FILE.stat().st_mode) == 0o600


def test_a_connection_is_configured_only_with_everything_it_needs() -> None:
    assert not Settings(icloud_email="me@icloud.com").icloud_configured
    assert Settings(icloud_email="me@icloud.com", icloud_app_password="x").icloud_configured
    assert not Settings(pushover_token="t").pushover_configured
    assert Settings(pushover_token="t", pushover_user_key="u").pushover_configured
    assert not Settings(watchtower_url="http://watchtower:8080").watchtower_configured
    assert Settings(watchtower_url="watchtower:8080", watchtower_token="t").watchtower_configured
