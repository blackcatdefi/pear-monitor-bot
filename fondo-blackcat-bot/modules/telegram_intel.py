"""
Telethon-powered reader para canales de inteligencia.

Usa StringSession (via env var TELETHON_SESSION) para correr en Railway sin
necesidad de archivo de sesión. Genera la session localmente una sola vez con:

    from telethon.sync import TelegramClient
    from telethon.sessions import StringSession
    with TelegramClient(StringSession(), API_ID, API_HASH) as c:
        print(c.session.save())

Y luego guardá el string en Railway env vars como TELETHON_SESSION.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient
from telethon.sessions import StringSession

from config import (
    CHANNELS,
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
    TELETHON_SESSION,
)

log = logging.getLogger(__name__)

TIER_LIMITS = {"tier1": 200, "tier2": 50, "tier3": 20}
TIER_HOURS = 24


def _client() -> TelegramClient:
    """Client Telethon global (session string)."""
    return TelegramClient(
        StringSession(TELETHON_SESSION),
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH,
    )


async def _read_channel(client: TelegramClient, handle: str, limit: int,
                        hours: int = TIER_HOURS) -> list[dict]:
    try:
        entity = await client.get_entity(handle)
        messages = await client.get_messages(entity, limit=limit)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        out = []
        for m in messages:
            if not m.text:
                continue
            if m.date and m.date < cutoff:
                continue
            out.append({
                "text": m.text,
                "date": m.date.isoformat() if m.date else None,
                "views": getattr(m, "views", None),
            })
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("Telethon read %s failed: %s", handle, e)
        return []


async def fetch_telegram_intel() -> dict:
    """Devuelve mensajes crudos agrupados por tier."""
    if not TELETHON_SESSION or not TELEGRAM_API_ID:
        log.warning("Telethon no configurado — skipping intel")
        return {"tier1": [], "tier2": [], "tier3": [], "error": "not_configured"}

    out: dict = {"tier1": [], "tier2": [], "tier3": []}

    async with _client() as client:
        for tier, channels in CHANNELS.items():
            limit = TIER_LIMITS.get(tier, 20)
            for ch in channels:
                msgs = await _read_channel(client, ch["handle"], limit)
                if not msgs:
                    continue
                out[tier].append({
                    "name": ch["name"],
                    "handle": ch["handle"],
                    "focus": ch["focus"],
                    "messages": msgs,
                })

    # Totales para debug
    for tier in ("tier1", "tier2", "tier3"):
        total_msgs = sum(len(c["messages"]) for c in out[tier])
        log.info("Telegram intel %s: %d channels, %d msgs", tier, len(out[tier]), total_msgs)

    return out


def compile_intel_for_claude(intel: dict) -> str:
    """Texto plano concatenado para pasarle al LLM."""
    if not intel or intel.get("error"):
        return "(Telegram intel no disponible)"
    parts = []
    for tier_name, priority in [("tier1", "TIER 1 (full read)"),
                                  ("tier2", "TIER 2 (highlights)"),
                                  ("tier3", "TIER 3 (high-impact)")]:
        channels = intel.get(tier_name) or []
        if not channels:
            continue
        parts.append(f"\n=== {priority} ===")
        for ch in channels:
            parts.append(f"\n--- {ch['name']} (@{ch['handle']}) — {ch['focus']} ---")
            for m in ch["messages"]:
                text = (m.get("text") or "").strip().replace("\n", " ")
                if len(text) > 500:
                    text = text[:500] + "..."
                parts.append(f"[{m.get('date', '')[:16]}] {text}")
    return "\n".join(parts) if parts else "(sin mensajes recientes)"
