from __future__ import annotations

from email_to_cal.places import airport_location, format_address, resolve_address
from email_to_cal.schema import EventLocation, ExtractedEvent


def event(**kwargs: object) -> ExtractedEvent:
    defaults: dict[str, object] = {
        "kind": "other",
        "title": "Something",
        "all_day": False,
        "start_local": "2026-09-14T18:35:00",
        "confidence": 0.9,
        "reasoning": "Confirmed.",
    }
    return ExtractedEvent(**{**defaults, **kwargs})  # type: ignore[arg-type]


def test_full_address_renders_most_specific_part_first() -> None:
    address = format_address(
        EventLocation(
            name="AT&T Park",
            street="24 Willie Mays Plaza",
            locality="San Francisco",
            region="CA",
            postal_code="94107",
            country="US",
        )
    )
    assert address == "AT&T Park, 24 Willie Mays Plaza, San Francisco, CA 94107, US"


def test_postal_code_qualifies_the_region_rather_than_standing_alone() -> None:
    assert format_address(EventLocation(locality="Zurich", postal_code="8001")) == "Zurich, 8001"


def test_partial_addresses_keep_whatever_the_email_gave() -> None:
    assert format_address(EventLocation(name="The O2 Arena", locality="London")) == (
        "The O2 Arena, London"
    )
    assert format_address(EventLocation(name="Praxis Dr. Müller")) == "Praxis Dr. Müller"
    assert format_address(EventLocation()) is None
    assert format_address(None) is None


def test_a_repeated_part_is_dropped_rather_than_said_twice() -> None:
    assert format_address(EventLocation(locality="New York", region="New York")) == "New York"


def test_a_comma_inside_one_part_does_not_read_as_a_boundary() -> None:
    assert format_address(EventLocation(street="Hauptstrasse 3, 2. Stock")) == (
        "Hauptstrasse 3 2. Stock"
    )


def test_airport_addresses_come_from_the_offline_dataset() -> None:
    assert format_address(airport_location("LGA")) == "Laguardia Airport, New York, US"
    assert format_address(airport_location("ZRH")) == "Zurich Airport, Zurich, CH"
    assert airport_location("XXX") is None
    assert airport_location(None) is None


def test_a_flight_is_located_at_its_departure_airport() -> None:
    flight = event(kind="flight", departure_iata="HND", arrival_iata="LAX")
    assert resolve_address(flight) == "Tokyo International Airport, Tokyo, JP"


def test_an_airport_beats_a_bare_name_the_model_supplied() -> None:
    flight = event(kind="flight", departure_iata="HND", location=EventLocation(name="Tokyo Haneda"))
    assert resolve_address(flight) == "Tokyo International Airport, Tokyo, JP"


def test_a_real_address_in_the_email_beats_the_dataset() -> None:
    flight = event(
        kind="flight",
        departure_iata="HND",
        location=EventLocation(
            name="Haneda Terminal 3", street="2-6-5 Hanedakuko", locality="Ota City"
        ),
    )
    assert resolve_address(flight) == "Haneda Terminal 3, 2-6-5 Hanedakuko, Ota City"


def test_events_with_no_location_at_all_get_none() -> None:
    assert resolve_address(event()) is None
