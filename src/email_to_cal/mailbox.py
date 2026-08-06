"""iCloud IMAP: one connection, IDLE for new mail, a durable UID cursor.

iCloud has three habits that shape this module:
  - it does not advertise IDLE in the pre-auth CAPABILITY greeting, only after LOGIN
  - it allows only a handful of concurrent connections, shared with the owner's phone
  - it has been seen emitting malformed ENVELOPEs, so we fetch and parse raw MIME
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from imap_tools import AND, MailBox
from imap_tools.errors import MailboxLoginError

from .config import Settings
from .store import Store

log = logging.getLogger(__name__)

# How many times one message may fail before it is written off and skipped.
MAX_ATTEMPTS = 3
# IDLE is re-issued in slices this long so shutdown is not stuck behind a full cycle.
IDLE_SLICE_SECONDS = 10.0


class AuthenticationFatal(RuntimeError):
    """Credentials were rejected. Retrying will not help and will hammer Apple."""


def _discard(box: MailBox) -> None:
    """Release a connection without letting teardown raise."""
    try:
        box.logout()
    except Exception:
        log.debug("IMAP logout failed", exc_info=True)


class Mailbox:
    """A single supervised IMAP connection with a persistent UID cursor."""

    def __init__(self, settings: Settings, store: Store) -> None:
        self._settings = settings
        self._store = store
        self._box: MailBox | None = None
        self._ack: tuple[int, str | None] | None = None

    # -- connection -----------------------------------------------------------------

    def connect(self) -> MailBox:
        settings = self._settings
        # Read timeout must outlast an IDLE cycle so a dead socket raises instead of hanging.
        timeout = settings.imap_idle_seconds + 60

        candidates = [settings.imap_username]
        if "@" in settings.imap_username:
            # Apple documents the local part as the username and the full address as a
            # fallback; in the wild either can be the one that works.
            candidates.append(settings.imap_username.split("@", 1)[0])

        last_error: Exception | None = None
        for username in candidates:
            # A fresh MailBox per attempt: a rejected login leaves the old one unusable.
            box = MailBox(settings.imap_host, port=settings.imap_port, timeout=timeout)
            try:
                box.login(username, settings.imap_password, initial_folder=settings.imap_folder)
            except MailboxLoginError as exc:
                last_error = exc
                log.warning("IMAP login rejected for username %r", username)
                _discard(box)
                continue
            log.info("connected to %s as %r", settings.imap_host, username)
            self._box = box
            return box

        raise AuthenticationFatal(
            "iCloud rejected the credentials. Note that changing the Apple ID password "
            "revokes every app-specific password; generate a new one at appleid.apple.com. "
            f"Last error: {last_error}"
        )

    def close(self) -> None:
        if self._box is not None:
            # Log out rather than dropping the socket: abandoned connections hold one of
            # the few slots iCloud allows until the server times them out.
            _discard(self._box)
            self._box = None

    # -- cursor ---------------------------------------------------------------------

    def _uidvalidity(self, box: MailBox) -> int:
        return int(box.folder.status(self._settings.imap_folder, ["UIDVALIDITY"])["UIDVALIDITY"])

    def _sync_cursor(self, box: MailBox, uidvalidity: int) -> int:
        """Return the UID to start fetching from, resyncing if UIDVALIDITY moved."""
        folder = self._settings.imap_folder
        stored = self._store.get_cursor(folder)

        if stored is None:
            start = self._initial_uid(box)
            log.info("no cursor for %s; starting at UID %d", folder, start)
            self._store.set_cursor(folder, uidvalidity, max(start - 1, 0))
            return start

        stored_validity, last_uid = stored
        if stored_validity != uidvalidity:
            # RFC 4549: every cached UID is meaningless now. The Message-ID set keeps the
            # resulting re-read idempotent.
            log.warning(
                "UIDVALIDITY changed for %s (%d -> %d); resyncing",
                folder,
                stored_validity,
                uidvalidity,
            )
            start = self._initial_uid(box)
            self._store.set_cursor(folder, uidvalidity, max(start - 1, 0))
            return start

        return last_uid + 1

    def _initial_uid(self, box: MailBox) -> int:
        """Where a fresh cursor begins, honouring the configured backfill window."""
        all_uids = [int(u) for u in box.uids()]
        next_uid = max(all_uids) + 1 if all_uids else 1

        days = self._settings.first_run_lookback_days
        if days <= 0:
            return next_uid

        since = (datetime.now(UTC) - timedelta(days=days)).date()
        recent = [int(u) for u in box.uids(AND(date_gte=since))]
        # Nothing inside the window means nothing to backfill. Falling back to UID 1 here
        # would silently replay the entire mailbox through the model.
        return min(recent) if recent else next_uid

    # -- fetching -------------------------------------------------------------------

    def ack(self, uid: int, *, error: str | None = None) -> None:
        """Report the outcome of the message just yielded.

        Without this the cursor would advance on a message the consumer failed to handle,
        which loses it silently: nothing retries it and nothing records that it existed.
        """
        self._ack = (uid, error)

    def fetch_new(self, box: MailBox) -> Iterator[tuple[int, bytes]]:
        """Yield (uid, raw_rfc822) for everything past the cursor.

        The cursor advances only for messages the consumer acknowledges. A failure holds
        the cursor so the next pass retries, and after MAX_ATTEMPTS the message is written
        off to failed_messages and skipped so one poison email cannot stall the mailbox.
        """
        folder = self._settings.imap_folder
        uidvalidity = self._uidvalidity(box)
        start = self._sync_cursor(box, uidvalidity)

        # `n:*` always returns at least the highest UID, so filter against the cursor.
        for message in box.fetch(f"UID {start}:*", mark_seen=False, bulk=False):
            uid = int(message.uid) if message.uid else 0
            if uid < start:
                continue

            self._ack = None
            yield uid, message.obj.as_bytes()

            if self._ack is None or self._ack[0] != uid:
                # The consumer never reported back — treat it as a failure, not a success.
                log.error("UID %d was not acknowledged; holding the cursor", uid)
                return

            _, error = self._ack
            if error is None:
                self._store.clear_failure(folder, uidvalidity, uid)
                self._store.set_cursor(folder, uidvalidity, uid)
                continue

            attempts = self._store.record_failure(folder, uidvalidity, uid, error)
            if attempts < MAX_ATTEMPTS:
                log.warning(
                    "UID %d failed (attempt %d/%d); retrying next cycle: %s",
                    uid,
                    attempts,
                    MAX_ATTEMPTS,
                    error,
                )
                return

            log.critical(
                "giving up on UID %d after %d attempts; skipping it: %s", uid, attempts, error
            )
            self._store.set_cursor(folder, uidvalidity, uid)

    def idle(self, box: MailBox, stopping: threading.Event) -> bool:
        """Wait for activity, in slices short enough that SIGTERM is honoured promptly.

        A single long IDLE would not do: a Python signal handler that returns normally
        makes the interrupted syscall resume with its remaining timeout (PEP 475), so a
        five-minute wait would outlast Docker's ten-second stop grace and get SIGKILLed.
        """
        deadline = time.monotonic() + self._settings.imap_idle_seconds
        while not stopping.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if box.idle.wait(timeout=min(IDLE_SLICE_SECONDS, remaining)):
                return True
        return False


def backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter. iCloud punishes reconnect storms."""
    return float(min(2**attempt, 300)) + random.uniform(0, 5)


def sleep_with_backoff(attempt: int, stopping: threading.Event) -> None:
    """Interruptible backoff, so a shutdown during a reconnect wait is immediate."""
    delay = backoff_delay(attempt)
    log.info("reconnecting in %.1fs", delay)
    stopping.wait(delay)
