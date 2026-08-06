"""Orchestration: mail in, calendar events out."""

from __future__ import annotations

import logging
import signal
import types
from dataclasses import dataclass, field

import anthropic

from .config import Settings
from .gcal import CalendarClient, CredentialsExpired, build_event_body
from .llm import Extractor, cache_key
from .mailbox import AuthenticationFatal, Mailbox, sleep_with_backoff
from .mime import parse_email
from .schema import EmailDocument, ExtractedEvent, ExtractionResult
from .store import Store
from .timezones import resolve_zones, valid_zone

log = logging.getLogger(__name__)


@dataclass
class Outcome:
    """What one email produced, for logging and for the replay command."""

    message_id: str
    subject: str
    committed: bool
    reason: str
    created: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)


class Pipeline:
    """Everything that happens to one email, from raw bytes to calendar entries."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        extractor: Extractor,
        calendar: CalendarClient | None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._extractor = extractor
        self._calendar = calendar

    def process(self, raw: bytes, *, skip_seen: bool = True) -> Outcome:
        settings = self._settings
        doc = parse_email(raw, max_attachment_bytes=settings.max_attachment_bytes)

        if skip_seen and self._store.has_seen(doc.message_id):
            # The cheap early-out after a UIDVALIDITY resync, when every message in the
            # folder gets re-offered with fresh UIDs.
            log.debug("already processed %s", doc.message_id)
            return Outcome(
                message_id=doc.message_id,
                subject=doc.subject,
                committed=False,
                reason="already processed",
            )

        log.info(
            "processing %s from %s (tier=%s, attachments=%d)",
            doc.subject[:80],
            doc.sender[:60],
            doc.source_tier,
            len(doc.attachments),
        )

        result = self._extract_cached(doc)
        outcome = Outcome(
            message_id=doc.message_id,
            subject=doc.subject,
            committed=result.is_committed,
            reason=result.gate_reasoning,
        )

        if not result.is_committed:
            log.info("skipping %s: %s", doc.subject[:60], result.gate_reasoning)
            return outcome

        for event in result.events:
            if event.confidence < settings.min_confidence:
                log.info(
                    "skipping low-confidence event %r (%.2f < %.2f): %s",
                    event.title,
                    event.confidence,
                    settings.min_confidence,
                    event.reasoning,
                )
                outcome.skipped.append((event.title, f"confidence {event.confidence:.2f}"))
                continue
            self._emit(doc, event, outcome)

        return outcome

    def _extract_cached(self, doc: EmailDocument) -> ExtractionResult:
        key = cache_key(doc, self._settings)
        cached = self._store.get_cached(key)
        if cached is not None:
            log.debug("using cached extraction for %s", doc.message_id)
            return ExtractionResult.model_validate(cached)
        result = self._extractor.extract(doc)
        self._store.put_cached(key, result.model_dump(mode="json"))
        return result

    def _emit(self, doc: EmailDocument, event: ExtractedEvent, outcome: Outcome) -> None:
        settings = self._settings
        calendar_name = settings.calendar_for(event.category)

        self._resolve_missing_timezone(event)
        body = build_event_body(event, settings, message_id=doc.message_id)

        if settings.dry_run or self._calendar is None:
            log.info("[dry-run] would create on %r: %s", calendar_name, body)
            outcome.created.append((calendar_name, event.title))
            return

        if self._store.has_event(body["id"]):
            log.info("event %s already recorded; skipping", body["id"])
            return

        calendar_id = self._calendar.resolve_calendar(calendar_name)
        event_id = self._calendar.insert(calendar_id, body)
        self._store.record_event(event_id, doc.message_id, calendar_id)
        log.info("created %r on %r (%s)", event.title, calendar_name, event_id)
        outcome.created.append((calendar_name, event.title))

    def _resolve_missing_timezone(self, event: ExtractedEvent) -> None:
        """Only reach for the network when local resolution genuinely failed."""
        if event.all_day or valid_zone(event.start_tz):
            return
        resolved, _ = resolve_zones(
            start_tz=event.start_tz,
            end_tz=event.end_tz,
            departure_iata=event.departure_iata,
            arrival_iata=event.arrival_iata,
            location=event.location,
            default_timezone="",
        )
        if resolved or not event.location:
            return
        looked_up = valid_zone(self._extractor.lookup_timezone(event.location))
        if looked_up:
            log.info("web lookup resolved %r to %s", event.location, looked_up)
            event.start_tz = looked_up


def run(settings: Settings) -> None:
    """Main loop: connect, catch up, idle, repeat. Survives everything but bad credentials."""
    stopping = False

    def _stop(signum: int, _frame: types.FrameType | None) -> None:
        nonlocal stopping
        log.info("received signal %d; shutting down", signum)
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    with Store(settings.state_db) as store:
        calendar = None if settings.dry_run else CalendarClient(settings, store)
        if calendar is not None:
            _bootstrap_calendars(settings, calendar)

        pipeline = Pipeline(settings, store, Extractor(settings), calendar)
        mailbox = Mailbox(settings, store)
        attempt = 0

        while not stopping:
            try:
                box = mailbox.connect()
                attempt = 0
                while not stopping:
                    for uid, raw in mailbox.fetch_new(box):
                        _process_one(pipeline, store, uid, raw)
                    store.beat()
                    mailbox.idle(box)
            except AuthenticationFatal:
                log.critical("fatal authentication failure", exc_info=True)
                raise
            except Exception:
                log.warning("mailbox loop failed; will reconnect", exc_info=True)
                mailbox.close()
                if stopping:
                    break
                sleep_with_backoff(attempt)
                attempt += 1

        mailbox.close()
        log.info("stopped cleanly")


def _process_one(pipeline: Pipeline, store: Store, uid: int, raw: bytes) -> None:
    """One email must never wedge the loop; the cursor advances either way.

    Credential failures are the exception: swallowing those would quietly discard every
    message that arrives until someone notices, so they stop the service instead.
    """
    try:
        doc_id = pipeline.process(raw).message_id
        store.mark_seen(doc_id)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
        log.critical("Anthropic rejected the API key", exc_info=True)
        raise AuthenticationFatal("Anthropic rejected the API key") from None
    except CredentialsExpired:
        log.critical("Google credentials are no longer usable", exc_info=True)
        raise AuthenticationFatal("Google credentials are no longer usable") from None
    except Exception:
        log.error("failed to process UID %d", uid, exc_info=True)


def _bootstrap_calendars(settings: Settings, calendar: CalendarClient) -> None:
    """Resolve or create every configured calendar once, before any mail is handled."""
    wanted = {settings.default_calendar} | {rule.calendar for rule in settings.categories}
    for name in sorted(wanted):
        calendar_id = calendar.resolve_calendar(name)
        log.info("calendar %r -> %s", name, calendar_id)
