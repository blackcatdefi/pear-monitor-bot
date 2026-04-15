"""Telegram channel intelligence via Telethon (userbot session).

Reads recent messages from tiered channels for analysis. Requires
TELETHON_SESSION (StringSession) generated locally via scripts/generate_session.py.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from telethon import TelegramClient
from telethon.sessions import StringSession

from config import (
    CHANNEL_LIMITS,
    CHANNELS,
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
    TELETHON_SESSION,
)

log = logging.getLogger(__name__)

_client: TelegramClient | None = None
_lock = asyncio.Lock()


async def get_client() -> TelegramClient | None:
    """Lazily create + connect the Telethon client. Returns None if not configured."""
    global _client
    if not (TELETHON_SESSION and TELEGRAM_API_ID and TELEGRAM_API_HASH):
        return None
    async with _lock:
        if _client is None:
            _client = TelegramClient(StringSession(TELETHON_SESSION), TELEGRAM_API_ID, TELEGRAM_API_HASH)
            await _client.connect()
            if not await _client.is_user_authorized():
                log.error("Telethon session is not authorized — regenerate StringSession locally.")
                await _client.disconnect()
                _client = None
                return None
        return _client


async def stop_client() -> None:
    global _client
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        _client = None


async def _read_channel(client: TelegramClient, handle: str, limit: int, hours: int = 24) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    try:
        messages = await client.get_messages(handle, limit=limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed reading channel %s: %s", handle, exc)
        return []
    out: list[dict[str, Any]] = []
    for m in messages:
        if not getattr(m, "text", None):
            continue
        if m.date and m.date < cutoff:
            continue
        out.append({
            "date": m.date.isoformat() if m.date else None,
            "views": getattr(m, "views", None),
            "text": (m.text or "").strip(),
        })
    return out


async def fetch_telegram_intel(hours: int = 24) -> dict[str, Any]:
    """Read all channels per tier with appropriate limits. Returns tiered text bundle."""
    client = await get_client()
    if client is None:
        return {"status": "error", "error": "telethon_not_configured"}

    result: dict[str, list[dict[str, Any]]] = {"tier1": [], "tier2": [], "tier3": []}
    for tier, channels in CHANNELS.items():
        limit = CHANNEL_LIMITS.get(tier, 50)
        # Read channels in tier in parallel (be conservative — Telegram throttles)
        sem = asyncio.Semaphore(3)

        async def _do(channel: dict[str, str]) -> dict[str, Any]:
            async with sem:
                msgs = await _read_channel(client, channel["handle"], limit, hours)
                return {
                    "channel": channel["name"],
                    "handle": channel["handle"],
                    "focus": channel.get("focus", ""),
                    "messages": msgs,
                }

        gathered = await asyncio.gather(*[_do(c) for c in channels])
        result[tier] = gathered

    return {"status": "ok", "data": result}
