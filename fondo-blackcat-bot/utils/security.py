"""Authorization decorator for python-telegram-bot handlers."""
from __future__ import annotations

import functools
import logging
from typing import Awaitable, Callable

from telegram import Update
from telegram.ext import ContextTypes

from config import TELEGRAM_CHAT_ID

log = logging.getLogger(__name__)

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def authorized(func: Handler) -> Handler:
    """Decorator: only allow the configured TELEGRAM_CHAT_ID, ignore others silently."""

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None or str(chat.id) != str(TELEGRAM_CHAT_ID):
            log.info("Ignoring unauthorized chat id=%s user=%s", chat.id if chat else None, update.effective_user)
            return
        return await func(update, context)

    return wrapper
