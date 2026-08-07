from __future__ import annotations

import base64
import hashlib
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
    "sweep_folder": ["Archive", "Sent"],
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


def test_swept_folder_names_may_contain_spaces_and_commas(client: FlaskClient) -> None:
    """Each chip is its own form field, so no character in a folder name is special."""
    folders = ["Deleted Messages", "Receipts, Travel"]
    client.post("/settings", data={**FORM, "sweep_folder": folders})
    assert Settings().sweep_folders == folders

    page = client.get("/settings")
    assert b"Receipts, Travel" in page.data


def test_settings_page_offers_timezone_suggestions(client: FlaskClient) -> None:
    page = client.get("/settings")
    assert b"timezone-suggestions" in page.data
    assert b"Europe/Zurich" in page.data


def test_unchecked_checkboxes_come_back_false(client: FlaskClient) -> None:
    form = {key: value for key, value in FORM.items() if key != "dry_run"}
    client.post("/settings", data=form)
    assert Settings().dry_run is False


def test_invalid_input_rerenders_the_form_and_writes_nothing(client: FlaskClient) -> None:
    reply = client.post("/settings", data={**FORM, "default_timezone": "Europe/Atlantis"})
    assert reply.status_code == 200
    assert b"not a recognised time zone" in reply.data
    assert not Path(CONFIG_FILE).exists()


def test_connect_button_saves_the_form_then_redirects_to_google(client: FlaskClient) -> None:
    reply = client.post("/settings", data={**FORM, "action": "connect"})
    assert reply.status_code == 302
    assert reply.headers["Location"].startswith("https://accounts.google.com/")
    # The form was saved on the way out, so nothing is lost during the consent trip.
    assert Settings().imap_username == "test@icloud.com"


def test_connect_keeps_the_pkce_verifier_the_callback_must_present(
    client: FlaskClient,
) -> None:
    """The auth URL carries a hashed code verifier, and the callback rebuilds the flow
    from scratch; without the stored verifier Google answers 'Missing code verifier'."""
    reply = client.post("/settings", data={**FORM, "action": "connect"})
    with client.session_transaction() as session:
        verifier = session["code_verifier"]

    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert f"code_challenge={challenge}" in reply.headers["Location"]


def test_connect_from_a_remote_http_hostname_explains_instead_of_calling_google(
    client: FlaskClient,
) -> None:
    """Google rejects plain-http non-loopback redirects with a cryptic policy page;
    the portal must catch it first and say what to do."""
    reply = client.post(
        "/settings", data={**FORM, "action": "connect"}, base_url="http://zoloft:8484"
    )
    assert reply.status_code == 302
    assert reply.headers["Location"].endswith("/settings")
    # The form was still saved before the redirect back.
    assert Settings().imap_username == "test@icloud.com"

    follow = client.get("/settings", base_url="http://zoloft:8484")
    assert b"ssh -L 8484:localhost:8484 zoloft" in follow.data


def test_connect_over_remote_https_goes_to_google_with_that_redirect(
    client: FlaskClient,
) -> None:
    """A registered https redirect (Web application client) is valid, so an HTTPS
    page - e.g. behind tailscale serve - connects without any tunnel."""
    reply = client.post(
        "/settings",
        data={**FORM, "action": "connect"},
        base_url="https://zoloft.tail1234.ts.net",
    )
    assert reply.status_code == 302
    location = reply.headers["Location"]
    assert location.startswith("https://accounts.google.com/")
    assert "redirect_uri=https%3A%2F%2Fzoloft.tail1234.ts.net%2Fgoogle%2Fcallback" in location


def test_connect_without_a_client_id_returns_to_settings(client: FlaskClient) -> None:
    form = {key: value for key, value in FORM.items() if not key.startswith("google_")}
    reply = client.post("/settings", data={**form, "action": "connect"})
    assert reply.status_code == 302
    assert reply.headers["Location"].endswith("/settings")


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
