"""Cursor correctness, exercised against a fake IMAP server.

These are the cases that lose or replay mail in production, and none of them are
reachable without simulating UIDVALIDITY changes and mid-iteration failures.
"""

from __future__ import annotations

from typing import Any

import pytest

from email_to_cal.config import Settings
from email_to_cal.mailbox import Mailbox
from email_to_cal.store import Store

from .conftest import fixture_bytes


class FakeMessage:
    def __init__(self, uid: int, raw: bytes) -> None:
        self.uid = str(uid)
        self.obj = FakeObj(raw)


class FakeObj:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def as_bytes(self) -> bytes:
        return self._raw


class FakeFolderManager:
    def __init__(self, box: FakeBox) -> None:
        self._box = box

    def status(self, folder: str, options: list[str]) -> dict[str, int]:
        return {"UIDVALIDITY": self._box.uidvalidity}


class FakeBox:
    """Just enough IMAP to exercise the cursor: a UID-keyed store and a UIDVALIDITY."""

    def __init__(self, uids: list[int], uidvalidity: int = 100) -> None:
        self.messages = {uid: fixture_bytes("restaurant_plain.eml") for uid in uids}
        self.uidvalidity = uidvalidity
        self.folder = FakeFolderManager(self)
        self.fetch_criteria: list[str] = []

    def uids(self, criteria: Any = "ALL") -> list[str]:
        if criteria == "ALL":
            return [str(u) for u in sorted(self.messages)]
        # Stand-in for a date-range search: the caller's window matches nothing here.
        return []

    def fetch(self, criteria: str, **_: Any) -> list[FakeMessage]:
        self.fetch_criteria.append(criteria)
        start = int(criteria.split()[1].split(":")[0])
        matching = sorted(u for u in self.messages if u >= start)
        if not matching and self.messages:
            # Real IMAP always returns the highest UID for `n:*`, even when n is past it.
            matching = [max(self.messages)]
        return [FakeMessage(u, self.messages[u]) for u in matching]


@pytest.fixture
def mailbox(settings: Settings) -> tuple[Mailbox, Store]:
    store = Store(settings.state_db)
    return Mailbox(settings, store), store


def test_first_run_skips_existing_mail_by_default(
    settings: Settings, mailbox: tuple[Mailbox, Store]
) -> None:
    mb, store = mailbox
    box = FakeBox([1, 2, 3])

    assert list(mb.fetch_new(box)) == []
    assert store.get_cursor("INBOX") == (100, 3)
    store.close()


def test_new_mail_after_the_cursor_is_yielded_once(
    settings: Settings, mailbox: tuple[Mailbox, Store]
) -> None:
    mb, store = mailbox
    box = FakeBox([1, 2, 3])
    list(mb.fetch_new(box))

    box.messages[4] = fixture_bytes("concert_ics.eml")
    assert [uid for uid, _ in mb.fetch_new(box)] == [4]
    assert store.get_cursor("INBOX") == (100, 4)

    # A second pass with nothing new must yield nothing, despite `4:*` returning UID 4.
    assert list(mb.fetch_new(box)) == []
    store.close()


def test_cursor_does_not_advance_past_a_message_that_raises(
    settings: Settings, mailbox: tuple[Mailbox, Store]
) -> None:
    mb, store = mailbox
    box = FakeBox([1])
    list(mb.fetch_new(box))
    box.messages.update({2: b"a", 3: b"b", 4: b"c"})

    with pytest.raises(RuntimeError):
        for uid, _ in mb.fetch_new(box):
            if uid == 3:
                raise RuntimeError("boom")

    # UID 2 completed, 3 did not. Next pass must retry from 3.
    assert store.get_cursor("INBOX") == (100, 2)
    assert [uid for uid, _ in mb.fetch_new(box)] == [3, 4]
    store.close()


def test_uidvalidity_change_resyncs_instead_of_replaying(
    settings: Settings, mailbox: tuple[Mailbox, Store]
) -> None:
    mb, store = mailbox
    box = FakeBox([1, 2, 3])
    list(mb.fetch_new(box))

    # The server renumbered everything; the old cursor is meaningless.
    box.uidvalidity = 200
    box.messages = {u: fixture_bytes("restaurant_plain.eml") for u in (1, 2, 3)}

    assert list(mb.fetch_new(box)) == []
    assert store.get_cursor("INBOX") == (200, 3)
    store.close()


def test_lookback_window_with_no_matches_does_not_replay_the_mailbox(
    settings: Settings,
) -> None:
    """The bug this guards: an empty date search must not reset the cursor to UID 1."""
    settings.first_run_lookback_days = 7
    store = Store(settings.state_db)
    mb = Mailbox(settings, store)
    box = FakeBox([1, 2, 3, 4, 5])

    assert list(mb.fetch_new(box)) == []
    assert store.get_cursor("INBOX") == (100, 5)
    store.close()


def test_empty_mailbox(settings: Settings, mailbox: tuple[Mailbox, Store]) -> None:
    mb, store = mailbox
    assert list(mb.fetch_new(FakeBox([]))) == []
    assert store.get_cursor("INBOX") == (100, 0)
    store.close()
