"""Anthropic calls: decide whether an email is a commitment, and extract the events."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import Any

import anthropic

from .config import Settings
from .schema import EmailDocument, ExtractionResult

log = logging.getLogger(__name__)

# Shared by thinking and the answer, so this is not just the size of the JSON.
MAX_TOKENS = 16000
# Base64 inflates by 4/3 and the API caps a request at 32 MB; leave clear air.
MAX_TOTAL_ENCODED_BYTES = 20 * 1024 * 1024

SYSTEM_PROMPT = """\
You read one email and decide whether it is evidence that the recipient personally \
committed to something that belongs on their calendar. You then extract those events.

# The gate

Set is_committed to true only when the recipient themselves booked, bought, reserved, \
registered for, checked in to, or was directly invited to something, or when the email \
confirms or reminds them about such a thing.

Examples that pass: "Your tickets for Radiohead at the O2", "Booking confirmed - table \
for 4 at 19:30", "Your flight LX318 departs tomorrow", "Order confirmation: 2 x Museum \
entry, Saturday", "Jane has invited you to a design review", "Reminder: your dentist \
appointment is on Thursday".

Examples that fail: "Concerts near you this weekend", "Radiohead tickets on sale Friday", \
"5 events you shouldn't miss in Berlin", a newsletter listing upcoming shows, a price \
alert on a flight route, a digest of what friends are attending, an ad for a restaurant, \
an event mentioned only as background in a longer message.

The distinction is commitment, not topic. A concert email can be either. If the email \
merely tells the recipient that an event exists, or invites them to buy something, it \
fails the gate even when it contains a precise date and venue. If you are unsure, set \
is_committed to false and say why. A missing calendar entry is a small annoyance; a \
calendar full of advertisements is a broken product.

# Extracting events

Emit one event per distinct commitment. A return flight is two events. A hotel stay is \
one event. Do not invent detail the email does not contain.

Times: report start_local and end_local as the local wall-clock time at the place the \
event happens, as naive ISO 8601 with no offset and no zone suffix. Never convert to UTC \
and never apply an offset yourself. Set start_tz only when the email states or clearly \
implies the zone. For flights, fill in departure_iata and arrival_iata instead and leave \
the zones null - the airport codes resolve to zones downstream, which is more reliable \
than inferring them.

Set all_day true only when the email gives no meaningful clock time. A hotel stay with a \
check-in time is not all-day.

Relative dates ("this Friday", "tomorrow") resolve against the email's sent date, which \
is given to you below.

Titles are short and scannable: "LX318 ZRH to LHR", "Radiohead at the O2", "Dinner at \
Kadeau". Put booking references, seats, terminals, and confirmation numbers in \
description, not the title.

Confidence reflects how certain you are that this specific event, with these specific \
times, is real and committed. Lower it when times are implied rather than stated.
"""


def _render_email(
    doc: EmailDocument, settings: Settings, pdf_texts: list[tuple[str, str]] | None = None
) -> str:
    """Lay the email out for the model, structured tiers first."""
    lines = [
        "<email>",
        f"From: {doc.sender}",
        f"To: {doc.to}",
        f"Subject: {doc.subject}",
        f"Sent: {doc.date.isoformat() if doc.date else 'unknown'}",
        f"Recipient's default timezone: {settings.default_timezone}",
        "",
    ]

    for filename, text in pdf_texts or []:
        lines += [
            f"<attachment_text file='{filename}' note='text layer of an attached PDF'>",
            text,
            "</attachment_text>",
            "",
        ]

    if doc.json_ld:
        lines += [
            "<structured_data format='schema.org JSON-LD, embedded by the sender'>",
            json.dumps(doc.json_ld, indent=2, default=str)[:20000],
            "</structured_data>",
            "",
        ]

    if doc.ics_events:
        lines += [
            "<calendar_attachment>",
            json.dumps(doc.ics_events, indent=2, default=str)[:8000],
            "</calendar_attachment>",
            "",
        ]

    body = doc.body_text[:40000]
    lines += ["<body>", body if body else "(no readable text body)", "</body>", "</email>"]
    return "\n".join(lines)


def _categories_block(settings: Settings) -> str:
    if not settings.categories:
        return "\n\nNo categories are configured. Always set category to null."
    rendered = "\n".join(f"- {r.name}: {r.description}" for r in settings.categories)
    return (
        "\n\n# Categories\n\nAssign each event to exactly one of these category names, or "
        "null if none genuinely fit. Do not invent names.\n\n" + rendered
    )


def _content_blocks(doc: EmailDocument, settings: Settings) -> list[Any]:
    """Attachments first, then the text. Documents read better placed before the prompt."""
    media: list[Any] = []

    if settings.enable_vision:
        # Attachments earn their tokens when the text is thin, or when a PDF is present at
        # all - e-tickets and boarding passes carry the real detail in the PDF.
        text_is_thin = len(doc.body_text) < 400 and not doc.has_structured_source
        encoded_total = 0

        for attachment in doc.attachments:
            if attachment.is_pdf:
                block_type, media_type = "document", "application/pdf"
            elif attachment.is_image and text_is_thin:
                block_type, media_type = "image", attachment.media_type
            else:
                continue

            encoded = base64.standard_b64encode(attachment.data).decode()
            # The API caps a request at 32 MB and base64 inflates by 4/3, so a handful of
            # large PDFs would 413. Stop well short rather than lose the whole email.
            if encoded_total + len(encoded) > MAX_TOTAL_ENCODED_BYTES:
                log.warning("skipping %s: attachment budget exhausted", attachment.filename)
                continue
            encoded_total += len(encoded)

            media.append(
                {
                    "type": block_type,
                    "source": {"type": "base64", "media_type": media_type, "data": encoded},
                }
            )

    return [*media, {"type": "text", "text": _render_email(doc, settings)}]


def cache_key(doc: EmailDocument, settings: Settings) -> str:
    """Hash everything that could change the answer, so replays are free.

    Attachments hash by content, not filename: two different tickets both called
    ticket.pdf must not share a verdict. The prompt is hashed too, so editing the gate
    invalidates every stale answer instead of silently preserving it.

    This material must stay byte-stable across releases: eval-local finds historical
    verdicts by recomputing these keys, so a gratuitous change orphans every cached
    answer. The local prefilter caches under its own keys (see local_llm).
    """
    material = json.dumps(
        {
            "message_id": doc.message_id,
            "body": doc.body_text,
            "json_ld": doc.json_ld,
            "ics": doc.ics_events,
            "attachments": sorted(hashlib.sha256(a.data).hexdigest() for a in doc.attachments),
            "prompt": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
            "model": settings.anthropic_model,
            "effort": settings.anthropic_effort,
            "categories": [r.model_dump() for r in settings.categories],
            "vision": settings.enable_vision,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(material.encode()).hexdigest()


class Extractor:
    """Wraps the Anthropic client with the prompt and schema."""

    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None) -> None:
        self._settings = settings
        self._client = client or anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def extract(self, doc: EmailDocument) -> ExtractionResult:
        settings = self._settings
        response = self._client.messages.parse(
            model=settings.anthropic_model,
            # Thinking is on by default on Opus 5 and shares this budget with the answer,
            # so a multi-leg itinerary at high effort needs real headroom here.
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT + _categories_block(settings),
            output_format=ExtractionResult,
            output_config={"effort": settings.anthropic_effort},
            messages=[{"role": "user", "content": _content_blocks(doc, settings)}],
        )
        if response.stop_reason == "max_tokens":
            raise RuntimeError(
                f"extraction hit the {MAX_TOKENS} token ceiling before finishing; "
                "lower ANTHROPIC_EFFORT or raise the ceiling"
            )
        if response.stop_reason == "refusal":
            raise RuntimeError(f"model declined to process the email: {response.stop_details}")
        result = response.parsed_output
        if result is None:
            raise RuntimeError(f"model returned no parseable output (stop={response.stop_reason})")
        return result
