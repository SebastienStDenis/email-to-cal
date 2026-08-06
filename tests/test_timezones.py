from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from email_to_cal.timezones import (
    localise,
    parse_naive,
    resolve_zones,
    timezone_for_iata,
    timezone_for_location,
    valid_zone,
)


def test_iata_lookup() -> None:
    assert timezone_for_iata("HND") == "Asia/Tokyo"
    assert timezone_for_iata("lax") == "America/Los_Angeles"
    assert timezone_for_iata("ZRH") == "Europe/Zurich"
    assert timezone_for_iata("ZZZ") is None
    assert timezone_for_iata(None) is None


def test_city_lookup_prefers_the_longest_match() -> None:
    assert timezone_for_location("The O2 Arena, London SE10") == "Europe/London"
    assert timezone_for_location("Somewhere in New York, NY") == "America/New_York"
    assert timezone_for_location("Nowhere at all") is None


def test_valid_zone_rejects_nonsense() -> None:
    assert valid_zone("Europe/Zurich") == "Europe/Zurich"
    assert valid_zone("Mars/Olympus") is None
    assert valid_zone("CEST") is None
    assert valid_zone(None) is None


def test_flight_resolves_distinct_start_and_end_zones() -> None:
    start, end = resolve_zones(
        start_tz=None,
        end_tz=None,
        departure_iata="HND",
        arrival_iata="LAX",
        location=None,
        default_timezone="Europe/Zurich",
    )
    assert start == "Asia/Tokyo"
    assert end == "America/Los_Angeles"
    assert start != end


def test_explicit_zone_beats_everything() -> None:
    start, end = resolve_zones(
        start_tz="Europe/Berlin",
        end_tz=None,
        departure_iata="HND",
        arrival_iata=None,
        location="London",
        default_timezone="UTC",
    )
    assert start == "Europe/Berlin"
    assert end == "Europe/Berlin"


def test_falls_back_to_default_when_nothing_resolves() -> None:
    start, end = resolve_zones(
        start_tz=None,
        end_tz=None,
        departure_iata=None,
        arrival_iata=None,
        location="A field somewhere",
        default_timezone="Europe/Zurich",
    )
    assert start == end == "Europe/Zurich"


def test_parse_naive_strips_any_offset_the_model_sneaks_in() -> None:
    assert parse_naive("2026-09-14T18:35:00") == datetime(2026, 9, 14, 18, 35)
    assert parse_naive("2026-09-14T18:35:00+09:00") == datetime(2026, 9, 14, 18, 35)
    assert parse_naive("2026-09-14T18:35:00Z") == datetime(2026, 9, 14, 18, 35)
    assert parse_naive("2026-09-14") == date(2026, 9, 14)


def test_localise_normal_time() -> None:
    result = localise(datetime(2026, 9, 14, 18, 35), "Asia/Tokyo")
    assert result.utcoffset() == timedelta(hours=9)
    assert result.isoformat() == "2026-09-14T18:35:00+09:00"


def test_localise_dst_gap_shifts_forward() -> None:
    # 02:30 on 29 March 2026 does not exist in Zurich; clocks jump 02:00 -> 03:00.
    result = localise(datetime(2026, 3, 29, 2, 30), "Europe/Zurich")
    assert result.hour == 3
    assert result.minute == 30
    assert result.utcoffset() == timedelta(hours=2)


def test_localise_dst_fold_takes_the_earlier_instant() -> None:
    # 02:30 on 25 October 2026 happens twice in Zurich.
    result = localise(datetime(2026, 10, 25, 2, 30), "Europe/Zurich")
    assert result.hour == 2
    assert result.utcoffset() == timedelta(hours=2)  # CEST, the first pass


@pytest.mark.parametrize("zone", ["Asia/Tokyo", "UTC", "Pacific/Kiritimati"])
def test_localise_is_a_noop_in_zones_without_dst(zone: str) -> None:
    naive = datetime(2026, 3, 29, 2, 30)
    assert localise(naive, zone).replace(tzinfo=None) == naive
