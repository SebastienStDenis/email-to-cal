"""Turn a raw RFC822 message into a normalised EmailDocument.

Extraction is tiered, cheapest and most reliable first:
  1. schema.org JSON-LD embedded in the HTML part
  2. text/calendar (.ics) parts, however they are labelled
  3. text/plain
  4. HTML rendered down to text
  5. inline images and PDF attachments, handed to the model as vision input
"""

from __future__ import annotations

import json
import logging
import re
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from typing import Any

import html2text
from bs4 import BeautifulSoup
from icalendar import Calendar

from .schema import Attachment, EmailDocument

log = logging.getLogger(__name__)

VISION_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_RESERVATION_TYPES = {
    "FlightReservation",
    "EventReservation",
    "LodgingReservation",
    "FoodEstablishmentReservation",
    "TrainReservation",
    "BusReservation",
    "RentalCarReservation",
    "Reservation",
    "Event",
}


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    converter = html2text.HTML2Text()
    converter.ignore_images = True
    converter.ignore_links = True
    converter.body_width = 0
    text = converter.handle(str(soup))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _extract_json_ld(html: str) -> list[dict[str, Any]]:
    """Pull schema.org reservation blobs out of the HTML part.

    Large senders (airlines, ticketing platforms) embed these because Gmail and Outlook
    consume them, which hands us exact codes and times with zero heuristics.
    """
    found: list[dict[str, Any]] = []
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            log.debug("skipping malformed JSON-LD block")
            continue
        for item in _iter_ld_nodes(parsed):
            if str(item.get("@type", "")) in _RESERVATION_TYPES:
                found.append(item)
    return found


def _iter_ld_nodes(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, list):
        return [child for item in node for child in _iter_ld_nodes(item)]
    if isinstance(node, dict):
        nodes = [node]
        for key in ("@graph", "subjectOf", "reservationFor"):
            if key in node:
                nodes.extend(_iter_ld_nodes(node[key]))
        return nodes
    return []


def _parse_ics(payload: bytes) -> list[dict[str, str]]:
    """Read VEVENTs out of a calendar part.

    Accepts METHOD:REQUEST as readily as METHOD:PUBLISH; ticketing platforms routinely
    mislabel a concert ticket as an invitation, and neither needs an RSVP from us.
    """
    events: list[dict[str, str]] = []
    try:
        calendar = Calendar.from_ical(payload)
    except Exception:
        log.debug("skipping unparseable text/calendar part")
        return events

    for component in calendar.walk("VEVENT"):
        event: dict[str, str] = {}
        for key in ("SUMMARY", "LOCATION", "DESCRIPTION", "UID"):
            value = component.get(key)
            if value is not None:
                event[key.lower()] = str(value)
        for key in ("DTSTART", "DTEND"):
            prop = component.get(key)
            if prop is None:
                continue
            value = prop.dt
            event[key.lower()] = value.isoformat()
            tzinfo = getattr(value, "tzinfo", None)
            if tzinfo is not None:
                event[f"{key.lower()}_tz"] = str(tzinfo)
        if event:
            events.append(event)
    return events


def _decoded_bytes(part: EmailMessage) -> bytes | None:
    """get_payload(decode=True) is only bytes for leaf parts; be explicit about that."""
    payload = part.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else None


def _is_calendar_part(part: EmailMessage) -> bool:
    if part.get_content_type() == "text/calendar":
        return True
    filename = part.get_filename() or ""
    return filename.lower().endswith(".ics")


def parse_email(raw: bytes, *, max_attachment_bytes: int) -> EmailDocument:
    """Flatten a raw message into everything downstream stages might use."""
    message = message_from_bytes(raw, policy=policy.default)

    plain_parts: list[str] = []
    html_parts: list[str] = []
    ics_events: list[dict[str, str]] = []
    attachments: list[Attachment] = []

    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()

        if _is_calendar_part(part):
            payload = _decoded_bytes(part)
            if payload:
                ics_events.extend(_parse_ics(payload))
            continue

        if content_type == "text/plain":
            plain_parts.append(part.get_content())
        elif content_type == "text/html":
            html_parts.append(part.get_content())
        elif content_type in VISION_IMAGE_TYPES or content_type == "application/pdf":
            payload = _decoded_bytes(part)
            if not payload or len(payload) > max_attachment_bytes:
                continue
            attachments.append(
                Attachment(
                    filename=part.get_filename() or f"attachment.{content_type.split('/')[-1]}",
                    media_type=content_type,
                    data=payload,
                )
            )

    html = "\n\n".join(html_parts)
    json_ld = _extract_json_ld(html) if html else []
    plain = "\n\n".join(p.strip() for p in plain_parts).strip()

    body_text, tier = _choose_body(json_ld, ics_events, plain, html)

    return EmailDocument(
        message_id=(message.get("Message-ID") or "").strip() or _synthetic_id(raw),
        subject=str(message.get("Subject") or ""),
        sender=str(message.get("From") or ""),
        to=str(message.get("To") or ""),
        date=_parse_date(message.get("Date")),
        body_text=body_text,
        json_ld=json_ld,
        ics_events=ics_events,
        attachments=attachments,
        source_tier=tier,
    )


def _choose_body(
    json_ld: list[dict[str, Any]],
    ics_events: list[dict[str, str]],
    plain: str,
    html: str,
) -> tuple[str, str]:
    """Pick the richest usable text body and report which tier it came from."""
    text = plain if plain else (_html_to_text(html) if html else "")
    if json_ld:
        return text, "json-ld"
    if ics_events:
        return text, "ics"
    if plain:
        return plain, "plain"
    if text:
        return text, "html"
    return "", "empty"


def _parse_date(value: object) -> Any:
    if not value:
        return None
    try:
        return parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None


def _synthetic_id(raw: bytes) -> str:
    """Fall back to a content hash when a sender omits Message-ID."""
    import hashlib

    return f"<sha256-{hashlib.sha256(raw).hexdigest()[:32]}@email-to-cal.local>"
