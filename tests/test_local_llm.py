"""The Ollama backend: request shape, retry behaviour, and the text-only render."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from email_to_cal.config import Settings
from email_to_cal.llm import cache_key
from email_to_cal.local_llm import (
    OllamaExtractor,
    OllamaUnavailable,
    _pdf_text,
    render_local,
)
from email_to_cal.mime import parse_email
from email_to_cal.schema import EmailDocument

from .conftest import fixture_bytes

MAX_BYTES = 32 * 1024 * 1024

VALID = {
    "is_committed": True,
    "gate_reasoning": "a confirmed booking",
    "events": [],
}


def doc(name: str) -> EmailDocument:
    return parse_email(fixture_bytes(name), max_attachment_bytes=MAX_BYTES)


def ok(content: object) -> httpx.Response:
    body = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(200, json={"message": {"content": body}})


def extractor_with(
    settings: Settings, responses: list[httpx.Response | Exception]
) -> tuple[OllamaExtractor, list[dict[str, Any]]]:
    """An extractor whose server is a canned script, plus the requests it received."""
    seen: list[dict[str, Any]] = []
    replies = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    client = httpx.Client(base_url="http://ollama.test", transport=httpx.MockTransport(handler))
    return OllamaExtractor(settings, client=client), seen


def test_request_carries_model_grammar_and_context(settings: Settings) -> None:
    extractor, seen = extractor_with(settings, [ok(VALID)])
    result = extractor.extract(doc("restaurant_plain.eml"))

    assert result.is_committed
    (request,) = seen
    assert request["model"] == settings.ollama_model
    assert request["options"]["num_ctx"] == settings.ollama_num_ctx
    assert request["format"]["properties"].keys() >= {"is_committed", "events"}
    system, user = request["messages"]
    # The grammar constrains the shape, but the field descriptions only reach the model
    # through the prompt, so the schema must be spelled out there too.
    assert "start_local" in system["content"]
    assert "Kadeau" in user["content"]


def test_invalid_output_is_retried_with_the_error(settings: Settings) -> None:
    incomplete = {"is_committed": True}  # missing gate_reasoning
    extractor, seen = extractor_with(settings, [ok(incomplete), ok(VALID)])

    result = extractor.extract(doc("restaurant_plain.eml"))

    assert result.gate_reasoning == "a confirmed booking"
    assert len(seen) == 2
    retry_messages = seen[1]["messages"]
    assert len(retry_messages) == 4
    assert "failed validation" in retry_messages[-1]["content"]


def test_gives_up_after_two_unusable_answers(settings: Settings) -> None:
    extractor, _ = extractor_with(settings, [ok("not json"), ok("still not json")])
    with pytest.raises(RuntimeError, match="unusable"):
        extractor.extract(doc("restaurant_plain.eml"))


def test_unreachable_server_is_reported_as_transient(settings: Settings) -> None:
    extractor, _ = extractor_with(settings, [httpx.ConnectError("connection refused")])
    with pytest.raises(OllamaUnavailable, match="cannot reach"):
        extractor.extract(doc("restaurant_plain.eml"))


def test_missing_model_names_the_pull_command(settings: Settings) -> None:
    extractor, _ = extractor_with(settings, [httpx.Response(404, text="model not found")])
    with pytest.raises(OllamaUnavailable, match="ollama pull"):
        extractor.extract(doc("restaurant_plain.eml"))


def test_render_includes_the_pdf_text_layer(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The thin-body boarding pass case: the detail must arrive as text, not vision."""
    monkeypatch.setattr("email_to_cal.local_llm._pdf_text", lambda data: "LX318 GATE B32")
    rendered = render_local(doc("boarding_pass_pdf.eml"), settings)
    assert "<attachment_text" in rendered
    assert "LX318 GATE B32" in rendered


def test_unreadable_pdf_degrades_to_nothing(settings: Settings) -> None:
    assert _pdf_text(b"not a pdf at all") == ""
    rendered = render_local(doc("promo_image_heavy.eml"), settings)
    assert "<attachment_text" not in rendered


def test_backends_key_separate_cache_namespaces(settings: Settings) -> None:
    """Flipping the engine must never reuse the other engine's verdicts."""
    message = doc("concert_ics.eml")
    anthropic_key = cache_key(message, settings)

    settings.llm_backend = "ollama"
    ollama_key = cache_key(message, settings)
    assert ollama_key != anthropic_key

    settings.ollama_model = "some-other-model"
    assert cache_key(message, settings) != ollama_key
