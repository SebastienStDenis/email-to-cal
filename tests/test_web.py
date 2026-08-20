from __future__ import annotations

import json

import pytest
from flask.testing import FlaskClient

from email_to_cal.config import CONFIG_FILE, Settings
from email_to_cal.store import Store
from email_to_cal.web import Supervisor, create_app, missing_for_start

FORM = {
    "apple_id": "test@icloud.com",
    "apple_password": "xxxx-xxxx",
    "provider": "anthropic",
    "anthropic_api_key": "sk-ant-test",
    "calendar_name": "Bookings",
    "default_timezone": "Europe/Zurich",
    "enable_vision": "on",
    "log_level": "INFO",
}


@pytest.fixture
def supervisor() -> Supervisor:
    # Never restarted in tests, so no watcher thread ever starts.
    return Supervisor()


@pytest.fixture
def client(supervisor: Supervisor) -> FlaskClient:
    app = create_app(supervisor)
    app.config["TESTING"] = True
    return app.test_client()


def test_saving_the_form_writes_config_json_that_settings_pick_up(client: FlaskClient) -> None:
    response = client.post("/settings", data=FORM)
    assert response.status_code == 302

    saved = json.loads(CONFIG_FILE.read_text())
    assert saved["apple_id"] == "test@icloud.com"
    assert saved["calendar_name"] == "Bookings"

    settings = Settings()
    assert settings.apple_password == "xxxx-xxxx"
    assert settings.default_timezone == "Europe/Zurich"


def test_unchecked_checkboxes_come_back_false(client: FlaskClient) -> None:
    # A browser sends nothing at all for an unchecked box, which must read as "off"
    # rather than "leave it as it was".
    client.post("/settings", data={k: v for k, v in FORM.items() if k != "enable_vision"})
    assert Settings().enable_vision is False


def test_invalid_input_rerenders_the_form_and_writes_nothing(client: FlaskClient) -> None:
    response = client.post("/settings", data={**FORM, "default_timezone": "Middle/Earth"})
    assert response.status_code == 200
    assert "not a recognised time zone" in response.text
    assert not CONFIG_FILE.exists()


def test_settings_page_offers_timezone_suggestions(client: FlaskClient) -> None:
    assert "Europe/Zurich" in client.get("/settings").text


def test_unconfigured_service_redirects_home_to_settings(client: FlaskClient) -> None:
    response = client.get("/")
    assert response.headers["Location"].endswith("/settings")


def test_setup_is_incomplete_without_a_calendar(settings: Settings) -> None:
    settings.calendar_name = ""
    assert "calendar name" in missing_for_start(settings)


def test_a_local_model_needs_no_api_key(settings: Settings) -> None:
    settings.provider = "ollama"
    settings.anthropic_api_key = ""
    assert missing_for_start(settings) == []


def test_healthz_is_ok_while_waiting_for_configuration(client: FlaskClient) -> None:
    response = client.get("/healthz")
    # Nothing configured yet is a healthy container, not a crashed one.
    assert response.status_code == 200
    assert "waiting for configuration" in response.text


def test_retrying_one_message_forgets_only_that_failure(client: FlaskClient) -> None:
    client.post("/settings", data=FORM)
    with Store(Settings().state_db) as store:
        store.record_failure("<a@b>", "First", 3, "boom", None)
        store.record_failure("<c@d>", "Second", 3, "boom", None)

    client.post("/retry", data={"message_id": "<a@b>"})

    with Store(Settings().state_db) as store:
        assert [f.message_id for f in store.list_failures()] == ["<c@d>"]


def test_retrying_everything_clears_them_all(client: FlaskClient) -> None:
    client.post("/settings", data=FORM)
    with Store(Settings().state_db) as store:
        store.record_failure("<a@b>", "First", 3, "boom", None)
        store.record_failure("<c@d>", "Second", 3, "boom", None)

    client.post("/retry", data={})

    with Store(Settings().state_db) as store:
        assert store.list_failures() == []


def test_the_status_page_lists_what_is_still_flagged(client: FlaskClient) -> None:
    client.post("/settings", data=FORM)
    with Store(Settings().state_db) as store:
        store.record_failure("<a@b>", "Your booking", 3, "nothing to put on a calendar", None)

    text = client.get("/status").text

    assert "Your booking" in text
    assert "nothing to put on a calendar" in text
