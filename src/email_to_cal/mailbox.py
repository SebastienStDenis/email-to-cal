"""iCloud IMAP: one connection, IDLE for new mail, a durable UID cursor.

iCloud has three habits that shape this module:
  - it does not advertise IDLE in the pre-auth CAPABILITY greeting, only after LOGIN
  - it allows only a handful of concurrent connections, shared with the owner's phone
  - it has been seen emitting malformed ENVELOPEs, so we fetch and parse raw MIME
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from imap_tools import AND, MailBox
from imap_tools.errors import MailboxLoginError

from .config import Settings
from .store import Store

log = logging.getLogger(__name__)


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

    def _sync_cursor(self, box: MailBox) -> int:
        """Return the UID to start fetching from, resyncing if UIDVALIDITY moved."""
        folder = self._settings.imap_folder
        uidvalidity = int(box.folder.status(folder, ["UIDVALIDITY"])["UIDVALIDITY"])
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
        days = self._settings.first_run_lookback_days
        if days <= 0:
            uids = box.uids()
            return (max(int(u) for u in uids) + 1) if uids else 1
        since = (datetime.now(UTC) - timedelta(days=days)).date()
        uids = box.uids(AND(date_gte=since))
        return min(int(u) for u in uids) if uids else 1

    def _save_cursor(self, box: MailBox, last_uid: int) -> None:
        folder = self._settings.imap_folder
        uidvalidity = int(box.folder.status(folder, ["UIDVALIDITY"])["UIDVALIDITY"])
        self._store.set_cursor(folder, uidvalidity, last_uid)

    # -- fetching -------------------------------------------------------------------

    def fetch_new(self, box: MailBox) -> Iterator[tuple[int, bytes]]:
        """Yield (uid, raw_rfc822) for everything past the cursor, advancing as we go."""
        start = self._sync_cursor(box)
        # `n:*` always returns at least the highest UID, so filter against the cursor.
        for message in box.fetch(f"UID {start}:*", mark_seen=False, bulk=False):
            uid = int(message.uid) if message.uid else 0
            if uid < start:
                continue
            yield uid, message.obj.as_bytes()
            self._save_cursor(box, uid)

    def idle(self, box: MailBox) -> bool:
        """Wait for activity. Returns True if the server reported something."""
        try:
            responses = box.idle.wait(timeout=self._settings.imap_idle_seconds)
        except TimeoutError:
            return False
        return bool(responses)


def backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter. iCloud punishes reconnect storms."""
    return float(min(2**attempt, 300)) + random.uniform(0, 5)


def sleep_with_backoff(attempt: int) -> None:
    delay = backoff_delay(attempt)
    log.info("reconnecting in %.1fs", delay)
    time.sleep(delay)
