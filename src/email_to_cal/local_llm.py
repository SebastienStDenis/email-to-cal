"""Local extraction through an Ollama server: the same contract as llm.py, run on-box.

The local path is text-only. Attachments are never sent as vision input; instead the
text layer of PDF attachments is extracted here and appended to the rendered email,
which covers e-tickets and itineraries whose body is just "see attached". Image-only
attachments (and scanned PDFs) contribute nothing, by design - the model that reads
them well lives behind the Anthropic backend.
"""

from __future__ import annotations

import hashlib
import json
import logging
from io import BytesIO
from typing import Any

import httpx
from pypdf import PdfReader

from .config import Settings
from .llm import SYSTEM_PROMPT, _categories_block, _render_email
from .schema import EmailDocument, ExtractionResult

log = logging.getLogger(__name__)

# Enough for a multi-leg itinerary; past this a PDF is boilerplate, not booking detail.
MAX_PDF_TEXT_CHARS = 15000
MAX_PDF_PAGES = 10

_OUTPUT_ADDENDUM = """\


# Output

Answer with a single JSON object conforming to this JSON schema, and nothing else. \
When is_committed is false, events is an empty list.

{schema}
"""


class OllamaUnavailable(RuntimeError):
    """The Ollama server is unreachable or lacks the model. Transient: hold the mail."""


def local_system_prompt(settings: Settings) -> str:
    """The shared gate prompt, plus the schema the grammar will enforce.

    Ollama's `format` constrains the output shape mechanically, but the model never
    sees the grammar - the schema goes in the prompt too so the field descriptions
    (naive local times, IATA codes, title style) actually reach it.
    """
    schema = json.dumps(ExtractionResult.model_json_schema(), sort_keys=True)
    return SYSTEM_PROMPT + _categories_block(settings) + _OUTPUT_ADDENDUM.format(schema=schema)


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
    """The email as the local model reads it: text tiers plus PDF text layers."""
    return _render_email(doc, settings, pdf_texts=_pdf_texts(doc))


def local_cache_material(doc: EmailDocument, settings: Settings) -> str:
    """Cache material for the local backend; a separate namespace from the Claude keys.

    Attachments hash by content: the PDF text layer is derived from them, so hashing
    the bytes covers it. The assembled prompt hash covers the gate, the categories,
    and the schema addendum at once.
    """
    return json.dumps(
        {
            "backend": "ollama",
            "message_id": doc.message_id,
            "body": doc.body_text,
            "json_ld": doc.json_ld,
            "ics": doc.ics_events,
            "attachments": sorted(hashlib.sha256(a.data).hexdigest() for a in doc.attachments),
            "prompt": hashlib.sha256(local_system_prompt(settings).encode()).hexdigest(),
            "model": settings.ollama_model,
        },
        sort_keys=True,
        default=str,
    )


class OllamaExtractor:
    """Wraps an Ollama server with the prompt, the schema grammar, and one retry."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.Client(
            base_url=settings.ollama_url,
            # Connect fast or fail fast; the generous budget is for generation.
            timeout=httpx.Timeout(settings.ollama_timeout_seconds, connect=10.0),
        )

    def extract(self, doc: EmailDocument) -> ExtractionResult:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": local_system_prompt(self._settings)},
            {"role": "user", "content": render_local(doc, self._settings)},
        ]
        last_error = ""
        for attempt in range(2):
            content = self._chat(messages)
            try:
                return ExtractionResult.model_validate_json(content)
            except ValueError as exc:
                # The grammar guarantees the shape but not the value constraints
                # (confidence bounds, category names), so feed the error back once.
                last_error = str(exc)
                log.warning(
                    "local model output failed validation (attempt %d): %s",
                    attempt + 1,
                    last_error[:500],
                )
                messages = [
                    *messages,
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": "That JSON failed validation:\n"
                        f"{last_error}\n\nReply again with one corrected JSON object.",
                    },
                ]
        raise RuntimeError(f"local model produced unusable output: {last_error[:500]}")

    def _chat(self, messages: list[dict[str, str]]) -> str:
        settings = self._settings
        payload: dict[str, Any] = {
            "model": settings.ollama_model,
            "stream": False,
            "keep_alive": settings.ollama_keep_alive,
            "format": ExtractionResult.model_json_schema(),
            "options": {"num_ctx": settings.ollama_num_ctx},
            "messages": messages,
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
        content = response.json().get("message", {}).get("content", "")
        if not str(content).strip():
            raise RuntimeError("Ollama returned an empty response")
        return str(content)
