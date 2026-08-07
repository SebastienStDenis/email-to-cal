"""Phone notifications via Pushover: created events and failures that need a person."""

from __future__ import annotations

import logging

import httpx

from .config import Settings

log = logging.getLogger(__name__)

MESSAGES_URL = "https://api.pushover.net/1/messages.json"
VALIDATE_URL = "https://api.pushover.net/1/users/validate.json"

# Pushover rejects longer messages outright; better a truncated alert than none.
MAX_MESSAGE_LENGTH = 1024


class Notifier:
    """Fire-and-forget pushes; a no-op until both Pushover keys are configured.

    Delivery is strictly best-effort: a Pushover outage must never stall or fail mail
    processing, so send failures are logged and swallowed.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def created(self, title: str, calendar: str) -> None:
        # Quiet priority: informational, so no chime when a 3am email books something.
        if self._settings.pushover_notify_events:
            self._send("Event created", f"{title} on {calendar}", priority=-1)

    def failure(self, message: str) -> None:
        if self._settings.pushover_notify_errors:
            self._send("Processing failure", message, priority=0)

    def fatal(self, message: str) -> None:
        # High priority: the service has stopped and stays stopped until someone acts.
        if self._settings.pushover_notify_errors:
            self._send("Service stopped", message, priority=1)

    def _send(self, title: str, message: str, *, priority: int) -> None:
        settings = self._settings
        if not settings.pushover_user or not settings.pushover_token:
            return
        try:
            response = httpx.post(
                MESSAGES_URL,
                data={
                    "token": settings.pushover_token,
                    "user": settings.pushover_user,
                    "title": title,
                    "message": message[:MAX_MESSAGE_LENGTH],
                    "priority": priority,
                },
                timeout=10,
            )
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
