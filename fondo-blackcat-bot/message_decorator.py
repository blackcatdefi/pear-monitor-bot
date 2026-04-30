"""Round 20 — Message decorator + helpers.

Ensures bot messages include explicit absolute UTC timestamps so BCD can
verify timing without relying on locally-rendered "in X hours" phrases.

Two surfaces:
- `with_timestamp(position)` — async decorator for handlers whose return
  value is a string. Useful for tidy command handlers.
- `add_timestamp_to_message(message, position)` — direct helper called
  inside utils.telegram.send_bot_message so EVERY outbound message gets
  a timestamp without needing to wrap each handler.

Toggle:
    MESSAGE_TIMESTAMP_ENABLED=true   (default)
    MESSAGE_TIMESTAMP_POSITION=bottom (default) | top
"""
from __future__ import annotations

import functools
import os

from time_awareness import format_timestamp


def _enabled() -> bool:
    return os.getenv("MESSAGE_TIMESTAMP_ENABLED", "true").strip().lower() != "false"


def _position() -> str:
    return os.getenv("MESSAGE_TIMESTAMP_POSITION", "bottom").strip().lower()


def add_timestamp_to_message(message: str, position: str | None = None) -> str:
    """Helper to add timestamp footer/header to a message string.

    If MESSAGE_TIMESTAMP_ENABLED=false, returns the message unchanged.
    Idempotent: if the message already starts with the clock emoji header
    or ends with the italicised footer, it is returned untouched.
    """
    if not _enabled() or not isinstance(message, str) or not message:
        return message

    pos = position or _position()
    ts = format_timestamp()

    # Idempotency: don't double-stamp if already present
    if "🕐" in message:
        return message

    if pos == "top":
        return f"{ts}\n\n{message}"
    # Use plain text (no markdown) to render correctly regardless of parse_mode.
    return f"{message}\n\n{ts}"


def with_timestamp(position: str = "bottom"):
    """Decorator that adds timestamp to bot message return values.

    Args:
        position: 'top' or 'bottom'
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            result = await func(*args, **kwargs)
            if isinstance(result, str):
                return add_timestamp_to_message(result, position=position)
            return result
        return wrapper
    return decorator
