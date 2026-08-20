from __future__ import annotations

from typing import Any

import httpx
import pytest

from email_to_cal.config import Settings
from email_to_cal.notify import Notifier, validate_keys


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
    return Settings(pushover_user="u", pushover_token="t", **overrides)


def test_nothing_is_sent_until_both_keys_are_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = RecordingPost()
    monkeypatch.setattr(httpx, "post", post)

    Notifier(Settings(pushover_user="u")).created(["e"], "Bookings", None)
    Notifier(Settings(pushover_token="t")).failed("s", "d", None)

    assert post.calls == []


def test_a_created_event_pushes_quietly_and_opens_the_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = RecordingPost()
    monkeypatch.setattr(httpx, "post", post)

    Notifier(configured()).created(["Radiohead - Mon 14 Sep 20:00"], "Bookings", "calshow:123")

    sent = post.calls[0]
    # Quiet: an email that books something at 3am should not wake anyone.
    assert sent["priority"] == -1
    assert sent["title"] == "Event added to Bookings"
    assert sent["url"] == "calshow:123"
    assert sent["url_title"] == "Open in Calendar"


def test_several_events_are_one_push_not_several(monkeypatch: pytest.MonkeyPatch) -> None:
    post = RecordingPost()
    monkeypatch.setattr(httpx, "post", post)

    Notifier(configured()).created(["Outbound", "Return"], "Bookings", "calshow:1")

    assert len(post.calls) == 1
    assert post.calls[0]["title"] == "2 events added to Bookings"
    assert post.calls[0]["message"] == "Outbound\nReturn"


def test_a_failure_pushes_a_way_back_to_the_email(monkeypatch: pytest.MonkeyPatch) -> None:
    post = RecordingPost()
    monkeypatch.setattr(httpx, "post", post)

    Notifier(configured()).failed("Your booking", "no events found", "message://%3Ca@b%3E")

    sent = post.calls[0]
    assert sent["priority"] == 0
    assert "Your booking" in sent["title"]
    # The push is the only report a failure gets, so it has to lead somewhere.
    assert sent["url"] == "message://%3Ca@b%3E"
    assert sent["url_title"] == "Open the email"


def test_a_stopped_service_pushes_at_high_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    post = RecordingPost()
    monkeypatch.setattr(httpx, "post", post)

    Notifier(configured()).fatal("iCloud rejected the credentials")

    assert post.calls[0]["priority"] == 1
    assert "url" not in post.calls[0]


def test_an_oversized_link_is_dropped_so_the_push_still_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = RecordingPost()
    monkeypatch.setattr(httpx, "post", post)

    Notifier(configured()).failed("Your booking", "no events found", "message://" + "9" * 512)

    # Pushover rejects the whole request over an oversized url, which would lose the
    # only report a failure gets.
    sent = post.calls[0]
    assert "url" not in sent
    assert sent["message"] == "no events found"


def test_oversized_messages_are_truncated_not_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    post = RecordingPost()
    monkeypatch.setattr(httpx, "post", post)

    Notifier(configured()).failed("s", "x" * 5000, None)

    # Pushover rejects a longer message outright; a truncated alert still arrives.
    assert len(post.calls[0]["message"]) == 1024


def test_send_failures_never_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("pushover is down")

    monkeypatch.setattr(httpx, "post", explode)

    # A notification outage must never stall or fail mail processing.
    Notifier(configured()).created(["e"], "Bookings", None)


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
