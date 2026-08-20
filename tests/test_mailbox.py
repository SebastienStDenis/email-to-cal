"""The flag gate: which mail is offered for processing, and how the flag comes off."""

from __future__ import annotations

from typing import Any

import pytest
from imap_tools.errors import MailboxLoginError

from email_to_cal.config import Settings
from email_to_cal.mailbox import RED_FLAG_CRITERIA, AuthenticationFatal, Mailbox

from .conftest import fixture_bytes


class FakeFolder:
    def __init__(self, name: str, flags: tuple[str, ...] = ()) -> None:
        self.name = name
        self.flags = flags


class FakeObj:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def as_bytes(self) -> bytes:
        return self._raw


class FakeMessage:
    def __init__(self, uid: int, raw: bytes) -> None:
        self.uid = str(uid)
        self.obj = FakeObj(raw)


class FakeFolderManager:
    def __init__(self, box: FakeBox) -> None:
        self._box = box

    def list(self) -> list[FakeFolder]:
        return self._box.folders

    def set(self, name: str) -> None:
        self._box.selected = name


class FakeBox:
    """Just enough imap_tools surface for the gate: folders, a search, and a flag write."""

    def __init__(self, contents: dict[str, list[FakeMessage]], folders: list[FakeFolder]) -> None:
        self.contents = contents
        self.folders = folders
        self.selected: str | None = None
        self.searches: list[tuple[str, str]] = []
        self.flag_calls: list[tuple[str, str, Any, bool]] = []
        self.folder = FakeFolderManager(self)
        self.logged_out = False

    def login(self, username: str, password: str) -> None:
        raise AssertionError("login is patched in the tests")

    def fetch(self, criteria: str, **_kwargs: Any) -> list[FakeMessage]:
        assert self.selected is not None
        self.searches.append((self.selected, criteria))
        return self.contents.get(self.selected, [])

    def uids(self, criteria: str) -> list[str]:
        assert self.selected is not None
        self.searches.append((self.selected, criteria))
        return [message.uid for message in self.contents.get(self.selected, [])]

    def flag(self, uid_set: str, flag_set: Any, value: bool) -> None:
        assert self.selected is not None
        self.flag_calls.append((self.selected, uid_set, flag_set, value))

    def logout(self) -> None:
        self.logged_out = True


def connected(settings: Settings, box: FakeBox, monkeypatch: pytest.MonkeyPatch) -> Mailbox:
    monkeypatch.setattr("email_to_cal.mailbox.MailBox", lambda *a, **k: box)
    monkeypatch.setattr(box, "login", lambda username, password: None)
    mailbox = Mailbox(settings)
    mailbox.connect()
    return mailbox


def test_only_red_flagged_mail_is_offered(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = fixture_bytes("flight_jsonld.eml")
    box = FakeBox({"INBOX": [FakeMessage(7, raw)]}, [FakeFolder("INBOX")])
    mailbox = connected(settings, box, monkeypatch)

    found = mailbox.flagged()

    assert [(m.folder, m.uid) for m in found] == [("INBOX", 7)]
    assert found[0].raw == raw
    # Every colour of Apple flag sets \Flagged; only red sets none of the colour bits.
    assert box.searches == [("INBOX", RED_FLAG_CRITERIA)]
    assert "UNKEYWORD $MailFlagBit0" in RED_FLAG_CRITERIA


def test_every_folder_is_searched_because_mail_moves(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = fixture_bytes("flight_jsonld.eml")
    box = FakeBox(
        {"INBOX": [], "Archive": [FakeMessage(3, raw)], "Receipts": [FakeMessage(9, raw)]},
        [FakeFolder("INBOX"), FakeFolder("Archive"), FakeFolder("Receipts")],
    )
    mailbox = connected(settings, box, monkeypatch)

    assert [(m.folder, m.uid) for m in mailbox.flagged()] == [("Archive", 3), ("Receipts", 9)]


def test_junk_and_deleted_mail_is_left_alone(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = FakeBox(
        {},
        [
            FakeFolder("INBOX"),
            FakeFolder("Junk", ("\\Junk",)),
            FakeFolder("Deleted Messages", ("\\Trash",)),
            FakeFolder("Drafts", ("\\Drafts",)),
            FakeFolder("Folders", ("\\Noselect",)),
        ],
    )
    mailbox = connected(settings, box, monkeypatch)

    # A flag on mail you threw away is not a request to put it on your calendar.
    assert mailbox.folders() == ["INBOX"]


def test_unflagging_clears_the_flag_in_the_right_folder(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = fixture_bytes("flight_jsonld.eml")
    box = FakeBox(
        {"INBOX": [], "Archive": [FakeMessage(3, raw)]},
        [FakeFolder("INBOX"), FakeFolder("Archive")],
    )
    mailbox = connected(settings, box, monkeypatch)
    mail = mailbox.flagged()[0]

    mailbox.unflag(mail)

    assert box.flag_calls == [("Archive", "3", "\\Flagged", False)]


def test_counting_what_is_waiting_downloads_nothing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = fixture_bytes("flight_jsonld.eml")
    box = FakeBox(
        {"INBOX": [FakeMessage(1, raw)], "Archive": [FakeMessage(3, raw)]},
        [FakeFolder("INBOX"), FakeFolder("Archive")],
    )
    mailbox = connected(settings, box, monkeypatch)

    def no_bodies(*_args: Any, **_kwargs: Any) -> list[FakeMessage]:
        raise AssertionError("the connection check must not download message bodies")

    monkeypatch.setattr(box, "fetch", no_bodies)

    assert mailbox.count_flagged() == 2


def test_rejected_credentials_stop_rather_than_retry(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = FakeBox({}, [])

    def refuse(username: str, password: str) -> None:
        raise MailboxLoginError("NO", "AUTHENTICATIONFAILED")

    monkeypatch.setattr("email_to_cal.mailbox.MailBox", lambda *a, **k: box)
    monkeypatch.setattr(box, "login", refuse)

    with pytest.raises(AuthenticationFatal, match="app-specific password"):
        Mailbox(settings).connect()


def test_the_local_part_is_tried_when_the_full_address_is_refused(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = FakeBox({}, [])
    tried: list[str] = []

    def login(username: str, password: str) -> None:
        tried.append(username)
        if "@" in username:
            raise MailboxLoginError("NO", "AUTHENTICATIONFAILED")

    monkeypatch.setattr("email_to_cal.mailbox.MailBox", lambda *a, **k: box)
    monkeypatch.setattr(box, "login", login)

    Mailbox(settings).connect()

    # Apple documents the local part as the username and the address as a fallback;
    # in the wild either can be the one that works.
    assert tried == ["test@icloud.com", "test"]
