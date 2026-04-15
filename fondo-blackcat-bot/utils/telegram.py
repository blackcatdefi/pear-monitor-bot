"""Telegram message helpers."""
from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode

MAX_LEN = 4000


async def send_long_message(update: Update, text: str, parse_mode: str | None = None) -> None:
    """Split a long message into chunks respecting Telegram's 4096 char limit.

    We try to break on paragraph/line boundaries to avoid cutting words.
    """
    if not text:
        return
    if update.message is None:
        return

    if len(text) <= MAX_LEN:
        await update.message.reply_text(text, parse_mode=parse_mode)
        return

    remaining = text
    while remaining:
        if len(remaining) <= MAX_LEN:
            chunk, remaining = remaining, ""
        else:
            # Prefer double-newline breakpoint, then newline, then space
            cut = remaining.rfind("\n\n", 0, MAX_LEN)
            if cut < MAX_LEN // 2:
                cut = remaining.rfind("\n", 0, MAX_LEN)
            if cut < MAX_LEN // 2:
                cut = remaining.rfind(" ", 0, MAX_LEN)
            if cut <= 0:
                cut = MAX_LEN
            chunk, remaining = remaining[:cut], remaining[cut:].lstrip()
        await update.message.reply_text(chunk, parse_mode=parse_mode)


async def send_bot_message(bot, chat_id: str | int, text: str, parse_mode: str | None = None) -> None:
    """Send a (possibly long) message outside of an update context (used by scheduler)."""
    if not text:
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
        await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode)
