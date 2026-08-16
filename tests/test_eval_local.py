"""The eval command: verdict comparison and the replay over cached baselines."""

from __future__ import annotations

import pytest

from email_to_cal.config import Settings
from email_to_cal.eval_local import compare_results, run_eval
from email_to_cal.llm import cache_key
from email_to_cal.mime import parse_email
from email_to_cal.schema import EmailDocument, ExtractedEvent, ExtractionResult
from email_to_cal.store import Store

from .conftest import FIXTURES


def event(start: str) -> ExtractedEvent:
    return ExtractedEvent(
        kind="other",
        title="t",
        all_day=False,
        start_local=start,
        confidence=0.9,
        reasoning="r",
    )


def result(committed: bool, starts: list[str] | None = None) -> ExtractionResult:
    return ExtractionResult(
        is_committed=committed,
        gate_reasoning="r",
        events=[event(s) for s in starts or []],
    )


def test_agreement_produces_no_diffs() -> None:
    comparison = compare_results(
        "s", result(True, ["2026-09-14T18:35:00"]), result(True, ["2026-09-14T18:35:00"])
    )
    assert comparison.gate_agrees
    assert not comparison.missed_commitment
    assert comparison.event_diffs == []


def test_a_locally_rejected_booking_is_flagged_as_missed() -> None:
    comparison = compare_results("s", result(True, ["2026-09-14T18:35:00"]), result(False))
    assert not comparison.gate_agrees
    assert comparison.missed_commitment


def test_diverging_start_times_are_spelled_out() -> None:
    comparison = compare_results(
        "s",
        result(True, ["2026-09-14T18:35:00", "2026-09-20T11:00:00"]),
        result(True, ["2026-09-14T19:35:00"]),
    )
    assert comparison.gate_agrees
    assert "1 events vs Claude's 2" in comparison.event_diffs
    assert any("missing Claude's start 2026-09-14T18:35:00" in d for d in comparison.event_diffs)
    assert any("extra start 2026-09-14T19:35:00" in d for d in comparison.event_diffs)


class StubExtractor:
    """Stands in for Ollama: always deems the email committed with no events."""

    def __init__(self, settings: Settings) -> None:
        pass

    def extract(self, doc: EmailDocument) -> ExtractionResult:
        return result(True)


def test_run_eval_compares_against_the_cached_claude_verdict(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("email_to_cal.eval_local.OllamaExtractor", StubExtractor)
    eml = FIXTURES / "restaurant_plain.eml"

    doc = parse_email(eml.read_bytes(), max_attachment_bytes=settings.max_attachment_bytes)
    baseline_settings = settings.model_copy(update={"llm_backend": "anthropic"})
    with Store(settings.state_db) as store:
        store.put_cached(
            cache_key(doc, baseline_settings),
            result(True, ["2026-08-22T19:30:00"]).model_dump(mode="json"),
        )

    lines: list[str] = []
    code = run_eval(
        settings, days=90, limit=0, folders=None, eml_paths=[str(eml)], out=lines.append
    )

    assert code == 0
    output = "\n".join(lines)
    assert "Compared 1 emails" in output
    assert "Gate agreement: 1/1" in output
    # The stub found no events, so the diff section must call the divergence out.
    assert "0 events vs Claude's 1" in output


def test_run_eval_counts_emails_without_a_baseline(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("email_to_cal.eval_local.OllamaExtractor", StubExtractor)
    eml = FIXTURES / "restaurant_plain.eml"

    lines: list[str] = []
    code = run_eval(
        settings, days=90, limit=0, folders=None, eml_paths=[str(eml)], out=lines.append
    )

    assert code == 0
    assert "Nothing compared" in "\n".join(lines)
