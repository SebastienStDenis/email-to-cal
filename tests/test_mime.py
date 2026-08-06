from __future__ import annotations

import pytest

from email_to_cal.mime import parse_email

from .conftest import fixture_bytes

MAX_BYTES = 8 * 1024 * 1024


def parse(name: str):
    return parse_email(fixture_bytes(name), max_attachment_bytes=MAX_BYTES)


def test_jsonld_is_preferred_tier() -> None:
    doc = parse("flight_jsonld.eml")
    assert doc.source_tier == "json-ld"
    assert len(doc.json_ld) == 1
    reservation = doc.json_ld[0]
    assert reservation["reservationNumber"] == "K3TQ9P"
    assert reservation["reservationFor"]["departureAirport"]["iataCode"] == "HND"
    # The text body still comes through for the model to read alongside.
    assert "K3TQ9P" in doc.body_text


def test_ics_attachment_is_parsed_despite_method_request() -> None:
    doc = parse("concert_ics.eml")
    assert doc.source_tier == "ics"
    assert len(doc.ics_events) == 1
    event = doc.ics_events[0]
    assert event["summary"] == "Radiohead - Live at the O2"
    assert event["dtstart"].startswith("2026-11-02T19:30")
    assert event["dtstart_tz"] == "Europe/London"


def test_plain_text_only() -> None:
    doc = parse("restaurant_plain.eml")
    assert doc.source_tier == "plain"
    assert "Kadeau" in doc.body_text
    assert doc.json_ld == []
    assert doc.attachments == []


def test_html_only_is_rendered_to_text() -> None:
    doc = parse("hotel_html_only.eml")
    assert doc.source_tier == "html"
    assert "Hotel Kong Arthur" in doc.body_text
    assert "Nørre Søgade" in doc.body_text
    # Markup must not leak into what the model reads.
    assert "<table>" not in doc.body_text


def test_inline_image_is_captured_as_attachment() -> None:
    doc = parse("promo_image_heavy.eml")
    assert doc.source_tier == "plain"
    assert [a.media_type for a in doc.attachments] == ["image/png"]
    assert doc.attachments[0].is_image


def test_pdf_attachment_is_captured() -> None:
    doc = parse("boarding_pass_pdf.eml")
    assert len(doc.attachments) == 1
    assert doc.attachments[0].is_pdf
    assert doc.attachments[0].filename == "boardingpass.pdf"


def test_oversized_attachments_are_dropped() -> None:
    doc = parse_email(fixture_bytes("boarding_pass_pdf.eml"), max_attachment_bytes=10)
    assert doc.attachments == []


def test_headers_and_date() -> None:
    doc = parse("flight_jsonld.eml")
    assert doc.message_id == "<flight-k3tq9p@ana.example>"
    assert "ana.example" in doc.sender
    assert doc.date is not None
    assert doc.date.year == 2026


def test_missing_message_id_falls_back_to_content_hash() -> None:
    raw = fixture_bytes("restaurant_plain.eml")
    stripped = b"\n".join(line for line in raw.split(b"\n") if not line.startswith(b"Message-ID:"))
    doc = parse_email(stripped, max_attachment_bytes=MAX_BYTES)
    assert doc.message_id.startswith("<sha256-")
    # Stable across repeated parses of identical bytes.
    assert doc.message_id == parse_email(stripped, max_attachment_bytes=MAX_BYTES).message_id


@pytest.mark.parametrize(
    "name",
    [
        "flight_jsonld.eml",
        "concert_ics.eml",
        "restaurant_plain.eml",
        "hotel_html_only.eml",
        "promo_image_heavy.eml",
        "boarding_pass_pdf.eml",
    ],
)
def test_every_fixture_parses_without_raising(name: str) -> None:
    doc = parse(name)
    assert doc.subject
