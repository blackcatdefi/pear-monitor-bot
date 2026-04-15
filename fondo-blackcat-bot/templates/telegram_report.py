"""Formatter for the telegram intel block."""
from __future__ import annotations

from typing import Any


def format_intel_summary(intel: dict[str, Any]) -> str:
    if intel.get("error"):
        return f"Telegram intel: no disponible ({intel['error']})"
    lines = ["📡 TELEGRAM INTEL (24h)"]
    for tier in ("tier1", "tier2", "tier3"):
        tier_channels = intel.get(tier) or []
        total_msgs = sum(len(c.get("messages") or []) for c in tier_channels)
        errs = sum(1 for c in tier_channels if c.get("error"))
        lines.append(f"  {tier.upper()}: {total_msgs} mensajes · {errs} errors · {len(tier_channels)} canales")
    return "\n".join(lines)
