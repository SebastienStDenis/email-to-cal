from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from email_to_cal.timezones import (
    country_code,
    localise,
    parse_naive,
    resolve_zones,
    same_clock,
    timezone_for_city,
    timezone_for_iata,
    valid_zone,
    zone_label,
)


def test_iata_lookup() -> None:
    assert timezone_for_iata("HND") == "Asia/Tokyo"
    assert timezone_for_iata("lax") == "America/Los_Angeles"
    assert timezone_for_iata("ZRH") == "Europe/Zurich"
    assert timezone_for_iata("ZZZ") is None
    assert timezone_for_iata(None) is None


def test_city_lookup_reads_the_city_field() -> None:
    assert timezone_for_city("London") == "Europe/London"
    assert timezone_for_city("New York") == "America/New_York"
    assert timezone_for_city("Nowhere at all") is None
    assert timezone_for_city(None) is None


def test_city_lookup_reads_a_qualified_city_name() -> None:
    assert timezone_for_city("Greater London") == "Europe/London"
    assert timezone_for_city("New York City") == "America/New_York"


def test_a_venue_or_street_never_decides_the_zone() -> None:
    # The bug this guards: matching a city name anywhere in the address line put a lunch
    # at "London House, Ottawa" in Europe/London, and the calendar showed it five hours
    # before the time the email gave. Only the city field is read now.
    assert timezone_for_city("Ottawa", "CA") is None
    assert timezone_for_city("Kitchener", "Canada") is None
    assert timezone_for_city("Vancouver", "CA") == "America/Vancouver"


def test_a_stated_country_settles_a_shared_city_name() -> None:
    # London, Ontario is not five hours ahead of itself.
    assert timezone_for_city("London", "CA") is None
    assert timezone_for_city("London", "United Kingdom") == "Europe/London"
    assert timezone_for_city("Milan", "US") is None
    assert timezone_for_city("Boston", "GB") is None
    # An unreadable country is no reason to throw away an otherwise good match.
    assert timezone_for_city("Tokyo", "Nihon") == "Asia/Tokyo"


def test_country_code_reads_what_emails_actually_write() -> None:
    assert country_code("United States of America") == "US"
    assert country_code("U.S.A.") == "US"
    assert country_code("UK") == "GB"
    assert country_code("gb") == "GB"
    assert country_code("Schweiz") == "CH"
    assert country_code("Freedonia") is None
    assert country_code(None) is None


def test_zone_label_names_the_city_a_human_would_say() -> None:
    assert zone_label("America/Los_Angeles") == "Los Angeles"
    assert zone_label("Europe/London") == "London"
    assert zone_label("UTC") == "UTC"


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
        locality=None,
        country=None,
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
        locality="London",
        country="GB",
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
        locality="A field somewhere",
        country=None,
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


def test_same_clock_compares_offsets_not_names() -> None:
    # Nobody in Zurich needs a Berlin event explained to them; they read the same clock.
    berlin = datetime(2026, 9, 14, 12, 30, tzinfo=ZoneInfo("Europe/Berlin"))
    assert same_clock(berlin, "Europe/Zurich")
    assert not same_clock(berlin, "Europe/London")
    # And a zone that agrees in summer can still differ in winter.
    assert same_clock(datetime(2026, 7, 1, 12, tzinfo=ZoneInfo("Europe/London")), "Europe/Lisbon")
