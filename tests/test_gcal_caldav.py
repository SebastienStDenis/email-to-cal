from __future__ import annotations

from typing import Any

import pytest

from email_to_cal.config import Settings
from email_to_cal.gcal import CalendarClient, fold_ical_line, with_url_property
from email_to_cal.store import Store

SERVED = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:abc@google.com\r\n"
    "SUMMARY:NH106 HND to LAX\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


class FakeResponse:
    def __init__(self, text: str = "", status: int = 200) -> None:
        self.text = text
        self.status = status

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class FakeSession:
    def __init__(self, served: str = SERVED, put_status: int = 204) -> None:
        self._served = served
        self._put_status = put_status
        self.gets: list[str] = []
        self.puts: list[tuple[str, bytes]] = []

    def get(self, href: str, **kwargs: Any) -> FakeResponse:
        self.gets.append(href)
        return FakeResponse(self._served)

    def put(self, href: str, data: bytes = b"", **kwargs: Any) -> FakeResponse:
        self.puts.append((href, data))
        return FakeResponse(status=self._put_status)


class FakeRequest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def execute(self) -> dict[str, Any]:
        return self._payload


class FakeService:
    """events().insert().execute(), plus calendars().get() for the primary alias."""

    def __init__(self) -> None:
        self.inserted: list[dict[str, Any]] = []

    def events(self) -> FakeService:
        return self

    def calendars(self) -> FakeService:
        return self

    def insert(self, calendarId: str, body: dict[str, Any]) -> FakeRequest:
        self.inserted.append(body)
        return FakeRequest({"id": body["id"], "iCalUID": f"{body['id']}@google.com"})

    def get(self, calendarId: str) -> FakeRequest:
        return FakeRequest({"id": "owner@gmail.com"})


@pytest.fixture
def client(settings: Settings) -> tuple[CalendarClient, FakeService, FakeSession]:
    service, session = FakeService(), FakeSession()
    return (
        CalendarClient(settings, Store(settings.state_db), service=service, caldav_session=session),
        service,
        session,
    )


def test_a_url_property_is_spliced_into_the_served_event() -> None:
    written = with_url_property(SERVED, "message://%3Ca@b%3E")
    assert "URL:message://%3Ca@b%3E\r\nEND:VEVENT" in written
    assert "SUMMARY:NH106 HND to LAX" in written
    assert written.count("BEGIN:VEVENT") == 1


def test_long_urls_are_folded_at_the_ical_line_limit() -> None:
    line = f"URL:message://{'a' * 200}"
    folded = fold_ical_line(line)
    assert all(len(part.encode()) <= 75 for part in folded.split("\r\n"))
    assert folded.startswith("URL:message://")
    assert "\r\n " in folded
    assert folded.replace("\r\n ", "") == line


def test_short_lines_are_left_alone() -> None:
    assert fold_ical_line("URL:message://%3Ca@b%3E") == "URL:message://%3Ca@b%3E"


def test_insert_attaches_the_link_over_caldav(
    client: tuple[CalendarClient, FakeService, FakeSession],
) -> None:
    calendar, _, session = client
    event_id = calendar.insert("cal@group.calendar.google.com", {"id": "abc"}, url="message://x")

    assert event_id == "abc"
    (href, payload) = session.puts[0]
    assert href.endswith("/cal%40group.calendar.google.com/events/abc@google.com.ics")
    assert b"URL:message://x" in payload
    assert b"SUMMARY:NH106 HND to LAX" in payload


def test_the_primary_alias_is_resolved_to_a_real_calendar_id(
    client: tuple[CalendarClient, FakeService, FakeSession],
) -> None:
    calendar, _, session = client
    calendar.insert("primary", {"id": "abc"}, url="message://x")

    assert "/owner%40gmail.com/events/" in session.puts[0][0]


def test_an_event_without_a_link_is_left_untouched(
    client: tuple[CalendarClient, FakeService, FakeSession],
) -> None:
    calendar, _, session = client
    calendar.insert("cal", {"id": "abc"})

    assert session.gets == []
    assert session.puts == []


def test_a_failed_attach_does_not_fail_the_event(settings: Settings) -> None:
    session = FakeSession(put_status=500)
    calendar = CalendarClient(
        settings, Store(settings.state_db), service=FakeService(), caldav_session=session
    )

    assert calendar.insert("cal", {"id": "abc"}, url="message://x") == "abc"
