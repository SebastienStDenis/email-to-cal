"""What happens to one flagged email, and what happens when it cannot be processed."""

from __future__ import annotations

import time
from typing import Any

import anthropic
import httpx
import pytest

from email_to_cal.app import RETRY_DELAYS, NoEventsFound, Pipeline, _handle
from email_to_cal.config import Settings
from email_to_cal.mailbox import AuthenticationFatal, FlaggedMail
from email_to_cal.schema import EmailDocument, ExtractedEvent, ExtractionResult
from email_to_cal.store import Store

from .conftest import fixture_bytes

CALENDAR_URL = "https://example.test/calendars/home/"


def concert(title: str = "Radiohead at the O2") -> ExtractedEvent:
    return ExtractedEvent(
        kind="concert",
        title=title,
        all_day=False,
        start_local="2026-09-14T20:00:00",
        start_tz="Europe/London",
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
        self.written: list[tuple[str, bytes]] = []

    def put(self, calendar_url: str, uid: str, ics: bytes) -> None:
        assert calendar_url == CALENDAR_URL
        if self.error is not None:
            raise self.error
        self.written.append((uid, ics))


class StubMailbox:
    def __init__(self) -> None:
        self.unflagged: list[int] = []

    def unflag(self, mail: FlaggedMail) -> None:
        self.unflagged.append(mail.uid)


class StubNotifier:
    def __init__(self) -> None:
        self.created_pushes: list[tuple[list[str], str, str | None]] = []
        self.failed_pushes: list[tuple[str, str, str | None]] = []

    def created(self, events: list[str], calendar: str, link: str | None) -> None:
        self.created_pushes.append((events, calendar, link))

    def failed(self, subject: str, detail: str, link: str | None) -> None:
        self.failed_pushes.append((subject, detail, link))


def build(
    settings: Settings,
    store: Store,
    result: ExtractionResult | Exception,
    calendar: StubCalendar | None = None,
) -> tuple[Pipeline, StubExtractor, StubCalendar]:
    extractor = StubExtractor(result)
    calendar = calendar or StubCalendar()
    pipeline = Pipeline(settings, store, extractor, calendar, CALENDAR_URL)  # type: ignore[arg-type]
    return pipeline, extractor, calendar


def run_handle(
    settings: Settings,
    store: Store,
    result: ExtractionResult | Exception,
    calendar: StubCalendar | None = None,
) -> tuple[StubMailbox, StubNotifier, StubExtractor]:
    pipeline, extractor, _ = build(settings, store, result, calendar)
    mailbox, notifier = StubMailbox(), StubNotifier()
    mail = FlaggedMail(folder="INBOX", uid=7, raw=fixture_bytes("concert_ics.eml"))
    _handle(settings, pipeline, mailbox, store, notifier, mail)  # type: ignore[arg-type]
    return mailbox, notifier, extractor


def test_a_flagged_email_is_written_unflagged_and_pushed(settings: Settings, tmp_path: Any) -> None:
    with Store(settings.state_db) as store:
        mailbox, notifier, _ = run_handle(settings, store, ExtractionResult(events=[concert()]))

        # The flag coming off is the signal to the human that it is done.
        assert mailbox.unflagged == [7]
        assert store.list_failures() == []
        assert len(store.recent_events()) == 1

        events, calendar, link = notifier.created_pushes[0]
        assert events == ["Radiohead at the O2 - Mon 14 Sep 20:00"]
        assert calendar == "Bookings"
        # Success opens the calendar on the day, not the email it came from.
        assert link is not None and link.startswith("calshow:")


def test_every_event_in_one_email_is_written(settings: Settings) -> None:
    with Store(settings.state_db) as store:
        result = ExtractionResult(events=[concert("Outbound"), concert("Return")])
        pipeline, _, calendar = build(settings, store, result)
        doc = EmailDocument(
            message_id="<a@b>", subject="Trip", sender="x", to="y", date=None, body_text=""
        )

        written = pipeline.process(doc)

        # A return flight is two events, and each gets its own resource on the server.
        assert [built.title for built in written] == ["Outbound", "Return"]
        assert len({uid for uid, _ in calendar.written}) == 2


def test_an_email_with_nothing_to_add_keeps_its_flag_and_says_so(settings: Settings) -> None:
    with Store(settings.state_db) as store:
        mailbox, notifier, _ = run_handle(settings, store, ExtractionResult(events=[]))

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


def test_a_transient_failure_is_retried_quietly_before_giving_up(settings: Settings) -> None:
    with Store(settings.state_db) as store:
        error = httpx.ConnectError("calendar server unreachable")
        calendar = StubCalendar(error=error)

        for attempt in range(len(RETRY_DELAYS)):
            mailbox, notifier, _ = run_handle(
                settings, store, ExtractionResult(events=[concert()]), calendar
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
            settings, store, ExtractionResult(events=[concert()]), calendar
        )
        assert len(notifier.failed_pushes) == 1
        assert store.list_failures()[0].given_up


def test_a_message_waiting_for_its_retry_is_left_alone(settings: Settings) -> None:
    with Store(settings.state_db) as store:
        doc_id = "<order-tck88213@ticketing.example>"
        store.record_failure(doc_id, "Concert", 1, "boom", time.time() + 3600)

        _, notifier, extractor = run_handle(settings, store, ExtractionResult(events=[concert()]))

        # Retrying every pass would re-run the model every minute, forever.
        assert extractor.calls == 0
        assert notifier.created_pushes == []


def test_a_message_given_up_on_is_never_tried_again(settings: Settings) -> None:
    with Store(settings.state_db) as store:
        doc_id = "<order-tck88213@ticketing.example>"
        store.record_failure(doc_id, "Concert", 3, "boom", None)

        _, _, extractor = run_handle(settings, store, ExtractionResult(events=[concert()]))

        assert extractor.calls == 0


def test_clearing_the_failure_lets_it_run_again(settings: Settings) -> None:
    with Store(settings.state_db) as store:
        doc_id = "<order-tck88213@ticketing.example>"
        store.record_failure(doc_id, "Concert", 3, "boom", None)
        store.clear_failure(doc_id)

        mailbox, _, extractor = run_handle(settings, store, ExtractionResult(events=[concert()]))

        assert extractor.calls == 1
        assert mailbox.unflagged == [7]


def test_a_recovered_message_forgets_it_ever_failed(settings: Settings) -> None:
    with Store(settings.state_db) as store:
        doc_id = "<order-tck88213@ticketing.example>"
        store.record_failure(doc_id, "Concert", 1, "boom", 0.0)

        run_handle(settings, store, ExtractionResult(events=[concert()]))

        assert store.list_failures() == []


def test_a_rejected_api_key_stops_the_service_instead_of_burning_the_flag(
    settings: Settings,
) -> None:
    with Store(settings.state_db) as store:
        error = anthropic.AuthenticationError(
            "bad key", response=httpx.Response(401, request=httpx.Request("POST", "/")), body=None
        )
        pipeline, _, _ = build(settings, store, error)
        mailbox, notifier = StubMailbox(), StubNotifier()
        mail = FlaggedMail(folder="INBOX", uid=7, raw=fixture_bytes("concert_ics.eml"))

        # Recording this as a per-message failure would quietly write off every flagged
        # email until someone noticed the key was dead.
        with pytest.raises(AuthenticationFatal):
            _handle(settings, pipeline, mailbox, store, notifier, mail)  # type: ignore[arg-type]

        assert store.list_failures() == []
        assert mailbox.unflagged == []


def test_process_raises_rather_than_writing_an_empty_answer(settings: Settings) -> None:
    with Store(settings.state_db) as store:
        pipeline, _, calendar = build(settings, store, ExtractionResult(events=[]))
        doc = EmailDocument(
            message_id="<a@b>", subject="Newsletter", sender="x", to="y", date=None, body_text=""
        )

        with pytest.raises(NoEventsFound):
            pipeline.process(doc)

        assert calendar.written == []
