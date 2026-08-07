"""Cursor correctness, exercised against a fake IMAP server.

These are the cases that lose or replay mail in production, and none of them are
reachable without simulating UIDVALIDITY changes and mid-iteration failures.
"""

from __future__ import annotations

from typing import Any

import pytest

from email_to_cal.config import Settings
from email_to_cal.mailbox import MAX_ATTEMPTS, Mailbox
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


class FakeFolderManagerMulti(FakeFolderManager):
    def set(self, folder: str) -> None:
        self._box.selected = folder


class FakeBox:
    """Just enough IMAP to exercise the cursor: UID-keyed folders and a UIDVALIDITY."""

    def __init__(
        self,
        uids: list[int],
        uidvalidity: int = 100,
        folders: dict[str, dict[int, bytes]] | None = None,
    ) -> None:
        self.folders: dict[str, dict[int, bytes]] = folders or {}
        self.folders["INBOX"] = {uid: fixture_bytes("restaurant_plain.eml") for uid in uids}
        self.selected = "INBOX"
        self.uidvalidity = uidvalidity
        self.folder = FakeFolderManagerMulti(self)
        self.fetch_criteria: list[str] = []

    @property
    def messages(self) -> dict[int, bytes]:
        return self.folders[self.selected]

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


def drain(mb: Mailbox, box: FakeBox, fail_on: set[int] | None = None) -> list[int]:
    """Consume a pass the way the app does: acknowledge every message either way."""
    seen = []
    for uid, _ in mb.fetch_new(box):
        seen.append(uid)
        if fail_on and uid in fail_on:
            mb.ack(uid, error="boom")
        else:
            mb.ack(uid)
    return seen


def test_first_run_skips_existing_mail_by_default(
    settings: Settings, mailbox: tuple[Mailbox, Store]
) -> None:
    mb, store = mailbox
    box = FakeBox([1, 2, 3])

    assert drain(mb, box) == []
    assert store.get_cursor("INBOX") == (100, 3)
    store.close()


def test_new_mail_after_the_cursor_is_yielded_once(
    settings: Settings, mailbox: tuple[Mailbox, Store]
) -> None:
    mb, store = mailbox
    box = FakeBox([1, 2, 3])
    drain(mb, box)

    box.messages[4] = fixture_bytes("concert_ics.eml")
    assert drain(mb, box) == [4]
    assert store.get_cursor("INBOX") == (100, 4)

    # A second pass with nothing new must yield nothing, despite `4:*` returning UID 4.
    assert drain(mb, box) == []
    store.close()


def test_cursor_does_not_advance_past_a_message_that_raises(
    settings: Settings, mailbox: tuple[Mailbox, Store]
) -> None:
    mb, store = mailbox
    box = FakeBox([1])
    drain(mb, box)
    box.messages.update({2: b"a", 3: b"b", 4: b"c"})

    with pytest.raises(RuntimeError):
        for uid, _ in mb.fetch_new(box):
            if uid == 3:
                raise RuntimeError("boom")
            mb.ack(uid)

    # UID 2 completed, 3 did not. Next pass must retry from 3.
    assert store.get_cursor("INBOX") == (100, 2)
    assert drain(mb, box) == [3, 4]
    store.close()


def test_a_failed_message_is_retried_and_then_written_off(
    settings: Settings, mailbox: tuple[Mailbox, Store]
) -> None:
    """The failure that used to vanish: caught by the consumer, so the generator resumed
    normally and the cursor sailed past a message nothing had handled."""
    mb, store = mailbox
    box = FakeBox([1])
    drain(mb, box)
    box.messages.update({2: b"a", 3: b"b"})

    # Each pass retries UID 2 and stops there, leaving 3 untouched.
    for expected_attempts in (1, 2):
        assert drain(mb, box, fail_on={2}) == [2]
        assert store.get_cursor("INBOX") == (100, 1)
        assert store.list_failures()[0][3] == expected_attempts

    # Third strike: UID 2 is written off rather than stalling the mailbox forever, and the
    # pass carries on to the message stuck behind it.
    assert drain(mb, box, fail_on={2}) == [2, 3]
    assert store.get_cursor("INBOX") == (100, 3)
    assert store.list_failures()[0][3] == MAX_ATTEMPTS

    assert drain(mb, box) == []
    store.close()


def test_a_recovered_message_clears_its_failure_record(
    settings: Settings, mailbox: tuple[Mailbox, Store]
) -> None:
    mb, store = mailbox
    box = FakeBox([1])
    drain(mb, box)
    box.messages[2] = b"a"

    drain(mb, box, fail_on={2})
    assert store.list_failures()

    drain(mb, box)
    assert store.list_failures() == []
    assert store.get_cursor("INBOX") == (100, 2)
    store.close()


def test_an_unacknowledged_message_holds_the_cursor(
    settings: Settings, mailbox: tuple[Mailbox, Store]
) -> None:
    mb, store = mailbox
    box = FakeBox([1])
    drain(mb, box)
    box.messages.update({2: b"a", 3: b"b"})

    # A consumer that forgets to ack must not be read as success.
    assert [uid for uid, _ in mb.fetch_new(box)] == [2]
    assert store.get_cursor("INBOX") == (100, 1)
    store.close()


def test_uidvalidity_change_resyncs_instead_of_replaying(
    settings: Settings, mailbox: tuple[Mailbox, Store]
) -> None:
    mb, store = mailbox
    box = FakeBox([1, 2, 3])
    drain(mb, box)

    # The server renumbered everything; the old cursor is meaningless.
    box.uidvalidity = 200
    box.folders["INBOX"] = {u: fixture_bytes("restaurant_plain.eml") for u in (1, 2, 3)}

    assert drain(mb, box) == []
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

    assert drain(mb, box) == []
    assert store.get_cursor("INBOX") == (100, 5)
    store.close()


def test_sweep_picks_up_mail_filed_away_before_idle_saw_it(settings: Settings) -> None:
    """The archive race: a message moved out of INBOX gets a new UID somewhere else."""
    settings.sweep_folders = ["Archive"]
    store = Store(settings.state_db)
    mb = Mailbox(settings, store)
    box = FakeBox([1], folders={"Archive": {50: fixture_bytes("concert_ics.eml")}})

    # First contact with Archive establishes its cursor without replaying its history.
    assert list(mb.sweep(box, "Archive")) == []
    assert store.get_cursor("Archive") == (100, 50)

    box.folders["Archive"][51] = fixture_bytes("flight_jsonld.eml")
    swept = []
    for uid, _ in mb.sweep(box, "Archive"):
        swept.append(uid)
        mb.ack(uid)

    assert swept == [51]
    assert store.get_cursor("Archive") == (100, 51)
    store.close()


def test_sweep_never_backfills_even_with_a_lookback_configured(settings: Settings) -> None:
    """An archive holds years of mail that already passed through INBOX."""
    settings.sweep_folders = ["Archive"]
    settings.first_run_lookback_days = 30
    store = Store(settings.state_db)
    mb = Mailbox(settings, store)
    box = FakeBox([1], folders={"Archive": {u: b"old" for u in range(1, 500)}})

    assert list(mb.sweep(box, "Archive")) == []
    assert store.get_cursor("Archive") == (100, 499)
    store.close()


def test_sweep_restores_the_watched_folder_even_when_it_fails(settings: Settings) -> None:
    """IDLE only applies to the selected folder, so leaving Archive selected would mean
    silently never seeing new INBOX mail again."""
    settings.sweep_folders = ["Archive"]
    store = Store(settings.state_db)
    mb = Mailbox(settings, store)
    box = FakeBox([1], folders={"Archive": {50: b"x"}})
    list(mb.sweep(box, "Archive"))
    assert box.selected == "INBOX"

    box.folders["Archive"][51] = b"y"
    with pytest.raises(RuntimeError):
        for _uid, _raw in mb.sweep(box, "Archive"):
            raise RuntimeError("boom")
    assert box.selected == "INBOX"
    store.close()


def test_each_folder_keeps_its_own_cursor(settings: Settings) -> None:
    settings.sweep_folders = ["Archive"]
    store = Store(settings.state_db)
    mb = Mailbox(settings, store)
    box = FakeBox([1, 2], folders={"Archive": {50: b"x"}})

    drain(mb, box)
    list(mb.sweep(box, "Archive"))

    assert store.get_cursor("INBOX") == (100, 2)
    assert store.get_cursor("Archive") == (100, 50)
    store.close()


def test_empty_mailbox(settings: Settings, mailbox: tuple[Mailbox, Store]) -> None:
    mb, store = mailbox
    assert drain(mb, FakeBox([])) == []
    assert store.get_cursor("INBOX") == (100, 0)
    store.close()
