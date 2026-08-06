"""Resolve wall-clock times from emails into unambiguous calendar times.

Emails say "18:35". Calendars need an instant. Everything here exists to close that gap
without guessing: an explicit zone from the email beats an airport lookup, which beats a
city match, which beats the operator's default.
"""

from __future__ import annotations

import functools
import logging
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import airportsdata

log = logging.getLogger(__name__)

UTC = ZoneInfo("UTC")

# A small set of cities worth recognising without a geocoding dependency. The default
# timezone catches everything else, and a wrong guess is worse than a sane fallback.
CITY_ZONES: dict[str, str] = {
    "amsterdam": "Europe/Amsterdam",
    "barcelona": "Europe/Madrid",
    "berlin": "Europe/Berlin",
    "boston": "America/New_York",
    "brussels": "Europe/Brussels",
    "chicago": "America/Chicago",
    "copenhagen": "Europe/Copenhagen",
    "dublin": "Europe/Dublin",
    "geneva": "Europe/Zurich",
    "hong kong": "Asia/Hong_Kong",
    "lisbon": "Europe/Lisbon",
    "london": "Europe/London",
    "los angeles": "America/Los_Angeles",
    "madrid": "Europe/Madrid",
    "melbourne": "Australia/Melbourne",
    "milan": "Europe/Rome",
    "montreal": "America/Toronto",
    "munich": "Europe/Berlin",
    "new york": "America/New_York",
    "paris": "Europe/Paris",
    "rome": "Europe/Rome",
    "san francisco": "America/Los_Angeles",
    "seattle": "America/Los_Angeles",
    "singapore": "Asia/Singapore",
    "stockholm": "Europe/Stockholm",
    "sydney": "Australia/Sydney",
    "tokyo": "Asia/Tokyo",
    "toronto": "America/Toronto",
    "vancouver": "America/Vancouver",
    "vienna": "Europe/Vienna",
    "zurich": "Europe/Zurich",
}


@functools.cache
def _iata_index() -> Mapping[str, Mapping[str, Any]]:
    return airportsdata.load("IATA")


def timezone_for_iata(code: str | None) -> str | None:
    """Map an IATA airport code to its IANA zone using the offline dataset."""
    if not code:
        return None
    entry = _iata_index().get(code.strip().upper())
    if entry is None:
        return None
    zone = entry.get("tz")
    return str(zone) if zone else None


def timezone_for_location(location: str | None) -> str | None:
    """Best-effort city match. Returns None rather than guessing wildly."""
    if not location:
        return None
    haystack = location.lower()
    matches = [(city, zone) for city, zone in CITY_ZONES.items() if city in haystack]
    if not matches:
        return None
    # Prefer the longest city name so "New York" wins over a substring collision.
    matches.sort(key=lambda pair: len(pair[0]), reverse=True)
    return matches[0][1]


def valid_zone(name: str | None) -> str | None:
    if not name:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.debug("model proposed unknown timezone %r", name)
        return None
    return name


def parse_naive(value: str) -> datetime | date:
    """Parse the naive local time the model emits, tolerating a stray offset or Z."""
    cleaned = re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", value.strip())
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned):
        return date.fromisoformat(cleaned)
    parsed = datetime.fromisoformat(cleaned)
    return parsed.replace(tzinfo=None)


def localise(naive: datetime, zone: str) -> datetime:
    """Attach a zone to a wall-clock time, resolving DST gaps and folds explicitly.

    Spring-forward gap: the stated time never happens, so move forward by the width of the
    gap to the first instant that does. Fall-back fold: the time happens twice, and fold=0
    picks the earlier occurrence.
    """
    tz = ZoneInfo(zone)
    attached = naive.replace(tzinfo=tz, fold=0)

    # An imaginary local time is the only kind that changes wall clock on a UTC round trip.
    if attached.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != naive:
        before = attached.utcoffset() or timedelta()
        after = naive.replace(tzinfo=tz, fold=1).utcoffset() or timedelta()
        shifted = naive + abs(after - before)
        log.info("local time %s does not exist in %s; shifting to %s", naive, zone, shifted)
        return shifted.replace(tzinfo=tz, fold=0)

    return attached


def resolve_zones(
    *,
    start_tz: str | None,
    end_tz: str | None,
    departure_iata: str | None,
    arrival_iata: str | None,
    location: str | None,
    default_timezone: str,
) -> tuple[str, str]:
    """Decide the start and end zones for one event.

    Start and end are resolved independently so a Tokyo departure and a Los Angeles
    arrival each render in their own local time, which is how a flight should look.
    """
    start = (
        valid_zone(start_tz)
        or timezone_for_iata(departure_iata)
        or timezone_for_location(location)
        or default_timezone
    )
    end = valid_zone(end_tz) or timezone_for_iata(arrival_iata) or start
    return start, end
