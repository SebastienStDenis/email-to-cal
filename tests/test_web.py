"""The pages, rendered against a faked mailbox and calendar.

Nothing here touches the network: every check the settings page runs before it keeps a
credential is stood in for, so what is tested is what the page does with the answer.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from flask.testing import FlaskClient

from email_to_cal import prefs, web
from email_to_cal.checks import CheckResult
from email_to_cal.config import STATE_FILE, Settings, write_secrets
from email_to_cal.prefs import Prefs
from email_to_cal.store import Store
from email_to_cal.web import Supervisor, create_app, missing_for_start

ICLOUD = {"icloud_email": "test@icloud.com", "icloud_app_password": "xxxx-xxxx"}


@pytest.fixture
def supervisor() -> Supervisor:
    # Never restarted for real in tests, so no watcher thread ever starts.
    return Supervisor()


@pytest.fixture
def client(supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch) -> FlaskClient:
    monkeypatch.setattr(supervisor, "restart", lambda: None)
    app = create_app(supervisor)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def checks_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "check_service", lambda s, p, name: CheckResult(name, True, "ok"))
    monkeypatch.setattr(web, "check_calendar", lambda s, names: CheckResult("calendar", True, "ok"))


@pytest.fixture
def calendars(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeCalendars:
        def __init__(self, settings: Settings) -> None:
            pass

        def calendars(self) -> dict[str, str]:
            return {"Bookings": "https://caldav.example/b/", "Home": "https://caldav.example/h/"}

    monkeypatch.setattr(web, "CalendarClient", FakeCalendars)


def store() -> Store:
    return Store(STATE_FILE)


# -- connections ---------------------------------------------------------------------


def test_a_connection_that_works_is_kept(client: FlaskClient, checks_pass: None) -> None:
    response = client.post("/settings/credentials", data={"service": "icloud", **ICLOUD})

    assert response.status_code == 303
    assert response.headers["Location"].endswith("/settings?tab=connections")
    assert Settings().icloud_app_password == "xxxx-xxxx"


def test_a_connection_the_service_refuses_is_put_back(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_secrets({"anthropic_api_key": "old-key"})
    monkeypatch.setattr(
        web, "check_service", lambda s, p, name: CheckResult(name, False, "key rejected")
    )

    response = client.post(
        "/settings/credentials",
        data={"service": "anthropic", "anthropic_api_key": "typo"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 400
    assert response.json == {"error": "Anthropic: key rejected"}
    # A typo saved and noticed on the next email is the failure this exists to prevent.
    assert Settings().anthropic_api_key == "old-key"


def test_an_empty_box_leaves_the_stored_credential_alone(
    client: FlaskClient, checks_pass: None
) -> None:
    write_secrets(ICLOUD)

    client.post(
        "/settings/credentials",
        data={"service": "icloud", "icloud_email": "new@icloud.com", "icloud_app_password": ""},
    )

    settings = Settings()
    assert settings.icloud_email == "new@icloud.com"
    assert settings.icloud_app_password == "xxxx-xxxx"


def test_forgetting_clears_every_credential_the_service_needs(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_secrets(ICLOUD)

    def refuse(*_: Any) -> CheckResult:
        raise AssertionError("forgetting must not be checked")

    monkeypatch.setattr(web, "check_service", refuse)

    client.post("/settings/credentials", data={"service": "icloud", "forget": "1"})

    settings = Settings()
    assert (settings.icloud_email, settings.icloud_app_password) == ("", "")


def test_a_connection_that_does_not_exist_is_a_404(client: FlaskClient) -> None:
    assert client.post("/settings/credentials", data={"service": "fax"}).status_code == 404


def test_no_stored_credential_is_ever_rendered_back(client: FlaskClient) -> None:
    write_secrets({**ICLOUD, "anthropic_api_key": "sk-ant-secret", "pushover_token": "tok-secret"})

    text = client.get("/settings").text

    assert "xxxx-xxxx" not in text
    assert "sk-ant-secret" not in text
    assert "tok-secret" not in text
    # The Apple ID names the account rather than proving anything about it.
    assert "test@icloud.com" in text


def test_the_settings_page_shows_what_is_connected(client: FlaskClient) -> None:
    write_secrets(ICLOUD)
    text = client.get("/settings").text
    assert text.count("Connected") >= 1
    assert "Not connected" in text
    assert "Required" in text


# -- preferences ---------------------------------------------------------------------


def test_a_preference_is_saved_and_redirects_to_its_tab(
    client: FlaskClient, checks_pass: None
) -> None:
    response = client.post("/settings", data={"tab": "preferences", "flag_colour": "purple"})

    assert response.status_code == 303
    assert response.headers["Location"].endswith("/settings?tab=preferences")
    assert prefs.current().flag_colour == "purple"
    with store() as db:
        assert prefs.load(db).flag_colour == "purple"


def test_a_field_nobody_sent_is_left_alone(client: FlaskClient, checks_pass: None) -> None:
    client.post("/settings", data={"tab": "preferences", "flag_colour": "green"})
    assert prefs.current().timezone == "Europe/Zurich"


def test_a_refused_preference_answers_the_script_beside_the_box(
    client: FlaskClient, checks_pass: None
) -> None:
    response = client.post(
        "/settings",
        data={"tab": "preferences", "timezone": "Middle/Earth"},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 400
    assert "not a recognised time zone" in response.json["error"]
    assert prefs.current().timezone == "Europe/Zurich"


def test_a_refused_preference_stays_on_the_tab_it_was_typed_on(
    client: FlaskClient, checks_pass: None
) -> None:
    response = client.post("/settings", data={"tab": "preferences", "timezone": "Middle/Earth"})
    assert response.status_code == 400
    assert "not a recognised time zone" in response.text
    assert 'aria-selected="true"' in response.text
    # The preferences tab is the second button, and it is the one standing selected.
    tabs = re.findall(r'role="tab"[^>]*aria-selected="(\w+)"', response.text)
    assert tabs == ["false", "true"]


def test_a_calendar_is_proved_before_it_is_kept(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        web,
        "check_calendar",
        lambda s, names: CheckResult("calendar", False, "no calendar named 'Gone'"),
    )
    response = client.post(
        "/settings",
        data={"tab": "preferences", "calendar": "Gone"},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 400
    assert response.json["error"] == "Calendar: no calendar named 'Gone'"
    assert prefs.current().calendar == "Bookings"


def test_the_log_level_takes_effect_without_a_restart(
    client: FlaskClient, checks_pass: None, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    import logging

    restarted = []
    monkeypatch.setattr(supervisor, "restart", lambda: restarted.append(True))
    monkeypatch.setattr(logging.getLogger(), "setLevel", lambda level: restarted.append(level))

    # The form carries every switch each time; these are the ones the page drew ticked.
    client.post(
        "/settings",
        data={
            "tab": "preferences",
            "log_level": "DEBUG",
            "notify_events": "true",
            "notify_failures": "true",
        },
    )

    assert restarted == ["DEBUG"]


def test_the_page_offers_every_usable_flag_colour(client: FlaskClient) -> None:
    write_secrets(ICLOUD)
    text = client.get("/settings").text
    for colour in ("orange", "yellow", "green", "blue", "purple", "grey"):
        assert f'value="{colour}"' in text
    assert 'value="red"' not in text


def test_the_calendar_pickers_wait_for_icloud(client: FlaskClient) -> None:
    # Nothing to pick from until there is an account to list them from.
    text = client.get("/settings").text
    assert "Connect iCloud in" in text
    assert "Loading calendars" not in text


def test_the_calendar_options_are_listed_from_the_account(
    client: FlaskClient, calendars: None
) -> None:
    write_secrets(ICLOUD)
    text = client.get("/settings/calendars").text
    assert '<option value="Bookings">Bookings</option>' in text
    assert '<option value="Home">Home</option>' in text


def test_the_calendar_options_say_when_icloud_cannot_be_reached(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_secrets(ICLOUD)

    class Broken:
        def __init__(self, settings: Settings) -> None:
            pass

        def calendars(self) -> dict[str, str]:
            raise RuntimeError("iCloud is down")

    monkeypatch.setattr(web, "CalendarClient", Broken)

    text = client.get("/settings/calendars").text
    assert "data-error" in text
    assert "iCloud could not be reached" in text


# -- categories ----------------------------------------------------------------------


def test_a_category_is_added_one_at_a_time(client: FlaskClient, checks_pass: None) -> None:
    write_secrets(ICLOUD)
    client.post(
        "/settings/categories",
        data={"name": "Travel", "description": "Flights and hotels.", "calendar": "Travel"},
    )
    client.post(
        "/settings/categories",
        data={"name": "music", "description": "Concerts.", "calendar": "Music"},
    )

    assert [(c.name, c.calendar) for c in prefs.current().categories] == [
        ("travel", "Travel"),
        ("music", "Music"),
    ]
    assert "Flights and hotels." in client.get("/settings").text


def test_a_category_is_changed_under_the_name_it_had(
    client: FlaskClient, checks_pass: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        prefs,
        "_current",
        Prefs(
            calendar="Bookings",
            categories=[{"name": "travel", "description": "Flights.", "calendar": "Travel"}],
        ),
    )

    client.post(
        "/settings/categories",
        data={
            "original": "travel",
            "name": "trips",
            "description": "Flights and trains.",
            "calendar": "Travel",
        },
    )

    assert [(c.name, c.description) for c in prefs.current().categories] == [
        ("trips", "Flights and trains.")
    ]


def test_a_category_is_removed(
    client: FlaskClient, checks_pass: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        prefs,
        "_current",
        Prefs(
            calendar="Bookings",
            categories=[{"name": "travel", "description": "Flights.", "calendar": "Travel"}],
        ),
    )

    client.post("/settings/categories", data={"original": "travel", "remove": "1"})

    assert prefs.current().categories == ()


def test_a_category_without_a_calendar_is_refused(client: FlaskClient, checks_pass: None) -> None:
    response = client.post(
        "/settings/categories",
        data={"name": "travel", "description": "Flights.", "calendar": ""},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 400
    assert "calendar" in response.json["error"]
    assert prefs.current().categories == ()


# -- notifications -------------------------------------------------------------------

PUSHOVER = {"pushover_token": "tok", "pushover_user_key": "usr"}


def test_the_notifications_card_waits_for_pushover(client: FlaskClient) -> None:
    text = client.get("/settings").text
    assert "Connect Pushover in" in text
    assert 'id="notifications_enabled"' in text and "disabled" in text


def test_an_unticked_switch_on_the_preferences_form_reads_as_off(
    client: FlaskClient, checks_pass: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_secrets(PUSHOVER)
    monkeypatch.setattr(prefs, "_current", Prefs(calendar="Bookings", notifications_enabled=True))

    # A browser sends nothing at all for an unticked box.
    client.post("/settings", data={"tab": "preferences", "flag_colour": "blue"})

    assert prefs.current().notifications_enabled is False


def test_ticked_switches_are_saved(client: FlaskClient, checks_pass: None) -> None:
    write_secrets(PUSHOVER)
    client.post(
        "/settings",
        data={"tab": "preferences", "notifications_enabled": "true", "notify_events": "true"},
    )
    current = prefs.current()
    assert current.notifications_enabled and current.notify_events
    assert current.notify_failures is False


def test_forgetting_pushover_puts_the_notifications_down(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_secrets(PUSHOVER)
    monkeypatch.setattr(prefs, "_current", Prefs(calendar="Bookings", notifications_enabled=True))

    client.post("/settings/credentials", data={"service": "pushover", "forget": "1"})

    assert prefs.current().notifications_enabled is False


# -- updates -------------------------------------------------------------------------


def test_the_version_row_stands_without_watchtower(client: FlaskClient) -> None:
    body = client.get("/settings").text
    assert "Version" in body
    assert "Check for updates" in body
    assert 'id="update-form"' in body
    assert 'data-watchtower=""' in body


def test_a_connected_watchtower_is_marked_on_the_row(client: FlaskClient) -> None:
    write_secrets({"watchtower_url": "http://watchtower:8080", "watchtower_token": "wt-secret"})
    body = client.get("/settings").text
    assert 'data-watchtower="1"' in body
    # The address is shown back; the token never is.
    assert "http://watchtower:8080" in body
    assert "wt-secret" not in body


def test_the_update_check_says_what_runs_and_what_is_published(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def checked(*, refresh: bool = True, force: bool = False) -> Any:
        assert refresh and force
        return web.updates.UpdateStatus(
            running="a" * 40, latest="b" * 40, checked=1.0, latest_version="v321"
        )

    monkeypatch.setattr(web.updates, "status", checked)
    answer = client.get("/settings/update/check?force=1").json
    assert answer["available"] is True
    assert answer["latest"] == "b" * 40
    # The name a person reads for that build, beside the commit that identifies it.
    assert answer["latest_version"] == "v321"
    # What the poll after an update actually watches: any new image changes it.
    assert answer["build"]


def test_the_poll_stays_off_the_registry(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unchecked(*, refresh: bool = True, force: bool = False) -> Any:
        assert not refresh
        return web.updates.UpdateStatus(running="")

    monkeypatch.setattr(web.updates, "status", unchecked)
    assert client.get("/settings/update/check?poll=1").json["available"] is None


def test_an_update_without_watchtower_is_refused(client: FlaskClient) -> None:
    response = client.post("/settings/update", headers={"Accept": "application/json"})
    assert response.status_code == 400
    assert "Watchtower" in response.json["detail"]


def test_an_update_is_handed_to_watchtower(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_secrets({"watchtower_url": "http://watchtower:8080", "watchtower_token": "secret"})

    def restarting(handed: Settings) -> Any:
        assert handed.watchtower_token == "secret"
        return web.updates.Outcome(True, "Watchtower is updating.", restarting=True)

    monkeypatch.setattr(web.updates, "trigger", restarting)
    answer = client.post("/settings/update", headers={"Accept": "application/json"}).json
    assert answer["restarting"] is True


def test_a_watchtower_refusal_lands_on_the_page_for_a_browser_with_no_script(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_secrets({"watchtower_url": "http://watchtower:8080", "watchtower_token": "secret"})
    monkeypatch.setattr(
        web.updates,
        "trigger",
        lambda handed: web.updates.Outcome(False, "Watchtower rejected the token."),
    )
    response = client.post("/settings/update")
    assert response.status_code == 502
    assert "Watchtower: Watchtower rejected the token." in response.text


# -- the email page ------------------------------------------------------------------


def test_setup_is_incomplete_without_a_calendar(settings: Settings) -> None:
    assert missing_for_start(settings, Prefs()) == ["a calendar"]


def test_setup_names_every_missing_connection_in_order(unconfigured: Settings) -> None:
    assert missing_for_start(unconfigured, Prefs()) == ["iCloud", "Anthropic", "a calendar"]


def test_an_unconfigured_deployment_says_what_to_connect(client: FlaskClient) -> None:
    text = client.get("/").text
    assert "No email read yet" in text
    assert "Connect iCloud and Anthropic in" in text
    assert "to get started" in text


def test_with_the_connections_made_the_page_asks_for_a_calendar(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_secrets({**ICLOUD, "anthropic_api_key": "sk"})
    monkeypatch.setattr(prefs, "_current", Prefs())
    text = client.get("/").text
    assert "Pick a calendar in" in text
    assert "Connect" not in text


def test_the_page_lists_what_is_still_flagged(client: FlaskClient) -> None:
    with store() as db:
        db.record_failure("<a@b>", "Your booking", 3, "nothing to put on a calendar", None)

    text = client.get("/").text

    assert "Needs attention" in text
    assert "Your booking" in text
    assert "nothing to put on a calendar" in text
    assert "Failed" in text
    assert 'href="message://%3Ca@b%3E"' in text


def test_a_message_waiting_on_its_retry_is_marked_so(client: FlaskClient) -> None:
    with store() as db:
        db.record_failure("<a@b>", "Your booking", 1, "boom", 9e12)
    assert "Retrying" in client.get("/").text


def test_the_page_lists_what_reached_the_calendar(client: FlaskClient) -> None:
    with store() as db:
        db.record_event("u1", "<a@b>", "Radiohead at the O2", "2026-09-14T20:00:00+01:00")
        db.record_event("u2", "<c@d>", "Museum entry", "2026-08-22")

    text = client.get("/").text

    assert "Added to calendar" in text
    assert "Radiohead at the O2" in text
    assert "Mon 14 Sep 20:00" in text
    # An all-day event is stored by its day, and said that way.
    assert "Sat 22 Aug" in text
    assert "Sat 22 Aug 00:00" not in text
    # Each row opens the Calendar app on the event's day, aimed at its noon.
    assert text.count('href="calshow:') == 2


def test_retrying_forgets_only_that_failure(client: FlaskClient) -> None:
    with store() as db:
        db.record_failure("<a@b>", "First", 3, "boom", None)
        db.record_failure("<c@d>", "Second", 3, "boom", None)

    response = client.post("/retry", data={"message_id": "<a@b>"})

    assert response.status_code == 303
    with store() as db:
        assert [f.message_id for f in db.list_failures()] == ["<c@d>"]


def test_ignoring_hands_the_unflagging_to_the_watcher(client: FlaskClient) -> None:
    with store() as db:
        db.record_failure("<a@b>", "First", 3, "boom", None)

    client.post("/ignore", data={"message_id": "<a@b>"})

    with store() as db:
        # Off the page at once; the flag comes off on the next pass.
        assert db.list_failures() == []
        assert db.is_dismissed("<a@b>")


def test_a_stopped_watcher_is_said_first(client: FlaskClient, supervisor: Supervisor) -> None:
    supervisor.error = "iCloud rejected the credentials"
    text = client.get("/").text
    assert "Stopped" in text
    assert "iCloud rejected the credentials" in text
    assert 'action="/restart"' in text


def test_healthz_is_ok_while_waiting_for_setup(client: FlaskClient) -> None:
    response = client.get("/healthz")
    # Nothing configured yet is a healthy container, not a crashed one.
    assert response.status_code == 200
    assert "waiting for setup" in response.text


def test_healthz_reports_a_watcher_that_died(client: FlaskClient, supervisor: Supervisor) -> None:
    supervisor.error = "boom"
    assert client.get("/healthz").status_code == 500


def test_an_unknown_page_is_drawn_in_the_layout(client: FlaskClient) -> None:
    response = client.get("/nowhere")
    assert response.status_code == 404
    assert "Back to email" in response.text


def test_the_state_lives_under_data(client: FlaskClient, tmp_path: Path) -> None:
    client.get("/")
    assert (tmp_path / "data" / "state.sqlite").exists()
