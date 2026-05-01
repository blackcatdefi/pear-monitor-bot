"""R-FINAL — Bug #1 fix: fund_state autodetect from on-chain reality.

Symptom (apr-30 2026):
    /reporte fired a "⚠️ ANOMALÍA CRÍTICA" block claiming the SHORT positions
    on 0xc7AE "should have been closed since 2026-04-20" and asked BCD for
    urgent manual verification. False positive. The basket v6 (DYDX/OP/ARB/
    PYTH/ENA SHORT) is alive and correct — it was deployed 29 abr 2026
    21:45 UTC after TWAP completion.

Root cause:
    fund_state.BASKET_STATUS["active"] = False is hardcoded and points to
    "v4 closed 2026-04-20". Neither v5 nor v6 ever updated this constant.
    The system_prompt block injects this state as ground truth into every
    LLM call → model sees real positions but state declared inactive →
    classifies as anomaly.

Fix:
    Bypass the hardcoded BASKET_STATUS by querying on-chain reality. If
    any KNOWN fund wallet has open SHORTs on basket-perp tokens, the state
    is ACTIVE — full stop. Only emit ANOMALY if positions appear on a
    wallet NOT registered in the fund (rare, would indicate a real
    operational mismatch worth flagging).

Public API:
    detect_active_baskets(fetch_wallets_fn=None) -> dict
        {
          "ts_utc": iso,
          "wallets": {
            "0xabc...": {
                "status": "ACTIVE" | "IDLE" | "UNKNOWN",
                "label": "...",
                "positions": [{coin, side, szi, ntl, entryPx, upnl}, ...],
                "shorts": [...],   # legacy view, SHORT-side filtered
                "basket_id_inferido": "v6" | None,
                "is_registered": True | False,
            }, ...
          },
          "summary": {
            "any_active": bool,
            "total_basket_notional_usd": float,
            "anomalies": [{wallet, reason}, ...],   # only unregistered/mismatch
          }
        }

R-DASH-FIX (1 may 2026):
    The detector is now BASKET-AGNOSTIC. No token whitelist, no basket-id
    pre-filter, no SHORT-only assumption. Side is detected dynamically.
    The ONLY filter is a dust gate (notional < $50). Rationale: previous
    detector used fund_state.BASKET_PERP_TOKENS as whitelist — that
    constant froze on the v4/v5 token universe and silently dropped
    DYDX/OP/ARB/PYTH from v6 (deployed 29 abr 2026), so the dashboard
    showed only 1 of 5 active positions on 1 may 2026.

    build_authoritative_state_block(...) -> str
        Replacement for templates.system_prompt.build_fund_state_block(),
        with on-chain truth shadowing the hardcoded constants.

Kill switch: FUND_STATE_AUTODETECT=false (default true)

Anomaly threshold (only flag big mismatches, not dust):
    ANOMALY_NOTIONAL_USD = float(env "FUND_STATE_ANOMALY_USD" default 500)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

ENABLED = os.getenv("FUND_STATE_AUTODETECT", "true").strip().lower() != "false"
ANOMALY_NOTIONAL_USD = float(os.getenv("FUND_STATE_ANOMALY_USD", "500") or 500)
# R-DASH-FIX: dust gate — the ONLY filter applied to raw positions.
# A real basket leg in v6 is ~$4.5K notional. $50 keeps headroom for
# small hedges while excluding floating-point noise / closed-but-not-yet-
# pruned ghost positions.
DUST_NOTIONAL_USD = float(os.getenv("FUND_STATE_DUST_USD", "50") or 50)


def _basket_perp_tokens() -> set[str]:
    """Best-effort import of BASKET_PERP_TOKENS from fund_state.

    Falls back to a hardcoded set if import fails (defensive — fund_state
    can also be missing in tests).
    """
    try:
        from fund_state import BASKET_PERP_TOKENS  # type: ignore

        return {str(t).upper() for t in BASKET_PERP_TOKENS}
    except Exception:  # noqa: BLE001
        return {
            "WLD",
            "STRK",
            "ZRO",
            "AVAX",
            "ENA",
            "EIGEN",
            "SCR",
            "ZETA",
            # v6 basket tokens (29 abr 2026)
            "DYDX",
            "OP",
            "ARB",
            "PYTH",
        }


def _registered_wallets() -> dict[str, str]:
    """Lower-cased map of {address: label} for all known fund wallets.

    Fallback to empty dict if config cannot be imported (tests).
    """
    try:
        from config import FUND_WALLETS, HYPERLEND_WALLET  # type: ignore

        out: dict[str, str] = {}
        for addr, label in (FUND_WALLETS or {}).items():
            if not addr:
                continue
            out[addr.lower()] = label
        if HYPERLEND_WALLET:
            hw = HYPERLEND_WALLET.lower()
            out.setdefault(hw, "HyperLend Principal")
        return out
    except Exception:  # noqa: BLE001
        return {}


def _infer_basket_id(coins: set[str]) -> str | None:
    """Pattern-match the active SHORT basket against known historical baskets.

    Used purely for human-readable labelling — the bot's authority is
    on-chain, not the inferred id.
    """
    coins_u = {c.upper() for c in coins}
    if {"DYDX", "OP", "ARB", "PYTH", "ENA"}.issubset(coins_u):
        return "v6"
    if {"WLD", "STRK", "ZRO", "AVAX", "ENA"}.issubset(coins_u):
        return "v4/v5"
    if coins_u and coins_u.issubset(_basket_perp_tokens()):
        return "v?"
    return None


async def _fetch_all_wallets_default() -> list[dict[str, Any]]:
    """Default wallet fetcher — uses modules.portfolio.fetch_all_wallets."""
    from modules.portfolio import fetch_all_wallets  # type: ignore

    return await fetch_all_wallets()


async def detect_active_baskets(
    fetch_wallets_fn: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
) -> dict[str, Any]:
    """Query on-chain reality and return the authoritative basket state.

    Parameters
    ----------
    fetch_wallets_fn :
        Async callable returning the same shape as
        ``modules.portfolio.fetch_all_wallets`` — i.e. ``list[{status, data}]``
        with ``data.positions = [{coin, szi, ntl_pos|position_value, entryPx?}]``.
        Defaults to the production fetcher; injected in tests.
    """
    if not ENABLED:
        return {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "wallets": {},
            "summary": {
                "any_active": False,
                "total_basket_notional_usd": 0.0,
                "anomalies": [],
                "disabled": True,
            },
        }

    fetch = fetch_wallets_fn or _fetch_all_wallets_default
    try:
        wallets = await fetch()
    except Exception:  # noqa: BLE001
        log.exception("fund_state_v2: fetch_all_wallets failed")
        return {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "wallets": {},
            "summary": {
                "any_active": False,
                "total_basket_notional_usd": 0.0,
                "anomalies": [],
                "fetch_error": True,
            },
        }

    registered = _registered_wallets()

    log.info(
        "fund_state_v2: detect_active_baskets START — wallets_in=%d registered=%d dust_threshold=$%.0f",
        len(wallets or []),
        len(registered),
        DUST_NOTIONAL_USD,
    )

    wallets_out: dict[str, dict[str, Any]] = {}
    total_notional = 0.0
    anomalies: list[dict[str, Any]] = []

    for w in wallets or []:
        if not isinstance(w, dict):
            continue
        if w.get("status") != "ok":
            continue
        d = w.get("data") or {}
        addr = (d.get("wallet") or "").lower()
        if not addr:
            continue
        label = d.get("label") or registered.get(addr) or addr[:10]
        is_registered = addr in registered

        raw_positions = d.get("positions") or []
        log.info(
            "fund_state_v2: wallet=%s label=%r registered=%s raw_positions=%d",
            addr,
            label,
            is_registered,
            len(raw_positions),
        )

        # R-DASH-FIX: basket-agnostic detection.
        # NO whitelist by token, NO basket-id pre-filter, NO SHORT-only
        # assumption. Side is detected dynamically from szi sign. The only
        # filter is a dust gate (notional < $50).
        #
        # Why: previous fund_state_v2 used fund_state.BASKET_PERP_TOKENS as
        # a whitelist (line 256, deleted). That constant lists only the
        # v4/v5 universe (WLD/STRK/ZRO/AVAX/ENA/EIGEN/SCR/ZETA) — DYDX/OP/
        # ARB/PYTH from v6 (deployed 29 abr 2026 21:45 UTC) were never
        # added, so 4 of the 5 v6 SHORTs got silently dropped, and the
        # dashboard reported only ENA. Symptom: 1 may 2026 14:17 UTC,
        # Pear shows 5 positions but bot shows 1.
        positions: list[dict[str, Any]] = []
        wallet_basket_notional = 0.0
        for pos in raw_positions:
            coin = (pos.get("coin") or "").upper()
            if not coin:
                continue
            # Accept ALL aliases for size: szi (raw HL), size (portfolio.py)
            try:
                size_val = pos.get("szi")
                if size_val is None:
                    size_val = pos.get("size")
                szi = float(size_val or 0.0)
            except Exception:  # noqa: BLE001
                szi = 0.0
            if szi == 0.0:
                continue
            # Accept ALL aliases for notional
            try:
                ntl_val = (
                    pos.get("position_value")
                    or pos.get("notional_usd")
                    or pos.get("ntl_pos")
                    or pos.get("positionValue")
                    or pos.get("notional")
                )
                ntl = abs(float(ntl_val or 0.0))
            except Exception:  # noqa: BLE001
                ntl = 0.0
            # DUST FILTER — the ONLY filter
            if ntl < DUST_NOTIONAL_USD:
                log.debug(
                    "fund_state_v2: skip dust pos coin=%s szi=%s ntl=%.2f wallet=%s threshold=$%.0f",
                    coin,
                    szi,
                    ntl,
                    addr,
                    DUST_NOTIONAL_USD,
                )
                continue
            # Accept ALL aliases for entry price
            try:
                entry_px = float(
                    pos.get("entryPx") or pos.get("entry_px") or 0.0
                )
            except Exception:  # noqa: BLE001
                entry_px = 0.0
            # Accept ALL aliases for unrealized pnl
            try:
                upnl = float(
                    pos.get("unrealizedPnl")
                    or pos.get("unrealized_pnl")
                    or pos.get("upnl")
                    or 0.0
                )
            except Exception:  # noqa: BLE001
                upnl = 0.0
            # Side: prefer explicit field if present, else derive from szi
            side = (pos.get("side") or "").upper().strip()
            if side not in ("LONG", "SHORT"):
                side = "SHORT" if szi < 0 else "LONG"
            positions.append(
                {
                    "coin": coin,
                    "side": side,
                    "szi": szi,
                    "ntl": ntl,
                    "entryPx": entry_px,
                    "upnl": upnl,
                }
            )
            wallet_basket_notional += ntl

        # Backward-compat: legacy consumers (dashboard rendering) read the
        # ``shorts`` key. Keep it populated as a derived view of SHORT-side
        # positions so old code paths don't break.
        shorts = [p for p in positions if p["side"] == "SHORT"]

        log.info(
            "fund_state_v2: wallet=%s positions_kept=%d shorts=%d basket_notional=%.2f",
            addr,
            len(positions),
            len(shorts),
            wallet_basket_notional,
        )

        if positions:
            coins_short = {p["coin"] for p in shorts}
            basket_id = _infer_basket_id(coins_short) if coins_short else None
            status = "ACTIVE"
            total_notional += wallet_basket_notional
            if not is_registered and wallet_basket_notional >= ANOMALY_NOTIONAL_USD:
                anomalies.append(
                    {
                        "wallet": addr,
                        "reason": "UNREGISTERED_WALLET_HOLDS_BASKET",
                        "notional_usd": wallet_basket_notional,
                    }
                )
        else:
            basket_id = None
            status = "IDLE"

        wallets_out[addr] = {
            "status": status,
            "label": label,
            "positions": positions,  # NEW: all sides, basket-agnostic
            "shorts": shorts,         # legacy compat (SHORT-side view)
            "basket_id_inferido": basket_id,
            "is_registered": is_registered,
            "basket_notional_usd": wallet_basket_notional,
        }

    summary = {
        "any_active": any(v["status"] == "ACTIVE" for v in wallets_out.values()),
        "total_basket_notional_usd": total_notional,
        "anomalies": anomalies,
    }
    log.info(
        "fund_state_v2: detect_active_baskets DONE — any_active=%s total_ntl=%.2f anomalies=%d wallets_out=%d",
        summary["any_active"],
        summary["total_basket_notional_usd"],
        len(anomalies),
        len(wallets_out),
    )
    return {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "wallets": wallets_out,
        "summary": summary,
    }


def render_state_block(detected: dict[str, Any]) -> str:
    """Render an authoritative on-chain state block for the LLM prompt.

    This block shadows the hardcoded fund_state.BASKET_STATUS — the LLM is
    instructed to treat it as ground truth.
    """
    wallets = detected.get("wallets") or {}
    summary = detected.get("summary") or {}
    any_active = bool(summary.get("any_active"))
    total = float(summary.get("total_basket_notional_usd") or 0.0)
    anomalies = summary.get("anomalies") or []

    lines: list[str] = []
    lines.append(
        "═══════ BASKET STATE — ON-CHAIN AUTORITATIVO (R-FINAL autodetect) ═══════"
    )
    lines.append(
        "Esta sección es la VERDAD AUTORITATIVA. Si una constante hardcodeada "
        "más abajo dice algo distinto, ignorala — la realidad on-chain "
        "PREVALECE."
    )
    lines.append("")
    if any_active:
        active_baskets = sorted(
            {
                w.get("basket_id_inferido") or "?"
                for w in wallets.values()
                if w["status"] == "ACTIVE"
            }
        )
        lines.append(
            f"Basket activa: SÍ ({', '.join(active_baskets)}) — "
            f"notional total ${total:,.0f}"
        )
        for addr, w in wallets.items():
            if w["status"] != "ACTIVE":
                continue
            coins = ",".join(s["coin"] for s in w["shorts"])
            lines.append(
                f"  • {w['label']} ({addr[:10]}…): SHORT {coins} "
                f"— ntl ${w['basket_notional_usd']:,.0f}"
            )
    else:
        lines.append("Basket activa: NO (todas las wallets IDLE on-chain)")

    if anomalies:
        lines.append("")
        lines.append("⚠️ ANOMALÍAS (wallets NO registradas con basket activa):")
        for a in anomalies:
            lines.append(
                f"  • {a['wallet']} — {a['reason']} (ntl ${a['notional_usd']:,.0f})"
            )
    else:
        lines.append("")
        lines.append(
            "Sin anomalías (todas las basket positions están en wallets "
            "registradas del fondo)."
        )
    lines.append("═══════ FIN ON-CHAIN ═══════")
    lines.append("")
    return "\n".join(lines)


async def build_authoritative_state_block() -> str:
    """Produce the prompt block to inject above legacy fund_state.

    Wraps detect + render. Safe to call from inside an async LLM caller.
    Returns empty string if disabled.
    """
    if not ENABLED:
        return ""
    try:
        detected = await detect_active_baskets()
    except Exception:  # noqa: BLE001
        log.exception("fund_state_v2: detect_active_baskets failed")
        return ""
    return render_state_block(detected)
