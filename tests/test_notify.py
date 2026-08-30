from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from email_to_cal.config import Settings
from email_to_cal.notify import Notifier, validate_keys
from email_to_cal.prefs import Prefs


class RecordingPost:
    """Stands in for httpx.post and keeps what would have been sent."""

    def __init__(self, response: httpx.Response | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response

    def __call__(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(kwargs.get("data", {}))
        if self._response is not None:
            return self._response
        return httpx.Response(200, json={"status": 1}, request=httpx.Request("POST", url))


def configured(**overrides: Any) -> Settings:
    return Settings(pushover_user_key="u", pushover_token="t", **overrides)


def notifier(settings: Settings | None = None, **switches: bool) -> Notifier:
    return Notifier(settings or configured(), Prefs(notifications_enabled=True, **switches))


@pytest.fixture
def post(monkeypatch: pytest.MonkeyPatch) -> RecordingPost:
    recording = RecordingPost()
    monkeypatch.setattr(httpx, "post", recording)
    return recording


@dataclass
class FakeWritten:
    """Stands in for a BuiltEvent: what a notification reads off a created event."""

    line: str
    calendar: str

    def describe(self) -> str:
        return self.line


def test_nothing_is_sent_until_both_keys_are_configured(post: RecordingPost) -> None:
    notifier(Settings(pushover_user_key="u")).created([FakeWritten("e", "Bookings")], None)
    notifier(Settings(pushover_token="t")).failed("s", "d", None)

    assert post.calls == []


def test_nothing_about_an_email_is_sent_while_notifications_are_off(post: RecordingPost) -> None:
    quiet = Notifier(configured(), Prefs())

    quiet.created([FakeWritten("e", "Bookings")], None)
    quiet.failed("s", "d", None)

    # Connecting Pushover is not the same as asking to be pushed at.
    assert post.calls == []


def test_each_outcome_has_its_own_switch(post: RecordingPost) -> None:
    notifier(notify_events=False).created([FakeWritten("e", "Bookings")], None)
    notifier(notify_failures=False).failed("s", "d", None)
    assert post.calls == []

    notifier(notify_failures=False).created([FakeWritten("e", "Bookings")], None)
    notifier(notify_events=False).failed("s", "d", None)
    assert len(post.calls) == 2


def test_a_stopped_service_is_pushed_whatever_the_switches_say(post: RecordingPost) -> None:
    # This is how somebody finds out nothing is being read.
    Notifier(configured(), Prefs()).fatal("iCloud rejected the credentials")
    assert len(post.calls) == 1


def test_a_created_event_pushes_normally_and_opens_the_calendar(post: RecordingPost) -> None:
    notifier().created([FakeWritten("Radiohead - Mon 14 Sep 20:00", "Bookings")], "calshow:123")

    sent = post.calls[0]
    assert sent["priority"] == 0
    assert sent["title"] == "Event added to Bookings"
    assert sent["url"] == "calshow:123"
    assert sent["url_title"] == "Open in Calendar"


def test_several_events_are_one_push_not_several(post: RecordingPost) -> None:
    notifier().created(
        [FakeWritten("Outbound", "Bookings"), FakeWritten("Return", "Bookings")], "calshow:1"
    )

    assert len(post.calls) == 1
    assert post.calls[0]["title"] == "2 events added to Bookings"
    assert post.calls[0]["message"] == "Outbound\nReturn"


def test_events_on_different_calendars_are_named_per_line(post: RecordingPost) -> None:
    notifier().created(
        [FakeWritten("LX318", "Travel"), FakeWritten("Radiohead", "Music")], "calshow:1"
    )

    # Naming one calendar in the title would be wrong for the other event.
    sent = post.calls[0]
    assert sent["title"] == "2 events added"
    assert sent["message"] == "LX318 → Travel\nRadiohead → Music"


def test_a_failure_pushes_a_way_back_to_the_email(post: RecordingPost) -> None:
    notifier().failed("Your booking", "no events found", "message://%3Ca@b%3E")

    sent = post.calls[0]
    assert sent["priority"] == 0
    assert "Your booking" in sent["title"]
    # The push is the only report a failure gets, so it has to lead somewhere.
    assert sent["url"] == "message://%3Ca@b%3E"
    assert sent["url_title"] == "Open the email"


def test_a_stopped_service_pushes_at_high_priority(post: RecordingPost) -> None:
    notifier().fatal("iCloud rejected the credentials")

    assert post.calls[0]["priority"] == 1
    assert "url" not in post.calls[0]


def test_an_oversized_link_is_dropped_so_the_push_still_arrives(post: RecordingPost) -> None:
    notifier().failed("Your booking", "no events found", "message://" + "9" * 512)

    # Pushover rejects the whole request over an oversized url, which would lose the
    # only report a failure gets.
    sent = post.calls[0]
    assert "url" not in sent
    assert sent["message"] == "no events found"


def test_oversized_messages_are_truncated_not_dropped(post: RecordingPost) -> None:
    notifier().failed("s", "x" * 5000, None)

    # Pushover rejects a longer message outright; a truncated alert still arrives.
    assert len(post.calls[0]["message"]) == 1024


def test_send_failures_never_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("pushover is down")

    monkeypatch.setattr(httpx, "post", explode)

    # A notification outage must never stall or fail mail processing.
    notifier().created([FakeWritten("e", "Bookings")], None)


def test_validate_keys_reports_the_registered_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx,
        "post",
        RecordingPost(httpx.Response(200, json={"status": 1, "devices": ["iphone"]})),
    )
    assert "iphone" in validate_keys(configured())


def test_validate_keys_surfaces_pushover_error_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        httpx,
        "post",
        RecordingPost(httpx.Response(400, json={"status": 0, "errors": ["user key is invalid"]})),
    )
    with pytest.raises(RuntimeError, match="user key is invalid"):
        validate_keys(configured())
