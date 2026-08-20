"""Preflight: exercise every external dependency, shared by `check` and the portal."""

from __future__ import annotations

from dataclasses import dataclass

import anthropic
import httpx

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
    """Validate the state db, IMAP login, the Anthropic key, and both calendar APIs.

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

        if settings.local_filter_enabled:
            results.append(_check_ollama(settings))

        if settings.pushover_user and settings.pushover_token:
            try:
                results.append(CheckResult("pushover", True, validate_keys(settings)))
            except Exception as exc:
                results.append(CheckResult("pushover", False, str(exc)))

        try:
            calendar = CalendarClient(settings, store)
            wanted = {settings.default_calendar} | {r.calendar for r in settings.categories}
            calendars = {name: calendar.resolve_calendar(name) for name in sorted(wanted)}
            resolved = ", ".join(f"{name!r} -> {cal}" for name, cal in calendars.items())
            results.append(CheckResult("google", True, resolved))
        except Exception as exc:
            results.append(CheckResult("google", False, str(exc)))
            calendars = {}

        if calendars:
            results.append(_check_caldav(calendar, next(iter(calendars.values()))))

    return results


def _check_caldav(calendar: CalendarClient, calendar_id: str) -> CheckResult:
    """The CalDAV API answers, so links back to the source email can be written.

    It is a second API on the same Cloud project, enabled separately. Events still land
    without it - they just arrive with no way back to the email that made them.
    """
    try:
        calendar.probe_caldav(calendar_id)
    except Exception as exc:
        return CheckResult("caldav", False, f"events will be created without mail links: {exc}")
    return CheckResult("caldav", True, "source-email links can be written")


def _check_ollama(settings: Settings) -> CheckResult:
    """The filter's server answers and the configured model is actually pulled.

    A failure here never blocks mail - the filter fails open - but it does mean every
    email is going to the paid API, which is exactly what the operator turned the
    filter on to avoid.
    """
    try:
        with httpx.Client(base_url=settings.ollama_url, timeout=10.0) as client:
            reply = client.get("/api/tags")
            reply.raise_for_status()
            names = {str(m.get("name", "")) for m in reply.json().get("models", [])}
    except Exception as exc:
        return CheckResult("ollama", False, f"cannot reach {settings.ollama_url}: {exc}")

    wanted = settings.ollama_model
    # Ollama lists a bare pull as "name:latest"; accept either spelling.
    if wanted in names or (":" not in wanted and f"{wanted}:latest" in names):
        return CheckResult("ollama", True, f"{wanted} available at {settings.ollama_url}")
    return CheckResult(
        "ollama", False, f"model {wanted!r} is not pulled; run: ollama pull {wanted}"
    )
