"""Regenerate the .eml fixtures. Run with: python tests/make_fixtures.py

The fixtures are checked in so tests do not depend on this script, but keeping the
generator around makes it obvious how each MIME shape was constructed.
"""

from __future__ import annotations

import json
import zlib
from email.message import EmailMessage
from pathlib import Path

OUT = Path(__file__).parent / "fixtures"

FLIGHT_LD = {
    "@context": "http://schema.org",
    "@type": "FlightReservation",
    "reservationNumber": "K3TQ9P",
    "reservationFor": {
        "@type": "Flight",
        "flightNumber": "NH106",
        "airline": {"@type": "Airline", "iataCode": "NH"},
        "departureAirport": {"@type": "Airport", "iataCode": "HND"},
        "departureTime": "2026-09-14T18:35:00+09:00",
        "arrivalAirport": {"@type": "Airport", "iataCode": "LAX"},
        "arrivalTime": "2026-09-14T11:25:00-07:00",
    },
}

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Ticketing Co//EN
METHOD:REQUEST
BEGIN:VEVENT
UID:evt-99213@ticketing.example
SUMMARY:Radiohead - Live at the O2
LOCATION:The O2 Arena, Peninsula Square, London SE10 0DX
DTSTART;TZID=Europe/London:20261102T193000
DTEND;TZID=Europe/London:20261102T223000
DESCRIPTION:Doors 18:30. Order TCK-88213.
END:VEVENT
END:VCALENDAR
"""


def _png() -> bytes:
    """A minimal valid 1x1 PNG, built rather than pasted as an opaque blob."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return len(payload).to_bytes(4, "big") + body + zlib.crc32(body).to_bytes(4, "big")

    header = (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + bytes([8, 2, 0, 0, 0])
    pixels = zlib.compress(b"\x00\xff\xff\xff")
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    )


def _base(subject: str, sender: str, message_id: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = "Sebastien <test@icloud.com>"
    msg["Date"] = "Mon, 10 Aug 2026 09:14:02 +0200"
    msg["Message-ID"] = message_id
    return msg


def flight_jsonld() -> EmailMessage:
    msg = _base(
        "Your ANA booking is confirmed - NH106 Tokyo to Los Angeles",
        "All Nippon Airways <no-reply@ana.example>",
        "<flight-k3tq9p@ana.example>",
    )
    msg.set_content(
        "Thanks for booking with ANA.\n\n"
        "Confirmation: K3TQ9P\n"
        "NH106  Tokyo Haneda (HND) 18:35  ->  Los Angeles (LAX) 11:25\n"
        "Monday 14 September 2026\n"
    )
    msg.add_alternative(
        "<html><head>"
        f'<script type="application/ld+json">{json.dumps(FLIGHT_LD)}</script>'
        "</head><body><h1>Booking confirmed</h1>"
        "<p>Confirmation <b>K3TQ9P</b></p>"
        "<table><tr><td>NH106</td><td>HND 18:35</td><td>LAX 11:25</td></tr></table>"
        "</body></html>",
        subtype="html",
    )
    return msg


def concert_ics() -> EmailMessage:
    msg = _base(
        "Your tickets: Radiohead at The O2",
        "Ticketing Co <orders@ticketing.example>",
        "<order-tck88213@ticketing.example>",
    )
    msg.set_content(
        "Your order TCK-88213 is confirmed.\n\n"
        "Radiohead, The O2 Arena, London\n"
        "Monday 2 November 2026, 19:30\n"
        "2 x standing tickets\n"
    )
    msg.add_attachment(ICS.encode(), maintype="text", subtype="calendar", filename="invite.ics")
    return msg


def restaurant_plain() -> EmailMessage:
    msg = _base(
        "Reservation confirmed - Kadeau, Saturday 19:30",
        "Kadeau <bookings@kadeau.example>",
        "<res-4471@kadeau.example>",
    )
    msg.set_content(
        "Hi Sebastien,\n\n"
        "Your table for 2 at Kadeau, Wildersgade 10A, Copenhagen is confirmed for\n"
        "Saturday 22 August 2026 at 19:30.\n\n"
        "Reference 4471. Please let us know 24 hours ahead if you need to cancel.\n"
    )
    return msg


def hotel_html_only() -> EmailMessage:
    msg = _base(
        "Booking confirmation - Hotel Kong Arthur",
        "Bookings <noreply@hotelbooking.example>",
        "<hb-99120@hotelbooking.example>",
    )
    # No text/plain alternative at all: this is the HTML-only shape.
    msg.set_content(
        "<html><body><table>"
        "<tr><td>Hotel Kong Arthur, Nørre Søgade 11, Copenhagen</td></tr>"
        "<tr><td>Check-in</td><td>21 August 2026, 15:00</td></tr>"
        "<tr><td>Check-out</td><td>23 August 2026, 11:00</td></tr>"
        "<tr><td>Reference</td><td>HB-99120</td></tr>"
        "</table></body></html>",
        subtype="html",
    )
    return msg


def promo_image_heavy() -> EmailMessage:
    msg = _base(
        "Concerts near you this weekend",
        "Spotify <no-reply@spotify.example>",
        "<digest-2026-08-10@spotify.example>",
    )
    msg.set_content("Open this email to see concerts near you.")
    msg.add_alternative(
        '<html><body><img src="cid:banner001"><p>Radiohead - The O2, 2 Nov. '
        "Tickets on sale Friday.</p></body></html>",
        subtype="html",
    )
    html_part = msg.get_payload()[-1]
    html_part.add_related(
        _png(), maintype="image", subtype="png", cid="<banner001>", filename="banner.png"
    )
    return msg


def boarding_pass_pdf() -> EmailMessage:
    msg = _base(
        "Your boarding pass",
        "SWISS <checkin@swiss.example>",
        "<bp-lx318@swiss.example>",
    )
    msg.set_content("Your boarding pass is attached.")
    # A tiny but structurally valid PDF; enough to exercise the attachment path.
    pdf = (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )
    msg.add_attachment(pdf, maintype="application", subtype="pdf", filename="boardingpass.pdf")
    return msg


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    builders = {
        "flight_jsonld.eml": flight_jsonld,
        "concert_ics.eml": concert_ics,
        "restaurant_plain.eml": restaurant_plain,
        "hotel_html_only.eml": hotel_html_only,
        "promo_image_heavy.eml": promo_image_heavy,
        "boarding_pass_pdf.eml": boarding_pass_pdf,
    }
    for name, build in builders.items():
        (OUT / name).write_bytes(build().as_bytes())
        print(f"wrote {name}")


if __name__ == "__main__":
    main()
