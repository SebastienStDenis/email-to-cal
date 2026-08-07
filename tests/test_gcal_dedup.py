from __future__ import annotations

from typing import Any

from email_to_cal.config import Settings
from email_to_cal.gcal import CalendarClient, build_event_body, titles_match
from email_to_cal.schema import ExtractedEvent
from email_to_cal.store import Store

from .test_gcal_body import flight


class FakeRequest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def execute(self) -> dict[str, Any]:
        return self._payload


class FakeService:
    """Just enough of the discovery client for find_similar: events().list().execute()."""

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = items
        self.queries: list[dict[str, Any]] = []

    def events(self) -> FakeService:
        return self

    def list(self, **kwargs: Any) -> FakeRequest:
        self.queries.append(kwargs)
        return FakeRequest({"items": self._items})


def client_with(settings: Settings, items: list[dict[str, Any]]) -> CalendarClient:
    return CalendarClient(settings, Store(settings.state_db), service=FakeService(items))


def test_titles_match_survives_restatement() -> None:
    assert titles_match("NH106 HND to LAX", "Flight NH106 HND to LAX")
    assert titles_match("Radiohead at The O2", "Radiohead at the O2 Arena")
    assert titles_match("LX318 ZRH to LHR", "LX318: ZRH-LHR")
    assert not titles_match("Dinner at Nobu", "NH106 HND to LAX")
    assert not titles_match("", "NH106 HND to LAX")


def test_a_restated_flight_is_found(settings: Settings) -> None:
    body = build_event_body(flight(), settings, message_id="<a@b>")
    twin = {
        "id": "other1",
        "summary": "Flight NH106 HND to LAX",
        "start": {"dateTime": "2026-09-14T19:05:00+09:00"},
    }
    client = client_with(settings, [twin])

    assert client.find_similar("cal", body) == twin


def test_the_same_booking_reference_matches_despite_the_title(settings: Settings) -> None:
    body = build_event_body(flight(), settings, message_id="<a@b>")
    twin = {
        "id": "other1",
        "summary": "ANA reservation",
        "description": "Booking reference: K3TQ9P",
        "start": {"dateTime": "2026-09-14T18:35:00+09:00"},
    }
    client = client_with(settings, [twin])

    assert client.find_similar("cal", body, booking_reference="K3TQ9P") == twin
    assert client.find_similar("cal", body) is None


def test_the_same_event_id_is_not_a_twin(settings: Settings) -> None:
    """That id is this very email again; insert() already treats it as success."""
    body = build_event_body(flight(), settings, message_id="<a@b>")
    client = client_with(
        settings,
        [{"id": body["id"], "summary": body["summary"], "start": dict(body["start"])}],
    )

    assert client.find_similar("cal", body) is None


def test_an_overlapping_event_outside_the_window_is_not_a_twin(settings: Settings) -> None:
    """The list call returns anything overlapping the window, such as a long event
    that started hours earlier; only a nearby start counts."""
    body = build_event_body(flight(), settings, message_id="<a@b>")
    client = client_with(
        settings,
        [
            {
                "id": "other1",
                "summary": "NH106 HND to LAX",
                "start": {"dateTime": "2026-09-14T13:35:00+09:00"},
            }
        ],
    )

    assert client.find_similar("cal", body) is None


def test_all_day_twins_match_on_the_same_date_only(settings: Settings) -> None:
    stay = ExtractedEvent(
        kind="hotel",
        title="Kong Arthur",
        all_day=True,
        start_local="2026-08-21",
        end_local="2026-08-23",
        confidence=0.9,
        reasoning="Booking confirmed.",
    )
    body = build_event_body(stay, settings, message_id="<a@b>")
    same_day = {"id": "other1", "summary": "Hotel Kong Arthur", "start": {"date": "2026-08-21"}}
    other_day = {"id": "other2", "summary": "Hotel Kong Arthur", "start": {"date": "2026-08-22"}}

    assert client_with(settings, [same_day]).find_similar("cal", body) == same_day
    assert client_with(settings, [other_day]).find_similar("cal", body) is None


def test_the_query_brackets_the_start_by_the_window(settings: Settings) -> None:
    body = build_event_body(flight(), settings, message_id="<a@b>")
    service = FakeService([])
    client = CalendarClient(settings, Store(settings.state_db), service=service)

    client.find_similar("cal", body)

    (query,) = service.queries
    assert query["calendarId"] == "cal"
    assert query["timeMin"] == "2026-09-14T17:35:00+09:00"
    assert query["timeMax"] == "2026-09-14T19:35:00+09:00"
    assert query["singleEvents"] is True
