"""Preflight: exercise every external dependency, shared by `check` and the portal."""

from __future__ import annotations

from dataclasses import dataclass

import anthropic
import httpx

from .cal import CalendarClient
from .config import Settings
from .mailbox import Mailbox
from .notify import validate_keys
from .store import Store


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def run_checks(settings: Settings) -> list[CheckResult]:
    """Validate the state db, the mailbox, the model, and the calendar.

    Safe to run against production: nothing is written anywhere.
    """
    results = [CheckResult("state db", True, str(settings.state_db))]

    with Store(settings.state_db) as store:
        failures = store.list_failures()
    if failures:
        summary = ", ".join(f"{f.subject[:40]!r} ({f.attempts} attempts)" for f in failures[:5])
        results.append(CheckResult("failed messages", True, f"{len(failures)} recorded: {summary}"))

    results.append(_check_mailbox(settings))
    if settings.provider == "ollama":
        results.append(_check_ollama(settings))
    else:
        results.append(_check_claude(settings))
    if settings.pushover_user and settings.pushover_token:
        results.append(_check_pushover(settings))
    results.append(_check_calendar(settings))
    return results


def _check_mailbox(settings: Settings) -> CheckResult:
    try:
        mailbox = Mailbox(settings)
        mailbox.connect()
        folders = mailbox.folders()
        flagged = mailbox.count_flagged()
        mailbox.close()
    except Exception as exc:
        return CheckResult("mailbox", False, str(exc))
    return CheckResult(
        "mailbox", True, f"{len(folders)} folders searched, {flagged} message(s) flagged now"
    )


def _check_claude(settings: Settings) -> CheckResult:
    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        client.models.retrieve(settings.anthropic_model)
    except Exception as exc:
        return CheckResult("anthropic", False, str(exc))
    return CheckResult("anthropic", True, f"{settings.anthropic_model} reachable")


def _check_ollama(settings: Settings) -> CheckResult:
    """The server answers and the configured model is actually pulled."""
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


def _check_pushover(settings: Settings) -> CheckResult:
    try:
        return CheckResult("pushover", True, validate_keys(settings))
    except Exception as exc:
        return CheckResult("pushover", False, str(exc))


def _check_calendar(settings: Settings) -> CheckResult:
    try:
        url = CalendarClient(settings).resolve(settings.calendar_name)
    except Exception as exc:
        return CheckResult("calendar", False, str(exc))
    return CheckResult("calendar", True, f"{settings.calendar_name!r} -> {url}")
