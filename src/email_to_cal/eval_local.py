"""Measure the local pre-filter against cached Claude verdicts before trusting it.

Every email the service has processed left Claude's answer in the llm_cache, keyed by
content. This replays real mail - fetched from IMAP or read from .eml files - through
the filter and checks each discard against Claude's verdict, so switching the filter
on is a decision made on the operator's own mailbox rather than on faith. No Anthropic
calls are made: emails without a cached Claude verdict are counted and skipped.

The only failure that matters is a wrong discard: the filter said drop, Claude had
said real commitment. That email would silently never reach the calendar. A wrong
pass merely costs one API call - the whole point of the recall-biased design.

Filter verdicts are cached as they are computed, so an eval run doubles as a warm
start for the switch.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from imap_tools import AND

from .config import Settings
from .llm import cache_key
from .local_llm import FilterVerdict, OllamaFilter, filter_cache_key
from .mime import parse_email
from .schema import ExtractionResult
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class Comparison:
    """One email: what the filter would do, against what Claude concluded."""

    subject: str
    filter_passes: bool
    claude_committed: bool
    filter_reasoning: str
    bypassed: bool = False  # structured source; the filter never sees these

    @property
    def wrong_discard(self) -> bool:
        """The only dangerous outcome: filtered out, but Claude saw a commitment."""
        return not self.filter_passes and self.claude_committed


@dataclass
class Report:
    compared: list[Comparison] = field(default_factory=list)
    no_baseline: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        lines: list[str] = [""]
        total = len(self.compared)
        if not total:
            lines.append("Nothing compared. Emails without a cached Claude verdict are")
            lines.append("skipped; process some mail first, or check that model/effort/")
            lines.append("categories still match the settings the cache was written under.")
            lines.append(f"({self.no_baseline} without baseline.)")
            return lines

        discarded = [c for c in self.compared if not c.filter_passes]
        wrong = [c for c in self.compared if c.wrong_discard]
        bypassed = sum(1 for c in self.compared if c.bypassed)
        passed_junk = sum(1 for c in self.compared if c.filter_passes and not c.claude_committed)

        lines.append(f"Compared {total} emails; skipped {self.no_baseline} without a cached Claude")
        lines.append(f"verdict; {len(self.failures)} filter failures (those fail open).")
        lines.append("")
        lines.append(
            f"Would discard {len(discarded)}/{total} "
            f"({100 * len(discarded) / total:.0f}% fewer API calls)."
        )
        lines.append(
            f"Passed to Claude: {total - len(discarded)} "
            f"({bypassed} bypassed as structured, {passed_junk} junk Claude then rejects)."
        )
        if wrong:
            lines.append("")
            lines.append(f"WRONG DISCARDS ({len(wrong)}) - Claude saw a real commitment, the")
            lines.append("filter would have dropped it before Claude ever looked:")
            for c in wrong:
                lines.append(f"  - {c.subject}")
                lines.append(f"      filter said: {c.filter_reasoning}")
        else:
            lines.append("")
            lines.append("No wrong discards: every commitment Claude found would still")
            lines.append("have reached it.")
        if self.failures:
            lines.append("")
            lines.append("Filter failures (harmless in production - these go to Claude):")
            lines += [f"  - {subject}: {error}" for subject, error in self.failures]
        return lines


def _fetch_imap(settings: Settings, days: int, folders: list[str]) -> Iterator[bytes]:
    """Recent raw messages, read-only, without touching the watcher's UID cursors."""
    # Local import: the eval command must stay usable in tests without an IMAP stack.
    from .mailbox import Mailbox

    with Store(settings.state_db) as store:
        mailbox = Mailbox(settings, store)
        box = mailbox.connect()
        try:
            since = (datetime.now(UTC) - timedelta(days=days)).date()
            for folder in folders:
                box.folder.set(folder)
                for message in box.fetch(AND(date_gte=since), mark_seen=False, bulk=False):
                    yield message.obj.as_bytes()
        finally:
            mailbox.close()


def run_eval(
    settings: Settings,
    *,
    days: int,
    limit: int,
    folders: list[str] | None,
    eml_paths: list[str],
    out: Callable[[str], None] = print,
) -> int:
    prefilter = OllamaFilter(settings)
    report = Report()

    if eml_paths:
        raws: Iterator[bytes] = (Path(p).read_bytes() for p in eml_paths)
    else:
        raws = _fetch_imap(settings, days, folders or [settings.imap_folder])

    with Store(settings.state_db) as store:
        for raw in raws:
            if limit and len(report.compared) >= limit:
                break
            doc = parse_email(raw, max_attachment_bytes=settings.max_attachment_bytes)

            cached = store.get_cached(cache_key(doc, settings))
            if cached is None:
                report.no_baseline += 1
                continue
            claude = ExtractionResult.model_validate(cached)

            if doc.has_structured_source:
                # The production path never filters these; count them as passes so the
                # savings figure reflects what the filter will really do.
                comparison = Comparison(
                    subject=doc.subject,
                    filter_passes=True,
                    claude_committed=claude.is_committed,
                    filter_reasoning="bypassed: structured data present",
                    bypassed=True,
                )
                report.compared.append(comparison)
                out(f"pass  {doc.subject[:70]}  (structured; straight to Claude)")
                continue

            key = filter_cache_key(doc, settings)
            cached_verdict = store.get_cached(key)
            try:
                if cached_verdict is not None:
                    verdict = FilterVerdict.model_validate(cached_verdict)
                else:
                    verdict = prefilter.judge(doc)
                    store.put_cached(key, verdict.model_dump(mode="json"))
            except Exception as exc:
                report.failures.append((doc.subject, f"{type(exc).__name__}: {exc}"))
                out(f"FAIL  {doc.subject[:70]}: {exc}")
                continue

            comparison = Comparison(
                subject=doc.subject,
                filter_passes=verdict.could_contain_commitment,
                claude_committed=claude.is_committed,
                filter_reasoning=verdict.reasoning,
            )
            report.compared.append(comparison)
            if comparison.wrong_discard:
                marker = "MISS"
            elif comparison.filter_passes:
                marker = "pass"
            else:
                marker = "drop"
            out(f"{marker}  {doc.subject[:70]}  (claude={'yes' if claude.is_committed else 'no'})")

    for line in report.summary_lines():
        out(line)
    return 1 if any(c.wrong_discard for c in report.compared) else 0
