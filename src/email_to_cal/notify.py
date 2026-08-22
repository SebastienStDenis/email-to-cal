"""Phone notifications via Pushover: every flagged email is answered, either way.

The flag coming off the message is the quiet signal that it worked. This is the loud
one, and it is the only report a failure gets - so both outcomes carry a link that opens
the original email in Mail.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

import httpx

from .config import Settings

log = logging.getLogger(__name__)

MESSAGES_URL = "https://api.pushover.net/1/messages.json"
VALIDATE_URL = "https://api.pushover.net/1/users/validate.json"

# Pushover rejects longer messages outright; better a truncated alert than none.
MAX_MESSAGE_LENGTH = 1024
# A url over the limit fails the whole request, so an unusable link is dropped instead.
MAX_URL_LENGTH = 512


class Written(Protocol):
    """One created event, as a notification needs to see it."""

    calendar: str

    def describe(self) -> str: ...


class Notifier:
    """Fire-and-forget pushes; a no-op until both Pushover keys are configured.

    Delivery is strictly best-effort: a Pushover outage must never stall or fail mail
    processing, so send failures are logged and swallowed.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def created(self, events: Sequence[Written], link: str | None) -> None:
        """One push per email, however many events and calendars it produced."""
        count = "Event added" if len(events) == 1 else f"{len(events)} events added"
        calendars = {event.calendar for event in events}

        if len(calendars) == 1:
            title = f"{count} to {calendars.pop()}"
            lines = [event.describe() for event in events]
        else:
            # Naming one calendar in the title would be wrong for the other events, so
            # each line carries its own.
            title = count
            lines = [f"{event.describe()} → {event.calendar}" for event in events]

        self._send(title, "\n".join(lines), priority=0, link=link, link_title="Open in Calendar")

    def failed(self, subject: str, detail: str, link: str | None) -> None:
        self._send(
            f"Couldn't process: {subject}"[:250],
            detail,
            priority=0,
            link=link,
            link_title="Open the email",
        )

    def fatal(self, message: str) -> None:
        # High priority: the service has stopped and stays stopped until someone acts.
        self._send("Service stopped", message, priority=1, link=None, link_title="")

    def _send(
        self, title: str, message: str, *, priority: int, link: str | None, link_title: str
    ) -> None:
        settings = self._settings
        if not settings.pushover_user or not settings.pushover_token:
            return
        payload = {
            "token": settings.pushover_token,
            "user": settings.pushover_user,
            "title": title,
            "message": message[:MAX_MESSAGE_LENGTH],
            "priority": priority,
        }
        if link and len(link) <= MAX_URL_LENGTH:
            payload["url"] = link
            payload["url_title"] = link_title
        try:
            response = httpx.post(MESSAGES_URL, data=payload, timeout=10)
            response.raise_for_status()
        except Exception:
            log.warning("pushover notification failed", exc_info=True)


def validate_keys(settings: Settings) -> str:
    """Confirm the user key and application token without sending a notification.

    Pushover answers key problems with a 400 whose body names the bad field, so the
    body is read before the status is judged.
    """
    response = httpx.post(
        VALIDATE_URL,
        data={"token": settings.pushover_token, "user": settings.pushover_user},
        timeout=10,
    )
    payload = response.json()
    if payload.get("status") != 1:
        raise RuntimeError("; ".join(payload.get("errors") or ["validation failed"]))
    devices = ", ".join(payload.get("devices") or []) or "no devices"
    return f"keys valid, delivering to: {devices}"
