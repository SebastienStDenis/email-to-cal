"""Turn a place into the one line a calendar can actually resolve.

Google Calendar's location is free-form text, and it only earns a map pin, travel time,
and a directions link when that text geocodes to a real place. A bare venue name usually
does not: "LaGuardia Airport" is a label, "LaGuardia Airport, East Elmhurst, NY 11371,
US" is an address. So the model fills in the address parts it can see, this renders them
in the order geocoders expect, and airports - which name a place the sender never spells
out - are filled in from the offline IATA dataset.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping
from typing import Any

import airportsdata

from .schema import EventLocation, ExtractedEvent


@functools.cache
def _iata_index() -> Mapping[str, Mapping[str, Any]]:
    return airportsdata.load("IATA")


def airport_record(code: str | None) -> Mapping[str, Any] | None:
    """The offline dataset's entry for an IATA airport code."""
    if not code:
        return None
    return _iata_index().get(code.strip().upper())


def airport_location(code: str | None) -> EventLocation | None:
    """An airport's address, good enough to geocode, from its IATA code.

    The dataset carries no street address, but name, city, subdivision, and country
    resolve an airport unambiguously - which a code like LGA on its own does not.
    """
    record = airport_record(code)
    if record is None:
        return None
    return EventLocation(
        name=str(record.get("name") or "") or None,
        locality=str(record.get("city") or "") or None,
        region=str(record.get("subd") or "") or None,
        country=str(record.get("country") or "") or None,
    )


def resolve_address(event: ExtractedEvent) -> str | None:
    """The location line for one event.

    A flight happens at its departure airport, and the airport dataset knows that place
    better than the email does - so it wins unless the email gave a real address.
    """
    location = event.location
    if not (location and location.has_address):
        location = airport_location(event.departure_iata) or location
    return format_address(location)


def format_address(location: EventLocation | None) -> str | None:
    """Render a structured address as one geocodable line, most specific part first."""
    if location is None:
        return None

    # "NY 11371", not "NY, 11371": the postal code qualifies the region rather than
    # naming a level of its own, and that is the form geocoders return.
    region = " ".join(p for p in (_clean(location.region), _clean(location.postal_code)) if p)
    candidates = [
        _clean(location.name),
        _clean(location.street),
        _clean(location.locality),
        region,
        _clean(location.country),
    ]

    parts: list[str] = []
    seen: set[str] = set()
    for part in candidates:
        # Datasets repeat themselves ("New York, New York"); a repeat adds no precision.
        if not part or part.casefold() in seen:
            continue
        seen.add(part.casefold())
        parts.append(part)
    return ", ".join(parts) or None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    # A comma inside one part would read as a boundary between parts.
    return " ".join(value.replace(",", " ").split()) or None
