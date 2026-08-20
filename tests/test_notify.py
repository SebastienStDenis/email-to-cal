from __future__ import annotations

from typing import Any

import httpx
import pytest

from email_to_cal.config import Settings
from email_to_cal.notify import Notifier, validate_keys


class RecordingPost:
    """Captures the form payload of every send instead of talking to Pushover."""

    def __init__(self, reply: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reply = reply if reply is not None else {"status": 1}

    def __call__(self, url: str, *, data: dict[str, Any], timeout: float) -> httpx.Response:
        self.calls.append(data)
        return httpx.Response(200, request=httpx.Request("POST", url), json=self.reply)


def configured() -> Settings:
    return Settings(pushover_user="u123", pushover_token="a456")


def test_nothing_is_sent_until_both_keys_are_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = RecordingPost()
    monkeypatch.setattr("email_to_cal.notify.httpx.post", post)

    Notifier(Settings(pushover_user="u123")).created("Radiohead at The O2", "Music")
    Notifier(Settings(pushover_token="a456")).failure("boom")
    Notifier(Settings()).fatal("boom")

    assert post.calls == []


def test_created_events_push_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    post = RecordingPost()
    monkeypatch.setattr("email_to_cal.notify.httpx.post", post)

    Notifier(configured()).created("Radiohead at The O2", "Music")

    (call,) = post.calls
    assert call["token"] == "a456"
    assert call["user"] == "u123"
    assert call["message"] == "Radiohead at The O2 on Music"
    assert call["priority"] == -1
    assert "url" not in call


def test_created_events_carry_a_tappable_link_to_the_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = RecordingPost()
    monkeypatch.setattr("email_to_cal.notify.httpx.post", post)

    Notifier(configured()).created("Radiohead at The O2", "Music", url="calshow:751201200")

    (call,) = post.calls
    assert call["url"] == "calshow:751201200"
    assert call["url_title"] == "Open in Calendar"


def test_an_oversized_link_is_dropped_so_the_push_still_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = RecordingPost()
    monkeypatch.setattr("email_to_cal.notify.httpx.post", post)

    Notifier(configured()).created("Radiohead at The O2", "Music", url="calshow:" + "9" * 512)

    (call,) = post.calls
    assert "url" not in call
    assert call["message"] == "Radiohead at The O2 on Music"


def test_failures_escalate_from_normal_to_high_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = RecordingPost()
    monkeypatch.setattr("email_to_cal.notify.httpx.post", post)
    notifier = Notifier(configured())

    notifier.failure("UID 7: ValueError: model returned nonsense")
    notifier.fatal("Anthropic rejected the API key")

    assert [call["priority"] for call in post.calls] == [0, 1]
    assert post.calls[1]["title"] == "Service stopped"


def test_each_kind_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    post = RecordingPost()
    monkeypatch.setattr("email_to_cal.notify.httpx.post", post)
    settings = configured()
    settings.pushover_notify_events = False
    settings.pushover_notify_errors = False

    notifier = Notifier(settings)
    notifier.created("Radiohead at The O2", "Music")
    notifier.failure("boom")
    notifier.fatal("boom")

    assert post.calls == []


def test_oversized_messages_are_truncated_not_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = RecordingPost()
    monkeypatch.setattr("email_to_cal.notify.httpx.post", post)

    Notifier(configured()).failure("x" * 5000)

    assert len(post.calls[0]["message"]) == 1024


def test_send_failures_never_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(url: str, *, data: dict[str, Any], timeout: float) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr("email_to_cal.notify.httpx.post", explode)

    Notifier(configured()).failure("boom")  # must not raise


def test_validate_keys_reports_the_registered_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = RecordingPost(reply={"status": 1, "devices": ["iphone", "ipad"]})
    monkeypatch.setattr("email_to_cal.notify.httpx.post", post)

    assert validate_keys(configured()) == "keys valid, delivering to: iphone, ipad"


def test_validate_keys_surfaces_pushover_error_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = RecordingPost(reply={"status": 0, "errors": ["application token is invalid"]})
    monkeypatch.setattr("email_to_cal.notify.httpx.post", post)

    with pytest.raises(RuntimeError, match="application token is invalid"):
        validate_keys(configured())
