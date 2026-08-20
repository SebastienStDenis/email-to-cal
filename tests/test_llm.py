"""How each provider is handed an email, and what it does with a bad answer."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from email_to_cal.config import Settings
from email_to_cal.llm import (
    MAX_TOTAL_ENCODED_BYTES,
    AnthropicExtractor,
    ExtractionFailed,
    OllamaExtractor,
    make_extractor,
    render_email,
)
from email_to_cal.mime import parse_email
from email_to_cal.schema import Attachment, EmailDocument

from .conftest import fixture_bytes


def document(name: str, settings: Settings) -> EmailDocument:
    return parse_email(fixture_bytes(name), max_attachment_bytes=settings.max_attachment_bytes)


def blocks(doc: EmailDocument, settings: Settings) -> list[Any]:
    # The client is only stored, never called, when the blocks are assembled.
    return AnthropicExtractor(settings, client=object())._content_blocks(doc)  # type: ignore[arg-type]


def test_the_provider_setting_picks_the_extractor(settings: Settings) -> None:
    assert isinstance(make_extractor(settings), AnthropicExtractor)
    settings.provider = "ollama"
    assert isinstance(make_extractor(settings), OllamaExtractor)


def test_pdfs_are_sent_and_placed_before_the_prompt(settings: Settings) -> None:
    content = blocks(document("boarding_pass_pdf.eml", settings), settings)
    assert content[0]["type"] == "document"
    assert content[-1]["type"] == "text"


def test_images_are_sent_only_when_the_text_is_thin(settings: Settings) -> None:
    doc = document("promo_image_heavy.eml", settings)
    assert any(block["type"] == "image" for block in blocks(doc, settings))

    doc.body_text = "x" * 500
    assert not any(block["type"] == "image" for block in blocks(doc, settings))


def test_vision_can_be_switched_off(settings: Settings) -> None:
    settings.enable_vision = False
    content = blocks(document("boarding_pass_pdf.eml", settings), settings)
    assert [block["type"] for block in content] == ["text"]


def test_oversized_attachment_set_is_capped_rather_than_413ing(settings: Settings) -> None:
    doc = document("boarding_pass_pdf.eml", settings)
    # Two of these fit in the budget once base64 inflates them; three do not.
    big = b"%PDF-" + b"0" * int(MAX_TOTAL_ENCODED_BYTES * 0.4)
    doc.attachments = [
        Attachment(filename=f"{i}.pdf", media_type="application/pdf", data=big) for i in range(3)
    ]

    content = blocks(doc, settings)

    # Sending them all would exceed the request ceiling and lose the whole email.
    assert sum(1 for block in content if block["type"] == "document") == 1


def test_the_local_model_is_shown_the_text_layer_of_a_pdf(settings: Settings) -> None:
    doc = document("boarding_pass_pdf.eml", settings)

    rendered = render_email(doc, settings, pdf_texts=[("ticket.pdf", "Seat 14A, gate B22")])

    # A text-only model reading an email whose body is just "your ticket is attached"
    # would otherwise see nothing at all.
    assert "<attachment_text file='ticket.pdf'" in rendered
    assert "Seat 14A, gate B22" in rendered


def ollama(settings: Settings, handler: Any) -> OllamaExtractor:
    settings.provider = "ollama"
    return OllamaExtractor(
        settings,
        client=httpx.Client(base_url=settings.ollama_url, transport=httpx.MockTransport(handler)),
    )


def test_the_local_model_answer_is_parsed(settings: Settings) -> None:
    payload = {
        "message": {
            "content": '{"events": [{"kind": "concert", "title": "Radiohead", '
            '"all_day": false, "start_local": "2026-09-14T20:00:00"}]}'
        }
    }
    extractor = ollama(settings, lambda _: httpx.Response(200, json=payload))
    doc = EmailDocument(
        message_id="<a@b>", subject="s", sender="x", to="y", date=None, body_text="b"
    )

    result = extractor.extract(doc)

    assert [event.title for event in result.events] == ["Radiohead"]


def test_an_unpulled_local_model_says_how_to_fix_it(settings: Settings) -> None:
    extractor = ollama(settings, lambda _: httpx.Response(404))
    doc = EmailDocument(
        message_id="<a@b>", subject="s", sender="x", to="y", date=None, body_text="b"
    )

    with pytest.raises(ExtractionFailed, match="ollama pull"):
        extractor.extract(doc)


def test_unusable_local_output_fails_loudly_rather_than_silently(settings: Settings) -> None:
    extractor = ollama(settings, lambda _: httpx.Response(200, json={"message": {"content": "?"}}))
    doc = EmailDocument(
        message_id="<a@b>", subject="s", sender="x", to="y", date=None, body_text="b"
    )

    # Swallowing this would look exactly like "this email has no events".
    with pytest.raises(ExtractionFailed, match="unusable JSON"):
        extractor.extract(doc)
