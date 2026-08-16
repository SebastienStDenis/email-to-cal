"""The eval command: filter verdicts checked against cached Claude baselines."""

from __future__ import annotations

import pytest

from email_to_cal.config import Settings
from email_to_cal.eval_local import run_eval
from email_to_cal.llm import cache_key
from email_to_cal.local_llm import FilterVerdict
from email_to_cal.mime import parse_email
from email_to_cal.schema import EmailDocument, ExtractionResult
from email_to_cal.store import Store

from .conftest import FIXTURES


def claude_verdict(committed: bool) -> ExtractionResult:
    return ExtractionResult(is_committed=committed, gate_reasoning="r", events=[])


class StubFilter:
    """Stands in for Ollama with a fixed verdict."""

    passes = True

    def __init__(self, settings: Settings) -> None:
        pass

    def judge(self, doc: EmailDocument) -> FilterVerdict:
        return FilterVerdict(could_contain_commitment=self.passes, reasoning="stubbed")


def seed_baseline(settings: Settings, eml_name: str, committed: bool) -> str:
    path = FIXTURES / eml_name
    doc = parse_email(path.read_bytes(), max_attachment_bytes=settings.max_attachment_bytes)
    with Store(settings.state_db) as store:
        store.put_cached(
            cache_key(doc, settings), claude_verdict(committed).model_dump(mode="json")
        )
    return str(path)


def run(settings: Settings, paths: list[str]) -> tuple[int, str]:
    lines: list[str] = []
    code = run_eval(settings, days=90, limit=0, folders=None, eml_paths=paths, out=lines.append)
    return code, "\n".join(lines)


def test_a_correct_discard_counts_as_savings(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("email_to_cal.eval_local.OllamaFilter", StubFilter)
    monkeypatch.setattr(StubFilter, "passes", False)
    path = seed_baseline(settings, "restaurant_plain.eml", committed=False)

    code, output = run(settings, [path])

    assert code == 0
    assert "Would discard 1/1" in output
    assert "No wrong discards" in output


def test_a_wrong_discard_is_fatal_to_the_report(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("email_to_cal.eval_local.OllamaFilter", StubFilter)
    monkeypatch.setattr(StubFilter, "passes", False)
    path = seed_baseline(settings, "restaurant_plain.eml", committed=True)

    code, output = run(settings, [path])

    assert code == 1
    assert "WRONG DISCARDS (1)" in output
    assert "Kadeau" in output


def test_structured_emails_bypass_the_filter_in_the_eval_too(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .ics email counts as a pass without the stub filter ever being consulted."""

    class ExplodingFilter(StubFilter):
        def judge(self, doc: EmailDocument) -> FilterVerdict:
            raise AssertionError("the filter must not see structured emails")

    monkeypatch.setattr("email_to_cal.eval_local.OllamaFilter", ExplodingFilter)
    path = seed_baseline(settings, "concert_ics.eml", committed=True)

    code, output = run(settings, [path])

    assert code == 0
    assert "1 bypassed as structured" in output


def test_emails_without_a_baseline_are_counted_and_skipped(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("email_to_cal.eval_local.OllamaFilter", StubFilter)

    code, output = run(settings, [str(FIXTURES / "restaurant_plain.eml")])

    assert code == 0
    assert "Nothing compared" in output
