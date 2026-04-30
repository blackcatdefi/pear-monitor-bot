"""Round 21 — Message header injector.

Complements `message_decorator.add_timestamp_to_message` (footer) with an
explicit day/hour HEADER at the start of every outbound bot message.

Why:
    BCD reported 2026-04-30 momentary confusion thinking a Powell catalyst
    was "yesterday" when it was scheduled for the same day. R20 fixed bot
    timing internally; R21 closes the human-perception gap by anchoring
    every message with an explicit ``📅 <day> <date> — HH:MM UTC`` header.

Toggle:
    MESSAGE_HEADER_ENABLED=true   (default)
"""
from __future__ import annotations

import os

from time_awareness import now_utc


def _enabled() -> bool:
    return os.getenv("MESSAGE_HEADER_ENABLED", "true").strip().lower() != "false"


def format_header() -> str:
    """Return the canonical day/hour header line.

    Example:
        '📅 Jueves 30 abril 2026 — 16:25 UTC\\n━━━━━━━━━━━━━━━━━━'
    """
    now = now_utc()

    days_es = [
        "Lunes", "Martes", "Miércoles", "Jueves",
        "Viernes", "Sábado", "Domingo",
    ]
    months_es = [
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    ]

    day_name = days_es[now.weekday()]
    month_name = months_es[now.month - 1]

    return (
        f"📅 {day_name} {now.day} {month_name} {now.year} — "
        f"{now.strftime('%H:%M')} UTC\n"
        f"━━━━━━━━━━━━━━━━━━"
    )


def add_header_to_message(message: str) -> str:
    """Prepend day/hour header to a message string.

    Idempotent — if the message already starts with ``📅`` it is returned
    untouched. Returns the message unchanged when the toggle is disabled
    or when the input is empty / non-string.
    """
    if not _enabled() or not isinstance(message, str) or not message:
        return message

    if message.lstrip().startswith("📅"):
        return message

    return f"{format_header()}\n\n{message}"
