"""The knobs a person turns, and the one place they live.

Every preference has a working default, so a fresh deployment runs before anyone opens
the settings page. They are stored as one row of the state database, edited at
`/settings`, and never read from the environment: a value with two homes is a value that
eventually disagrees with itself.

The row holds one JSON blob whose shape is `Prefs`. Adding a knob is a field here and a
form control, never a migration, and an older row missing the field falls back to the
default in the same breath.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .mailbox import FLAG_COLOURS
from .store import Store

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class Category(BaseModel):
    """One kind of event and the calendar it goes to.

    The description is what the model reads, so it says what belongs in the category
    rather than merely naming it.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    calendar: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _normalise_name(cls, value: str) -> str:
        return value.strip().lower()


class Prefs(BaseModel):
    """Frozen so a caller that grabbed it cannot quietly edit the deployment."""

    model_config = ConfigDict(frozen=True)

    # The Apple Mail flag colour that means "put this on my calendar".
    flag_colour: str = "blue"
    # Where events land when they match no category, by the name the Calendar app shows.
    # Every calendar named here or in a category has to exist already; the service never
    # creates one.
    calendar: str = ""
    # Used when an email does not say where the event happens.
    timezone: str = "UTC"
    categories: tuple[Category, ...] = ()
    log_level: str = "INFO"

    # What the phone is told about. The master switch stops every push about an email;
    # the two under it choose between the outcomes. The master starts off: nothing can
    # be sent until a Pushover account is on the app, and connecting one is not the
    # same as asking to be pushed at. A service that has stopped is pushed regardless,
    # because that is how somebody finds out nothing is being read.
    notifications_enabled: bool = False
    notify_events: bool = True
    notify_failures: bool = True

    @field_validator("flag_colour")
    @classmethod
    def _known_colour(cls, value: str) -> str:
        if value not in FLAG_COLOURS:
            raise ValueError(f"pick one of {', '.join(FLAG_COLOURS)}")
        return value

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"{value!r} is not a recognised time zone; use a name like Europe/Zurich"
            ) from exc
        return value

    @field_validator("log_level")
    @classmethod
    def _known_level(cls, value: str) -> str:
        value = value.upper()
        if value not in LOG_LEVELS:
            raise ValueError(f"pick one of {', '.join(LOG_LEVELS)}")
        return value

    @model_validator(mode="after")
    def _unique_category_names(self) -> Prefs:
        seen: set[str] = set()
        for category in self.categories:
            if category.name in seen:
                raise ValueError(f"duplicate category name {category.name!r}")
            seen.add(category.name)
        return self

    @property
    def calendar_configured(self) -> bool:
        return bool(self.calendar)

    @property
    def calendars(self) -> set[str]:
        """Every calendar the service writes to, which all have to exist up front."""
        return {self.calendar} | {category.calendar for category in self.categories}

    def calendar_for(self, category: str | None) -> str:
        """Map an extracted category name onto a calendar, defaulting when unmatched."""
        if category:
            wanted = category.strip().lower()
            for rule in self.categories:
                if rule.name == wanted:
                    return rule.calendar
        return self.calendar


_current = Prefs()


def current() -> Prefs:
    """The live preferences.

    Defaults until `load` has run, which is what keeps every pure function in here
    testable without a database behind it.
    """
    return _current


def load(store: Store) -> Prefs:
    """Read the row, falling back to the defaults for anything it does not hold."""
    global _current
    _current = Prefs.model_validate(store.load_prefs())
    return _current


def save(store: Store, values: Mapping[str, Any]) -> Prefs:
    """Validate a partial update against the whole model and store the result."""
    global _current
    updated = Prefs.model_validate({**_current.model_dump(mode="json"), **values})
    store.save_prefs(updated.model_dump(mode="json"))
    _current = updated
    return updated
