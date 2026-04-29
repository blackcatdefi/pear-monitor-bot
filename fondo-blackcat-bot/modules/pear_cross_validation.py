"""Round 18 — Cross-validate bot-reported PnL against Pear Protocol API.

If the bot's UPnL for a basket position diverges from Pear's view by more
than CROSS_VALIDATION_THRESHOLD (default 10%), surface a warning that gets
appended to /reporte so BCD spots inconsistencies before acting on stale
numbers.

The Pear API endpoint is best-effort. If it's unreachable we degrade
gracefully — a single "Pear API no disponible — no se puede cross-validate"
note instead of failing the report.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

PEAR_API_BASE = os.getenv(
    "PEAR_API_BASE",
    "https://api.pear.garden/positions",
)
DEFAULT_THRESHOLD = float(os.getenv("PEAR_CROSS_VALIDATION_THRESHOLD", "0.10"))
TIMEOUT_S = float(os.getenv("PEAR_CROSS_VALIDATION_TIMEOUT", "8"))


def is_enabled() -> bool:
    return os.getenv("PEAR_CROSS_VALIDATION_ENABLED", "true").strip().lower() != "false"


async def _fetch_pear_positions(wallet: str) -> list[dict[str, Any]] | None:
    """Best-effort fetch. Returns None on any failure (timeout, http error)."""
    if not wallet:
        return None
    url = f"{PEAR_API_BASE}?address={wallet}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True) as client:
            r = await client.get(url)
            if r.status_code != 200:
                log.info("pear_cross_validation: pear api %s for %s", r.status_code, wallet)
                return None
            payload = r.json()
    except Exception as exc:
        log.info("pear_cross_validation: fetch failed: %s", exc)
        return None

    # Try several common shapes
    if isinstance(payload, list):
        positions = payload
    elif isinstance(payload, dict):
        positions = (
            payload.get("positions")
            or payload.get("data")
            or payload.get("result")
            or []
        )
    else:
        positions = []
    return positions if isinstance(positions, list) else None


def _extract_asset(pos: dict[str, Any]) -> str | None:
    for k in ("asset", "coin", "symbol", "ticker"):
        v = pos.get(k)
        if v:
            return str(v).upper()
    return None


def _extract_pnl(pos: dict[str, Any]) -> float | None:
    for k in ("unrealized_pnl", "unrealizedPnl", "upnl", "uPnl", "pnl"):
        v = pos.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


async def cross_validate_pnl(
    wallet: str,
    bot_positions: list[dict[str, Any]],
    threshold: float | None = None,
) -> list[dict[str, Any]]:
    """Compare bot-side UPnL with Pear API. Return list of warnings."""
    if not is_enabled():
        return []
    if not bot_positions:
        return []
    threshold = threshold if threshold is not None else DEFAULT_THRESHOLD

    pear_positions = await _fetch_pear_positions(wallet)
    if pear_positions is None:
        return [
            {
                "wallet": wallet,
                "level": "info",
                "warning": "Pear API no disponible — cross-validate skipped",
            }
        ]

    pear_by_asset: dict[str, dict[str, Any]] = {}
    for p in pear_positions:
        sym = _extract_asset(p)
        if sym:
            pear_by_asset[sym] = p

    warnings: list[dict[str, Any]] = []
    for pos in bot_positions:
        sym = _extract_asset(pos)
        if not sym:
            continue
        bot_pnl = _extract_pnl(pos)
        if bot_pnl is None:
            continue
        pear = pear_by_asset.get(sym)
        if pear is None:
            warnings.append(
                {
                    "wallet": wallet,
                    "asset": sym,
                    "level": "info",
                    "warning": f"{sym}: posición no encontrada en Pear API",
                }
            )
            continue
        pear_pnl = _extract_pnl(pear)
        if pear_pnl is None:
            continue
        denom = abs(pear_pnl) if pear_pnl != 0 else max(abs(bot_pnl), 1.0)
        diff_pct = abs(bot_pnl - pear_pnl) / denom
        if diff_pct > threshold:
            warnings.append(
                {
                    "wallet": wallet,
                    "asset": sym,
                    "bot_pnl": bot_pnl,
                    "pear_pnl": pear_pnl,
                    "diff_pct": diff_pct,
                    "level": "warn",
                    "warning": (
                        f"\u26a0\ufe0f {sym}: bot ${bot_pnl:+.2f} vs Pear "
                        f"${pear_pnl:+.2f} ({diff_pct*100:.1f}% diff)"
                    ),
                }
            )
    return warnings


def format_warnings(warnings: list[dict[str, Any]]) -> str:
    if not warnings:
        return ""
    actionable = [w for w in warnings if w.get("level") == "warn"]
    if not actionable:
        return ""
    lines: list[str] = ["\u26a0\ufe0f PEAR CROSS-VALIDATION", "\u2500" * 30]
    for w in actionable:
        lines.append(f"  \u2022 {w.get('warning', '?')}")
    lines.append("")
    lines.append("Bot vs Pear API divergente — verificar antes de decidir.")
    return "\n".join(lines)


async def cross_validate_all(wallets: list[dict[str, Any]]) -> str:
    """Best-effort: take all wallets from fetch_all_wallets() and cross-check
    each one with positions in Pear. Returns a single formatted block ready
    to append to /reporte.
    """
    if not is_enabled():
        return ""
    out: list[dict[str, Any]] = []
    for w in wallets or []:
        if not isinstance(w, dict):
            continue
        if w.get("status") != "ok":
            continue
        d = w.get("data") or {}
        addr = (d.get("wallet") or w.get("wallet") or "").lower()
        positions = d.get("positions") or w.get("positions") or []
        if not addr or not positions:
            continue
        try:
            warnings = await cross_validate_pnl(addr, positions)
        except Exception:
            log.exception("cross_validate_pnl failed for %s", addr)
            continue
        out.extend(warnings)
    return format_warnings(out)
