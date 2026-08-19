"""A free local pre-filter in front of the paid extraction call.

A small model on an Ollama server answers one recall-biased question per email: could
this plausibly contain a personal commitment? Obvious junk - newsletters, promos,
digests, receipts for things already over - is discarded without ever reaching the
API; everything else goes to Claude, which still makes every real decision. The filter
is deliberately not asked the hard question (is this a commitment?); small models get
that wrong in ways that lose real bookings.

Fail open, always: an unreachable Ollama or unusable output means the email goes to
Claude as if the filter were off. The filter can only ever save money, never mail.

The filter is text-only, but PDF attachments contribute their text layer, so a
"boarding pass attached" email with an empty body still shows the filter its flight.
Emails with structured data (.ics, JSON-LD) bypass the filter entirely - senders embed
those for real bookings, and a cheap model should not get a veto over them.
"""

from __future__ import annotations

import hashlib
import json
import logging
from io import BytesIO
from typing import Any

import httpx
from pydantic import BaseModel, Field
from pypdf import PdfReader

from .config import Settings
from .llm import _render_email
from .schema import EmailDocument

log = logging.getLogger(__name__)

# Enough for a multi-leg itinerary; past this a PDF is boilerplate, not booking detail.
MAX_PDF_TEXT_CHARS = 15000
MAX_PDF_PAGES = 10

FILTER_PROMPT = """\
You are a cheap pre-filter in front of a more capable model that turns emails into
calendar events for the recipient. Your only job is to discard email that obviously
cannot contain a personal booking, reservation, ticket, appointment, or invitation.

Set could_contain_commitment to false only when the email is clearly one of these:
marketing or promotional mail, newsletters and digests, price alerts and deal lists,
social-media or app notifications, account/security/billing notices, receipts for
purchases or rides that are already over with nothing upcoming, shipping updates, or
automated reports.

Set it to true for everything else, including anything you are unsure about. Bookings,
order and reservation confirmations, tickets, itineraries, schedule or gate changes,
check-in notices, reminders, and personal invitations must always pass - you are not
deciding whether the email really is a commitment; the capable model does that. An
email you wrongly discard is lost forever; an email you wrongly pass merely costs one
extra look. When in doubt, true.

Answer with a single JSON object: {"could_contain_commitment": true or false,
"reasoning": "one short sentence"}.
"""


class FilterVerdict(BaseModel):
    """The filter's one-bit answer, plus a sentence for the logs."""

    could_contain_commitment: bool
    reasoning: str = Field(description="One short sentence justifying the decision.")


class OllamaUnavailable(RuntimeError):
    """The Ollama server is unreachable or lacks the model. The filter fails open."""


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


def render_local(doc: EmailDocument, settings: Settings) -> str:
    """The email as the filter reads it: the text tiers plus PDF text layers."""
    return _render_email(doc, settings, pdf_texts=_pdf_texts(doc))


def filter_cache_key(doc: EmailDocument, settings: Settings) -> str:
    """Filter verdicts cache in their own namespace, apart from the Claude keys."""
    material = json.dumps(
        {
            "kind": "local-filter",
            "message_id": doc.message_id,
            "body": doc.body_text,
            "json_ld": doc.json_ld,
            "ics": doc.ics_events,
            "attachments": sorted(hashlib.sha256(a.data).hexdigest() for a in doc.attachments),
            "prompt": hashlib.sha256(FILTER_PROMPT.encode()).hexdigest(),
            "model": settings.ollama_model,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(material.encode()).hexdigest()


def make_prefilter(settings: Settings) -> OllamaFilter | None:
    return OllamaFilter(settings) if settings.local_filter_enabled else None


class OllamaFilter:
    """Asks an Ollama server the one cheap question, with the shape grammar-enforced."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.Client(
            base_url=settings.ollama_url,
            # Connect fast or fail fast; the generous budget is for generation.
            timeout=httpx.Timeout(settings.ollama_timeout_seconds, connect=10.0),
        )

    def judge(self, doc: EmailDocument) -> FilterVerdict:
        """One verdict for one email. Raises rather than guessing; callers fail open."""
        settings = self._settings
        payload: dict[str, Any] = {
            "model": settings.ollama_model,
            "stream": False,
            "keep_alive": settings.ollama_keep_alive,
            "format": FilterVerdict.model_json_schema(),
            "options": {"num_ctx": settings.ollama_num_ctx},
            "messages": [
                {"role": "system", "content": FILTER_PROMPT},
                {"role": "user", "content": render_local(doc, settings)},
            ],
        }
        try:
            response = self._client.post("/api/chat", json=payload)
        except httpx.HTTPError as exc:
            raise OllamaUnavailable(f"cannot reach Ollama at {settings.ollama_url}: {exc}") from exc
        if response.status_code == 404:
            raise OllamaUnavailable(
                f"Ollama has no model {settings.ollama_model!r}; "
                f"run: ollama pull {settings.ollama_model}"
            )
        if response.status_code >= 500:
            raise OllamaUnavailable(f"Ollama error {response.status_code}: {response.text[:300]}")
        response.raise_for_status()
        content = str(response.json().get("message", {}).get("content", ""))
        return FilterVerdict.model_validate_json(content)
