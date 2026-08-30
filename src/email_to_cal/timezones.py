"""Resolve wall-clock times from emails into unambiguous calendar times.

Emails say "18:35". Calendars need an instant. Everything here exists to close that gap
without guessing: an explicit zone from the email beats an airport lookup, which beats a
city match, which beats the operator's default.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .places import airport_record

log = logging.getLogger(__name__)

UTC = ZoneInfo("UTC")

# A small set of cities worth recognising without a geocoding dependency, each with the
# country it is in - because most of these names are also carried by a town, a street,
# or a restaurant somewhere else entirely. The default timezone catches everything else,
# and a wrong guess is worse than a sane fallback.
CITY_ZONES: dict[str, tuple[str, str]] = {
    "amsterdam": ("Europe/Amsterdam", "NL"),
    "barcelona": ("Europe/Madrid", "ES"),
    "berlin": ("Europe/Berlin", "DE"),
    "boston": ("America/New_York", "US"),
    "brussels": ("Europe/Brussels", "BE"),
    "chicago": ("America/Chicago", "US"),
    "copenhagen": ("Europe/Copenhagen", "DK"),
    "dublin": ("Europe/Dublin", "IE"),
    "geneva": ("Europe/Zurich", "CH"),
    "hong kong": ("Asia/Hong_Kong", "HK"),
    "lisbon": ("Europe/Lisbon", "PT"),
    "london": ("Europe/London", "GB"),
    "los angeles": ("America/Los_Angeles", "US"),
    "madrid": ("Europe/Madrid", "ES"),
    "melbourne": ("Australia/Melbourne", "AU"),
    "milan": ("Europe/Rome", "IT"),
    "montreal": ("America/Toronto", "CA"),
    "munich": ("Europe/Berlin", "DE"),
    "new york": ("America/New_York", "US"),
    "paris": ("Europe/Paris", "FR"),
    "rome": ("Europe/Rome", "IT"),
    "san francisco": ("America/Los_Angeles", "US"),
    "seattle": ("America/Los_Angeles", "US"),
    "singapore": ("Asia/Singapore", "SG"),
    "stockholm": ("Europe/Stockholm", "SE"),
    "sydney": ("Australia/Sydney", "AU"),
    "tokyo": ("Asia/Tokyo", "JP"),
    "toronto": ("America/Toronto", "CA"),
    "vancouver": ("America/Vancouver", "CA"),
    "vienna": ("Europe/Vienna", "AT"),
    "zurich": ("Europe/Zurich", "CH"),
}

# Emails write a country however they like, and only a code compares cleanly. Two-letter
# values are taken as ISO codes already, so this only has to name the countries above -
# plus "UK", which is the one common form that is not its own ISO code.
COUNTRY_ALIASES: dict[str, str] = {
    "america": "US",
    "australia": "AU",
    "austria": "AT",
    "belgie": "BE",
    "belgique": "BE",
    "belgium": "BE",
    "britain": "GB",
    "canada": "CA",
    "danmark": "DK",
    "denmark": "DK",
    "deutschland": "DE",
    "england": "GB",
    "espana": "ES",
    "españa": "ES",
    "france": "FR",
    "germany": "DE",
    "great britain": "GB",
    "holland": "NL",
    "hong kong": "HK",
    "ireland": "IE",
    "italia": "IT",
    "italy": "IT",
    "japan": "JP",
    "nederland": "NL",
    "netherlands": "NL",
    "nippon": "JP",
    "portugal": "PT",
    "schweiz": "CH",
    "scotland": "GB",
    "singapore": "SG",
    "spain": "ES",
    "suisse": "CH",
    "sverige": "SE",
    "sweden": "SE",
    "switzerland": "CH",
    "the netherlands": "NL",
    "uk": "GB",
    "united kingdom": "GB",
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
    "wales": "GB",
    "osterreich": "AT",
    "österreich": "AT",
}


def timezone_for_iata(code: str | None) -> str | None:
    """Map an IATA airport code to its IANA zone using the offline dataset."""
    entry = airport_record(code)
    if entry is None:
        return None
    zone = entry.get("tz")
    return str(zone) if zone else None


def country_code(name: str | None) -> str | None:
    """An address's country as an ISO 3166-1 alpha-2 code, or None if unreadable."""
    if not name:
        return None
    # "U.S.A." and "U.K." are written with stops as often as without.
    cleaned = " ".join(name.replace(".", "").casefold().split())
    if cleaned in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[cleaned]
    if len(cleaned) == 2 and cleaned.isalpha():
        return cleaned.upper()
    return None


def timezone_for_city(locality: str | None, country: str | None = None) -> str | None:
    """Best-effort match on the city an address names. None rather than a wild guess.

    Only the city field is read. A venue or a street carries a famous city's name all the
    time without being anywhere near it - "Cafe Berlin" in Kitchener, "London House" in
    Ottawa, a "Boston Pizza" in Vancouver - and matching one of those puts the event
    hours away from when it happens, in a direction nothing downstream can catch.

    A stated country settles the rest: London is in Europe/London unless the address says
    Canada, in which case the default zone is a better answer than five hours out.
    """
    if not locality:
        return None
    # The city field can carry a qualifier ("Greater London", "New York City"); a whole
    # word inside it still names the city, which is not true of the rest of an address.
    words = set(re.findall(r"[a-z]+", locality.casefold()))
    matches = [
        (city, zone, nation)
        for city, (zone, nation) in CITY_ZONES.items()
        if set(city.split()) <= words
    ]
    if not matches:
        return None

    stated = country_code(country)
    if stated is not None:
        allowed = [match for match in matches if match[2] == stated]
        if not allowed:
            log.info("city %r is not in %s; falling back", locality, stated)
            return None
        matches = allowed

    # Prefer the longest city name so "New York" wins over a one-word collision.
    matches.sort(key=lambda match: len(match[0]), reverse=True)
    return matches[0][1]


def zone_label(zone: str) -> str:
    """The city an IANA zone names, for a human to read: 'America/New_York' -> 'New York'."""
    return zone.rsplit("/", 1)[-1].replace("_", " ")


def same_clock(moment: datetime, zone: str) -> bool:
    """Whether an instant reads the same on a clock in `zone` as it does in its own.

    Offsets, not names: an event in Europe/Berlin needs no explaining to someone whose
    own zone is Europe/Zurich, because the two show the same time.
    """
    return moment.utcoffset() == moment.astimezone(ZoneInfo(zone)).utcoffset()


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
    locality: str | None,
    country: str | None,
    default_timezone: str,
) -> tuple[str, str]:
    """Decide the start and end zones for one event.

    Start and end are resolved independently so a Tokyo departure and a Los Angeles
    arrival each render in their own local time, which is how a flight should look.

    The city is taken from the address's own city field rather than the rendered line,
    so a venue or street that borrows a city's name cannot decide the zone.
    """
    start = (
        valid_zone(start_tz)
        or timezone_for_iata(departure_iata)
        or timezone_for_city(locality, country)
        or default_timezone
    )
    end = valid_zone(end_tz) or timezone_for_iata(arrival_iata) or start
    return start, end
