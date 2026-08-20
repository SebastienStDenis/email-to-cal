"""One model call per flagged email: read it, and return the events it commits to.

The human already decided the email matters by flagging it, so the model is never asked
whether an event belongs on the calendar - only what the event is. Either Claude or a
local Ollama model does the whole job; there is no cascade between them.
"""

from __future__ import annotations

import base64
import json
import logging
from io import BytesIO
from typing import Any, Protocol

import anthropic
import httpx
from pypdf import PdfReader

from .config import Settings
from .schema import EmailDocument, ExtractionResult

log = logging.getLogger(__name__)

# Shared by thinking and the answer, so this is not just the size of the JSON.
MAX_TOKENS = 16000
# Base64 inflates by 4/3 and the API caps a request at 32 MB; leave clear air.
MAX_TOTAL_ENCODED_BYTES = 20 * 1024 * 1024
# Enough for a multi-leg itinerary; past this a PDF is boilerplate, not booking detail.
MAX_PDF_TEXT_CHARS = 15000
MAX_PDF_PAGES = 10

SYSTEM_PROMPT = """\
You read one email and extract the events in it that belong on the recipient's calendar. \
The recipient has already flagged this email themselves, so it is not your job to judge \
whether it deserves a calendar entry - it does. Find the events and describe them \
precisely.

Emit one event per distinct commitment. A return flight is two events. A hotel stay is \
one event. Do not invent detail the email does not contain. If the email genuinely \
describes nothing that can be placed on a calendar - no date, no time, nothing to \
attend - return an empty list rather than guessing; the recipient is told when that \
happens.

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

Locations: the calendar geocodes whatever you put in location, so a venue name on its own \
usually resolves to nothing and the event gets no map, no directions, and no travel time. \
Fill in every address part the email actually gives - the structured data block, a \
"getting here" section, and the sender's footer are where they usually hide - and leave \
the rest null. Never invent a street, postal code, or city you cannot see: a name with no \
address is far better than a name with a wrong one. For flights leave location null \
entirely, the departure airport's address is filled in downstream from the IATA code.
"""


class ExtractionFailed(RuntimeError):
    """The model could not answer. Distinct from an answer of "no events"."""


def _pdf_text(data: bytes) -> str:
    """Best-effort text layer of a PDF; scanned or image-only PDFs come back empty."""
    try:
        reader = PdfReader(BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages[:MAX_PDF_PAGES]]
        return "\n".join(pages).strip()
    except Exception:
        log.debug("PDF text extraction failed", exc_info=True)
        return ""


def _pdf_texts(doc: EmailDocument) -> list[tuple[str, str]]:
    texts: list[tuple[str, str]] = []
    remaining = MAX_PDF_TEXT_CHARS
    for attachment in doc.attachments:
        if not attachment.is_pdf or remaining <= 0:
            continue
        extracted = _pdf_text(attachment.data)[:remaining]
        if not extracted:
            continue
        remaining -= len(extracted)
        texts.append((attachment.filename, extracted))
    return texts


def render_email(
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


class Extractor(Protocol):
    """Whatever reads an email and returns its events."""

    def extract(self, doc: EmailDocument) -> ExtractionResult: ...


def make_extractor(settings: Settings) -> Extractor:
    if settings.provider == "ollama":
        return OllamaExtractor(settings)
    return AnthropicExtractor(settings)


class AnthropicExtractor:
    """Claude, with attachments as vision input when the text tiers come up short."""

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
            system=SYSTEM_PROMPT,
            output_format=ExtractionResult,
            output_config={"effort": settings.anthropic_effort},
            messages=[{"role": "user", "content": self._content_blocks(doc)}],
        )
        if response.stop_reason == "max_tokens":
            raise ExtractionFailed(
                f"hit the {MAX_TOKENS} token ceiling before finishing; "
                "lower the effort setting or raise the ceiling"
            )
        if response.stop_reason == "refusal":
            raise ExtractionFailed(
                f"the model declined to read this email: {response.stop_details}"
            )
        result = response.parsed_output
        if result is None:
            raise ExtractionFailed(f"no parseable output (stop={response.stop_reason})")
        return result

    def _content_blocks(self, doc: EmailDocument) -> list[Any]:
        """Attachments first, then the text. Documents read better placed before the prompt."""
        media: list[Any] = []

        if self._settings.enable_vision:
            # Attachments earn their tokens when the text is thin, or when a PDF is present
            # at all - e-tickets and boarding passes carry the real detail in the PDF.
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
                # The API caps a request at 32 MB and base64 inflates by 4/3, so a handful
                # of large PDFs would 413. Stop well short rather than lose the email.
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

        return [*media, {"type": "text", "text": render_email(doc, self._settings)}]


class OllamaExtractor:
    """A local model, free and private, reading text only.

    PDF attachments contribute their text layer, so an e-ticket with an empty body still
    shows the model its flight. Scanned tickets and image attachments carry no text and
    will come back with nothing - which the user is told about rather than losing.
    """

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.Client(
            base_url=settings.ollama_url,
            # Connect fast or fail fast; the generous budget is for generation.
            timeout=httpx.Timeout(settings.ollama_timeout_seconds, connect=10.0),
        )

    def extract(self, doc: EmailDocument) -> ExtractionResult:
        settings = self._settings
        payload: dict[str, Any] = {
            "model": settings.ollama_model,
            "stream": False,
            "keep_alive": settings.ollama_keep_alive,
            "format": ExtractionResult.model_json_schema(),
            "options": {"num_ctx": settings.ollama_num_ctx},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": render_email(doc, settings, pdf_texts=_pdf_texts(doc)),
                },
            ],
        }
        try:
            response = self._client.post("/api/chat", json=payload)
        except httpx.HTTPError as exc:
            raise ExtractionFailed(f"cannot reach Ollama at {settings.ollama_url}: {exc}") from exc
        if response.status_code == 404:
            raise ExtractionFailed(
                f"Ollama has no model {settings.ollama_model!r}; "
                f"run: ollama pull {settings.ollama_model}"
            )
        if response.status_code >= 400:
            raise ExtractionFailed(f"Ollama error {response.status_code}: {response.text[:300]}")
        content = str(response.json().get("message", {}).get("content", ""))
        try:
            return ExtractionResult.model_validate_json(content)
        except ValueError as exc:
            raise ExtractionFailed(
                f"{settings.ollama_model} returned unusable JSON: {exc}"
            ) from exc
