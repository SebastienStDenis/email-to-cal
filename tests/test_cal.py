from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from email_to_cal.cal import CalendarClient, CalendarUnavailable, build_ical, event_uid
from email_to_cal.config import Settings
from email_to_cal.schema import EventLocation, ExtractedEvent


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
    )


def ical_text(event: ExtractedEvent, settings: Settings, message_id: str = "<a@b>") -> str:
    # Folded lines would split property values mid-word and defeat plain substring checks.
    return build_ical(event, settings, message_id=message_id).ics.decode().replace("\r\n ", "")


def test_uid_is_stable_and_tracks_the_event() -> None:
    assert event_uid("<a@b>", flight()) == event_uid("<a@b>", flight())

    later = flight()
    later.start_local = "2026-09-15T18:35:00"
    assert event_uid("<a@b>", later) != event_uid("<a@b>", flight())
    assert event_uid("<c@d>", flight()) != event_uid("<a@b>", flight())


def test_flight_gets_each_leg_in_its_own_timezone(settings: Settings) -> None:
    text = ical_text(flight(), settings)
    assert "DTSTART;TZID=Asia/Tokyo:20260914T183500" in text
    assert "DTEND;TZID=America/Los_Angeles:20260914T112500" in text
    # A TZID nobody defined renders as UTC in most clients, which silently moves the event.
    assert "TZID:Asia/Tokyo" in text
    assert "TZID:America/Los_Angeles" in text


def test_flight_location_is_a_geocodable_airport_address(settings: Settings) -> None:
    # "HND" and "Tokyo Haneda" resolve to nothing on a map; this resolves to the airport.
    # Commas inside a TEXT value are escaped, which is how a client tells them from the
    # separators between several values.
    assert r"LOCATION:Tokyo International Airport\, Tokyo\, JP" in ical_text(flight(), settings)


def test_all_day_event_ends_the_next_morning(settings: Settings) -> None:
    event = ExtractedEvent(
        kind="other", title="Museum entry", all_day=True, start_local="2026-08-22"
    )
    text = ical_text(event, settings)
    # DTEND is exclusive for dates: a same-day end would give a zero-length event.
    assert "DTSTART;VALUE=DATE:20260822" in text
    assert "DTEND;VALUE=DATE:20260823" in text


def test_a_date_only_start_is_all_day_whatever_the_flag_says(settings: Settings) -> None:
    event = ExtractedEvent(kind="other", title="Festival", all_day=False, start_local="2026-08-22")
    assert "DTSTART;VALUE=DATE:20260822" in ical_text(event, settings)


def test_missing_end_gets_a_one_hour_default(settings: Settings) -> None:
    event = ExtractedEvent(
        kind="restaurant",
        title="Dinner at Kadeau",
        all_day=False,
        start_local="2026-08-22T19:30:00",
        start_tz="Europe/Copenhagen",
    )
    text = ical_text(event, settings)
    assert "DTSTART;TZID=Europe/Copenhagen:20260822T193000" in text
    assert "DTEND;TZID=Europe/Copenhagen:20260822T203000" in text


def test_the_event_links_back_to_the_email(settings: Settings) -> None:
    text = ical_text(flight(), settings, message_id="<abc@mail.example>")
    assert "URL:message://%3Cabc@mail.example%3E" in text
    assert "Open in Apple Mail: message://%3Cabc@mail.example%3E" in text
    assert "Booking reference: K3TQ9P" in text


def test_invented_message_ids_get_no_mail_link(settings: Settings) -> None:
    # A synthetic id names no message, so a link built from it would open nothing.
    text = ical_text(flight(), settings, message_id="<x@email-to-cal.local>")
    assert "URL:" not in text
    assert "message://" not in text


def test_location_without_an_address_still_names_the_venue(settings: Settings) -> None:
    event = ExtractedEvent(
        kind="concert",
        title="Radiohead",
        all_day=False,
        start_local="2026-08-22T20:00:00",
        location=EventLocation(name="The O2 Arena", locality="London", country="GB"),
    )
    assert r"LOCATION:The O2 Arena\, London\, GB" in ical_text(event, settings)


def test_notification_link_opens_the_calendar_on_the_day(settings: Settings) -> None:
    built = build_ical(flight(), settings, message_id="<a@b>")
    assert built.starts_at.isoformat() == "2026-09-14T18:35:00+09:00"
    assert built.describe() == "NH106 HND to LAX - Mon 14 Sep 18:35"

    # calshow takes an instant, which the phone renders in whatever zone it is in. Noon
    # where the event happens survives that; the 18:35 start would show as the 13th on a
    # phone in California.
    noon_in_tokyo = datetime(2026, 9, 14, 12, tzinfo=ZoneInfo("Asia/Tokyo"))
    assert built.calendar_link == f"calshow:{int(noon_in_tokyo.timestamp() - 978307200)}"


def test_an_all_day_event_is_linked_by_its_own_morning(settings: Settings) -> None:
    event = ExtractedEvent(
        kind="other", title="Museum entry", all_day=True, start_local="2026-08-22"
    )
    built = build_ical(event, settings, message_id="<a@b>")
    # Local midnight where the event is, not the server's - otherwise the link can
    # land on the day before.
    assert built.starts_at.isoformat() == "2026-08-22T00:00:00+02:00"
    assert built.describe() == "Museum entry - Sat 22 Aug"


# -- the CalDAV client ---------------------------------------------------------------

MULTISTATUS_PRINCIPAL = """<?xml version="1.0"?>
<multistatus xmlns="DAV:"><response><href>/</href><propstat><prop>
<current-user-principal><href>/1234/principal/</href></current-user-principal>
</prop></propstat></response></multistatus>"""

MULTISTATUS_HOME = """<?xml version="1.0"?>
<multistatus xmlns="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<response><href>/1234/principal/</href><propstat><prop>
<c:calendar-home-set><href>https://p01-caldav.icloud.com/1234/calendars/</href></c:calendar-home-set>
</prop></propstat></response></multistatus>"""

MULTISTATUS_CALENDARS = """<?xml version="1.0"?>
<multistatus xmlns="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<response><href>/1234/calendars/home/</href><propstat><prop>
<displayname>Bookings</displayname>
<resourcetype><collection/><c:calendar/></resourcetype>
<c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set>
</prop></propstat></response>
<response><href>/1234/calendars/tasks/</href><propstat><prop>
<displayname>Reminders</displayname>
<resourcetype><collection/><c:calendar/></resourcetype>
<c:supported-calendar-component-set><c:comp name="VTODO"/></c:supported-calendar-component-set>
</prop></propstat></response>
<response><href>/1234/calendars/inbox/</href><propstat><prop>
<displayname>Inbox</displayname>
<resourcetype><collection/></resourcetype>
</prop></propstat></response>
</multistatus>"""


def caldav_client(settings: Settings, requests: list[httpx.Request]) -> CalendarClient:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "PUT":
            return httpx.Response(201)
        body = {
            "/": MULTISTATUS_PRINCIPAL,
            "/1234/principal/": MULTISTATUS_HOME,
            "/1234/calendars/": MULTISTATUS_CALENDARS,
        }[request.url.path]
        return httpx.Response(207, text=body)

    return CalendarClient(
        settings, client=httpx.Client(transport=httpx.MockTransport(handle), auth=("u", "p"))
    )


def test_discovery_walks_principal_then_home_then_calendars(settings: Settings) -> None:
    requests: list[httpx.Request] = []
    found = caldav_client(settings, requests).calendars()

    assert [str(r.url) for r in requests] == [
        "https://caldav.icloud.com",
        "https://caldav.icloud.com/1234/principal/",
        "https://p01-caldav.icloud.com/1234/calendars/",
    ]
    # Reminder lists and the scheduling inbox are collections too, and writing an event
    # to one fails only at the moment it matters.
    assert found == {"Bookings": "https://p01-caldav.icloud.com/1234/calendars/home/"}


def test_resolve_matches_the_name_case_insensitively(settings: Settings) -> None:
    settings.calendar_name = "bookings"
    url = caldav_client(settings, []).resolve(settings.calendar_name)
    assert url == "https://p01-caldav.icloud.com/1234/calendars/home/"


def test_a_missing_calendar_names_the_ones_that_exist(settings: Settings) -> None:
    with pytest.raises(CalendarUnavailable, match="this account has: Bookings"):
        caldav_client(settings, []).resolve("Holidays")


def test_writing_an_event_puts_it_under_its_own_uid(settings: Settings) -> None:
    requests: list[httpx.Request] = []
    client = caldav_client(settings, requests)
    built = build_ical(flight(), settings, message_id="<a@b>")

    client.put("https://p01-caldav.icloud.com/1234/calendars/home/", built.uid, built.ics)

    put = requests[-1]
    assert put.method == "PUT"
    # The UID names the resource, so processing the same email twice replaces the event
    # rather than adding a second one.
    assert str(put.url).endswith(f"/{built.uid}.ics")
    assert put.headers["Content-Type"] == "text/calendar; charset=utf-8"


def test_a_rejected_password_says_so_rather_than_retrying(settings: Settings) -> None:
    client = CalendarClient(
        settings,
        client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(401))),
    )
    with pytest.raises(CalendarUnavailable, match="app-specific password"):
        client.calendars()
