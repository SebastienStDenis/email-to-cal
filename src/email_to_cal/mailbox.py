r"""iCloud IMAP: find the mail the human flagged, and clear the flag once it is done.

The gate is a red flag in Mail. Apple encodes flag colours as `\Flagged` plus a
`$MailFlagBitN` keyword per colour, and red - the default flag - is the one with no
colour bits at all, so it is the one colour that can be recognised anywhere.

Every selectable folder is searched on every pass, because a flag is set wherever the
mail happens to be filed and mail moves between folders on its own.
"""

from __future__ import annotations

import logging
import random
import threading
from dataclasses import dataclass

from imap_tools import MailBox, MailMessageFlags
from imap_tools.errors import MailboxLoginError

from .config import Settings

log = logging.getLogger(__name__)

# `\Flagged` with none of the colour bits set: Apple's red flag, and nothing else.
RED_FLAG_CRITERIA = (
    "FLAGGED UNKEYWORD $MailFlagBit0 UNKEYWORD $MailFlagBit1 UNKEYWORD $MailFlagBit2"
)
# Folders whose contents are not mail the user is asking about.
SKIP_FOLDER_FLAGS = {"\\Noselect", "\\Junk", "\\Trash", "\\Drafts"}


class AuthenticationFatal(RuntimeError):
    """Credentials were rejected. Retrying will not help and will hammer Apple."""


@dataclass
class FlaggedMail:
    """One red-flagged message, and where it was found."""

    folder: str
    uid: int
    raw: bytes


class Mailbox:
    """A supervised IMAP connection that reads flags and clears them."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._box: MailBox | None = None
        self._selected: str | None = None

    def connect(self) -> None:
        settings = self._settings
        candidates = [settings.apple_id]
        if "@" in settings.apple_id:
            # Apple documents the local part as the username and the full address as a
            # fallback; in the wild either can be the one that works.
            candidates.append(settings.apple_id.split("@", 1)[0])

        last_error: Exception | None = None
        for username in candidates:
            # A fresh MailBox per attempt: a rejected login leaves the old one unusable.
            box = MailBox(settings.imap_host, port=settings.imap_port, timeout=120)
            try:
                box.login(username, settings.apple_password)
            except MailboxLoginError as exc:
                last_error = exc
                log.warning("IMAP login rejected for username %r", username)
                _discard(box)
                continue
            log.info("connected to %s as %r", settings.imap_host, username)
            self._box = box
            # Login selects nothing, so the first search must select its folder itself.
            self._selected = None
            return

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
            self._selected = None

    @property
    def _connection(self) -> MailBox:
        if self._box is None:
            raise RuntimeError("mailbox is not connected")
        return self._box

    def folders(self) -> list[str]:
        """Every folder worth searching, junk and deleted mail excluded."""
        return [
            folder.name
            for folder in self._connection.folder.list()
            if not SKIP_FOLDER_FLAGS.intersection(folder.flags)
        ]

    def _select(self, folder: str) -> None:
        if self._selected != folder:
            self._connection.folder.set(folder)
            self._selected = folder

    def flagged(self) -> list[FlaggedMail]:
        """Every red-flagged message in the account, read in one pass.

        Read eagerly rather than streamed: processing one email takes model calls and
        network writes, and holding a half-consumed IMAP fetch open across all of that
        is what makes iCloud drop the connection.
        """
        found: list[FlaggedMail] = []
        for folder in self.folders():
            self._select(folder)
            for message in self._connection.fetch(RED_FLAG_CRITERIA, mark_seen=False, bulk=False):
                found.append(
                    FlaggedMail(
                        folder=folder, uid=int(message.uid or 0), raw=message.obj.as_bytes()
                    )
                )
        if found:
            log.info("found %d flagged message(s)", len(found))
        return found

    def count_flagged(self) -> int:
        """How many messages are waiting, without downloading any of them."""
        total = 0
        for folder in self.folders():
            self._select(folder)
            total += len(self._connection.uids(RED_FLAG_CRITERIA))
        return total

    def unflag(self, mail: FlaggedMail) -> None:
        """Clear the red flag: the only signal the user gets that a message is done."""
        self._select(mail.folder)
        self._connection.flag(str(mail.uid), MailMessageFlags.FLAGGED, False)


def _discard(box: MailBox) -> None:
    """Release a connection without letting teardown raise."""
    try:
        box.logout()
    except Exception:
        log.debug("IMAP logout failed", exc_info=True)


def backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter. iCloud punishes reconnect storms."""
    return float(min(2**attempt, 300)) + random.uniform(0, 5)


def sleep_with_backoff(attempt: int, stopping: threading.Event) -> None:
    """Interruptible backoff, so a shutdown during a reconnect wait is immediate."""
    delay = backoff_delay(attempt)
    log.info("reconnecting in %.1fs", delay)
    stopping.wait(delay)
