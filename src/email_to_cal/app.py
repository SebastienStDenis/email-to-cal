"""Orchestration: flagged mail in, calendar events out.

One pass over the account per interval. Every red-flagged message is read, turned into
events, and unflagged. A message that fails keeps its flag - so it stays visible in Mail
- and is retried a couple of times before the service stops trying and says so.
"""

from __future__ import annotations

import logging
import threading
import time

import anthropic

from .cal import BuiltEvent, CalendarClient, CalendarUnavailable, build_ical, mail_link
from .config import Settings
from .llm import Extractor, make_extractor
from .mailbox import AuthenticationFatal, FlaggedMail, Mailbox, sleep_with_backoff
from .mime import parse_email
from .notify import Notifier
from .schema import EmailDocument
from .store import Failure, Store

log = logging.getLogger(__name__)

# How long to wait before each retry of a message that failed for a transient reason.
# One more failure than there are delays here and the service gives up on the message.
RETRY_DELAYS = (120.0, 600.0)


class NoEventsFound(RuntimeError):
    """The email was read fine, but holds nothing that belongs on a calendar."""


class Pipeline:
    """Everything that happens to one flagged email, from raw bytes to calendar entries."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        extractor: Extractor,
        calendar: CalendarClient,
        calendar_url: str,
    ) -> None:
        self._settings = settings
        self._store = store
        self._extractor = extractor
        self._calendar = calendar
        self._calendar_url = calendar_url

    def process(self, doc: EmailDocument) -> list[BuiltEvent]:
        """Write every event in one email, and report what was written."""
        log.info(
            "processing %s from %s (tier=%s, attachments=%d)",
            doc.subject[:80],
            doc.sender[:60],
            doc.source_tier,
            len(doc.attachments),
        )

        result = self._extractor.extract(doc)
        if not result.events:
            raise NoEventsFound("nothing on this email could be placed on a calendar")

        written = []
        for event in result.events:
            built = build_ical(event, self._settings, message_id=doc.message_id)
            self._calendar.put(self._calendar_url, built.uid, built.ics)
            self._store.record_event(
                built.uid, doc.message_id, built.title, built.starts_at.isoformat()
            )
            log.info("wrote %r (%s)", built.title, built.uid)
            written.append(built)
        return written


def run(settings: Settings, stopping: threading.Event) -> None:
    """Main loop: connect, sweep every folder for flags, sleep, repeat.

    `stopping` is owned by the caller: the CLI sets it from signal handlers, the portal
    sets it to restart the watcher after a config change.
    """
    notifier = Notifier(settings)
    with Store(settings.state_db) as store:
        calendar = CalendarClient(settings)
        try:
            calendar_url = calendar.resolve(settings.calendar_name)
        except CalendarUnavailable as exc:
            log.critical("cannot open the calendar", exc_info=True)
            notifier.fatal(str(exc))
            raise
        log.info("writing to calendar %r (%s)", settings.calendar_name, calendar_url)

        pipeline = Pipeline(settings, store, make_extractor(settings), calendar, calendar_url)
        mailbox = Mailbox(settings)
        attempt = 0
        store.beat()

        while not stopping.is_set():
            try:
                mailbox.connect()
                while not stopping.is_set():
                    for mail in mailbox.flagged():
                        if stopping.is_set():
                            break
                        _handle(settings, pipeline, mailbox, store, notifier, mail)
                        # Beat per message: a long backlog is healthy, not wedged.
                        store.beat()
                    store.beat()
                    # Only a completed pass counts as progress. Resetting on connect
                    # alone would pin the backoff at its first rung when the failure is
                    # downstream of login, which is exactly the reconnect storm iCloud
                    # punishes.
                    attempt = 0
                    stopping.wait(settings.poll_interval_seconds)
            except AuthenticationFatal as exc:
                log.critical("fatal authentication failure", exc_info=True)
                notifier.fatal(str(exc))
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


def _handle(
    settings: Settings,
    pipeline: Pipeline,
    mailbox: Mailbox,
    store: Store,
    notifier: Notifier,
    mail: FlaggedMail,
) -> None:
    """Process one flagged message, then clear its flag or record why it stayed on.

    Credential failures are never swallowed: quietly skipping them would leave every
    flagged email unanswered until someone noticed, so they stop the service instead.
    """
    doc = parse_email(mail.raw, max_attachment_bytes=settings.max_attachment_bytes)
    previous = store.failure(doc.message_id)
    if previous is not None and not _due(previous):
        return

    link = mail_link(doc.message_id)
    try:
        written = pipeline.process(doc)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise AuthenticationFatal("Anthropic rejected the API key") from exc
    except NoEventsFound as exc:
        # Reading the email worked; the answer was "nothing here". Trying again would
        # ask the same model the same question, so the user is told straight away.
        _record_failure(store, notifier, doc, str(exc), previous, link, retryable=False)
    except Exception as exc:
        _record_failure(
            store, notifier, doc, f"{type(exc).__name__}: {exc}", previous, link, retryable=True
        )
        log.error("failed to process %r", doc.subject[:60], exc_info=True)
    else:
        store.clear_failure(doc.message_id)
        mailbox.unflag(mail)
        # The push lands on the event itself: the calendar, on the day it happens. The
        # way back to the email is the link on the event.
        notifier.created(
            [built.describe() for built in written],
            settings.calendar_name,
            written[0].calendar_link,
        )


def _due(failure: Failure) -> bool:
    """Whether a message that failed before may be tried again now."""
    return failure.retry_at is not None and failure.retry_at <= time.time()


def _record_failure(
    store: Store,
    notifier: Notifier,
    doc: EmailDocument,
    detail: str,
    previous: Failure | None,
    link: str | None,
    *,
    retryable: bool,
) -> None:
    """Count the failure, and push once - when there is nothing left to try."""
    attempts = (previous.attempts if previous else 0) + 1
    if retryable and attempts <= len(RETRY_DELAYS):
        delay = RETRY_DELAYS[attempts - 1]
        store.record_failure(doc.message_id, doc.subject, attempts, detail, time.time() + delay)
        log.warning("attempt %d on %r failed; retrying in %.0fs", attempts, doc.subject[:60], delay)
        return

    store.record_failure(doc.message_id, doc.subject, attempts, detail, None)
    log.error("giving up on %r after %d attempt(s): %s", doc.subject[:60], attempts, detail)
    notifier.failed(doc.subject, detail, link)
