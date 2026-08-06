"""Request assembly and cache-key behaviour, without touching the network."""

from __future__ import annotations

from email_to_cal.config import Settings
from email_to_cal.llm import MAX_TOTAL_ENCODED_BYTES, _content_blocks, cache_key
from email_to_cal.mime import parse_email
from email_to_cal.schema import Attachment, EmailDocument

from .conftest import fixture_bytes

MAX_BYTES = 32 * 1024 * 1024


def doc(name: str) -> EmailDocument:
    return parse_email(fixture_bytes(name), max_attachment_bytes=MAX_BYTES)


def test_pdfs_are_sent_and_placed_before_the_prompt(settings: Settings) -> None:
    blocks = _content_blocks(doc("boarding_pass_pdf.eml"), settings)
    assert [b["type"] for b in blocks] == ["document", "text"]
    assert blocks[0]["source"]["media_type"] == "application/pdf"


def test_images_are_sent_only_when_the_text_is_thin(settings: Settings) -> None:
    promo = doc("promo_image_heavy.eml")
    assert [b["type"] for b in _content_blocks(promo, settings)] == ["image", "text"]

    promo.body_text = "x" * 500  # substantial text, so the image is not worth the tokens
    assert [b["type"] for b in _content_blocks(promo, settings)] == ["text"]


def test_vision_can_be_switched_off(settings: Settings) -> None:
    settings.enable_vision = False
    assert [b["type"] for b in _content_blocks(doc("boarding_pass_pdf.eml"), settings)] == ["text"]


def test_oversized_attachment_set_is_capped_rather_than_413ing(settings: Settings) -> None:
    """Three 8 MB PDFs base64 to ~32 MB, which is the API's hard request limit."""
    big = doc("boarding_pass_pdf.eml")
    big.attachments = [
        Attachment(
            filename=f"{i}.pdf", media_type="application/pdf", data=b"%PDF-" + b"x" * 9_000_000
        )
        for i in range(4)
    ]
    blocks = _content_blocks(big, settings)
    encoded = sum(len(b["source"]["data"]) for b in blocks if b["type"] == "document")
    assert encoded <= MAX_TOTAL_ENCODED_BYTES
    assert 0 < len([b for b in blocks if b["type"] == "document"]) < 4


def test_cache_key_distinguishes_attachments_with_the_same_name(settings: Settings) -> None:
    """Two different tickets both called ticket.pdf must not share one verdict."""
    first = doc("restaurant_plain.eml")
    first.attachments = [Attachment(filename="ticket.pdf", media_type="application/pdf", data=b"A")]
    second = doc("restaurant_plain.eml")
    second.attachments = [
        Attachment(filename="ticket.pdf", media_type="application/pdf", data=b"B")
    ]
    assert cache_key(first, settings) != cache_key(second, settings)


def test_cache_key_is_stable_for_identical_input(settings: Settings) -> None:
    assert cache_key(doc("concert_ics.eml"), settings) == cache_key(
        doc("concert_ics.eml"), settings
    )


def test_cache_key_changes_when_the_configuration_does(settings: Settings) -> None:
    baseline = cache_key(doc("concert_ics.eml"), settings)

    settings.anthropic_effort = "high"
    assert cache_key(doc("concert_ics.eml"), settings) != baseline

    settings.anthropic_effort = "medium"
    settings.categories = []
    assert cache_key(doc("concert_ics.eml"), settings) != baseline


def test_cache_key_tracks_the_prompt(settings: Settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Editing the gate must invalidate old verdicts instead of preserving them."""
    baseline = cache_key(doc("concert_ics.eml"), settings)
    monkeypatch.setattr("email_to_cal.llm.SYSTEM_PROMPT", "a different gate")
    assert cache_key(doc("concert_ics.eml"), settings) != baseline
