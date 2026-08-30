"""CalDAV: find the calendar, and write events to it.

Four requests are all CalDAV needs for this: discover the principal, ask it where the
calendars live, list them, and PUT an event. Apple's app-specific password is the only
credential, and it is the same one the mailbox uses.

Writes are idempotent by construction. Each event gets a deterministic UID derived from
the source message, the UID names the resource on the server, and a PUT to the same
resource replaces it - so re-processing an email rewrites its events instead of
duplicating them.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from urllib.parse import quote, urljoin
from xml.etree import ElementTree

import httpx
from icalendar import Calendar, Event

from .config import Settings
from .mime import SYNTHETIC_ID_DOMAIN
from .places import resolve_address
from .prefs import Prefs
from .schema import ExtractedEvent
from .timezones import localise, parse_naive, resolve_zones

log = logging.getLogger(__name__)

CALDAV_URL = "https://caldav.icloud.com"

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
# Apple counts time from 2001-01-01 UTC, which is what `calshow:` links are measured in.
APPLE_EPOCH_OFFSET = 978307200

_PRINCIPAL_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>'
)
_HOME_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    "<d:prop><c:calendar-home-set/></d:prop></d:propfind>"
)
_CALENDARS_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    "<d:prop><d:displayname/><d:resourcetype/>"
    "<c:supported-calendar-component-set/></d:prop></d:propfind>"
)


class CalendarUnavailable(RuntimeError):
    """The calendar server refused us, or the configured calendar is not there."""


def event_uid(message_id: str, event: ExtractedEvent) -> str:
    """A stable UID derived from the source message and the event's identity.

    The same email always maps to the same UID, so a message processed twice - a retry,
    or a flag set again by hand - overwrites its events rather than duplicating them.
    """
    fingerprint = "|".join(
        [
            message_id,
            event.kind,
            event.title.strip().lower(),
            event.start_local,
            event.end_local or "",
            event.departure_iata or "",
            event.arrival_iata or "",
        ]
    )
    return f"{hashlib.sha256(fingerprint.encode()).hexdigest()[:40]}@email-to-cal"


def mail_link(message_id: str) -> str | None:
    """An Apple Mail deep link to the source message.

    Message-IDs we invented ourselves name no real message, so they get no link.
    """
    if message_id.endswith(f"@{SYNTHETIC_ID_DOMAIN}>"):
        return None
    return "message://" + quote(message_id, safe="@")


@dataclass
class BuiltEvent:
    """One event rendered for the server, and the facts a notification needs about it."""

    uid: str
    ics: bytes
    title: str
    # The calendar this event is routed to, by display name.
    calendar: str
    # Always an aware instant, so it can be linked to and sorted; local midnight for
    # all-day events.
    starts_at: datetime
    all_day: bool

    def describe(self) -> str:
        """One line naming the event and when it starts, for pushes and the portal."""
        stamp = f"{self.starts_at:%a %d %b}" if self.all_day else f"{self.starts_at:%a %d %b %H:%M}"
        return f"{self.title} - {stamp}"

    @property
    def calendar_link(self) -> str:
        """A link that opens the Calendar app on the day of the event.

        Apple offers no scheme for one specific event, only `calshow:`, which takes an
        instant - counted from Apple's 2001 epoch - that the phone then renders in
        whatever zone it is currently in. Aiming at noon where the event happens keeps
        the date right even when the phone is a good half-day away from it.
        """
        noon = self.starts_at.replace(hour=12, minute=0, second=0, microsecond=0)
        return f"calshow:{int(noon.timestamp() - APPLE_EPOCH_OFFSET)}"


def build_ical(
    event: ExtractedEvent, prefs: Prefs, *, message_id: str, calendar: str = ""
) -> BuiltEvent:
    """Render one extracted event as a single-event iCalendar object."""
    address = resolve_address(event)
    start_zone, end_zone = resolve_zones(
        start_tz=event.start_tz,
        end_tz=event.end_tz,
        departure_iata=event.departure_iata,
        arrival_iata=event.arrival_iata,
        location=address,
        default_timezone=prefs.timezone,
    )

    start_value = parse_naive(event.start_local)
    end_value = parse_naive(event.end_local) if event.end_local else None

    entry = Event()
    uid = event_uid(message_id, event)
    entry.add("uid", uid)
    entry.add("dtstamp", datetime.now(UTC))
    entry.add("summary", event.title)

    # A model that emits "2026-08-22" has said all-day whatever the flag claims. Note
    # datetime subclasses date, so the order of these checks matters.
    date_only = not isinstance(start_value, datetime)

    if event.all_day or date_only:
        start_date = start_value.date() if isinstance(start_value, datetime) else start_value
        if isinstance(end_value, datetime):
            end_date = end_value.date()
        elif end_value is not None:
            end_date = end_value
        else:
            end_date = start_date
        # DTEND is exclusive for date values, so a single-day event ends the next morning.
        if end_date <= start_date:
            end_date = start_date + timedelta(days=1)
        entry.add("dtstart", start_date)
        entry.add("dtend", end_date)
        # An all-day event still needs one real instant to be linked to: its own morning,
        # where the event happens rather than where the server is.
        starts_at = localise(datetime.combine(start_date, time()), start_zone)
    else:
        assert isinstance(start_value, datetime)
        start_dt = localise(start_value, start_zone)
        if isinstance(end_value, datetime):
            end_dt = localise(end_value, end_zone)
        else:
            end_dt = localise(start_value + timedelta(hours=1), start_zone)
        if end_dt <= start_dt:
            end_dt = start_dt + timedelta(hours=1)
        entry.add("dtstart", start_dt)
        entry.add("dtend", end_dt)
        starts_at = start_dt

    description_lines = []
    if event.description:
        description_lines.append(event.description)
    if event.booking_reference:
        description_lines.append(f"Booking reference: {event.booking_reference}")
    link = mail_link(message_id)
    if link:
        description_lines.append(f"Open in Apple Mail: {link}")
        entry.add("url", link)
    if description_lines:
        entry.add("description", "\n\n".join(description_lines))

    if address:
        entry.add("location", address)

    document = Calendar()
    document.add("prodid", "-//email-to-cal//EN")
    document.add("version", "2.0")
    document.add_component(entry)
    # A TZID reference is meaningless to a client without the VTIMEZONE that defines it.
    document.add_missing_timezones()
    return BuiltEvent(
        uid=uid,
        ics=document.to_ical(),
        title=event.title,
        calendar=calendar,
        starts_at=starts_at,
        all_day=event.all_day or date_only,
    )


class CalendarClient:
    """A CalDAV account, resolved down to the one calendar events are written to."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.Client(
            auth=(settings.icloud_email, settings.icloud_app_password),
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": "email-to-cal"},
        )

    def _propfind(self, url: str, body: str, depth: str) -> tuple[ElementTree.Element, str]:
        """A PROPFIND's parsed body, plus the URL it was finally answered from.

        Relative hrefs resolve against the answering URL, which is not the requested one
        when the server redirects the account onto its own partition.
        """
        try:
            response = self._client.request(
                "PROPFIND",
                url,
                content=body.encode(),
                headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
            )
        except httpx.HTTPError as exc:
            raise CalendarUnavailable(f"cannot reach {url}: {exc}") from exc
        if response.status_code in (401, 403):
            raise CalendarUnavailable(
                "the calendar server rejected the Apple ID and app-specific password. "
                "Changing the Apple ID password revokes every app-specific password."
            )
        if response.status_code != 207:
            raise CalendarUnavailable(
                f"{url} answered {response.status_code}: {response.text[:300]}"
            )
        return ElementTree.fromstring(response.content), str(response.url)

    def _find_href(self, url: str, body: str, prop: str) -> str:
        """The single href a discovery PROPFIND returns, as an absolute URL."""
        root, answered_from = self._propfind(url, body, depth="0")
        element = root.find(f".//{prop}/{{{DAV}}}href")
        if element is None or not element.text:
            raise CalendarUnavailable(f"{url} returned no {prop}")
        return urljoin(answered_from, element.text)

    def calendars(self) -> dict[str, str]:
        """Every writable calendar that holds events, by display name."""
        principal = self._find_href(CALDAV_URL, _PRINCIPAL_BODY, f"{{{DAV}}}current-user-principal")
        home = self._find_href(principal, _HOME_BODY, f"{{{CALDAV}}}calendar-home-set")

        found: dict[str, str] = {}
        root, answered_from = self._propfind(home, _CALENDARS_BODY, depth="1")
        for response in root.findall(f"{{{DAV}}}response"):
            href = response.find(f"{{{DAV}}}href")
            resourcetype = response.find(f".//{{{DAV}}}resourcetype")
            if href is None or href.text is None or resourcetype is None:
                continue
            if resourcetype.find(f"{{{CALDAV}}}calendar") is None:
                continue
            components = {
                str(node.get("name")) for node in response.findall(f".//{{{CALDAV}}}comp")
            }
            # Reminder lists and subscribed feeds live alongside calendars in the same
            # collection and would fail, confusingly, only at the moment of writing.
            if components and "VEVENT" not in components:
                continue
            name = response.find(f".//{{{DAV}}}displayname")
            label = (name.text or "").strip() if name is not None else ""
            if label:
                found[label] = urljoin(answered_from, href.text)
        return found

    def resolve(self, names: Iterable[str]) -> dict[str, str]:
        """The collection URL for each configured calendar name.

        Every name is resolved against one discovery pass, and a name that matches
        nothing fails here rather than on the first email that routes to it.
        """
        available = self.calendars()
        by_label = {label.strip().lower(): url for label, url in available.items()}

        resolved: dict[str, str] = {}
        missing: list[str] = []
        for name in names:
            url = by_label.get(name.strip().lower())
            if url is None:
                missing.append(name)
            else:
                resolved[name] = url

        if missing:
            listed = ", ".join(sorted(available)) or "none"
            raise CalendarUnavailable(
                f"no calendar named {', '.join(repr(name) for name in sorted(missing))}; "
                f"this account has: {listed}"
            )
        return resolved

    def put(self, calendar_url: str, uid: str, ics: bytes) -> None:
        """Write one event, replacing any earlier version of itself."""
        url = urljoin(calendar_url.rstrip("/") + "/", f"{quote(uid, safe='@')}.ics")
        try:
            response = self._client.put(
                url, content=ics, headers={"Content-Type": "text/calendar; charset=utf-8"}
            )
        except httpx.HTTPError as exc:
            raise CalendarUnavailable(f"cannot reach {url}: {exc}") from exc
        if response.status_code not in (200, 201, 204):
            raise CalendarUnavailable(
                f"writing {uid} answered {response.status_code}: {response.text[:300]}"
            )
