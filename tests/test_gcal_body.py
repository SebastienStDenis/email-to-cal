from __future__ import annotations

import re

from email_to_cal.config import Settings
from email_to_cal.gcal import build_event_body, event_id_for
from email_to_cal.schema import EventLocation, ExtractedEvent

GOOGLE_ID_CHARSET = re.compile(r"^[a-v0-9]{5,1024}$")


def flight() -> ExtractedEvent:
    return ExtractedEvent(
        kind="flight",
        title="NH106 HND to LAX",
        description="Confirmation K3TQ9P",
        all_day=False,
        start_local="2026-09-14T18:35:00",
        end_local="2026-09-14T11:25:00",
        departure_iata="HND",
        arrival_iata="LAX",
        booking_reference="K3TQ9P",
        category="travel",
        confidence=0.95,
        reasoning="Booking confirmation with a reservation number.",
    )


def test_event_id_is_google_legal_and_stable() -> None:
    first = event_id_for("<a@b>", flight())
    second = event_id_for("<a@b>", flight())
    assert first == second
    assert GOOGLE_ID_CHARSET.match(first), first


def test_event_id_changes_with_the_event() -> None:
    other = flight()
    other.start_local = "2026-09-15T18:35:00"
    assert event_id_for("<a@b>", flight()) != event_id_for("<a@b>", other)
    assert event_id_for("<a@b>", flight()) != event_id_for("<c@d>", flight())


def test_flight_gets_two_different_timezones(settings: Settings) -> None:
    body = build_event_body(flight(), settings, message_id="<a@b>")
    assert body["start"]["timeZone"] == "Asia/Tokyo"
    assert body["end"]["timeZone"] == "America/Los_Angeles"
    # Both an absolute offset and the IANA name, so the instant is pinned and the
    # rendering is right for whoever looks at it.
    assert body["start"]["dateTime"] == "2026-09-14T18:35:00+09:00"
    assert body["end"]["dateTime"] == "2026-09-14T11:25:00-07:00"
    # Sanity: a westbound transpacific flight really does land "before" it departs.
    assert body["end"]["dateTime"] < body["start"]["dateTime"]


def test_flight_location_is_a_geocodable_airport_address(settings: Settings) -> None:
    body = build_event_body(flight(), settings, message_id="<a@b>")
    # "HND" and "Tokyo Haneda" resolve to nothing on a map; this resolves to the airport.
    assert body["location"] == "Tokyo International Airport, Tokyo, JP"


def test_all_day_uses_exclusive_end_date(settings: Settings) -> None:
    event = ExtractedEvent(
        kind="other",
        title="Museum entry",
        all_day=True,
        start_local="2026-08-22",
        confidence=0.9,
        reasoning="Ticket purchased.",
    )
    body = build_event_body(event, settings, message_id="<a@b>")
    assert body["start"] == {"date": "2026-08-22"}
    assert body["end"] == {"date": "2026-08-23"}
    assert "timeZone" not in body["start"]


def test_multi_day_all_day_extends_past_the_last_day(settings: Settings) -> None:
    event = ExtractedEvent(
        kind="hotel",
        title="Kong Arthur",
        all_day=True,
        start_local="2026-08-21",
        end_local="2026-08-23",
        confidence=0.9,
        reasoning="Booking confirmed.",
    )
    body = build_event_body(event, settings, message_id="<a@b>")
    assert body["start"]["date"] == "2026-08-21"
    assert body["end"]["date"] == "2026-08-23"


def test_missing_end_gets_a_one_hour_default(settings: Settings) -> None:
    event = ExtractedEvent(
        kind="restaurant",
        title="Kadeau",
        location=EventLocation(locality="Copenhagen"),
        all_day=False,
        start_local="2026-08-22T19:30:00",
        confidence=0.9,
        reasoning="Reservation confirmed.",
    )
    body = build_event_body(event, settings, message_id="<a@b>")
    assert body["start"]["dateTime"].startswith("2026-08-22T19:30:00")
    assert body["end"]["dateTime"].startswith("2026-08-22T20:30:00")
    assert body["start"]["timeZone"] == "Europe/Copenhagen"


def test_source_message_is_recorded_for_later_reconciliation(settings: Settings) -> None:
    body = build_event_body(flight(), settings, message_id="<a@b>")
    private = body["extendedProperties"]["private"]
    assert private["e2c_msg_id"] == "<a@b>"
    # Google silently drops private property keys longer than 44 characters.
    assert all(len(key) <= 44 for key in private)
    assert "K3TQ9P" in body["description"]


def test_description_links_to_the_source_email(settings: Settings) -> None:
    body = build_event_body(flight(), settings, message_id="<a@b>")
    assert "Open in Apple Mail: message://%3Ca@b%3E" in body["description"]
    assert "<a@b>" not in body["description"]


def test_synthetic_message_ids_get_no_mail_link(settings: Settings) -> None:
    body = build_event_body(flight(), settings, message_id="<sha256-0f@email-to-cal.local>")
    assert "message://" not in body["description"]


def test_unresolvable_location_uses_the_configured_default(settings: Settings) -> None:
    event = ExtractedEvent(
        kind="appointment",
        title="Dentist",
        location=EventLocation(name="Praxis Dr. Müller"),
        all_day=False,
        start_local="2026-08-22T09:00:00",
        confidence=0.9,
        reasoning="Appointment reminder.",
    )
    body = build_event_body(event, settings, message_id="<a@b>")
    assert body["start"]["timeZone"] == "Europe/Zurich"
