from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from email_to_cal import prefs
from email_to_cal.prefs import Prefs
from email_to_cal.store import Store


def test_bad_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not a recognised time zone"):
        Prefs(timezone="Middle/Earth")


def test_red_is_not_a_flag_colour() -> None:
    # Red carries no colour bits, so it cannot be told from a plain flag.
    with pytest.raises(ValidationError, match="pick one of"):
        Prefs(flag_colour="red")


def test_category_names_fold_to_lower_case() -> None:
    rule = Prefs(
        categories=[{"name": "Travel", "description": "Flights.", "calendar": "Trips"}]
    ).categories[0]
    # Names are matched against what the model returns, so they fold to lower case.
    assert (rule.name, rule.calendar) == ("travel", "Trips")


def test_duplicate_category_names_are_rejected() -> None:
    rules = [
        {"name": "travel", "description": "Flights.", "calendar": "Trips"},
        {"name": "Travel", "description": "Trains.", "calendar": "Other"},
    ]
    # Two rules with one name make routing depend on ordering, silently.
    with pytest.raises(ValidationError, match="duplicate category name"):
        Prefs(categories=rules)


def test_a_category_without_a_calendar_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Prefs(categories=[{"name": "travel", "description": "Flights.", "calendar": ""}])


def test_routing_falls_back_to_the_main_calendar() -> None:
    current = Prefs(
        calendar="Bookings",
        categories=[{"name": "travel", "description": "Flights.", "calendar": "Trips"}],
    )
    assert current.calendar_for("Travel") == "Trips"
    assert current.calendar_for("nonsense") == "Bookings"
    assert current.calendar_for(None) == "Bookings"
    # Every calendar has to be resolved up front, so they are listed together.
    assert current.calendars == {"Bookings", "Trips"}


def test_a_partial_save_keeps_what_it_did_not_mention(tmp_path: Path) -> None:
    with Store(tmp_path / "state.sqlite") as store:
        prefs.save(store, {"calendar": "Bookings", "timezone": "Europe/Zurich"})
        prefs.save(store, {"flag_colour": "purple"})

        loaded = prefs.load(store)

    assert (loaded.calendar, loaded.timezone, loaded.flag_colour) == (
        "Bookings",
        "Europe/Zurich",
        "purple",
    )
    assert prefs.current() == loaded


def test_a_refused_save_changes_nothing(tmp_path: Path) -> None:
    with Store(tmp_path / "state.sqlite") as store:
        prefs.save(store, {"timezone": "Europe/Zurich"})
        with pytest.raises(ValidationError):
            prefs.save(store, {"timezone": "Middle/Earth"})
        assert prefs.load(store).timezone == "Europe/Zurich"


def test_an_older_row_falls_back_to_the_defaults(tmp_path: Path) -> None:
    with Store(tmp_path / "state.sqlite") as store:
        store.save_prefs({"calendar": "Bookings"})
        loaded = prefs.load(store)
    # A knob added later is a field here and nothing else: no migration.
    assert loaded.log_level == "INFO"
    assert loaded.calendar == "Bookings"
