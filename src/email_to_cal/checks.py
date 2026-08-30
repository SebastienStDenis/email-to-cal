"""Exercise every external dependency and name the broken one.

Shared by the `check` command and the settings page, which proves a credential before it
keeps it: a typo saved and only noticed the next time an email quietly fails to appear
is the failure this exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass

import anthropic

from .cal import CalendarClient
from .config import Settings
from .mailbox import Mailbox
from .notify import validate_keys
from .prefs import Prefs


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def check_mail(settings: Settings, prefs: Prefs) -> CheckResult:
    """Logs in and counts what carries the flag, across every folder the sweep looks in."""
    if not settings.icloud_configured:
        return CheckResult("mail", False, "connect iCloud under Connections")
    mailbox = Mailbox(settings, prefs.flag_colour)
    try:
        mailbox.connect()
        folders = mailbox.folders()
        flagged = mailbox.count_flagged()
    except Exception as exc:
        return CheckResult("mail", False, str(exc))
    finally:
        mailbox.close()
    return CheckResult(
        "mail",
        True,
        f"{flagged} message(s) flagged {prefs.flag_colour} across {len(folders)} folder(s)",
    )


def check_calendar(settings: Settings, names: set[str]) -> CheckResult:
    """Signs in over CalDAV and looks for every calendar named, in one discovery pass."""
    if not settings.icloud_configured:
        return CheckResult("calendar", False, "connect iCloud under Connections")
    wanted = {name for name in names if name}
    if not wanted:
        return CheckResult("calendar", False, "pick a calendar under Preferences")
    try:
        resolved = CalendarClient(settings).resolve(wanted)
    except Exception as exc:
        return CheckResult("calendar", False, str(exc))
    listed = ", ".join(f"{name} at {url}" for name, url in sorted(resolved.items()))
    return CheckResult("calendar", True, f"writing to {listed}")


def check_anthropic(settings: Settings) -> CheckResult:
    """Lists the models, which proves the key without spending a token to do it."""
    if not settings.anthropic_configured:
        return CheckResult("anthropic", False, "connect Anthropic under Connections")
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=0)
    try:
        client.models.list(limit=1)
    except anthropic.AuthenticationError:
        return CheckResult("anthropic", False, "key rejected")
    except Exception as exc:
        return CheckResult("anthropic", False, str(exc))
    return CheckResult("anthropic", True, "key accepted")


def check_pushover(settings: Settings) -> CheckResult:
    if not settings.pushover_configured:
        return CheckResult("pushover", False, "connect Pushover under Connections")
    try:
        return CheckResult("pushover", True, validate_keys(settings))
    except Exception as exc:
        return CheckResult("pushover", False, str(exc))


def check_watchtower(settings: Settings) -> CheckResult:
    """Asks the API to prove itself without asking it to update anything; see `updates`."""
    if not settings.watchtower_configured:
        return CheckResult("watchtower", False, "connect Watchtower under Connections")
    from . import updates

    ok, detail = updates.probe(settings)
    return CheckResult("watchtower", ok, detail)


def check_service(settings: Settings, prefs: Prefs, service: str) -> CheckResult:
    """Prove one connection's credentials, as the settings page does before saving them."""
    if service == "icloud":
        return check_mail(settings, prefs)
    if service == "anthropic":
        return check_anthropic(settings)
    if service == "pushover":
        return check_pushover(settings)
    if service == "watchtower":
        return check_watchtower(settings)
    raise ValueError(f"no such connection: {service!r}")


def run_checks(settings: Settings, prefs: Prefs) -> list[CheckResult]:
    """Everything the watcher depends on, in the order it depends on them.

    Safe to run against production: nothing is written anywhere.
    """
    results = [
        check_mail(settings, prefs),
        check_anthropic(settings),
        check_calendar(settings, prefs.calendars),
    ]
    if settings.pushover_configured:
        results.append(check_pushover(settings))
    return results
