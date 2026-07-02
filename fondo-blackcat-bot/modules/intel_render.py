"""R-BOT-DEFINITIVE-2 T3 — deterministic Telegram + Gmail intel render for
/reporte ($0, zero LLM).

After R-COST3 removed the LLM narrative, the Telegram/Gmail intel was still
FETCHED every /reporte (and consumed by the integrity-halt scan) but never
SHOWN. These pure formatters restore visibility deterministically:

* ``format_telegram_intel_block(legacy, unread)`` — tiered channels (tier1/2/3
  from config.CHANNELS) each with up to 3 most-recent messages truncated at
  200 chars; unread-scan channels NOT in any tier go under OTROS; totals line.
* ``format_gmail_intel_block(gmail)`` — one entry per email (sender / subject /
  first 150 chars of snippet / date) + totals (processed = archived, the scan
  archives everything it reads); a single line when there are zero unread.

Both NEVER raise and render an explicit note on error dicts (e.g.
``telethon_disabled``) instead of silently vanishing. Read/mark-read/archive
behaviour of the underlying scanners is UNTOUCHED — these are render-only.
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_TIER_LABELS = {"tier1": "TIER 1", "tier2": "TIER 2", "tier3": "TIER 3"}
_MSG_TRUNC = 200
_GMAIL_TRUNC = 150
_MAX_MSGS_PER_CHANNEL = 3
_MAX_OTHER_CHANNELS = 12

_URL_RE = re.compile(r"https?://\S+")
_MEDIA_RE = re.compile(
    r"\[(?:photo|video|media|sticker|gif|voice|document|audio|poll)\]",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")


def _clean(text: str, limit: int = _MSG_TRUNC) -> str:
    """Strip links + media placeholders, collapse whitespace, truncate."""
    try:
        t = _URL_RE.sub("", str(text or ""))
        t = _MEDIA_RE.sub("", t)
        t = _WS_RE.sub(" ", t).strip()
        if len(t) > limit:
            t = t[: limit - 1].rstrip() + "…"
        return t
    except Exception:  # noqa: BLE001
        return ""


def _short_date(iso: Any) -> str:
    """'2026-07-02T14:03:00+00:00' → '07-02 14:03'. NEVER raises."""
    try:
        s = str(iso or "")
        if len(s) >= 16 and s[4] == "-":
            return f"{s[5:10]} {s[11:16]}"
        return s[:16]
    except Exception:  # noqa: BLE001
        return ""


def _tier_handles() -> set[str]:
    """Lowercased handles of every configured tier channel. NEVER raises."""
    out: set[str] = set()
    try:
        from config import CHANNELS
        for channels in (CHANNELS or {}).values():
            for c in channels or []:
                h = str((c or {}).get("handle") or "").strip().lstrip("@").lower()
                if h:
                    out.add(h)
    except Exception:  # noqa: BLE001
        pass
    return out


def _render_channel(ch: dict[str, Any]) -> list[str]:
    """Lines for one channel: name + up to 3 most-recent cleaned messages."""
    lines: list[str] = []
    name = str(ch.get("channel") or ch.get("name") or "?")
    msgs = [m for m in (ch.get("messages") or []) if _clean(m.get("text", ""))]
    if not msgs:
        return lines
    # Most recent first (scan/legacy order is already newest-first from
    # Telethon get_messages/iter_messages; sort defensively by date desc).
    try:
        msgs = sorted(msgs, key=lambda m: str(m.get("date") or ""), reverse=True)
    except Exception:  # noqa: BLE001
        pass
    lines.append(f"  📣 {name} ({len(msgs)} msgs)")
    for m in msgs[:_MAX_MSGS_PER_CHANNEL]:
        d = _short_date(m.get("date"))
        prefix = f"    · [{d}] " if d else "    · "
        lines.append(prefix + _clean(m.get("text", "")))
    return lines


def format_telegram_intel_block(
    legacy: dict[str, Any] | None, unread: dict[str, Any] | None,
) -> str:
    """Deterministic TELEGRAM INTEL 24H block. NEVER raises; '' hides nothing
    silently — errors and empty feeds render an explicit one-liner."""
    try:
        header = "📨 TELEGRAM INTEL — 24H\n" + ("─" * 30)
        legacy = legacy if isinstance(legacy, dict) else {}
        unread = unread if isinstance(unread, dict) else {}

        legacy_err = legacy.get("status") != "ok"
        unread_err = unread.get("status") != "ok"
        if legacy_err and unread_err:
            err = legacy.get("error") or unread.get("error") or "sin datos"
            return f"{header}\nn/d — {err} (feeds Telegram no disponibles)"

        lines: list[str] = [header]
        total_msgs = 0
        total_channels = 0

        # ── Tiered channels (legacy path) ──
        if not legacy_err:
            data = legacy.get("data") or {}
            for tier in ("tier1", "tier2", "tier3"):
                tier_lines: list[str] = []
                for ch in data.get(tier) or []:
                    cl = _render_channel(ch)
                    if cl:
                        tier_lines.extend(cl)
                        total_channels += 1
                        total_msgs += len(ch.get("messages") or [])
                if tier_lines:
                    lines.append(f"\n▪️ {_TIER_LABELS.get(tier, tier.upper())}")
                    lines.extend(tier_lines)

        # ── OTROS: unread-scan channels not covered by a tier ──
        if not unread_err:
            tiered = _tier_handles()
            other_lines: list[str] = []
            shown = 0
            for ch in unread.get("data") or []:
                handle = str(ch.get("handle") or "").strip().lstrip("@").lower()
                if handle and handle in tiered:
                    continue  # already rendered via its tier
                cl = _render_channel(ch)
                if cl:
                    if shown < _MAX_OTHER_CHANNELS:
                        other_lines.extend(cl)
                        shown += 1
                    total_channels += 1
                    total_msgs += len(ch.get("messages") or [])
            if other_lines:
                lines.append("\n▪️ OTROS (unread scan)")
                lines.extend(other_lines)

        if total_channels == 0:
            lines.append("Sin mensajes nuevos en las últimas 24h.")
        else:
            lines.append(
                f"\nTotales: {total_msgs} mensajes · {total_channels} canales"
            )
        if legacy_err:
            lines.append(f"⚠️ tiers n/d ({legacy.get('error') or 'error'})")
        if unread_err:
            lines.append(f"⚠️ unread scan n/d ({unread.get('error') or 'error'})")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        log.exception("format_telegram_intel_block failed")
        return ""


def format_gmail_intel_block(gmail: dict[str, Any] | None) -> str:
    """Deterministic GMAIL INTEL block. NEVER raises."""
    try:
        header = "📧 GMAIL INTEL\n" + ("─" * 30)
        gmail = gmail if isinstance(gmail, dict) else {}
        if gmail.get("status") != "ok":
            err = gmail.get("error") or "sin datos"
            return f"{header}\nn/d — {err}"
        emails = gmail.get("emails") or []
        if not emails:
            return f"{header}\nSin emails sin leer."
        lines: list[str] = [header]
        for e in emails:
            sender = _clean(e.get("from", ""), 60) or "?"
            subject = _clean(e.get("subject", ""), 90) or "(sin asunto)"
            snippet = _clean(e.get("snippet", ""), _GMAIL_TRUNC)
            date = _clean(str(e.get("date", "")), 32)
            lines.append(f"  ✉️ {sender} — {subject}" + (f" [{date}]" if date else ""))
            if snippet:
                lines.append(f"    {snippet}")
        n = len(emails)
        # The scanner archives everything it processes (mark-read + archive).
        lines.append(f"\nTotales: {n} procesados · {n} archivados")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        log.exception("format_gmail_intel_block failed")
        return ""
