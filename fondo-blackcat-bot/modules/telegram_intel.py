"""Telethon userbot — read Telegram channels from last N hours.

Three tiers:
  Tier 1: full read, up to 200 messages per channel, last 24h
  Tier 2: last 50 messages, filter high-signal
  Tier 3: last 20 messages, only high-impact keywords

Persistence: StringSession stored in config.TELETHON_SESSION (generated once
locally via scripts/generate_session.py and then set as env var on Railway).

All network errors are swallowed so the main report still builds.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from config import TELEGRAM_API_HASH, TELEGRAM_API_ID, TELETHON_SESSION

log = logging.getLogger(__name__)

# Channels organised by tier (from user spec)
CHANNELS = {
    "tier1": [
        {"name": "Medusa Capital", "handle": "medusa_capital_es", "focus": "Spanish macro/equity, geopolitical"},
        {"name": "AIXBT Daily Reports", "handle": "aixbtfeed", "focus": "Daily insights, institutional flows"},
        {"name": "Agent Pear Signals", "handle": "agentpear", "focus": "Pair trade signals, HL stats"},
        {"name": "Felix Protocol", "handle": "felixprotocol", "focus": "Hyperliquid ecosystem"},
        {"name": "ZordXBT", "handle": "zordxbt", "focus": "BTC technicals"},
        {"name": "Monitoring The Situation", "handle": "monitoringbias", "focus": "Geopolitical"},
    ],
    "tier2": [
        {"name": "Prediction Desk News", "handle": "PredictionDeskNews", "focus": "Breaking news / Polymarket"},
        {"name": "Lookonchain", "handle": "lookonchainchannel", "focus": "Whale movements"},
        {"name": "Campbell Ramble", "handle": "campbellramble", "focus": "Macro"},
        {"name": "Crypto Ballena", "handle": "CryptoBallenaOficial", "focus": "Spanish whale alerts"},
        {"name": "Kleomedes", "handle": "kleomedes_channel", "focus": "Trading analysis"},
        {"name": "Leandro Zicarelli", "handle": "leandro_zicarelli", "focus": "Spanish market analysis"},
    ],
    "tier3": [
        {"name": "PolyBot", "handle": "TradePolyBot", "focus": "Polymarket signals"},
        {"name": "Hyperdash Flows", "handle": "hyperdashflows", "focus": "Liquidations, large positions"},
        {"name": "ProLiquid Whales", "handle": "proliquid_whales", "focus": "Whale positions on HL"},
        {"name": "MLM OnChain", "handle": "mlmonchain", "focus": "On-chain analytics"},
        {"name": "Havoc Calls", "handle": "havoc_calls", "focus": "Trading calls"},
        {"name": "Lady Market", "handle": "lady_market", "focus": "Market signals"},
        {"name": "Chung Daily Note", "handle": "chungdailynote", "focus": "Daily notes"},
        {"name": "C4", "handle": "c4dotgg", "focus": "Community signals"},
        {"name": "MNC Crypto", "handle": "MNCcrypto", "focus": "Crypto drops"},
        {"name": "ZachXBT Investigations", "handle": "investigations", "focus": "Fraud/exploit alerts"},
        {"name": "HL Whale Alerts", "handle": "HyperliquidWhaleAlert", "focus": "Whale alerts"},
        {"name": "Oracle Signals", "handle": "oracle_signals", "focus": "Trading signals"},
    ],
}

# Tier 3 filter: only keep messages matching any of these patterns
HIGH_IMPACT_PATTERNS = [
    r"liquidat", r"\$\d{2,}M", r"\$\d{3,}K", r"whale", r"breaking", r"unlock",
    r"ceasefire", r"hormuz", r"iran", r"israel", r"fed", r"cpi", r"rate cut",
    r"listing", r"delisting", r"exploit", r"hack", r"pump", r"dump",
    r"long", r"short", r"funding",
]
HIGH_IMPACT_RE = re.compile("|".join(HIGH_IMPACT_PATTERNS), re.IGNORECASE)


@dataclass
class ChannelReadout:
    name: str
    handle: str
    focus: str
    tier: str
    messages: list[dict[str, Any]]
    error: str | None = None


async def _read_channel(client, channel: dict, hours: int, limit: int, filter_impact: bool) -> ChannelReadout:
    handle = channel["handle"]
    readout = ChannelReadout(
        name=channel["name"], handle=handle, focus=channel.get("focus", ""),
        tier="", messages=[],
    )
    try:
        entity = await client.get_entity(handle)
    except Exception as e:  # noqa: BLE001
        readout.error = f"resolve fail: {e}"
        return readout
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    try:
        msgs = await client.get_messages(entity, limit=limit)
    except Exception as e:  # noqa: BLE001
        readout.error = f"fetch fail: {e}"
        return readout

    for m in msgs:
        if not m or not getattr(m, "text", None):
            continue
        if m.date and m.date < cutoff:
            continue
        text = m.text.strip()
        if filter_impact and not HIGH_IMPACT_RE.search(text):
            continue
        readout.messages.append({
            "text": text[:2000],
            "date": m.date.isoformat() if m.date else None,
            "views": getattr(m, "views", None),
        })
    return readout


async def fetch_telegram_intel(hours: int = 24) -> dict[str, Any]:
    """Fetch messages from all tiers. Returns {tier1: [...], tier2: [...], tier3: [...]}."""
    out = {"tier1": [], "tier2": [], "tier3": [], "error": None}

    if not (TELEGRAM_API_ID and TELEGRAM_API_HASH and TELETHON_SESSION):
        out["error"] = "Telethon no configurado (falta TELEGRAM_API_ID/HASH/TELETHON_SESSION)"
        return out

    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError as e:
        out["error"] = f"telethon import: {e}"
        return out

    try:
        client = TelegramClient(
            StringSession(TELETHON_SESSION), TELEGRAM_API_ID, TELEGRAM_API_HASH
        )
        await client.connect()
        if not await client.is_user_authorized():
            out["error"] = "Telethon session no autorizada. Regenerar StringSession."
            await client.disconnect()
            return out
    except Exception as e:  # noqa: BLE001
        out["error"] = f"telethon connect: {e}"
        return out

    try:
        tier_config = {
            "tier1": (hours, 200, False),
            "tier2": (hours, 50, False),
            "tier3": (hours, 20, True),
        }
        for tier_name, chans in CHANNELS.items():
            h, limit, filter_impact = tier_config[tier_name]
            for c in chans:
                r = await _read_channel(client, c, h, limit, filter_impact)
                r.tier = tier_name
                out[tier_name].append({
                    "name": r.name,
                    "handle": r.handle,
                    "focus": r.focus,
                    "messages": r.messages,
                    "error": r.error,
                })
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return out


def summarize_for_prompt(intel: dict[str, Any], max_chars_per_tier: int = 12000) -> str:
    """Flatten intel into a prompt-friendly string, truncated."""
    blocks: list[str] = []
    for tier in ("tier1", "tier2", "tier3"):
        tier_blocks = [f"=== {tier.upper()} ==="]
        for ch in intel.get(tier) or []:
            if ch.get("error"):
                tier_blocks.append(f"[{ch['name']}] ERROR: {ch['error']}")
                continue
            msgs = ch.get("messages") or []
            if not msgs:
                continue
            tier_blocks.append(f"\n--- {ch['name']} (@{ch['handle']}) — {ch.get('focus')} ---")
            for m in msgs:
                tier_blocks.append(f"[{m.get('date')}] {m['text']}")
        joined = "\n".join(tier_blocks)
        if len(joined) > max_chars_per_tier:
            joined = joined[:max_chars_per_tier] + "\n…[truncated]"
        blocks.append(joined)
    return "\n\n".join(blocks)
