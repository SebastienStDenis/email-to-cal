"""What happens to one flagged email, and what happens when it cannot be processed."""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import anthropic
import httpx
import pytest

from email_to_cal.app import RETRY_DELAYS, NoEventsFound, Pipeline, _handle
from email_to_cal.mailbox import AuthenticationFatal, FlaggedMail
from email_to_cal.prefs import Prefs
from email_to_cal.schema import EmailDocument, ExtractedEvent, ExtractionResult
from email_to_cal.store import Store

from .conftest import fixture_bytes

CALENDAR_URLS = {
    "Bookings": "https://example.test/calendars/home/",
    "Music": "https://example.test/calendars/music/",
}

# What the concert fixture's Message-ID is, which is what the store keys on.
CONCERT_ID = "<order-tck88213@ticketing.example>"


def concert(title: str = "Radiohead at the O2", category: str | None = None) -> ExtractedEvent:
    return ExtractedEvent(
        kind="concert",
        title=title,
        all_day=False,
        start_local="2026-09-14T20:00:00",
        start_tz="Europe/London",
        category=category,
    )


class StubExtractor:
    """Returns a canned answer, or raises one, and counts how often it was asked."""

    def __init__(self, result: ExtractionResult | Exception) -> None:
        self.result = result
        self.calls = 0

    def extract(self, doc: EmailDocument) -> ExtractionResult:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class StubCalendar:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.written: list[tuple[str, str]] = []

    def put(self, calendar_url: str, uid: str, ics: bytes) -> None:
        assert calendar_url in CALENDAR_URLS.values()
        if self.error is not None:
            raise self.error
        self.written.append((calendar_url, uid))


class StubMailbox:
    def __init__(self) -> None:
        self.unflagged: list[int] = []

    def unflag(self, mail: FlaggedMail) -> None:
        self.unflagged.append(mail.uid)


class StubNotifier:
    def __init__(self) -> None:
        self.created_pushes: list[tuple[list[str], list[str], str | None]] = []
        self.failed_pushes: list[tuple[str, str, str | None]] = []

    def created(self, events: Sequence[Any], link: str | None) -> None:
        self.created_pushes.append(
            ([e.describe() for e in events], [e.calendar for e in events], link)
        )

    def failed(self, subject: str, detail: str, link: str | None) -> None:
        self.failed_pushes.append((subject, detail, link))


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store(tmp_path / "state.sqlite") as opened:
        yield opened


def build(
    preferences: Prefs,
    store: Store,
    result: ExtractionResult | Exception,
    calendar: StubCalendar | None = None,
) -> tuple[Pipeline, StubExtractor, StubCalendar]:
    extractor = StubExtractor(result)
    calendar = calendar or StubCalendar()
    pipeline = Pipeline(preferences, store, extractor, calendar, CALENDAR_URLS)  # type: ignore[arg-type]
    return pipeline, extractor, calendar


def run_handle(
    preferences: Prefs,
    store: Store,
    result: ExtractionResult | Exception,
    calendar: StubCalendar | None = None,
) -> tuple[StubMailbox, StubNotifier, StubExtractor]:
    pipeline, extractor, _ = build(preferences, store, result, calendar)
    mailbox, notifier = StubMailbox(), StubNotifier()
    mail = FlaggedMail(folder="INBOX", uid=7, raw=fixture_bytes("concert_ics.eml"))
    _handle(pipeline, mailbox, store, notifier, mail)  # type: ignore[arg-type]
    return mailbox, notifier, extractor


def document(subject: str = "s") -> EmailDocument:
    return EmailDocument(
        message_id="<a@b>", subject=subject, sender="x", to="y", date=None, body_text=""
    )


def test_a_flagged_email_is_written_unflagged_and_pushed(preferences: Prefs, store: Store) -> None:
    mailbox, notifier, _ = run_handle(preferences, store, ExtractionResult(events=[concert()]))

    # The flag coming off is the signal to the human that it is done.
    assert mailbox.unflagged == [7]
    assert store.list_failures() == []
    assert len(store.recent_events()) == 1

    events, calendars, link = notifier.created_pushes[0]
    assert events == ["Radiohead at the O2 - Mon 14 Sep 20:00 London time"]
    assert calendars == ["Bookings"]
    # Success opens the calendar on the day, not the email it came from.
    assert link is not None and link.startswith("calshow:")


def test_every_event_in_one_email_is_written(preferences: Prefs, store: Store) -> None:
    result = ExtractionResult(events=[concert("Outbound"), concert("Return")])
    pipeline, _, calendar = build(preferences, store, result)

    written = pipeline.process(document("Trip"))

    # A return flight is two events, and each gets its own resource on the server.
    assert [built.title for built in written] == ["Outbound", "Return"]
    assert len({uid for _, uid in calendar.written}) == 2


def test_an_all_day_event_is_remembered_by_its_day(preferences: Prefs, store: Store) -> None:
    event = ExtractedEvent(kind="other", title="Festival", all_day=True, start_local="2026-08-22")
    pipeline, _, _ = build(preferences, store, ExtractionResult(events=[event]))

    pipeline.process(document())

    # The page says an all-day event by its day, so that is what is kept.
    assert store.recent_events()[0].starts_at == "2026-08-22"


def test_events_route_to_their_category_calendar(store: Store) -> None:
    preferences = Prefs(
        calendar="Bookings",
        categories=[{"name": "music", "description": "Concerts and gigs.", "calendar": "Music"}],
    )
    result = ExtractionResult(events=[concert("Radiohead", category="music"), concert("Dentist")])
    pipeline, _, calendar = build(preferences, store, result)

    written = pipeline.process(document("Mixed"))

    assert [built.calendar for built in written] == ["Music", "Bookings"]
    # An unmatched category falls back to the main calendar rather than failing.
    assert [url for url, _ in calendar.written] == [
        CALENDAR_URLS["Music"],
        CALENDAR_URLS["Bookings"],
    ]


def test_an_unknown_category_falls_back_to_the_main_calendar(store: Store) -> None:
    preferences = Prefs(
        calendar="Bookings",
        categories=[{"name": "music", "description": "Concerts and gigs.", "calendar": "Music"}],
    )
    result = ExtractionResult(events=[concert("Something", category="invented")])
    pipeline, _, _ = build(preferences, store, result)

    # A name the model invented must not send the event to a calendar that does not
    # exist, which would fail the whole email.
    assert pipeline.process(document())[0].calendar == "Bookings"


def test_an_email_with_nothing_to_add_keeps_its_flag_and_says_so(
    preferences: Prefs, store: Store
) -> None:
    mailbox, notifier, _ = run_handle(preferences, store, ExtractionResult(events=[]))

    assert mailbox.unflagged == []
    assert notifier.created_pushes == []
    # Asking the same model the same question again would get the same answer, so
    # the user hears about it immediately rather than after a retry cycle.
    assert len(notifier.failed_pushes) == 1
    _, detail, link = notifier.failed_pushes[0]
    assert "calendar" in detail
    assert link is not None and link.startswith("message://")

    failure = store.list_failures()[0]
    assert failure.given_up


def test_a_transient_failure_is_retried_quietly_before_giving_up(
    preferences: Prefs, store: Store
) -> None:
    error = httpx.ConnectError("calendar server unreachable")
    calendar = StubCalendar(error=error)

    for attempt in range(len(RETRY_DELAYS)):
        mailbox, notifier, _ = run_handle(
            preferences, store, ExtractionResult(events=[concert()]), calendar
        )
        assert mailbox.unflagged == []
        # A network blip that fixes itself should never reach the phone.
        assert notifier.failed_pushes == []
        failure = store.list_failures()[0]
        assert failure.attempts == attempt + 1
        assert not failure.given_up
        # The retry is due later, so the next pass has to leave it alone.
        store.record_failure(
            failure.message_id, failure.subject, failure.attempts, failure.detail, 0.0
        )

    mailbox, notifier, _ = run_handle(
        preferences, store, ExtractionResult(events=[concert()]), calendar
    )
    assert len(notifier.failed_pushes) == 1
    assert store.list_failures()[0].given_up


def test_a_message_waiting_for_its_retry_is_left_alone(preferences: Prefs, store: Store) -> None:
    store.record_failure(CONCERT_ID, "Concert", 1, "boom", time.time() + 3600)

    _, notifier, extractor = run_handle(preferences, store, ExtractionResult(events=[concert()]))

    # Retrying every pass would re-run the model every minute, forever.
    assert extractor.calls == 0
    assert notifier.created_pushes == []


def test_a_message_given_up_on_is_never_tried_again(preferences: Prefs, store: Store) -> None:
    store.record_failure(CONCERT_ID, "Concert", 3, "boom", None)

    _, _, extractor = run_handle(preferences, store, ExtractionResult(events=[concert()]))

    assert extractor.calls == 0


def test_clearing_the_failure_lets_it_run_again(preferences: Prefs, store: Store) -> None:
    store.record_failure(CONCERT_ID, "Concert", 3, "boom", None)
    store.clear_failure(CONCERT_ID)

    mailbox, _, extractor = run_handle(preferences, store, ExtractionResult(events=[concert()]))

    assert extractor.calls == 1
    assert mailbox.unflagged == [7]


def test_an_ignored_message_is_unflagged_without_being_read(
    preferences: Prefs, store: Store
) -> None:
    store.record_failure(CONCERT_ID, "Concert", 3, "boom", None)
    store.dismiss(CONCERT_ID)

    mailbox, notifier, extractor = run_handle(
        preferences, store, ExtractionResult(events=[concert()])
    )

    assert extractor.calls == 0
    assert mailbox.unflagged == [7]
    assert notifier.created_pushes == [] and notifier.failed_pushes == []
    # The decision is spent once the flag is off: a flag put back on later is a person
    # asking for the email to be read.
    assert not store.is_dismissed(CONCERT_ID)


def test_a_dismissal_the_person_beat_to_it_is_forgotten(store: Store) -> None:
    store.dismiss("<gone@b>")
    store.dismiss(CONCERT_ID)

    # A pass that no longer sees the message found its flag already off.
    store.prune_dismissed({CONCERT_ID})

    assert not store.is_dismissed("<gone@b>")
    assert store.is_dismissed(CONCERT_ID)


def test_a_recovered_message_forgets_it_ever_failed(preferences: Prefs, store: Store) -> None:
    store.record_failure(CONCERT_ID, "Concert", 1, "boom", 0.0)

    run_handle(preferences, store, ExtractionResult(events=[concert()]))

    assert store.list_failures() == []


def test_a_rejected_api_key_stops_the_service_instead_of_burning_the_flag(
    preferences: Prefs, store: Store
) -> None:
    error = anthropic.AuthenticationError(
        "bad key", response=httpx.Response(401, request=httpx.Request("POST", "/")), body=None
    )
    pipeline, _, _ = build(preferences, store, error)
    mailbox, notifier = StubMailbox(), StubNotifier()
    mail = FlaggedMail(folder="INBOX", uid=7, raw=fixture_bytes("concert_ics.eml"))

    # Recording this as a per-message failure would quietly write off every flagged
    # email until someone noticed the key was dead.
    with pytest.raises(AuthenticationFatal):
        _handle(pipeline, mailbox, store, notifier, mail)  # type: ignore[arg-type]

    assert store.list_failures() == []
    assert mailbox.unflagged == []


def test_process_raises_rather_than_writing_an_empty_answer(
    preferences: Prefs, store: Store
) -> None:
    pipeline, _, calendar = build(preferences, store, ExtractionResult(events=[]))

    with pytest.raises(NoEventsFound):
        pipeline.process(document("Newsletter"))

    assert calendar.written == []
