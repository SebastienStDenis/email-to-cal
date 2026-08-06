from __future__ import annotations

from typing import Any

import anthropic
import httpx
import pytest

from email_to_cal.app import Pipeline, _process_one
from email_to_cal.config import CategoryRule, Settings
from email_to_cal.mailbox import AuthenticationFatal
from email_to_cal.schema import EmailDocument, ExtractedEvent, ExtractionResult
from email_to_cal.store import Store

from .conftest import fixture_bytes


class StubExtractor:
    """Stands in for the Anthropic client; records calls so caching is observable."""

    def __init__(self, result: ExtractionResult) -> None:
        self.result = result
        self.calls = 0
        self.timezone_lookups: list[str] = []

    def extract(self, doc: EmailDocument) -> ExtractionResult:
        self.calls += 1
        return self.result

    def lookup_timezone(self, location: str) -> str | None:
        self.timezone_lookups.append(location)
        return None


class StubCalendar:
    def __init__(self) -> None:
        self.inserted: list[tuple[str, dict[str, Any]]] = []

    def resolve_calendar(self, name: str, *, create_missing: bool = True) -> str:
        return f"id-of-{name.lower().replace(' ', '-')}"

    def insert(self, calendar_id: str, body: dict[str, Any]) -> str:
        self.inserted.append((calendar_id, body))
        return str(body["id"])


def concert_event(confidence: float = 0.95, category: str | None = "music") -> ExtractedEvent:
    return ExtractedEvent(
        kind="concert",
        title="Radiohead at The O2",
        location="The O2 Arena, London",
        all_day=False,
        start_local="2026-11-02T19:30:00",
        end_local="2026-11-02T22:30:00",
        category=category,
        confidence=confidence,
        reasoning="Ticket order confirmed.",
    )


def build(
    settings: Settings, result: ExtractionResult
) -> tuple[Pipeline, StubExtractor, StubCalendar, Store]:
    store = Store(settings.state_db)
    extractor = StubExtractor(result)
    calendar = StubCalendar()
    return Pipeline(settings, store, extractor, calendar), extractor, calendar, store


def test_committed_email_creates_an_event_on_the_routed_calendar(settings: Settings) -> None:
    result = ExtractionResult(
        is_committed=True, gate_reasoning="Order confirmation.", events=[concert_event()]
    )
    pipeline, _, calendar, store = build(settings, result)

    outcome = pipeline.process(fixture_bytes("concert_ics.eml"))

    assert outcome.committed
    assert len(calendar.inserted) == 1
    calendar_id, body = calendar.inserted[0]
    assert calendar_id == "id-of-music"
    assert body["summary"] == "Radiohead at The O2"
    assert body["start"]["timeZone"] == "Europe/London"
    store.close()


def test_transpacific_flight_lands_on_the_travel_calendar_in_both_zones(
    settings: Settings,
) -> None:
    """The headline case, from real MIME through to the exact Google payload."""
    flight = ExtractedEvent(
        kind="flight",
        title="NH106 HND to LAX",
        description="Confirmation K3TQ9P",
        all_day=False,
        start_local="2026-09-14T18:35:00",
        end_local="2026-09-14T11:25:00",
        departure_iata="HND",
        arrival_iata="LAX",
        booking_reference="K3TQ9P",
        category="travel",
        confidence=0.98,
        reasoning="Airline booking confirmation with a reservation number.",
    )
    result = ExtractionResult(
        is_committed=True, gate_reasoning="Booking confirmation.", events=[flight]
    )
    pipeline, _, calendar, store = build(settings, result)

    pipeline.process(fixture_bytes("flight_jsonld.eml"))

    calendar_id, body = calendar.inserted[0]
    assert calendar_id == "id-of-sebastiens-travels"
    assert body["start"] == {"dateTime": "2026-09-14T18:35:00+09:00", "timeZone": "Asia/Tokyo"}
    assert body["end"] == {
        "dateTime": "2026-09-14T11:25:00-07:00",
        "timeZone": "America/Los_Angeles",
    }
    assert body["extendedProperties"]["private"]["e2c_msg_id"] == "<flight-k3tq9p@ana.example>"
    store.close()


def test_marketing_email_creates_nothing(settings: Settings) -> None:
    result = ExtractionResult(
        is_committed=False,
        gate_reasoning="A digest of concerts the recipient has not bought tickets for.",
        events=[],
    )
    pipeline, _, calendar, store = build(settings, result)

    outcome = pipeline.process(fixture_bytes("promo_image_heavy.eml"))

    assert not outcome.committed
    assert calendar.inserted == []
    assert "not bought" in outcome.reason
    store.close()


def test_low_confidence_events_are_skipped_not_written(settings: Settings) -> None:
    settings.min_confidence = 0.75
    result = ExtractionResult(
        is_committed=True,
        gate_reasoning="Looks like a booking.",
        events=[concert_event(confidence=0.4)],
    )
    pipeline, _, calendar, store = build(settings, result)

    outcome = pipeline.process(fixture_bytes("concert_ics.eml"))

    assert calendar.inserted == []
    assert outcome.skipped == [("Radiohead at The O2", "confidence 0.40")]
    store.close()


def test_unknown_category_falls_back_to_the_default_calendar(settings: Settings) -> None:
    result = ExtractionResult(
        is_committed=True,
        gate_reasoning="Confirmed.",
        events=[concert_event(category="gardening")],
    )
    pipeline, _, calendar, store = build(settings, result)
    pipeline.process(fixture_bytes("concert_ics.eml"))

    assert calendar.inserted[0][0] == "id-of-primary"
    store.close()


def test_reprocessing_the_same_email_does_not_duplicate(settings: Settings) -> None:
    result = ExtractionResult(
        is_committed=True, gate_reasoning="Confirmed.", events=[concert_event()]
    )
    pipeline, extractor, calendar, store = build(settings, result)
    raw = fixture_bytes("concert_ics.eml")

    pipeline.process(raw)
    store.record_event(calendar.inserted[0][1]["id"], "<order-tck88213@ticketing.example>", "x")
    pipeline.process(raw)

    assert len(calendar.inserted) == 1
    # The second pass was served from the cache rather than re-billing the model.
    assert extractor.calls == 1
    store.close()


def test_already_seen_messages_short_circuit_before_the_model(settings: Settings) -> None:
    """What protects us after a UIDVALIDITY resync re-offers the whole folder."""
    result = ExtractionResult(
        is_committed=True, gate_reasoning="Confirmed.", events=[concert_event()]
    )
    pipeline, extractor, calendar, store = build(settings, result)
    raw = fixture_bytes("concert_ics.eml")

    pipeline.process(raw)
    store.mark_seen("<order-tck88213@ticketing.example>")
    second = pipeline.process(raw)

    assert second.reason == "already processed"
    assert extractor.calls == 1
    assert len(calendar.inserted) == 1

    # replay deliberately opts out of the seen-check so it stays useful for debugging,
    # and the deterministic event id still stops it double-booking anything.
    replayed = pipeline.process(raw, skip_seen=False)
    assert replayed.committed
    assert replayed.reason == "Confirmed."
    assert len(calendar.inserted) == 1
    store.close()


def test_dry_run_writes_nothing(settings: Settings) -> None:
    settings.dry_run = True
    result = ExtractionResult(
        is_committed=True, gate_reasoning="Confirmed.", events=[concert_event()]
    )
    pipeline, _, calendar, store = build(settings, result)

    outcome = pipeline.process(fixture_bytes("concert_ics.eml"))

    assert calendar.inserted == []
    assert outcome.created == [("Music", "Radiohead at The O2")]
    store.close()


def test_web_lookup_only_fires_when_local_resolution_fails(settings: Settings) -> None:
    settings.enable_web_search = True
    unresolvable = concert_event()
    unresolvable.location = "Klubben Bakgården"
    result = ExtractionResult(is_committed=True, gate_reasoning="Confirmed.", events=[unresolvable])
    pipeline, extractor, _, store = build(settings, result)
    pipeline.process(fixture_bytes("concert_ics.eml"))
    assert extractor.timezone_lookups == ["Klubben Bakgården"]

    resolvable = concert_event()  # location contains "London"
    pipeline2, extractor2, _, store2 = build(
        settings, ExtractionResult(is_committed=True, gate_reasoning="ok", events=[resolvable])
    )
    pipeline2.process(fixture_bytes("restaurant_plain.eml"))
    assert extractor2.timezone_lookups == []

    store.close()
    store2.close()


def test_bad_api_key_stops_the_service_instead_of_dropping_mail(settings: Settings) -> None:
    """A per-message catch would silently bin every email until someone noticed."""

    class RejectingExtractor(StubExtractor):
        def extract(self, doc: EmailDocument) -> ExtractionResult:
            raise anthropic.AuthenticationError(
                "invalid x-api-key",
                response=httpx.Response(401, request=httpx.Request("POST", "https://x")),
                body=None,
            )

    store = Store(settings.state_db)
    pipeline = Pipeline(
        settings,
        store,
        RejectingExtractor(ExtractionResult(is_committed=False, gate_reasoning="")),
        StubCalendar(),
    )

    with pytest.raises(AuthenticationFatal):
        _process_one(pipeline, store, 1, fixture_bytes("concert_ics.eml"))
    store.close()


def test_ordinary_failures_do_not_stop_the_service(settings: Settings) -> None:
    class BrokenExtractor(StubExtractor):
        def extract(self, doc: EmailDocument) -> ExtractionResult:
            raise ValueError("model returned nonsense")

    store = Store(settings.state_db)
    pipeline = Pipeline(
        settings,
        store,
        BrokenExtractor(ExtractionResult(is_committed=False, gate_reasoning="")),
        StubCalendar(),
    )

    _process_one(pipeline, store, 1, fixture_bytes("concert_ics.eml"))  # must not raise
    store.close()


def test_calendar_routing_is_case_insensitive() -> None:
    settings = Settings(
        categories=[
            CategoryRule(name="Travel", description="Flights.", calendar="Sebastiens Travels")
        ],
        default_calendar="primary",
    )
    assert settings.calendar_for("TRAVEL") == "Sebastiens Travels"
    assert settings.calendar_for("travel") == "Sebastiens Travels"
    assert settings.calendar_for("music") == "primary"
    assert settings.calendar_for(None) == "primary"
