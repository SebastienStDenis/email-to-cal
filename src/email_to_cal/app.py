"""Orchestration: mail in, calendar events out."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import anthropic

from .config import Settings
from .gcal import (
    CalendarClient,
    CredentialsExpired,
    build_event_body,
    calendar_link,
    mail_link,
)
from .llm import Extractor, cache_key
from .local_llm import FilterVerdict, OllamaFilter, filter_cache_key, make_prefilter
from .mailbox import AuthenticationFatal, Mailbox, sleep_with_backoff
from .mime import parse_email
from .notify import Notifier
from .schema import EmailDocument, ExtractedEvent, ExtractionResult
from .store import Store

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
        prefilter: OllamaFilter | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._extractor = extractor
        self._calendar = calendar
        self._prefilter = prefilter
        self.notifier = Notifier(settings)

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

        verdict = self._prefilter_cached(doc)
        if verdict is not None and not verdict.could_contain_commitment:
            log.info("filtered locally %s: %s", doc.subject[:60], verdict.reasoning)
            return Outcome(
                message_id=doc.message_id,
                subject=doc.subject,
                committed=False,
                reason=f"filtered locally: {verdict.reasoning}",
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
            exclusion = settings.exclusion_named(event.excluded_by)
            if exclusion is not None:
                log.info("skipping %r: excluded by %r", event.title, exclusion.name)
                outcome.skipped.append((event.title, f"excluded by {exclusion.name!r}"))
                continue
            if event.excluded_by:
                # A name matching no configured rule is the model inventing one; routing
                # it normally beats silently losing an event to a typo.
                log.warning("ignoring unknown exclusion %r on %r", event.excluded_by, event.title)
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

    def _prefilter_cached(self, doc: EmailDocument) -> FilterVerdict | None:
        """The local filter's verdict, or None when the email must go to Claude.

        None comes back when the filter is off, when the email carries structured data
        (.ics / JSON-LD marks a near-certain real booking - the cheap model gets no
        veto over those), and on any filter failure. The filter is a cost optimisation:
        every failure mode falls through to the paid path, never to dropped mail.
        """
        if self._prefilter is None or doc.has_structured_source:
            return None
        key = filter_cache_key(doc, self._settings)
        cached = self._store.get_cached(key)
        if cached is not None:
            return FilterVerdict.model_validate(cached)
        try:
            verdict = self._prefilter.judge(doc)
        except Exception:
            log.warning("local filter failed; sending %s to Claude", doc.subject[:60])
            log.debug("local filter failure detail", exc_info=True)
            return None
        self._store.put_cached(key, verdict.model_dump(mode="json"))
        return verdict

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
        body = build_event_body(event, settings, message_id=doc.message_id)

        if settings.dry_run or self._calendar is None:
            log.info("[dry-run] would create on %r: %s", calendar_name, body)
            outcome.created.append((calendar_name, event.title))
            return

        if self._store.has_event(body["id"]):
            log.info("event %s already recorded; skipping", body["id"])
            return

        calendar_id = self._calendar.resolve_calendar(calendar_name)

        if settings.dedup_window_minutes:
            twin = self._calendar.find_similar(
                calendar_id, body, booking_reference=event.booking_reference
            )
            if twin is not None:
                log.info(
                    "skipping %r: %r (%s) already covers that slot on %r",
                    event.title,
                    twin.get("summary"),
                    twin.get("id"),
                    calendar_name,
                )
                outcome.skipped.append(
                    (event.title, f"already on calendar as {twin.get('summary')!r}")
                )
                return

        event_id = self._calendar.insert(calendar_id, body, url=mail_link(doc.message_id))
        self._store.record_event(event_id, doc.message_id, calendar_id, event.title)
        log.info("created %r on %r (%s)", event.title, calendar_name, event_id)
        outcome.created.append((calendar_name, event.title))
        self.notifier.created(
            event.title,
            calendar_name,
            url=calendar_link(body, default_timezone=settings.default_timezone),
        )


def run(settings: Settings, stopping: threading.Event) -> None:
    """Main loop: connect, catch up, idle, repeat. Survives everything but bad credentials.

    `stopping` is owned by the caller: the CLI sets it from signal handlers, the portal
    sets it to restart the watcher after a config change.
    """
    with Store(settings.state_db) as store:
        calendar = None if settings.dry_run else CalendarClient(settings, store)
        if calendar is not None:
            _bootstrap_calendars(settings, calendar)

        pipeline = Pipeline(
            settings, store, Extractor(settings), calendar, prefilter=make_prefilter(settings)
        )
        mailbox = Mailbox(settings, store)
        attempt = 0
        store.beat()

        sweep_due = 0.0

        while not stopping.is_set():
            try:
                box = mailbox.connect()
                while not stopping.is_set():
                    for uid, raw in mailbox.fetch_new(box):
                        _process_one(pipeline, mailbox, store, uid, raw)
                        # Beat per message: a long backfill is healthy, not wedged.
                        store.beat()

                    if settings.sweep_folders and time.monotonic() >= sweep_due:
                        _sweep(settings, pipeline, mailbox, store, box)
                        sweep_due = time.monotonic() + settings.sweep_interval_minutes * 60

                    store.beat()
                    # Only a completed cycle counts as progress. Resetting on connect
                    # alone would pin the backoff at its first rung when the failure is
                    # downstream of login, which is exactly the reconnect storm iCloud
                    # punishes.
                    attempt = 0
                    mailbox.idle(box, stopping)
            except AuthenticationFatal as exc:
                log.critical("fatal authentication failure", exc_info=True)
                pipeline.notifier.fatal(str(exc))
                raise
            except Exception:
                log.warning("mailbox loop failed; will reconnect", exc_info=True)
                mailbox.close()
                if stopping.is_set():
                    break
                sleep_with_backoff(attempt, stopping)
                attempt += 1

        mailbox.close()
        log.info("stopped cleanly")


def _process_one(pipeline: Pipeline, mailbox: Mailbox, store: Store, uid: int, raw: bytes) -> None:
    """Handle one email and tell the mailbox whether the cursor may move past it.

    Credential failures are never swallowed: quietly skipping them would discard every
    message that arrives until someone noticed, so they stop the service instead.
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
    except Exception as exc:
        log.error("failed to process UID %d", uid, exc_info=True)
        pipeline.notifier.failure(f"UID {uid}: {type(exc).__name__}: {exc}")
        mailbox.ack(uid, error=f"{type(exc).__name__}: {exc}")
    else:
        mailbox.ack(uid)


def _sweep(
    settings: Settings, pipeline: Pipeline, mailbox: Mailbox, store: Store, box: object
) -> None:
    """Catch up on folders mail may have been filed into before IDLE saw it."""
    for folder in settings.sweep_folders:
        try:
            for uid, raw in mailbox.sweep(box, folder):  # type: ignore[arg-type]
                _process_one(pipeline, mailbox, store, uid, raw)
                store.beat()
        except AuthenticationFatal:
            raise
        except Exception:
            # A missing or renamed folder must not take down the INBOX watcher.
            log.warning("sweep of folder %r failed", folder, exc_info=True)


def _bootstrap_calendars(settings: Settings, calendar: CalendarClient) -> None:
    """Resolve or create every configured calendar once, before any mail is handled."""
    wanted = {settings.default_calendar} | {
        rule.calendar for rule in settings.categories if rule.action == "include"
    }
    for name in sorted(wanted):
        calendar_id = calendar.resolve_calendar(name)
        log.info("calendar %r -> %s", name, calendar_id)
