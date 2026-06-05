"""R-AUDIT2-P1.3 — INTEGRITY-HALT detector (born from the ZEC liquidation).

Scans the intel feeds (Telegram + X) for integrity / credibility signals tied
to a HELD position or DCA-plan asset. When such a signal hits a HELD asset
whose position UPnL is NEGATIVE (the trade is going against us), it raises a
🛑 INTEGRITY-HALT flag for BCD:

    "STOP accumulation on [asset]: integrity rumor + adverse PnL. Do NOT
     DCA/add/average. Await news. Never catch a falling knife."

This is a MANUAL-REVIEW alert ONLY — never an auto-action of any kind. The bot
never closes, reduces, or trades; it surfaces the rumor + adverse PnL so BCD
decides.

Critical nuance (shielded / opaque assets — privacy coins, anything where an
exploit may be invisible on-chain): the flag must NOT be auto-cleared by
"no confirmed exploit yet." Absence of on-chain confirmation is NOT safety. A
raised flag clears ONLY on explicit BCD dismissal (``dismiss``) or a clear
positive resolution recorded by BCD — never automatically, and never just
because the signal didn't recur on a later run. To enforce that, raised flags
are PERSISTED (SQLite) and re-surfaced every run until dismissed.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# ── Config (env-extensible, safe defaults) ──────────────────────────────────
_DEFAULT_KEYWORDS = (
    "exploit", "bug", "double-spend", "double spend", "infinite mint",
    "unlimited mint", "counterfeit", "undetectable", "unrecoverable narrative",
    "insolvency", "insolvent", "hack", "hacked", "depeg", "delisting",
    "delist", "infinite supply", "backdoor", "secretly print", "secret print",
    "rug", "rugpull", "rug pull",
)

# Assets with opaque / shielded accounting where an exploit may be invisible
# on-chain — their flags NEVER auto-clear on "no confirmation".
_DEFAULT_SHIELDED = ("ZEC", "XMR", "DASH", "SCRT", "ROSE", "ARRR", "FIRO", "BEAM")


def _enabled() -> bool:
    return (os.getenv("INTEGRITY_HALT_ENABLED", "true") or "true").lower() == "true"


def _keywords() -> list[str]:
    raw = os.getenv("INTEGRITY_HALT_KEYWORDS", "") or ""
    extra = [k.strip().lower() for k in raw.split(",") if k.strip()]
    kws = list(_DEFAULT_KEYWORDS) + extra
    # Longest first so multi-word phrases match before their sub-words.
    return sorted(set(kws), key=len, reverse=True)


def shielded_assets() -> set[str]:
    raw = os.getenv("INTEGRITY_SHIELDED_ASSETS", "") or ""
    extra = {a.strip().upper() for a in raw.split(",") if a.strip()}
    return {a.upper() for a in _DEFAULT_SHIELDED} | extra


@dataclass(frozen=True)
class IntegrityHit:
    asset: str
    keyword: str
    excerpt: str
    source: str
    shielded: bool


# ── Feed text harvesting (robust to every intel shape we pass) ───────────────
_TEXT_KEYS = ("text", "message", "snippet", "title", "body", "summary", "content")


def _harvest_texts(node: Any, source: str = "intel", out: list | None = None) -> list[tuple[str, str]]:
    """Recursively collect (source, text) from any nested intel structure.

    Handles telegram (data: list/dict of channels → messages[{text}]),
    x_intel (tweets:[{text}] / data by_user), gmail, etc. NEVER raises.
    """
    if out is None:
        out = []
    try:
        if isinstance(node, str):
            if node.strip():
                out.append((source, node))
        elif isinstance(node, dict):
            # capture text-ish leaf values at this level
            label = node.get("channel") or node.get("handle") or node.get("username") or source
            for k, v in node.items():
                if k in _TEXT_KEYS and isinstance(v, str) and v.strip():
                    out.append((str(label), v))
                else:
                    _harvest_texts(v, str(label) if isinstance(label, str) else source, out)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _harvest_texts(item, source, out)
    except Exception:  # noqa: BLE001
        pass
    return out


def _held_negative(positions: list[dict[str, Any]] | None) -> dict[str, float]:
    """Map ``{COIN: upnl}`` for HELD positions whose UPnL is NEGATIVE."""
    held: dict[str, float] = {}
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        coin = str(p.get("coin") or "").upper().strip()
        if not coin or coin == "?":
            continue
        try:
            upnl = float(p.get("unrealized_pnl") if p.get("unrealized_pnl") is not None
                         else p.get("unrealizedPnl") or 0.0)
        except (TypeError, ValueError):
            upnl = 0.0
        # Keep the most-negative read if a coin appears across wallets.
        if upnl < 0 and (coin not in held or upnl < held[coin]):
            held[coin] = upnl
    return held


def _asset_in_text(asset: str, text_lower: str) -> bool:
    """Whole-word ticker match (case-insensitive), avoids substring noise."""
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(asset.lower())}(?![A-Za-z0-9])", text_lower) is not None


def scan_integrity_signals(
    positions: list[dict[str, Any]] | None,
    intel: Any,
    *,
    plan_assets: set[str] | None = None,
) -> list[IntegrityHit]:
    """Pure scan: held assets with NEGATIVE UPnL hit by an integrity keyword.

    A DCA-plan asset that is also held-and-negative is included. NEVER raises.
    """
    if not _enabled():
        return []
    held_neg = _held_negative(positions)
    if not held_neg:
        return []
    # The flag requires HELD + negative UPnL (per spec). plan_assets only
    # widens name-matching when those assets are themselves held-and-negative.
    eligible = set(held_neg)
    if plan_assets:
        eligible |= {a.upper() for a in plan_assets if a.upper() in held_neg}

    kws = _keywords()
    shielded = shielded_assets()
    texts = _harvest_texts(intel)
    hits: dict[str, IntegrityHit] = {}
    for source, text in texts:
        tl = text.lower()
        kw_found = next((k for k in kws if k in tl), None)
        if not kw_found:
            continue
        for asset in eligible:
            if asset in hits:
                continue
            if _asset_in_text(asset, tl):
                excerpt = text.strip().replace("\n", " ")
                if len(excerpt) > 180:
                    excerpt = excerpt[:177] + "…"
                hits[asset] = IntegrityHit(
                    asset=asset, keyword=kw_found, excerpt=excerpt,
                    source=str(source), shielded=(asset in shielded),
                )
    return list(hits.values())


# ── Persistence (raised flags never auto-clear) ──────────────────────────────
def _conn():
    from modules.intel_memory import _get_conn
    conn = _get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS integrity_halt (
            asset TEXT PRIMARY KEY,
            keyword TEXT,
            excerpt TEXT,
            source TEXT,
            shielded INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            raised_ts TEXT,
            dismissed_ts TEXT
        )
        """
    )
    conn.commit()
    return conn


def raise_flags(hits: list[IntegrityHit]) -> list[str]:
    """Persist hits as ACTIVE flags. Returns assets newly raised this run."""
    newly: list[str] = []
    if not hits:
        return newly
    try:
        conn = _conn()
        now = datetime.now(timezone.utc).isoformat()
        for h in hits:
            row = conn.execute(
                "SELECT active FROM integrity_halt WHERE asset=?", (h.asset,)
            ).fetchone()
            already_active = bool(row and row["active"])
            conn.execute(
                "INSERT INTO integrity_halt "
                "(asset, keyword, excerpt, source, shielded, active, raised_ts) "
                "VALUES (?,?,?,?,?,1,?) "
                "ON CONFLICT(asset) DO UPDATE SET "
                "  keyword=excluded.keyword, excerpt=excluded.excerpt, "
                "  source=excluded.source, shielded=excluded.shielded, active=1, "
                "  raised_ts=COALESCE(integrity_halt.raised_ts, excluded.raised_ts)",
                (h.asset, h.keyword, h.excerpt, h.source, int(h.shielded), now),
            )
            if not already_active:
                newly.append(h.asset)
        conn.commit()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("raise_flags failed: %s", exc)
    return newly


def get_active_flags() -> list[dict[str, Any]]:
    try:
        conn = _conn()
        rows = conn.execute(
            "SELECT asset, keyword, excerpt, source, shielded, raised_ts "
            "FROM integrity_halt WHERE active=1 ORDER BY raised_ts ASC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.warning("get_active_flags failed: %s", exc)
        return []


def dismiss(asset: str, *, resolution: str = "BCD dismissal") -> bool:
    """Explicit BCD dismissal / positive resolution — the ONLY way to clear."""
    try:
        conn = _conn()
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "UPDATE integrity_halt SET active=0, dismissed_ts=? WHERE asset=? AND active=1",
            (f"{now} ({resolution})", asset.upper()),
        )
        conn.commit()
        cleared = cur.rowcount > 0
        conn.close()
        return cleared
    except Exception as exc:  # noqa: BLE001
        log.warning("dismiss failed: %s", exc)
        return False


# ── Rendering ────────────────────────────────────────────────────────────────
def stop_line(asset: str) -> str:
    """The exact STOP wording (single source of truth)."""
    return (
        f"STOP accumulation on {asset}: integrity rumor + adverse PnL. "
        f"Do NOT DCA/add/average. Await news. Never catch a falling knife."
    )


def build_integrity_block(active_flags: list[dict[str, Any]] | None) -> str:
    """Render the 🛑 INTEGRITY-HALT block. Empty string when no active flags."""
    active_flags = active_flags or []
    if not active_flags:
        return ""
    lines = [
        "🛑 INTEGRITY-HALT — MANUAL REVIEW (NUNCA auto-acción; decide BCD)",
    ]
    for f in active_flags:
        asset = str(f.get("asset") or "?").upper()
        lines.append(f"🛑 {stop_line(asset)}")
        kw = f.get("keyword")
        src = f.get("source")
        if kw:
            lines.append(f"   señal: «{kw}» — fuente: {src}")
        exc = f.get("excerpt")
        if exc:
            lines.append(f"   “{exc}”")
        if f.get("shielded"):
            lines.append(
                "   ⚠ activo opaco/shielded: la AUSENCIA de exploit confirmado "
                "NO es seguridad — el flag NO se auto-limpia (solo lo cierra BCD)."
            )
    return "\n".join(lines)


def run_integrity_halt(
    positions: list[dict[str, Any]] | None,
    intel: Any,
    *,
    plan_assets: set[str] | None = None,
) -> tuple[str, list[str]]:
    """Scan → persist → render. Returns (block_text, newly_raised_assets).

    NEVER raises — integrity scanning must never break /reporte.
    """
    try:
        hits = scan_integrity_signals(positions, intel, plan_assets=plan_assets)
        newly = raise_flags(hits)
        block = build_integrity_block(get_active_flags())
        return block, newly
    except Exception as exc:  # noqa: BLE001
        log.warning("run_integrity_halt failed: %s", exc)
        return "", []
