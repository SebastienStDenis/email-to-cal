"""Measure the local extractor against cached Claude verdicts before trusting it.

Every email the service has processed left Claude's answer in the llm_cache, keyed by
content. This replays real mail - fetched from IMAP or read from .eml files - through
the Ollama backend and diffs the two, so switching engines is a decision made on the
operator's own mailbox rather than on faith. No Anthropic calls are made: emails whose
Claude verdict is not cached (or was cached under different settings) are counted and
skipped.

Local results are cached under the Ollama keyspace as they are computed, so an eval run
doubles as a warm start: mail already evaluated is free when the backend flips.
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
from .local_llm import OllamaExtractor
from .mime import parse_email
from .schema import ExtractionResult
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class Comparison:
    """One email, both verdicts."""

    subject: str
    baseline_committed: bool
    local_committed: bool
    event_diffs: list[str] = field(default_factory=list)

    @property
    def gate_agrees(self) -> bool:
        return self.baseline_committed == self.local_committed

    @property
    def missed_commitment(self) -> bool:
        """The dangerous direction: Claude saw a booking, the local model rejected it."""
        return self.baseline_committed and not self.local_committed


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
            lines.append("skipped; process some mail on the anthropic backend first, or")
            lines.append("check that model/effort/categories still match the settings the")
            lines.append(f"cache was written under. ({self.no_baseline} without baseline.)")
            return lines

        agreeing = sum(1 for c in self.compared if c.gate_agrees)
        missed = [c for c in self.compared if c.missed_commitment]
        extra = [c for c in self.compared if not c.gate_agrees and c.local_committed]
        with_diffs = [c for c in self.compared if c.gate_agrees and c.event_diffs]

        lines.append(f"Compared {total} emails; skipped {self.no_baseline} without a cached")
        lines.append(f"Claude verdict; {len(self.failures)} failed to process locally.")
        lines.append("")
        lines.append(f"Gate agreement: {agreeing}/{total} ({100 * agreeing / total:.0f}%)")
        if missed:
            lines.append("")
            lines.append(f"MISSED COMMITMENTS ({len(missed)}) - Claude said yes, local said no.")
            lines.append("These would be silently absent from your calendar:")
            lines += [f"  - {c.subject}" for c in missed]
        if extra:
            lines.append("")
            lines.append(f"Extra accepts ({len(extra)}) - local said yes, Claude said no.")
            lines.append("Junk risk; min_confidence may still catch these:")
            lines += [f"  - {c.subject}" for c in extra]
        if with_diffs:
            lines.append("")
            lines.append(f"Event differences on agreed commitments ({len(with_diffs)}):")
            for c in with_diffs:
                lines.append(f"  - {c.subject}")
                lines += [f"      {diff}" for diff in c.event_diffs]
        if self.failures:
            lines.append("")
            lines.append("Local processing failures:")
            lines += [f"  - {subject}: {error}" for subject, error in self.failures]
        return lines


def compare_results(
    subject: str, baseline: ExtractionResult, local: ExtractionResult
) -> Comparison:
    """Diff two verdicts on the axes that decide what lands on the calendar."""
    diffs: list[str] = []
    if baseline.is_committed and local.is_committed:
        if len(local.events) != len(baseline.events):
            diffs.append(f"{len(local.events)} events vs Claude's {len(baseline.events)}")
        base_starts = sorted(e.start_local for e in baseline.events)
        local_starts = sorted(e.start_local for e in local.events)
        for start in (s for s in base_starts if s not in local_starts):
            diffs.append(f"missing Claude's start {start}")
        for start in (s for s in local_starts if s not in base_starts):
            diffs.append(f"extra start {start}")
    return Comparison(
        subject=subject,
        baseline_committed=baseline.is_committed,
        local_committed=local.is_committed,
        event_diffs=diffs,
    )


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
    baseline_settings = settings.model_copy(update={"llm_backend": "anthropic"})
    local_settings = settings.model_copy(update={"llm_backend": "ollama"})
    extractor = OllamaExtractor(local_settings)
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

            cached = store.get_cached(cache_key(doc, baseline_settings))
            if cached is None:
                report.no_baseline += 1
                continue
            baseline = ExtractionResult.model_validate(cached)

            local_key = cache_key(doc, local_settings)
            cached_local = store.get_cached(local_key)
            try:
                if cached_local is not None:
                    local = ExtractionResult.model_validate(cached_local)
                else:
                    local = extractor.extract(doc)
                    store.put_cached(local_key, local.model_dump(mode="json"))
            except Exception as exc:
                report.failures.append((doc.subject, f"{type(exc).__name__}: {exc}"))
                out(f"FAIL  {doc.subject[:70]}: {exc}")
                continue

            comparison = compare_results(doc.subject, baseline, local)
            report.compared.append(comparison)
            marker = "ok " if comparison.gate_agrees and not comparison.event_diffs else "DIFF"
            out(
                f"{marker}  {doc.subject[:70]}  "
                f"(claude={'yes' if baseline.is_committed else 'no'}, "
                f"local={'yes' if local.is_committed else 'no'})"
            )

    for line in report.summary_lines():
        out(line)
    return 1 if any(c.missed_commitment for c in report.compared) else 0
