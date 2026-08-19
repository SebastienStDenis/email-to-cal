"""The local pre-filter: request shape, failure modes, and the text-only render."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from email_to_cal.config import Settings
from email_to_cal.llm import cache_key
from email_to_cal.local_llm import (
    OllamaFilter,
    OllamaUnavailable,
    _pdf_text,
    filter_cache_key,
    make_prefilter,
    render_local,
)
from email_to_cal.mime import parse_email
from email_to_cal.schema import EmailDocument

from .conftest import fixture_bytes

MAX_BYTES = 32 * 1024 * 1024


def doc(name: str) -> EmailDocument:
    return parse_email(fixture_bytes(name), max_attachment_bytes=MAX_BYTES)


def verdict_response(passes: bool, reasoning: str = "because") -> httpx.Response:
    body = json.dumps({"could_contain_commitment": passes, "reasoning": reasoning})
    return httpx.Response(200, json={"message": {"content": body}})


def filter_with(
    settings: Settings, responses: list[httpx.Response | Exception]
) -> tuple[OllamaFilter, list[dict[str, Any]]]:
    """A filter whose server is a canned script, plus the requests it received."""
    seen: list[dict[str, Any]] = []
    replies = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    client = httpx.Client(base_url="http://ollama.test", transport=httpx.MockTransport(handler))
    return OllamaFilter(settings, client=client), seen


def test_request_carries_model_grammar_and_context(settings: Settings) -> None:
    prefilter, seen = filter_with(settings, [verdict_response(False, "a promo digest")])
    verdict = prefilter.judge(doc("promo_image_heavy.eml"))

    assert not verdict.could_contain_commitment
    assert verdict.reasoning == "a promo digest"
    (request,) = seen
    assert request["model"] == settings.ollama_model
    assert request["options"]["num_ctx"] == settings.ollama_num_ctx
    assert set(request["format"]["properties"]) == {"could_contain_commitment", "reasoning"}
    system, user = request["messages"]
    assert "discard" in system["content"]
    assert "Concerts near you" in user["content"]


def test_unusable_output_raises_for_the_caller_to_fail_open(settings: Settings) -> None:
    response = httpx.Response(200, json={"message": {"content": "not json"}})
    prefilter, _ = filter_with(settings, [response])
    with pytest.raises(ValueError):
        prefilter.judge(doc("promo_image_heavy.eml"))


def test_unreachable_server_is_reported_as_such(settings: Settings) -> None:
    prefilter, _ = filter_with(settings, [httpx.ConnectError("connection refused")])
    with pytest.raises(OllamaUnavailable, match="cannot reach"):
        prefilter.judge(doc("promo_image_heavy.eml"))


def test_missing_model_names_the_pull_command(settings: Settings) -> None:
    prefilter, _ = filter_with(settings, [httpx.Response(404, text="model not found")])
    with pytest.raises(OllamaUnavailable, match="ollama pull"):
        prefilter.judge(doc("promo_image_heavy.eml"))


def test_render_includes_the_pdf_text_layer(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The thin-body boarding pass: the filter must see the flight, not an empty body."""
    monkeypatch.setattr("email_to_cal.local_llm._pdf_text", lambda data: "LX318 GATE B32")
    rendered = render_local(doc("boarding_pass_pdf.eml"), settings)
    assert "<attachment_text" in rendered
    assert "LX318 GATE B32" in rendered


def test_unreadable_pdf_degrades_to_nothing(settings: Settings) -> None:
    assert _pdf_text(b"not a pdf at all") == ""
    rendered = render_local(doc("promo_image_heavy.eml"), settings)
    assert "<attachment_text" not in rendered


def test_filter_keys_are_separate_from_claude_keys(settings: Settings) -> None:
    """A filter verdict must never be mistaken for an extraction, or vice versa."""
    message = doc("concert_ics.eml")
    assert filter_cache_key(message, settings) != cache_key(message, settings)

    baseline = filter_cache_key(message, settings)
    settings.ollama_model = "some-other-model"
    assert filter_cache_key(message, settings) != baseline


def test_prefilter_is_only_built_when_enabled(settings: Settings) -> None:
    assert make_prefilter(settings) is None
    settings.local_filter_enabled = True
    assert make_prefilter(settings) is not None
