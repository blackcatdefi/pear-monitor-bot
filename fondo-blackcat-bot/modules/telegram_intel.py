"""Telegram channel intelligence via Telethon (userbot session).

Reads messages from tiered channels AND scans all unread messages in the
main folder, marking them as read after processing. Requires TELETHON_SESSION
(StringSession) generated via scripts/regen_telethon.py.
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
            try:
                _client = TelegramClient(StringSession(TELETHON_SESSION), TELEGRAM_API_ID, TELEGRAM_API_HASH)
                await _client.connect()
                if not await _client.is_user_authorized():
                    log.error("Telethon session is not authorized — regenerate StringSession via scripts/regen_telethon.py.")
                    await _client.disconnect()
                    _client = None
                    return None
            except Exception:
                log.exception("Telethon connect failed — Telegram intel disabled")
                try:
                    if _client is not None:
                        await _client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
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
    """Read all tiered channels (legacy path). Returns tiered text bundle."""
    client = await get_client()
    if client is None:
        return {"status": "error", "error": "telethon_not_configured"}

    result: dict[str, list[dict[str, Any]]] = {"tier1": [], "tier2": [], "tier3": []}
    for tier, channels in CHANNELS.items():
        limit = CHANNEL_LIMITS.get(tier, 50)
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


async def scan_telegram_unread(max_per_dialog: int = 100) -> dict[str, Any]:
    """Scan unread messages in the MAIN folder (folder 0) only, across all
    channels the user follows. Marks each dialog as read after extraction so
    subsequent calls only see new messages.

    Returns {"status": "ok"|"error", "data": [{channel, handle, messages:[{date,text,views}]}], "total_messages": N, "channels_scanned": N}
    """
    client = await get_client()
    if client is None:
        return {"status": "error", "error": "telethon_not_configured"}

    out_channels: list[dict[str, Any]] = []
    total_messages = 0
    channels_scanned = 0

    try:
        # folder=0 is the "main" / all-chats folder (excludes archive/folders)
        dialogs = await client.get_dialogs(folder=0)
    except Exception as exc:  # noqa: BLE001
        log.exception("get_dialogs failed: %s", exc)
        return {"status": "error", "error": f"get_dialogs_failed: {exc}"}

    for dialog in dialogs:
        try:
            unread = int(getattr(dialog, "unread_count", 0) or 0)
            if unread <= 0:
                continue
            # Only channels/megagroups (not private DMs)
            if not getattr(dialog, "is_channel", False):
                continue

            limit = min(unread, max_per_dialog)
            messages: list[dict[str, Any]] = []
            async for msg in client.iter_messages(dialog.entity, limit=limit):
                text = getattr(msg, "text", None)
                if not text:
                    continue
                messages.append({
                    "date": msg.date.isoformat() if msg.date else None,
                    "views": getattr(msg, "views", None),
                    "text": text.strip(),
                })

            if messages:
                out_channels.append({
                    "channel": getattr(dialog, "name", "?"),
                    "handle": getattr(getattr(dialog, "entity", None), "username", None) or "",
                    "unread_count": unread,
                    "messages": messages,
                })
                total_messages += len(messages)
                channels_scanned += 1

            # Mark dialog as read (ack read up to the latest message)
            try:
                await client.send_read_acknowledge(dialog.entity)
            except Exception as exc:  # noqa: BLE001
                log.warning("send_read_acknowledge failed for %s: %s", getattr(dialog, "name", "?"), exc)
        except Exception as exc:  # noqa: BLE001
            # Don't let one bad dialog kill the whole scan
            log.warning("Error scanning dialog %s: %s", getattr(dialog, "name", "?"), exc)
            continue

    return {
        "status": "ok",
        "data": out_channels,
        "total_messages": total_messages,
        "channels_scanned": channels_scanned,
    }
