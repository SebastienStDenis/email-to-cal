"""Schemas the model must fill in, and the normalised email it reads from."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

EventKind = Literal[
    "flight",
    "train",
    "hotel",
    "concert",
    "restaurant",
    "appointment",
    "other",
]


class Attachment(BaseModel):
    """An image or PDF worth showing the model when the text tiers come up short."""

    filename: str
    media_type: str
    data: bytes

    @property
    def is_image(self) -> bool:
        return self.media_type.startswith("image/")

    @property
    def is_pdf(self) -> bool:
        return self.media_type == "application/pdf"


class EmailDocument(BaseModel):
    """One message, flattened into everything an extractor could want."""

    message_id: str
    subject: str
    sender: str
    to: str
    date: datetime | None
    body_text: str
    json_ld: list[dict[str, object]] = []
    ics_events: list[dict[str, str]] = []
    attachments: list[Attachment] = []
    # Which extraction tier produced body_text: json-ld, ics, plain, html, or empty.
    source_tier: str = "empty"

    @property
    def has_structured_source(self) -> bool:
        return bool(self.json_ld or self.ics_events)


class EventLocation(BaseModel):
    """Where an event happens, in parts.

    Calendars store the location as one string and geocode it, so a venue name alone
    often resolves to nothing. Collecting the address in parts lets the model copy what
    the email actually says without also having to decide how to write it out; places
    renders it.
    """

    name: str | None = Field(
        default=None, description="Venue or business name, e.g. 'The O2 Arena'."
    )
    street: str | None = Field(
        default=None, description="Street address including any building number."
    )
    locality: str | None = Field(default=None, description="City or town.")
    region: str | None = Field(
        default=None, description="State, province, or county, where the country uses one."
    )
    postal_code: str | None = None
    country: str | None = Field(
        default=None, description="Country name, or its ISO 3166-1 alpha-2 code."
    )

    @property
    def has_address(self) -> bool:
        """Whether this says more than a name, and so has some chance of geocoding."""
        return any((self.street, self.locality, self.region, self.postal_code, self.country))


class ExtractedEvent(BaseModel):
    """One calendar-worthy commitment found in an email."""

    kind: EventKind
    title: str = Field(description="Short calendar title, e.g. 'LX318 ZRH to LHR'.")
    description: str | None = Field(
        default=None, description="Useful detail: booking reference, seat, terminal, notes."
    )
    location: EventLocation | None = Field(
        default=None, description="Where the event happens, in as much detail as the email gives."
    )
    all_day: bool = Field(description="True only when the email gives no meaningful clock time.")
    start_local: str = Field(
        description="Local wall-clock start as naive ISO 8601 with no offset or zone suffix: "
        "'2026-09-14T18:35:00', or '2026-09-14' when all_day is true."
    )
    end_local: str | None = Field(
        default=None, description="Local wall-clock end in the same format, when known."
    )
    start_tz: str | None = Field(
        default=None,
        description="IANA zone for start_local if the email states or clearly implies it.",
    )
    end_tz: str | None = Field(
        default=None, description="IANA zone for end_local when it differs from start_tz."
    )
    departure_iata: str | None = Field(
        default=None, description="Departure airport IATA code, for flights only."
    )
    arrival_iata: str | None = Field(
        default=None, description="Arrival airport IATA code, for flights only."
    )
    booking_reference: str | None = None
    category: str | None = Field(
        default=None, description="Exactly one of the configured category names, or null."
    )
    excluded_by: str | None = Field(
        default=None,
        description="Name of the exclusion rule that describes this event, or null. "
        "Judged independently of category.",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(description="One sentence on why this is a real commitment.")


class ExtractionResult(BaseModel):
    """The model's verdict on a single email."""

    is_committed: bool = Field(
        description="True only if the recipient personally booked, bought, reserved, "
        "registered for, or was directly invited to something."
    )
    gate_reasoning: str = Field(description="One sentence justifying is_committed.")
    events: list[ExtractedEvent] = []
