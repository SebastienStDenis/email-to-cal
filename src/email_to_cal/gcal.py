"""Google Calendar: auth, calendar resolution, and idempotent event insertion."""

from __future__ import annotations

import hashlib
import logging
import random
import time
from datetime import UTC, date, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import quote

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import Settings
from .mime import SYNTHETIC_ID_DOMAIN
from .places import resolve_address
from .schema import ExtractedEvent
from .store import Store
from .timezones import localise, parse_naive, resolve_zones

log = logging.getLogger(__name__)

# Full calendar scope: creating a missing calendar needs calendars.insert, and routing
# needs calendarList.list. See the README for the narrower calendar.app.created setup.
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# The JSON API refuses any event source url outside http(s), so the Apple Mail deep
# link goes in over CalDAV, which stores the iCalendar URL property verbatim. Needs the
# CalDAV API enabled on the same Cloud project as the Calendar API.
CALDAV_BASE = "https://apidata.googleusercontent.com/caldav/v2"
CALDAV_TIMEOUT = 30

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# Google overloads 403 for both throttling and permanent permission failures.
_RETRYABLE_403_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}


class CredentialsExpired(RuntimeError):
    """The refresh token is dead and only a human can fix it."""


def _is_retryable(exc: HttpError) -> bool:
    """A missing scope also arrives as 403; sleeping through five attempts hides it."""
    status = exc.resp.status
    if status in _RETRYABLE_STATUS:
        return True
    if status != 403:
        return False
    details = exc.error_details if isinstance(exc.error_details, list) else []
    return any(isinstance(d, dict) and d.get("reason") in _RETRYABLE_403_REASONS for d in details)


def load_credentials(settings: Settings) -> Credentials:
    token_file = settings.google_token_file
    if not token_file.exists():
        raise CredentialsExpired(f"no Google token at {token_file}; connect Google in the portal")
    creds: Credentials = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise CredentialsExpired(
                "Google refused the refresh token. If the OAuth app is still in 'Testing' "
                "publishing status its tokens expire after 7 days: set it to 'In production' "
                "and re-authorise."
            ) from exc
        token_file.write_text(creds.to_json())
        return creds
    raise CredentialsExpired(
        "stored Google credentials are unusable; reconnect Google in the portal"
    )


def client_config(settings: Settings) -> dict[str, Any]:
    """The OAuth client description the consent flow expects, built from the two
    configured strings."""
    return {
        "installed": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def save_token(settings: Settings, creds: Credentials) -> None:
    token_file = settings.google_token_file
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json())
    token_file.chmod(0o600)


_TITLE_SIMILARITY = 0.75


def _fold_title(title: str) -> str:
    return " ".join("".join(c if c.isalnum() else " " for c in title.lower()).split())


def titles_match(a: str, b: str) -> bool:
    """Whether two titles plausibly name the same event.

    Restatements keep most of the wording ("LX318 ZRH to LHR" vs "Flight LX318
    ZRH-LHR"), so containment or a high similarity ratio after folding case and
    punctuation is enough; the caller has already required the same time slot.
    """
    fa, fb = _fold_title(a), _fold_title(b)
    if not fa or not fb:
        return False
    if fa in fb or fb in fa:
        return True
    return SequenceMatcher(None, fa, fb).ratio() >= _TITLE_SIMILARITY


def event_id_for(message_id: str, event: ExtractedEvent) -> str:
    """A stable id derived from the source message and the event's identity.

    Google allows base32hex characters (a-v, 0-9) and 5-1024 of them; a lowercase hex
    digest is a strict subset, so the same email always maps to the same event and a
    re-delivery is a no-op rather than a duplicate.
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
    return hashlib.sha256(fingerprint.encode()).hexdigest()[:40]


def mail_link(message_id: str) -> str | None:
    """An Apple Mail deep link to the source message.

    Synthetic ids name no real message.
    """
    if message_id.endswith(f"@{SYNTHETIC_ID_DOMAIN}>"):
        return None
    return "message://" + quote(message_id, safe="@")


def fold_ical_line(line: str) -> str:
    """Split a content line at the 75-octet limit RFC 5545 sets, continuing with a space."""
    octets = line.encode()
    if len(octets) <= 75:
        return line
    chunks = [octets[:75]]
    rest = octets[75:]
    while rest:
        chunks.append(rest[:74])
        rest = rest[74:]
    return "\r\n ".join(chunk.decode() for chunk in chunks)


def with_url_property(ics: str, url: str) -> str:
    """The served calendar object with a URL property on its event."""
    lines = ics.replace("\r\n", "\n").split("\n")
    out = []
    for line in lines:
        if line.startswith("END:VEVENT"):
            out.append(fold_ical_line(f"URL:{url}"))
        out.append(line)
    return "\r\n".join(out)


def build_event_body(
    event: ExtractedEvent,
    settings: Settings,
    *,
    message_id: str,
) -> dict[str, Any]:
    """Render one extracted event as a Google Calendar event resource."""
    address = resolve_address(event)
    start_zone, end_zone = resolve_zones(
        start_tz=event.start_tz,
        end_tz=event.end_tz,
        departure_iata=event.departure_iata,
        arrival_iata=event.arrival_iata,
        location=address,
        default_timezone=settings.default_timezone,
    )

    start_value = parse_naive(event.start_local)
    end_value = parse_naive(event.end_local) if event.end_local else None

    body: dict[str, Any] = {
        "id": event_id_for(message_id, event),
        "summary": event.title,
        "extendedProperties": {
            "private": {
                "e2c_msg_id": message_id[:1024],
                "e2c_kind": event.kind,
            }
        },
    }

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
        # Google treats end.date as exclusive, so a single-day event ends the next morning.
        if end_date <= start_date:
            end_date = start_date + timedelta(days=1)
        body["start"] = {"date": start_date.isoformat()}
        body["end"] = {"date": end_date.isoformat()}
    else:
        assert isinstance(start_value, datetime)
        start_dt = localise(start_value, start_zone)
        if isinstance(end_value, datetime):
            end_dt = localise(end_value, end_zone)
        else:
            end_dt = localise(start_value + timedelta(hours=1), start_zone)
            end_zone = start_zone
        if end_dt <= start_dt:
            end_dt = start_dt + timedelta(hours=1)
        # Both the offset and the IANA name: the offset pins the instant, the name makes
        # it render in the right local time for whoever is looking.
        body["start"] = {"dateTime": start_dt.isoformat(), "timeZone": start_zone}
        body["end"] = {"dateTime": end_dt.isoformat(), "timeZone": end_zone}

    description_lines = []
    if event.description:
        description_lines.append(event.description)
    if event.booking_reference:
        description_lines.append(f"Booking reference: {event.booking_reference}")
    if description_lines:
        body["description"] = "\n\n".join(description_lines)

    if address:
        body["location"] = address

    return body


class CalendarClient:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        service: Any | None = None,
        caldav_session: Any | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._service = service or build(
            "calendar", "v3", credentials=load_credentials(settings), cache_discovery=False
        )
        self._caldav_session = caldav_session
        self._primary_id: str | None = None

    def resolve_calendar(self, name: str, *, create_missing: bool = True) -> str:
        """Map a human calendar name to its id, creating the calendar once if needed."""
        if name == "primary":
            return "primary"

        cached = self._store.get_calendar_id(name)
        if cached:
            return cached

        wanted = name.strip().lower()
        page_token: str | None = None
        while True:
            response = self._retry(
                self._service.calendarList().list(
                    minAccessRole="writer", showHidden=True, pageToken=page_token
                )
            )
            for item in response.get("items", []):
                label = item.get("summaryOverride") or item.get("summary") or ""
                if label.strip().lower() == wanted:
                    self._store.set_calendar_id(name, item["id"])
                    return str(item["id"])
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        if not create_missing:
            raise KeyError(f"no writable calendar named {name!r}")

        created = self._retry(
            self._service.calendars().insert(
                body={"summary": name, "timeZone": self._settings.default_timezone}
            )
        )
        log.info("created calendar %r (%s)", name, created["id"])
        self._store.set_calendar_id(name, created["id"])
        return str(created["id"])

    def find_similar(
        self, calendar_id: str, body: dict[str, Any], *, booking_reference: str | None = None
    ) -> dict[str, Any] | None:
        """An existing event this one would duplicate, or None.

        Catches what the deterministic id cannot: the same booking arriving again in a
        different email. A duplicate starts within the dedup window (all-day: the same
        day) and either reads like the same title or carries the same booking reference.
        """
        window = timedelta(minutes=self._settings.dedup_window_minutes)
        all_day = "date" in body["start"]
        if all_day:
            start_date = date.fromisoformat(body["start"]["date"])
            midnight = datetime(start_date.year, start_date.month, start_date.day, tzinfo=UTC)
            # A calendar-zone day never strays more than a day from the UTC one.
            time_min, time_max = midnight - timedelta(days=1), midnight + timedelta(days=2)
        else:
            start = datetime.fromisoformat(body["start"]["dateTime"])
            time_min, time_max = start - window, start + window

        page_token: str | None = None
        while True:
            response = self._retry(
                self._service.events().list(
                    calendarId=calendar_id,
                    timeMin=time_min.isoformat(),
                    timeMax=time_max.isoformat(),
                    singleEvents=True,
                    pageToken=page_token,
                )
            )
            items: list[dict[str, Any]] = response.get("items", [])
            for item in items:
                # The same id is this very email again; insert() already absorbs that.
                if item.get("id") == body["id"] or item.get("status") == "cancelled":
                    continue
                item_start = item.get("start", {})
                if all_day:
                    if item_start.get("date") != body["start"]["date"]:
                        continue
                elif "dateTime" in item_start:
                    item_dt = datetime.fromisoformat(item_start["dateTime"])
                    if abs(item_dt - start) > window:
                        continue
                else:
                    continue
                if titles_match(item.get("summary", ""), body["summary"]):
                    return item
                if (
                    booking_reference
                    and booking_reference.lower() in item.get("description", "").lower()
                ):
                    return item
            page_token = response.get("nextPageToken")
            if not page_token:
                return None

    def insert(self, calendar_id: str, body: dict[str, Any], *, url: str | None = None) -> str:
        """Insert an event, treating an existing id as success."""
        try:
            created = self._retry(self._service.events().insert(calendarId=calendar_id, body=body))
        except HttpError as exc:
            if exc.resp.status == 409:
                log.info("event %s already exists on %s", body["id"], calendar_id)
                return str(body["id"])
            raise
        if url:
            self.attach_url(calendar_id, str(created["iCalUID"]), url)
        return str(created["id"])

    def attach_url(self, calendar_id: str, ical_uid: str, url: str) -> None:
        """Add a URL property to an event over CalDAV, where Apple Calendar reads it.

        Rewrites the object Google serves rather than composing one, so everything the
        JSON insert set - including the private properties dedup relies on - survives.
        A link is not worth failing an already-created event over, so this only logs.
        """
        calendar = quote(self._caldav_calendar(calendar_id), safe="")
        href = f"{CALDAV_BASE}/{calendar}/events/{ical_uid}.ics"
        try:
            session = self._session()
            response = session.get(href, timeout=CALDAV_TIMEOUT)
            response.raise_for_status()
            written = session.put(
                href,
                data=with_url_property(response.text, url).encode(),
                headers={"Content-Type": "text/calendar; charset=utf-8"},
                timeout=CALDAV_TIMEOUT,
            )
            written.raise_for_status()
        except Exception:
            log.warning("could not attach the mail link to %s", ical_uid, exc_info=True)

    def _session(self) -> Any:
        if self._caldav_session is None:
            self._caldav_session = AuthorizedSession(load_credentials(self._settings))
        return self._caldav_session

    def _caldav_calendar(self, calendar_id: str) -> str:
        """CalDAV addresses calendars by their real id; "primary" is a JSON API alias."""
        if calendar_id != "primary":
            return calendar_id
        if self._primary_id is None:
            resolved = self._retry(self._service.calendars().get(calendarId="primary"))
            self._primary_id = str(resolved["id"])
        return self._primary_id

    def _retry(self, request: Any, attempts: int = 5) -> dict[str, Any]:
        """Truncated exponential backoff with jitter, per Google's guidance."""
        for attempt in range(attempts):
            try:
                result: dict[str, Any] = request.execute()
                return result
            except HttpError as exc:
                status = exc.resp.status
                if not _is_retryable(exc) or attempt == attempts - 1:
                    raise
                delay = min(2**attempt, 32) + random.uniform(0, 1)
                log.warning("Google API %s; retrying in %.1fs", status, delay)
                time.sleep(delay)
        raise RuntimeError("unreachable")
