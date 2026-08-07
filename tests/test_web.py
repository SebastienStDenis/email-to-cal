from __future__ import annotations

import json
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from email_to_cal.config import CONFIG_FILE, Settings
from email_to_cal.web import Supervisor, create_app, missing_for_start

FORM = {
    "imap_username": "test@icloud.com",
    "imap_password": "xxxx-xxxx",
    "imap_folder": "INBOX",
    "sweep_folders": "Archive, Sent",
    "anthropic_api_key": "sk-ant-test",
    "min_confidence": "0.8",
    "google_client_id": "abc.apps.googleusercontent.com",
    "google_client_secret": "GOCSPX-x",
    "default_calendar": "primary",
    "default_timezone": "Europe/Zurich",
    "dry_run": "on",
    "log_level": "INFO",
    "category_name": ["travel"],
    "category_description": ["Flights and hotels."],
    "category_calendar": ["Travels"],
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
    reply = client.post("/settings", data=FORM)
    assert reply.status_code == 302

    saved = json.loads(Path(CONFIG_FILE).read_text())
    assert saved["imap_username"] == "test@icloud.com"

    settings = Settings()
    assert settings.imap_username == "test@icloud.com"
    assert settings.sweep_folders == ["Archive", "Sent"]
    assert settings.min_confidence == 0.8
    assert settings.dry_run is True
    assert [rule.name for rule in settings.categories] == ["travel"]
    # Fields the form left empty fall back to their defaults instead of going blank.
    assert settings.imap_host == "imap.mail.me.com"


def test_swept_folder_names_keep_their_inner_spaces(client: FlaskClient) -> None:
    client.post("/settings", data={**FORM, "sweep_folders": "Archive, Deleted Messages"})
    assert Settings().sweep_folders == ["Archive", "Deleted Messages"]


def test_settings_page_offers_timezone_suggestions(client: FlaskClient) -> None:
    page = client.get("/settings")
    assert b'list="timezones"' in page.data
    assert b"Europe/Zurich" in page.data


def test_unchecked_checkboxes_come_back_false(client: FlaskClient) -> None:
    form = {key: value for key, value in FORM.items() if key != "dry_run"}
    client.post("/settings", data=form)
    assert Settings().dry_run is False


def test_invalid_input_rerenders_the_form_and_writes_nothing(client: FlaskClient) -> None:
    reply = client.post("/settings", data={**FORM, "default_timezone": "Europe/Atlantis"})
    assert reply.status_code == 200
    assert b"not a known IANA zone" in reply.data
    assert not Path(CONFIG_FILE).exists()


def test_unconfigured_service_redirects_home_to_settings(client: FlaskClient) -> None:
    reply = client.get("/")
    assert reply.status_code == 302
    assert reply.headers["Location"].endswith("/settings")


def test_healthz_is_ok_while_waiting_for_configuration(
    client: FlaskClient, supervisor: Supervisor
) -> None:
    reply = client.get("/healthz")
    assert reply.status_code == 200
    assert b"waiting" in reply.data or b"ok" in reply.data

    supervisor.error = "AuthenticationFatal: iCloud rejected the credentials"
    assert client.get("/healthz").status_code == 500


def test_settings_page_renders_current_values(client: FlaskClient) -> None:
    client.post("/settings", data=FORM)
    page = client.get("/settings")
    assert b"test@icloud.com" in page.data
    assert b"Flights and hotels." in page.data


def test_missing_for_start_accepts_dry_run_without_google(settings: Settings) -> None:
    settings.dry_run = True
    assert missing_for_start(settings) == []

    settings.dry_run = False
    assert missing_for_start(settings) == ["Google authorisation"]

    settings.anthropic_api_key = ""
    assert "Anthropic API key" in missing_for_start(settings)
