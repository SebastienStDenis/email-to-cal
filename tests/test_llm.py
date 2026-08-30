"""How the model is handed an email."""

from __future__ import annotations

from typing import Any

from email_to_cal.config import Settings
from email_to_cal.llm import MAX_TOTAL_ENCODED_BYTES, Extractor, categories_block, render_email
from email_to_cal.mime import parse_email
from email_to_cal.prefs import Prefs
from email_to_cal.schema import Attachment, EmailDocument

from .conftest import fixture_bytes


def document(name: str) -> EmailDocument:
    return parse_email(fixture_bytes(name))


def blocks(doc: EmailDocument, settings: Settings, preferences: Prefs) -> list[Any]:
    # The client is only stored, never called, when the blocks are assembled.
    return Extractor(settings, preferences, client=object())._content_blocks(doc)  # type: ignore[arg-type]


def test_pdfs_are_sent_and_placed_before_the_prompt(settings: Settings, preferences: Prefs) -> None:
    content = blocks(document("boarding_pass_pdf.eml"), settings, preferences)
    assert content[0]["type"] == "document"
    assert content[-1]["type"] == "text"


def test_images_are_sent_only_when_the_text_is_thin(settings: Settings, preferences: Prefs) -> None:
    doc = document("promo_image_heavy.eml")
    assert any(block["type"] == "image" for block in blocks(doc, settings, preferences))

    doc.body_text = "x" * 500
    assert not any(block["type"] == "image" for block in blocks(doc, settings, preferences))


def test_oversized_attachment_set_is_capped_rather_than_413ing(
    settings: Settings, preferences: Prefs
) -> None:
    doc = document("boarding_pass_pdf.eml")
    # Two of these fit in the budget once base64 inflates them; three do not.
    big = b"%PDF-" + b"0" * int(MAX_TOTAL_ENCODED_BYTES * 0.4)
    doc.attachments = [
        Attachment(filename=f"{i}.pdf", media_type="application/pdf", data=big) for i in range(3)
    ]

    content = blocks(doc, settings, preferences)

    # Sending them all would exceed the request ceiling and lose the whole email.
    assert sum(1 for block in content if block["type"] == "document") == 1


def test_the_email_is_rendered_with_the_recipients_time_zone(preferences: Prefs) -> None:
    rendered = render_email(document("restaurant_plain.eml"), preferences)
    assert "Recipient's default timezone: Europe/Zurich" in rendered
    assert "<body>" in rendered


def test_the_categories_are_put_to_the_model_in_the_operators_words() -> None:
    assert "Always set category to null" in categories_block(Prefs())

    block = categories_block(
        Prefs(
            categories=[
                {"name": "music", "description": "Gigs I hold tickets for.", "calendar": "M"}
            ]
        )
    )
    assert "- music: Gigs I hold tickets for." in block
