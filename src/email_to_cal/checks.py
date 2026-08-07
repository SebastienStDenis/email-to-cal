"""Preflight: exercise every external dependency, shared by `check` and the portal."""

from __future__ import annotations

from dataclasses import dataclass

import anthropic

from .config import Settings
from .gcal import CalendarClient
from .mailbox import Mailbox
from .notify import validate_keys
from .store import Store


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def run_checks(settings: Settings) -> list[CheckResult]:
    """Validate the state db, IMAP login, the Anthropic key, and the Google calendars.

    Safe to run against production: nothing is written except configured calendars that
    do not exist yet.
    """
    results: list[CheckResult] = []
    with Store(settings.state_db) as store:
        results.append(CheckResult("state db", True, str(settings.state_db)))

        failures = store.list_failures()
        if failures:
            summary = ", ".join(
                f"{folder} UID {uid} ({attempts} attempts)"
                for folder, _, uid, attempts, _ in failures[:10]
            )
            results.append(
                CheckResult(
                    "failed messages",
                    True,
                    f"{len(failures)} recorded; retry with 'replay' or investigate: {summary}",
                )
            )

        try:
            mailbox = Mailbox(settings, store)
            box = mailbox.connect()
            count = len(box.uids())
            mailbox.close()
            results.append(
                CheckResult("imap", True, f"connected, {count} messages in {settings.imap_folder}")
            )
        except Exception as exc:
            results.append(CheckResult("imap", False, str(exc)))

        try:
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            client.models.retrieve(settings.anthropic_model)
            results.append(CheckResult("anthropic", True, f"{settings.anthropic_model} reachable"))
        except Exception as exc:
            results.append(CheckResult("anthropic", False, str(exc)))

        if settings.pushover_user and settings.pushover_token:
            try:
                results.append(CheckResult("pushover", True, validate_keys(settings)))
            except Exception as exc:
                results.append(CheckResult("pushover", False, str(exc)))

        try:
            calendar = CalendarClient(settings, store)
            wanted = {settings.default_calendar} | {r.calendar for r in settings.categories}
            resolved = ", ".join(
                f"{name!r} -> {calendar.resolve_calendar(name)}" for name in sorted(wanted)
            )
            results.append(CheckResult("google", True, resolved))
        except Exception as exc:
            results.append(CheckResult("google", False, str(exc)))

    return results
