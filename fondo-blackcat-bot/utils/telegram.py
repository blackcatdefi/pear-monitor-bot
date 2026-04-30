"""Telegram message helpers."""
from __future__ import annotations

from typing import Any

from telegram import Update
from telegram.constants import ParseMode

# R20: every outbound message gets an absolute UTC timestamp footer so BCD
# can verify timing without relying on locally-rendered relative phrases.
try:
    from message_decorator import add_timestamp_to_message as _stamp
except Exception:  # noqa: BLE001
    def _stamp(text: str) -> str:  # type: ignore[no-redef]
        return text

# R21: every outbound message also gets an explicit day/hour header so BCD
# can never confuse which day a message was sent.  Idempotent — does not
# duplicate when the header is already present.
try:
    from message_header import add_header_to_message as _header
except Exception:  # noqa: BLE001
    def _header(text: str) -> str:  # type: ignore[no-redef]
        return text


def _decorate(text: str) -> str:
    """Apply R21 header + R20 footer in canonical order (idempotent)."""
    return _stamp(_header(text))

MAX_LEN = 4000


async def send_long_message(
    update: Update,
    text: str,
    parse_mode: str | None = None,
    reply_markup: Any | None = None,
) -> None:
    """Split a long message into chunks respecting Telegram's 4096 char limit.

    We try to break on paragraph/line boundaries to avoid cutting words.
    If `reply_markup` is provided it's attached only to the LAST chunk.
    """
    if not text:
        return
    if update.message is None:
        return

    text = _decorate(text)

    if len(text) <= MAX_LEN:
        await update.message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        return

    remaining = text
    while remaining:
        if len(remaining) <= MAX_LEN:
            chunk, remaining = remaining, ""
        else:
            cut = remaining.rfind("\n\n", 0, MAX_LEN)
            if cut < MAX_LEN // 2:
                cut = remaining.rfind("\n", 0, MAX_LEN)
            if cut < MAX_LEN // 2:
                cut = remaining.rfind(" ", 0, MAX_LEN)
            if cut <= 0:
                cut = MAX_LEN
            chunk, remaining = remaining[:cut], remaining[cut:].lstrip()
        is_last = not remaining
        await update.message.reply_text(
            chunk,
            parse_mode=parse_mode,
            reply_markup=reply_markup if is_last else None,
        )


async def send_bot_message(bot, chat_id: str | int, text: str, parse_mode: str | None = None) -> None:
    """Send a (possibly long) message outside of an update context (used by scheduler)."""
    if not text:
        return
    text = _decorate(text)
    remaining = text
    while remaining:
        if len(remaining) <= MAX_LEN:
            chunk, remaining = remaining, ""
        else:
            cut = remaining.rfind("\n\n", 0, MAX_LEN)
            if cut < MAX_LEN // 2:
                cut = remaining.rfind("\n", 0, MAX_LEN)
            if cut < MAX_LEN // 2:
                cut = remaining.rfind(" ", 0, MAX_LEN)
            if cut <= 0:
                cut = MAX_LEN
            chunk, remaining = remaining[:cut], remaining[cut:].lstrip()
        await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode)
        
