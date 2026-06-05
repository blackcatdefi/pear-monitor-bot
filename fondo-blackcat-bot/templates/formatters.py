"""Formatters for quick replies (no Claude needed)."""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Any

from auto.wallet_labels import apply_wallet_label
from fund_state import (
    ALT_SHORT_BLEED_WALLETS,
    BASKET_STATUS,
    classify_fill,
)


def _is_alt_short_wallet(wallet_addr: str) -> bool:
    addr = (wallet_addr or "").lower()
    return any(prefix.lower() in addr for prefix in ALT_SHORT_BLEED_WALLETS)


def _fmt_usd(v: float | None) -> str:
    if v is None:
        return "—"
    if math.isinf(v):
        return "∞"
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1_000_000:
        return f"{sign}${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"{sign}${v/1_000:.1f}K"
    return f"{sign}${v:.2f}"


def _fmt_hf(v: float | None) -> str:
    """Render a HealthFactor numeric value.

    R-HF-RENDER (3 may 2026): defensive — must NEVER emit literal "nan".
    The cache-aware reader (auto.hyperlend_reader) flags rate-limited
    entries with hf_status='UNKNOWN' AND sets data.health_factor=NaN as a
    sentinel. Callers MUST branch on hf_status before formatting; this
    helper still degrades to "—" if a NaN slips through (defense-in-depth).
    """
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if math.isnan(f):
        return "—"
    if math.isinf(f):
        return "∞ (no debt)"
    return f"{f:.3f}"


def _fmt_hf_loose(v: Any) -> str:
    """Render a HF value that may be the cache 'inf' string sentinel.

    Used for ``last_known_hf`` rendering (cache stores 'inf' as a string
    when the wallet was last seen with collateral and zero debt — see
    auto.hyperlend_reader._persist_ok).
    """
    if v is None:
        return "—"
    if isinstance(v, str):
        if v.strip().lower() == "inf":
            return "∞"
        try:
            f = float(v)
        except (TypeError, ValueError):
            return "—"
    else:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return "—"
    if math.isnan(f):
        return "—"
    if math.isinf(f):
        return "∞"
    return f"{f:.3f}"


def _hl_age_label(age_seconds: int | None) -> str:
    """Human age label for cached HyperLend reads (single-source-of-truth
    with auto.hyperlend_reader._age_label semantics)."""
    if age_seconds is None:
        return "?"
    try:
        s = int(age_seconds)
    except (TypeError, ValueError):
        return "?"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}min"
    return f"{s // 3600}h"


# R-DASHBOARD-SPOT-FIX (2026-05-05): canonical stablecoin set.
# Mirrors modules.portfolio_snapshot.STABLECOINS — kept local to avoid
# a templates → modules import cycle. Anything NOT here is real exposure.
_STABLECOINS = frozenset({
    "USDC", "USDT", "USDT0", "USDH", "USDE", "USDHL", "USR", "SUSDE", "DAI",
})


def _price_lookup(prices: dict[str, Any] | None, coin: str) -> float | None:
    """Resolve a USD price for ``coin`` from a flexible ``prices`` map.

    Accepts either the oracle shape ``{COIN: 71.7}`` or the market shape
    ``{COIN: {"price_usd": 71.7}}``. kHYPE → HYPE proxy. None if unknown.
    """
    if not prices:
        return None
    c = (coin or "").upper()
    lookup = c[1:] if c.startswith("K") and len(c) > 1 else c
    for key in (lookup, c):
        v = prices.get(key)
        if isinstance(v, dict):
            v = v.get("price_usd") or v.get("usd")
        try:
            if v is not None:
                f = float(v)
                if f > 0:
                    return f
        except (TypeError, ValueError):
            continue
    return None


def _build_price_map(market: dict[str, Any] | None = None) -> dict[str, float]:
    """Build a robust ``{COIN: price_usd}`` map for spot valuation.

    Primary source = HL oracle prices (keyless, always available). Secondary
    = the CoinGecko-backed ``market`` map (fills coins HL doesn't list). The
    oracle wins on conflict because it is what HL itself uses to value
    Portfolio Margin collateral. NEVER raises — returns {} in the worst case.
    """
    out: dict[str, float] = {}
    # Secondary first so oracle overwrites it.
    try:
        if isinstance(market, dict):
            mprices = (market.get("data") or {}).get("prices") or market.get("prices") or {}
            for k, v in (mprices or {}).items():
                px = None
                if isinstance(v, dict):
                    px = v.get("price_usd") or v.get("usd")
                else:
                    px = v
                try:
                    if px is not None and float(px) > 0:
                        out[str(k).upper()] = float(px)
                except (TypeError, ValueError):
                    continue
    except Exception:  # noqa: BLE001
        pass
    try:
        from modules.hl_prices import get_oracle_prices
        for k, v in (get_oracle_prices() or {}).items():
            try:
                if float(v) > 0:
                    out[str(k).upper()] = float(v)
            except (TypeError, ValueError):
                continue
    except Exception:  # noqa: BLE001
        pass
    return out


def _estimate_spot_split(spot_balances: list[dict[str, Any]],
                          perp_account_value: float = 0.0,
                          prices: dict[str, Any] | None = None) -> tuple[float, float]:
    """Return ``(non_stable_usd, stables_usd)`` for a wallet's spot bag.

    R-DASHBOARD-SPOT-FIX: replaces the legacy ``_estimate_spot_usd`` that
    lumped USDH/USDT0/USDT/DAI into the "non-USDC" sum. The split lets
    the formatter accurately label "Spot non-stable" exposure and surface
    "Spot stables" as cash equivalent.

    R-DASHBOARD-DOUBLECOUNT-FIX (2026-05-06): every stablecoin (not just
    USDC) is part of the HyperLiquid Unified Account margin pool. When
    ``perp_account_value > 0.01`` we skip the entire stable bucket to
    avoid the double-count that inflated ``Spot stables`` by ~$2.6K on
    top of perp accountValue. When idle, all stables fold into the cash
    bucket 1:1.

    R-PMCORE (2026-06-01) — CRITICAL POST-MIGRATION FIX:
    Non-stable tokens are now valued at ``amount × live_price`` first
    (``prices`` = HL oracle map, with CoinGecko as secondary). The old
    behaviour fell back to ``entry_ntl`` (cost basis) ONLY — but for the
    fund's migrated HYPE balance ``entryNtl`` is **0.0** from HyperCore, so
    ~$75K of HYPE collateral was valued at **$0** and TOTAL EQUITY read
    ~$13K instead of Rabby's ~$94K. ``entry_ntl`` is now a last-resort
    proxy used only when no live price is available for the coin.
    """
    has_active_perp = perp_account_value > 0.01
    non_stable = 0.0
    stables = 0.0
    for sb in spot_balances:
        coin = (sb.get("coin") or "").upper()
        try:
            amt = float(sb.get("total", 0) or 0)
        except (TypeError, ValueError):
            amt = 0.0
        # ALL stablecoins are part of Unified Account margin when perp
        # is active. Skip them entirely. When idle, fold into cash bucket.
        if coin in _STABLECOINS:
            if has_active_perp:
                continue
            stables += amt
            continue
        # Non-stable: value at LIVE price first (HYPE oracle), cost basis last.
        px = _price_lookup(prices, coin)
        if px and amt:
            non_stable += amt * px
            continue
        entry_ntl = sb.get("entry_ntl", 0) or 0
        try:
            entry_ntl = float(entry_ntl)
        except (TypeError, ValueError):
            entry_ntl = 0.0
        if entry_ntl > 0:
            non_stable += entry_ntl
    return non_stable, stables


def _estimate_spot_usd(spot_balances: list[dict[str, Any]],
                       perp_account_value: float = 0.0,
                       prices: dict[str, Any] | None = None) -> float:
    """Backward-compatible wrapper — returns NON-STABLE only.

    Pre-R-DASHBOARD-SPOT-FIX this returned non-stable + stablecoins
    lumped together, which inflated the "Spot non-USDC" line in
    /reporte and the dashboard. After the fix it returns ONLY the real
    market exposure half. R-PMCORE: forwards ``prices`` so non-stable
    (HYPE) is valued at live oracle price, not cost basis.
    Use ``_estimate_spot_split`` directly when you also need the stables.
    """
    non_stable, _stables = _estimate_spot_split(spot_balances, perp_account_value, prices)
    return non_stable


def _current_usd_value(coin: str, amount: float, entry_ntl: float,
                       prices: dict[str, Any] | None) -> float:
    """Best-effort current USD valuation of a spot balance.

    Order of preference:
      1. Stablecoins (USDC/USDH/USDT0/DAI/USDT) → amount 1:1.
      2. Current price from market.prices[COIN].price_usd when available.
      3. Entry notional (cost basis) as last-resort proxy.
    """
    c = (coin or "").upper()
    if c in {"USDC", "USDH", "USDT", "USDT0", "DAI"}:
        return float(amount or 0)
    if prices:
        # market dict shape: {prices: {BTC: {price_usd, ...}}}
        # Handle kHYPE → use HYPE price as proxy (kHYPE pegs loosely to HYPE)
        lookup = c.removeprefix("K") if c.startswith("K") else c
        entry = (prices.get(lookup) or prices.get(c) or {})
        px = entry.get("price_usd")
        if px and amount:
            return float(amount) * float(px)
    return float(entry_ntl or 0)


# R-NOPRELIQ + REMOVE BLOFIN (2026-05-15): _fmt_cycle_upnl_block ELIMINADO.
# El vehículo Trade del Ciclo (Blofin) ya no forma parte del fondo.


# ─── R-BOT-TERMINOLOGY-UNIFY (2026-05-07) — Bug #4 ───────────────────────────
# Destacado header for /reporte: 4 critical metrics surfaced BEFORE the
# user has to scroll through the timeline. Single-source-of-truth with
# the existing capital_calc / hyperlend_reader / macro_calendar modules
# so the headline can never disagree with the body of the report.

def _flywheel_hf_for_header(hyperlend: list[dict[str, Any]] | dict[str, Any]) -> tuple[str, str]:
    """Resolve the HF to surface in the destacado header.

    Returns ``(hf_text, source_label)`` where ``hf_text`` is the formatted
    HF string ("1.214" / "∞" / "—") and ``source_label`` describes which
    wallet was selected ("Main Flywheel" / "Principal" / "n/a"). Picks
    the highest-collateral OK entry; falls back to last_known cache for
    UNKNOWN; returns "—" if nothing is usable.
    """
    # R-PMCORE (2026-06-01): the HyperLend flywheel is CLOSED — the fund
    # migrated 100% into Portfolio Margin. When deprecated, never surface a
    # stale flywheel HF as if live; the header line reads CLOSED.
    try:
        from config import FLYWHEEL_DEPRECATED as _FLY_DEP_HF
    except Exception:  # noqa: BLE001
        _FLY_DEP_HF = True
    if _FLY_DEP_HF:
        return "CERRADO", "flywheel migrado a Portfolio Margin"
    hl_list = hyperlend if isinstance(hyperlend, list) else [hyperlend] if hyperlend else []
    best_ok: dict[str, Any] | None = None
    best_ok_coll: float = -1.0
    fallback_unknown: dict[str, Any] | None = None
    for hl in hl_list:
        if not isinstance(hl, dict):
            continue
        if hl.get("status") != "ok":
            continue
        d = hl.get("data") or {}
        cls = hl.get("hf_status") or d.get("hf_status") or "OK"
        if cls == "OK":
            try:
                coll = float(d.get("total_collateral_usd") or 0.0)
            except (TypeError, ValueError):
                coll = 0.0
            if coll > best_ok_coll:
                best_ok = hl
                best_ok_coll = coll
        elif cls == "UNKNOWN" and fallback_unknown is None:
            fallback_unknown = hl
    if best_ok is not None:
        d = best_ok.get("data") or {}
        addr = (d.get("wallet") or "").lower()
        canonical = apply_wallet_label(addr, d.get("label"))
        hf_val = d.get("health_factor")
        if hf_val == float("inf") or hf_val is None:
            return "∞", canonical
        return _fmt_hf(hf_val), canonical
    if fallback_unknown is not None:
        d = fallback_unknown.get("data") or {}
        addr = (d.get("wallet") or "").lower()
        canonical = apply_wallet_label(addr, d.get("label"))
        last = d.get("last_known_hf")
        return _fmt_hf_loose(last) + " (cached)", canonical
    return "—", "n/a"


def _pm_health_for_header(
    wallets: list[dict[str, Any]],
    market: dict[str, Any] | None = None,
) -> str:
    """P1.4/P1.5: PM-core health KPI for the destacado header.

    The flywheel migrated to Portfolio Margin, so the live core-health metric
    is the PM margin ratio (band CALM/WARN/STRESS/CRÍTICO/LIQ) — NOT a legacy
    HyperLend HF. Returns a compact one-liner like
    ``12.3% 🟢 CALM (col $94K / deuda $0)`` or ``—`` if PM can't be computed.
    NEVER raises.
    """
    try:
        from config import PM_PRIMARY_WALLET as _PMW
        from modules.portfolio_margin import compute_pm_state, _display_band
    except Exception:  # noqa: BLE001
        return "—"
    try:
        pmw = (_PMW or "").lower()
        primary = None
        for w in wallets:
            if isinstance(w, dict) and w.get("status") == "ok":
                wd = w.get("data") or {}
                if (wd.get("wallet") or "").lower() == pmw:
                    primary = wd
                    break
        if primary is None:
            return "—"
        pm = compute_pm_state(
            primary.get("spot_balances") or [],
            primary.get("positions") or [],
            _build_price_map(market),
            open_orders=primary.get("open_orders") or [],
        )
        if pm is None or not pm.has_data or pm.collateral_usd <= 0:
            return "—"
        emoji, band = _display_band(pm.ratio)
        txt = (
            f"{pm.ratio * 100:.1f}% {emoji} {band} "
            f"(col {_fmt_usd(pm.collateral_usd)} / deuda {_fmt_usd(pm.debt_usd)})"
        )
        if pm.naked_long:
            txt += " 🚨 NAKED-LONG"
        return txt
    except Exception:  # noqa: BLE001
        return "—"


def _basket_upnl_for_header(wallets: list[dict[str, Any]]) -> tuple[float, int]:
    """Aggregate basket UPnL from the trading wallet's perp positions.

    Returns ``(upnl_usd, position_count)``. The basket lives on the
    BlackCatDeFi EVM (Trading) wallet (canonical 0xc7ae…1505) — sum the
    unrealized_pnl of its perp positions to surface basket UPnL on the
    header. Falls back to wallet-level ``unrealized_pnl_total`` if the
    per-position breakdown is missing.
    """
    basket_addr = "0xc7ae23316b47f7e75f455f53ad37873a18351505"
    upnl = 0.0
    n = 0
    for w in wallets:
        if not isinstance(w, dict) or w.get("status") != "ok":
            continue
        d = w.get("data") or {}
        addr = (d.get("wallet") or "").lower()
        if addr != basket_addr:
            continue
        positions = d.get("positions") or []
        if positions:
            for p in positions:
                try:
                    upnl += float(p.get("unrealized_pnl") or p.get("upnl") or 0.0)
                except (TypeError, ValueError):
                    pass
                n += 1
            return upnl, n
        # Fall back to wallet-level total.
        try:
            upnl = float(d.get("unrealized_pnl_total") or 0.0)
        except (TypeError, ValueError):
            upnl = 0.0
        return upnl, 0
    return 0.0, 0


def _cat_date_label(dt: datetime) -> str:
    """Compact UTC date like '6 Jun' (no leading zero, platform-agnostic)."""
    return f"{dt.day} {dt.strftime('%b')}"


def _cat_time_until(target: datetime, now: datetime) -> str:
    """Relative 'en 2d 4h' string; never negative (already-passed → '0m')."""
    total = int((target - now).total_seconds())
    if total <= 0:
        return "0m"
    days = total // 86400
    hours = (total % 86400) // 3600
    mins = (total % 3600) // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins and not days:
        parts.append(f"{mins}m")
    return " ".join(parts) or f"{total}s"


def _cat_token_key(label: str) -> str:
    """Leading token symbol (UPPER) used for dedup, e.g. 'HYPE unlock'→'HYPE'."""
    for tok in (label or "").replace("×", " ").replace("x", " ").split():
        cleaned = "".join(c for c in tok if c.isalnum()).upper()
        if cleaned:
            return cleaned
    return (label or "").strip().upper()


def _next_catalyst_for_header(
    window_hours: int = 72,
    unlocks: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> str:
    """Return the next REAL dated catalyst(s) within ``window_hours``.

    R-CATALYST-LIVE (2026-06-04): the header no longer trusts ONLY the
    hardcoded SQLite macro roadmap (which goes stale once its seeded
    events expire). It merges dated events from every wired source —
    the macro calendar AND the live token-unlock feed (``unlocks`` as
    returned by ``modules.unlocks.fetch_unlocks``) — purges anything
    past-dated relative to the report's run timestamp (UTC), keeps only
    what falls inside the window, and renders the nearest 1-3.

    Returns "ninguno <72h" only when NO source has a dated event inside
    the window (the genuinely-empty case). Never raises.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    window_secs = window_hours * 3600

    # candidate = {"label","dt","emoji","rank"}; rank for tie-break only.
    candidates: list[dict[str, Any]] = []
    impact_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    impact_emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}

    # ── Source 1: curated macro calendar (already purges ts < now in SQL,
    #    but we re-check the window here against the run timestamp). ──────
    try:
        from modules.macro_calendar import upcoming_events
        for ev in upcoming_events(limit=30) or []:
            ts = ev.timestamp_utc
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            name = (ev.name or "?").strip()
            if len(name) > 48:
                name = name[:45] + "…"
            candidates.append({
                "label": name,
                "dt": ts,
                "emoji": impact_emoji.get(ev.impact_level, "⚪"),
                "rank": impact_rank.get(ev.impact_level, 0),
            })
    except Exception:  # noqa: BLE001
        pass

    # ── Source 2: live token-unlock feed (knows real upcoming unlocks,
    #    e.g. the 6 Jun HYPE unlock that the stale roadmap never had). ───
    try:
        if isinstance(unlocks, dict) and unlocks.get("status") == "ok":
            for item in unlocks.get("data") or []:
                if not isinstance(item, dict):
                    continue
                raw_ts = item.get("timestamp") or item.get("next_unlock_ts")
                try:
                    epoch = int(float(raw_ts))
                except (TypeError, ValueError):
                    continue
                if epoch <= 0:
                    continue
                # P0.2 (2026-06-04): the live feed tracks PRIORITY tokens
                # even at $0 (linear / already-emitted), whose
                # "next_unlock_ts" can be a near-future emission tick — that
                # produced the bogus "SUI unlock 2 Jun (en 17m)" line for an
                # already-passed, $0-per-dropstab event. When the feed gives
                # an EXPLICIT value of $0 (or negative), the unlock is not a
                # material catalyst → drop it. A MISSING value field is
                # treated as "unknown but assume material" (the past-purge
                # below still guards against stale ticks).
                if "value_usd" in item:
                    try:
                        val_usd = float(item.get("value_usd") or 0)
                    except (TypeError, ValueError):
                        val_usd = 0.0
                    if val_usd <= 0:
                        continue
                dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
                sym = (item.get("symbol") or item.get("token") or "?")
                candidates.append({
                    "label": f"{sym} unlock",
                    "dt": dt,
                    "emoji": "🔓",
                    "rank": impact_rank["high"],
                })
    except Exception:  # noqa: BLE001
        pass

    # ── Window filter: strictly purge past-dated; keep only inside 72h. ──
    in_window: list[dict[str, Any]] = []
    for c in candidates:
        delta = (c["dt"] - now).total_seconds()
        if 0 <= delta <= window_secs:
            in_window.append(c)

    if not in_window:
        return f"ninguno <{window_hours}h"

    # Nearest first (the header is about *what's next*), tie-break impact.
    in_window.sort(key=lambda c: (c["dt"], -c["rank"]))

    # Dedup by (token, date) — same unlock from calendar + feed collapses.
    seen: set[tuple[str, Any]] = set()
    chosen: list[dict[str, Any]] = []
    for c in in_window:
        key = (_cat_token_key(c["label"]), c["dt"].date())
        if key in seen:
            continue
        seen.add(key)
        chosen.append(c)
        if len(chosen) >= 3:
            break

    parts = [f"{c['emoji']} {c['label']} {_cat_date_label(c['dt'])}" for c in chosen]
    line = " · ".join(parts)
    return f"{line} (en {_cat_time_until(chosen[0]['dt'], now)})"


def format_report_header(
    wallets: list[dict[str, Any]],
    hyperlend: list[dict[str, Any]] | dict[str, Any],
    market: dict[str, Any] | None = None,
    unlocks: dict[str, Any] | None = None,
) -> str:
    """Build the destacado header for /reporte (Bug #4).

    Surfaces 4 critical KPIs at the top of /reporte so BCD doesn't have
    to scroll past the X timeline + body to know fund health:

    1. TOTAL EQUITY — single-source-of-truth via auto.capital_calc
       (HL net + perp + non-stable spot + Pear staked, stables NOT
       double-counted under Unified Account).
    2. BASKET UPnL — aggregate UPnL across the BlackCatDeFi EVM
       (Trading) wallet's perp positions (Super Basket Stage 6).
    3. HF FLYWHEEL — Main Flywheel HF from auto.hyperlend_reader
       (cache-aware, branches on hf_status).
    4. NEXT CATALYST <72h — nearest REAL dated catalyst(s) within a 72h
       window, merged from modules.macro_calendar AND the live
       token-unlock feed (``unlocks``); past-dated entries are purged
       against the run timestamp so a stale roadmap can't read "ninguno".

    Returns a Telegram-ready multi-line string. Never raises — degrades
    each line independently to "—" if its data source is unavailable.
    """
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = []
    lines.append("⚡ DESTACADO — FONDO BLACK CAT")
    lines.append("─" * 30)
    lines.append(f"Snapshot: {now_utc}")

    # 1. TOTAL EQUITY
    total_equity_text = "—"
    try:
        from auto.capital_calc import compute_net_capital
        hl_list = hyperlend if isinstance(hyperlend, list) else [hyperlend] if hyperlend else []
        hl_coll = 0.0
        hl_debt = 0.0
        for hl in hl_list:
            if not isinstance(hl, dict):
                continue
            if hl.get("status") == "ok":
                hd = hl.get("data") or {}
                try:
                    hl_coll += float(hd.get("total_collateral_usd") or 0.0)
                except (TypeError, ValueError):
                    pass
                try:
                    hl_debt += float(hd.get("total_debt_usd") or 0.0)
                except (TypeError, ValueError):
                    pass
        # R-PMCORE: flywheel HyperLend deprecado → no contar HL stale en equity.
        try:
            from config import FLYWHEEL_DEPRECATED as _FLY_DEP_H
        except Exception:  # noqa: BLE001
            _FLY_DEP_H = True
        if _FLY_DEP_H:
            hl_coll = 0.0
            hl_debt = 0.0
        perp_total = 0.0
        spot_non_stable = 0.0
        spot_stables = 0.0
        _price_map = _build_price_map(market)
        for w in wallets:
            if not isinstance(w, dict) or w.get("status") != "ok":
                continue
            d = w.get("data") or {}
            try:
                pe = float(d.get("account_value") or 0.0)
            except (TypeError, ValueError):
                pe = 0.0
            perp_total += pe
            try:
                ns, st = _estimate_spot_split(
                    d.get("spot_balances") or [], pe, _price_map
                )
                spot_non_stable += float(ns)
                spot_stables += float(st)
            except Exception:  # noqa: BLE001
                pass
        try:
            pear_total = float(os.getenv("PEAR_STAKED_USD", "0") or 0)
        except (TypeError, ValueError):
            pear_total = 0.0
        # R-VAULTDEP: fund capital deposited INTO HL vaults (keyless read,
        # 0.0 on failure — never inflates, never crashes).
        try:
            from modules.vault_deposits import get_vault_deposits_total
            vault_dep_total = get_vault_deposits_total()
        except Exception:  # noqa: BLE001
            vault_dep_total = 0.0
        net = compute_net_capital({
            "hl_collateral_total": hl_coll,
            "hl_debt_total": hl_debt,
            "perp_equity_total": perp_total,
            "spot_usd_total": spot_non_stable,
            "spot_stables_total": spot_stables,
            "upnl_perp_total": 0.0,  # already in perp accountValue
            "pear_staked_total": pear_total,
            "vault_deposits_total": vault_dep_total,
        })
        # NetCapital dataclass — total_equity_usd is the Rabby-parity headline.
        try:
            total_equity_val = float(getattr(net, "total_equity_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            total_equity_val = float(getattr(net, "net_total_usd", 0.0) or 0.0)
        total_equity_text = _fmt_usd(total_equity_val)
    except Exception:  # noqa: BLE001
        total_equity_text = "—"
    lines.append(f"💰 TOTAL EQUITY: {total_equity_text}")

    # 2. BASKET UPnL
    try:
        upnl, n_pos = _basket_upnl_for_header(wallets)
        sign = "+" if upnl >= 0 else ""
        if n_pos > 0:
            lines.append(
                f"📉 BASKET UPnL: {sign}{_fmt_usd(upnl)} ({n_pos} legs · Super Basket Stage 6)"
            )
        elif upnl != 0:
            lines.append(
                f"📉 BASKET UPnL: {sign}{_fmt_usd(upnl)} (Super Basket Stage 6 — wallet-total)"
            )
        else:
            lines.append("📉 BASKET UPnL: $0 (basket idle / sin posiciones perp)")
    except Exception:  # noqa: BLE001
        lines.append("📉 BASKET UPnL: —")

    # 3. CORE HEALTH — PM margin ratio (flywheel migrated to Portfolio
    #    Margin). P1.4: the legacy "HF FLYWHEEL" KPI is replaced by the live
    #    PM-core health band. Rollback (FLYWHEEL_DEPRECATED=false) restores
    #    the legacy HyperLend HF line.
    try:
        from config import FLYWHEEL_DEPRECATED as _FLY_DEP_K3
    except Exception:  # noqa: BLE001
        _FLY_DEP_K3 = True
    if _FLY_DEP_K3:
        try:
            lines.append(f"⚖️ PM SALUD (core): {_pm_health_for_header(wallets, market)}")
        except Exception:  # noqa: BLE001
            lines.append("⚖️ PM SALUD (core): —")
    else:
        try:
            hf_text, hf_source = _flywheel_hf_for_header(hyperlend)
            lines.append(f"⚖️ HF FLYWHEEL: {hf_text} ({hf_source})")
        except Exception:  # noqa: BLE001
            lines.append("⚖️ HF FLYWHEEL: —")

    # 4. NEXT CATALYST <72h
    try:
        cat = _next_catalyst_for_header(window_hours=72, unlocks=unlocks)
        lines.append(f"🗓 NEXT CATALYST <72h: {cat}")
    except Exception:  # noqa: BLE001
        lines.append("🗓 NEXT CATALYST <72h: —")

    return "\n".join(lines)


def format_quick_positions(wallets: list[dict[str, Any]],
                           hyperlend: list[dict[str, Any]] | dict[str, Any],
                           bounce_tech: list[dict[str, Any]] | None = None,
                           recent_fills: list[dict[str, Any]] | None = None,
                           market: dict[str, Any] | None = None) -> str:
    lines: list[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"📊 Snapshot Fondo Black Cat — {now}")
    lines.append("")

    # ─── R-DASH: NET CAPITAL banner (single-source-of-truth) ────────────
    # Compute totals from the same wallet+HL data so the dashboard and
    # /reporte agree on the headline number. UPnL is NOT added separately
    # (already inside perp accountValue under Hyperliquid Unified Account).
    try:
        _hl_list = hyperlend if isinstance(hyperlend, list) else [hyperlend]
        _hl_coll_total = 0.0
        _hl_debt_total = 0.0
        for _hl in _hl_list:
            if isinstance(_hl, dict) and _hl.get("status") == "ok":
                _hd = _hl.get("data") or {}
                try:
                    _hl_coll_total += float(_hd.get("total_collateral_usd") or 0.0)
                except (TypeError, ValueError):
                    pass
                try:
                    _hl_debt_total += float(_hd.get("total_debt_usd") or 0.0)
                except (TypeError, ValueError):
                    pass

        # R-PMCORE (2026-06-01): flywheel HyperLend deprecado — su
        # colateral/deuda stale NO debe contar en TOTAL EQUITY (el HYPE real
        # vive en spot y se cuenta abajo). Cuando FLYWHEEL_DEPRECATED=true se
        # zeroean las contribuciones HL para evitar inflar/doble-contar.
        try:
            from config import FLYWHEEL_DEPRECATED as _FLY_DEP
        except Exception:  # noqa: BLE001
            _FLY_DEP = True
        if _FLY_DEP:
            _hl_coll_total = 0.0
            _hl_debt_total = 0.0

        _perp_total = 0.0
        _spot_non_stable_total = 0.0
        _spot_stables_total = 0.0
        _upnl_total = 0.0
        _price_map_qp = _build_price_map(market)
        for _w in wallets:
            if not isinstance(_w, dict) or _w.get("status") != "ok":
                continue
            _d = _w.get("data") or {}
            try:
                _pe = float(_d.get("account_value") or 0.0)
            except (TypeError, ValueError):
                _pe = 0.0
            _perp_total += _pe
            try:
                # R-DASHBOARD-SPOT-FIX: split into non-stable vs stables
                # so the banner labels exposure accurately and surfaces
                # stable cash equivalents separately.
                # R-PMCORE: value non-stable (HYPE) at LIVE oracle price.
                _ns, _st = _estimate_spot_split(
                    _d.get("spot_balances") or [], _pe, _price_map_qp
                )
                _spot_non_stable_total += float(_ns)
                _spot_stables_total += float(_st)
            except Exception:  # noqa: BLE001
                pass
            try:
                _upnl_total += float(_d.get("unrealized_pnl_total") or 0.0)
            except (TypeError, ValueError):
                pass

        from auto.capital_calc import compute_net_capital, format_net_capital_telegram
        # R-DASHBOARD-RABBY-PARITY (2026-05-06): Pear Protocol staked is
        # surfaced via env var PEAR_STAKED_USD (or future on-chain reader)
        # and folded into the TOTAL EQUITY headline alongside HL/perp/spot.
        try:
            _pear_total = float(os.getenv("PEAR_STAKED_USD", "0") or 0)
        except (TypeError, ValueError):
            _pear_total = 0.0
        # R-VAULTDEP: fund capital deposited INTO HL vaults — keyless live
        # read, folded into TOTAL EQUITY as its own line (never against perp
        # margin / wallet USDC). Detail block (label + PnL vs cost basis +
        # lockup) appended right after the capital tree below.
        _vault_result = None
        _vault_dep_total = 0.0
        try:
            from modules.vault_deposits import (
                fetch_vault_deposits,
                format_vault_deposits_telegram,
                get_vault_deposits_total,
            )
            _vault_result = fetch_vault_deposits()
            _vault_dep_total = get_vault_deposits_total()
        except Exception:  # noqa: BLE001
            _vault_result = None
            _vault_dep_total = 0.0
        _net = compute_net_capital({
            "hl_collateral_total": _hl_coll_total,
            "hl_debt_total": _hl_debt_total,
            "perp_equity_total": _perp_total,
            # spot_usd_total fed to capital_calc is NON-STABLE only —
            # stables are surfaced as a separate informative line.
            "spot_usd_total": _spot_non_stable_total,
            "spot_stables_total": _spot_stables_total,
            "upnl_perp_total": _upnl_total,
            "pear_staked_total": _pear_total,
            "vault_deposits_total": _vault_dep_total,
        })
        lines.append(format_net_capital_telegram(_net))
        # R-PMCORE (2026-06-01): Portfolio Margin state of the primary account
        # (HYPE collateral / debt / borrow capacity / margin ratio + naked-long
        # guard). The HYPE spot value is the bulk of TOTAL EQUITY now, so the
        # PM block sits right under the capital banner. Best-effort; the report
        # never breaks if PM can't be computed.
        try:
            from config import PM_PRIMARY_WALLET as _PMW
            from modules.portfolio_margin import (
                compute_pm_state,
                format_pm_state_telegram,
            )
            _pmw = (_PMW or "").lower()
            _primary = None
            for _w in wallets:
                if isinstance(_w, dict) and _w.get("status") == "ok":
                    _wd = _w.get("data") or {}
                    if (_wd.get("wallet") or "").lower() == _pmw:
                        _primary = _wd
                        break
            if _primary is not None:
                _pm = compute_pm_state(
                    _primary.get("spot_balances") or [],
                    _primary.get("positions") or [],
                    _price_map_qp,
                    open_orders=_primary.get("open_orders") or [],
                )
                _pm_block = format_pm_state_telegram(_pm)
                if _pm_block:
                    lines.append("")
                    lines.append(_pm_block)
        except Exception:  # noqa: BLE001
            pass
        # R-VAULTDEP detail block (label, current equity, PnL vs cost basis,
        # lockup). Only rendered when there's something to show; "n/a" on
        # read failure — never crashes the report.
        try:
            _vault_block = format_vault_deposits_telegram(_vault_result)
            if _vault_block:
                lines.append("")
                lines.append(_vault_block)
        except Exception:  # noqa: BLE001
            pass
        # R-VAULTDEP evolution: persist today's equity + show all-time vs cost
        # and delta vs the prior-day snapshot. Best-effort; never crashes.
        try:
            if _vault_result is not None and getattr(_vault_result, "ok", False):
                from modules.vault_history import format_vault_evolution_block

                _vault_evo = format_vault_evolution_block(_vault_result)
                if _vault_evo:
                    lines.append("")
                    lines.append(_vault_evo)
        except Exception:  # noqa: BLE001
            pass
        lines.append("")
    except Exception:  # noqa: BLE001
        # Never break the formatter — capital banner is best-effort.
        pass

    # ── Build HyperLend collateral map: wallet_addr (lower) → data ──
    # R-PMCORE: flywheel deprecado → no contar colateral/deuda HL stale en el
    # capital por-wallet (el HYPE real vive en spot y se cuenta ahí).
    try:
        from config import FLYWHEEL_DEPRECATED as _FLY_DEP_W
    except Exception:  # noqa: BLE001
        _FLY_DEP_W = True
    hl_list = hyperlend if isinstance(hyperlend, list) else [hyperlend]
    hl_by_wallet: dict[str, dict[str, float]] = {}
    for hl in hl_list:
        if hl.get("status") == "ok":
            h = hl["data"]
            addr = (h.get("wallet") or "").lower()
            coll = (h.get("total_collateral_usd", 0.0) or 0.0) if not _FLY_DEP_W else 0.0
            debt = (h.get("total_debt_usd", 0.0) or 0.0) if not _FLY_DEP_W else 0.0
            if addr:
                hl_by_wallet[addr] = {
                    "collateral_usd": coll,
                    "debt_usd": debt,
                    "net_usd": coll - debt,
                }

    # ── Compute total capital per wallet (perp + spot + HyperLend collateral) ──
    # R-PMCORE: value non-stable spot (HYPE) at live oracle price so the
    # per-wallet "Capital Total" matches the banner (was $0 via cost basis).
    _price_map_pw = _build_price_map(market)
    for w in wallets:
        if w.get("status") != "ok":
            continue
        d = w["data"]
        wallet_addr = (d.get("wallet") or "").lower()
        perp_eq = d.get("account_value") or 0.0
        spot_usd = _estimate_spot_usd(d.get("spot_balances") or [], perp_eq, _price_map_pw)
        hl_data = hl_by_wallet.get(wallet_addr, {})
        hl_coll = hl_data.get("collateral_usd", 0.0)
        hl_debt = hl_data.get("debt_usd", 0.0)
        total_capital = perp_eq + spot_usd + hl_coll
        d["_total_capital"] = total_capital
        d["_perp_equity"] = perp_eq
        d["_spot_usd"] = spot_usd
        d["_hl_collateral_usd"] = hl_coll
        d["_hl_debt_usd"] = hl_debt
        d["_margin_used"] = d.get("total_margin_used") or 0.0
        d["_withdrawable"] = d.get("withdrawable") or 0.0
        d["_total_ntl_pos"] = d.get("total_ntl_pos") or 0.0

    # Sort wallets by total capital descending (dynamic ordering)
    wallets = sorted(wallets,
                     key=lambda w: (w.get("data", {}).get("_total_capital") or 0)
                     if w.get("status") == "ok" else 0,
                     reverse=True)

    # R-BOT-TERMINOLOGY-UNIFY (2026-05-07) — Bug #1.
    # Wallet labels now flow from auto.wallet_labels.apply_wallet_label()
    # which resolves the canonical address→label map (single-source-of-
    # truth with /dashboard) and falls back to the env-var label
    # (FUND_WALLET_N_LABEL → d["label"]) for unknown addresses. Removed
    # the legacy hardcoded RANK_LABELS = ["PRINCIPAL", "SECUNDARIA"]
    # override that was masking FUND_WALLET_4_LABEL=BlackCatDeFi EVM
    # (Trading) on 0xc7AE because that wallet ranked #2 by capital.

    lines.append("PORTFOLIO CONSOLIDADO")

    total_fund_capital = 0.0
    total_upnl = 0.0
    all_spot: list[dict[str, Any]] = []

    for w in wallets:
        if w.get("status") != "ok":
            label = w.get("label", "?")
            short = (w.get("wallet") or "")[:6] + "…"
            error_msg = w.get("error", "error")
            if w.get("stale"):
                lines.append(f"  • {label} ({short}): ⚠️ {error_msg} (usando cache)")
            else:
                lines.append(f"  • {label} ({short}): ❌ {error_msg}")
            continue
        d = w["data"]
        short = d["wallet"][:6] + "…" + d["wallet"][-4:]
        tc = d.get("_total_capital") or 0.0
        perp_eq = d.get("_perp_equity") or 0.0
        spot_usd = d.get("_spot_usd") or 0.0
        hl_coll = d.get("_hl_collateral_usd") or 0.0
        hl_debt = d.get("_hl_debt_usd") or 0.0
        margin_used = d.get("_margin_used") or 0.0
        withdrawable = d.get("_withdrawable") or 0.0
        ntl_pos = d.get("_total_ntl_pos") or 0.0
        upnl_val = d.get("unrealized_pnl_total") or 0.0
        total_fund_capital += tc
        total_upnl += upnl_val

        # R-BOT-TERMINOLOGY-UNIFY (2026-05-07) — canonical wallet label.
        # apply_wallet_label resolves the address→label map first (so
        # 0xc7ae → "BlackCatDeFi EVM (Trading)", 0xa44e → "Main
        # Flywheel (DDS)", 0x171b → "DreamCash (WAR TRADE)", etc.) and
        # falls back to d["label"] (env-var) for unknown addresses.
        canonical_label = apply_wallet_label(d.get("wallet"), d.get("label"))
        display_label = f"💼 {canonical_label}"

        positions = d.get("positions") or []
        if positions:
            pos_summary = ", ".join(f"{p['side']} {p['coin']}" for p in positions[:5])
        elif _is_alt_short_wallet(d.get("wallet", "")) and not BASKET_STATUS.get("active"):
            # Wallet historically from Super Basket Stage 6 basket, now IDLE.
            last = BASKET_STATUS.get("last_basket", "?")
            net = BASKET_STATUS.get("last_basket_result_net_usd", 0.0)
            nxt = BASKET_STATUS.get("next_basket", "pending")
            pos_summary = (
                f"IDLE (basket {last} closed NET {_fmt_usd(net)}, {nxt}). "
                "Residual dust — no active position."
            )
        else:
            pos_summary = "sin posiciones perp"

        lines.append(f"  • {display_label} {short}")
        lines.append(f"    Capital Total: {_fmt_usd(tc)}")

        # Breakdown — Account Value already includes any USDC sitting in spot
        # under HyperLiquid Unified Account, so "Spot" here only ever shows
        # NON-STABLE tokens (HYPE, kHYPE, etc.). R-DASHBOARD-SPOT-FIX:
        # USDH/USDT0/USDT/DAI used to inflate this line — they're now
        # tracked as cash equivalent (see banner) and dropped from exposure.
        parts: list[str] = []
        if perp_eq > 0.01:
            parts.append(f"Account Value {_fmt_usd(perp_eq)}")
        if spot_usd > 0.01:
            parts.append(f"Spot non-stable {_fmt_usd(spot_usd)}")
        if hl_coll > 0.01:
            parts.append(f"HL Coll {_fmt_usd(hl_coll)}")
        if hl_debt > 0.01:
            parts.append(f"HL Debt -{_fmt_usd(hl_debt)}")
        if len(parts) > 0:
            lines.append(f"    ({' | '.join(parts)})")
        if upnl_val != 0:
            lines.append(f"    UPnL: {_fmt_usd(upnl_val)}")
        # Show margin / withdrawable / leverage when there's an active perp position.
        # R-LEVERAGE-AUTODETECT (2026-05-18): leverage SIEMPRE calculado
        # dinámicamente como notional/equity_perp y redondeado a 1 decimal.
        # NUNCA asumir un valor fijo (4x/5x/etc.) — la realidad on-chain manda.
        if ntl_pos > 50 or margin_used > 50:
            lev = round((ntl_pos / perp_eq), 1) if perp_eq > 0.01 else 0.0
            lines.append(
                f"    Margin used: {_fmt_usd(margin_used)} | "
                f"Withdrawable: {_fmt_usd(withdrawable)} | "
                f"Notional: {_fmt_usd(ntl_pos)} (~{lev:.1f}x)"
            )
        lines.append(f"    {pos_summary}")

        # Collect spot balances
        spot = d.get("spot_balances") or []
        for sb in spot:
            sb["_wallet_label"] = d["label"]
        all_spot.extend(spot)

    lines.append(f"  TOTAL FONDO: Capital {_fmt_usd(total_fund_capital)} | UPnL {_fmt_usd(total_upnl)}")

    # (R-NOPRELIQ + REMOVE BLOFIN 2026-05-15) Trade del Ciclo (BTC LONG en
    # Blofin) eliminado del fondo — no se renderiza más en /reporte.

    # ── Spot token balances (kHYPE, PEAR, etc.) con DUST threshold ──
    if all_spot:
        # Current-price map for USD valuation (kHYPE/HYPE/PEAR/etc.)
        price_map: dict[str, Any] = {}
        if isinstance(market, dict):
            price_map = (market.get("data") or {}).get("prices") or market.get("prices") or {}

        by_coin: dict[str, list[dict[str, Any]]] = {}
        for sb in all_spot:
            coin = sb.get("coin", "?")
            by_coin.setdefault(coin, []).append(sb)

        # Compute per-coin total USD and split into "real" vs "dust" (<$50)
        DUST_THRESHOLD_USD = 50.0
        real_coins: list[tuple[str, list[dict[str, Any]], float, float]] = []  # (coin, entries, amt, usd)
        dust_coins: list[tuple[str, float, float]] = []  # (coin, amount, usd_value)

        for coin, entries in by_coin.items():
            total_amount = sum(e.get("total", 0) for e in entries)
            total_entry_ntl = sum(e.get("entry_ntl", 0) for e in entries)
            # Current USD valuation (sum per-wallet to use each entry_ntl correctly)
            total_usd_now = 0.0
            for e in entries:
                total_usd_now += _current_usd_value(
                    coin,
                    e.get("total", 0) or 0,
                    e.get("entry_ntl", 0) or 0,
                    price_map,
                )
            if total_usd_now >= DUST_THRESHOLD_USD:
                real_coins.append((coin, entries, total_amount, total_usd_now))
            else:
                dust_coins.append((coin, total_amount, total_usd_now))

        if real_coins or dust_coins:
            lines.append("")
            lines.append("SPOT TOKENS")

        # Render real positions (per-wallet breakdown when multiple wallets hold)
        for coin, entries, total_amount, total_usd_now in sorted(real_coins, key=lambda x: -x[3]):
            total_entry = sum(e.get("entry_ntl", 0) for e in entries)
            if coin == "USDC":
                cost_basis_display = f"${total_amount:,.2f}"
            else:
                cost_basis_display = _fmt_usd(total_entry)

            # Unified Account note: when a wallet listed has an active perp,
            # its USDC shown here is already inside Account Value. The
            # capital math above already handles the dedup per-wallet, so
            # the SPOT TOKENS section is purely informational. We only flag
            # USDC entries when ANY of the holding wallets has an active
            # perp — those are the ones a reader could mistakenly re-add.
            usdc_note = ""
            if coin == "USDC":
                # Check per-wallet via the wallets list scoped above
                holding_addrs_with_perp = [
                    w for w in wallets
                    if w.get("status") == "ok"
                    and (w.get("data", {}).get("_perp_equity") or 0.0) > 0.01
                    and any(
                        (e.get("_wallet_label") == w.get("data", {}).get("label"))
                        for e in entries
                    )
                ]
                if holding_addrs_with_perp:
                    usdc_note = (
                        "  ⚠️ part of this USDC is in Account Value of an active "
                        "perp wallet (Unified Account) — see per-wallet breakdown above"
                    )

            if len(entries) == 1:
                wallet_label = entries[0].get("_wallet_label", "")
                lines.append(
                    f"  • {coin}: {total_amount:.4f} · {_fmt_usd(total_usd_now)} now "
                    f"(cost basis {cost_basis_display}) [{wallet_label}]"
                    f"{usdc_note}"
                )
            else:
                lines.append(
                    f"  • {coin}: {total_amount:.4f} total · {_fmt_usd(total_usd_now)} now "
                    f"(cost basis {cost_basis_display})"
                    f"{usdc_note}"
                )
                for e in entries:
                    amt = e.get("total", 0) or 0
                    lines.append(f"      {e.get('_wallet_label','?')}: {amt:.4f}")

        # Render dust in compact single-line block
        if dust_coins:
            dust_total = sum(u for _, _, u in dust_coins)
            dust_parts = []
            for coin, amount, usd in sorted(dust_coins, key=lambda x: -x[2]):
                dust_parts.append(f"{coin} {amount:.4f} ({_fmt_usd(usd)})")
            lines.append(
                f"  SPOT DUST (<${DUST_THRESHOLD_USD:.0f} c/u, residual post-trading, {_fmt_usd(dust_total)} total):"
            )
            # Wrap into chunks of 4 per line
            for i in range(0, len(dust_parts), 4):
                chunk = " | ".join(dust_parts[i:i+4])
                lines.append(f"    {chunk}")

    # HyperLend section — detailed view with HF, collateral breakdown, debt.
    # R-REPORTE-LIVE (2026-06-03) FIX 1: when the flywheel is migrated to
    # Portfolio Margin (default), HyperLend is CLOSED. Do NOT render any
    # stale HF/collateral/debt block — the live core state is the PM block
    # rendered above (collateral / debt / margin ratio / naked-long guard).
    # Rollback: set FLYWHEEL_DEPRECATED=false to restore the legacy block.
    try:
        from config import FLYWHEEL_DEPRECATED as _FLY_DEP_HLBLK
    except Exception:  # noqa: BLE001
        _FLY_DEP_HLBLK = True
    if not _FLY_DEP_HLBLK:
        lines.append("")
        lines.append("HYPERLEND")

        hl_list = sorted(hl_list,
                         key=lambda hl: (hl.get("data", {}).get("total_collateral_usd") or 0)
                         if hl.get("status") == "ok" else 0,
                         reverse=True)
    else:
        # Flywheel migrated to Portfolio Margin — render NO HyperLend block
        # (the loop below iterates an empty list). The live core state is the
        # PM block already rendered above the per-wallet portfolio.
        hl_list = []

    # ─── R-HF-RENDER (3 may 2026) ───────────────────────────────────────
    # Single-source-of-truth with auto.hyperlend_reader.read_all_with_cache.
    # Each entry carries hf_status ∈ {OK, UNKNOWN, ZERO}. The /reporte
    # block, the HF<1.20 alerts (modules/alerts.py), and the LLM analyzer
    # (modules/analysis.py) MUST all consume the same reader so they
    # never disagree (3 may bug: alert fired HF=1.2001 OK, /reporte
    # showed HF=nan + LTV=0.0% for the same wallet). The reader sets
    # data.health_factor=NaN as a sentinel for UNKNOWN; we never pass
    # that through _fmt_hf — we branch on hf_status instead.
    for hl in hl_list:
        if hl.get("status") != "ok":
            lines.append(f"  ❌ {hl.get('error','error')}")
            continue

        h = hl["data"]
        # hf_status absent → treat as OK (legacy fetch path / older cache).
        hf_status = (hl.get("hf_status") or "OK").upper()

        label = h.get("label") or hl.get("label") or "—"
        wallet_full = h.get("wallet") or ""
        wallet_short = (wallet_full[:6] + "…" + wallet_full[-4:]) if wallet_full else ""
        header = f"  [{label}]" + (f" {wallet_short}" if wallet_short else "")

        if hf_status == "UNKNOWN":
            # Degraded read: HyperEVM RPC rate-limited. Render last-known
            # HF from cache (or a clear "no prior read" message) so the
            # block NEVER shows literal "nan" / "0.0%" / "$0.00".
            lines.append(header)
            last_hf = h.get("last_known_hf")
            last_age = h.get("age_seconds")
            last_coll = h.get("last_known_collateral_usd")
            last_debt = h.get("last_known_debt_usd")

            if last_hf is None and not last_coll and not last_debt:
                lines.append(
                    "    ⚠️ HyperEVM RPC rate-limited — no prior cached read"
                )
                lines.append("    (HyperLend offline, no cache available)")
            else:
                hf_str = _fmt_hf_loose(last_hf)
                age_str = _hl_age_label(last_age)
                lines.append(
                    f"    ⚠️ HyperEVM RPC rate-limited — last known HF: {hf_str} "
                    f"(cached {age_str} ago)"
                )
                if last_coll is not None and last_coll > 0:
                    lines.append(
                        f"    Last cached Collateral: {_fmt_usd(last_coll)}"
                    )
                if last_debt is not None and last_debt > 0:
                    lines.append(
                        f"    Last cached Borrowed: {_fmt_usd(last_debt)}"
                    )
            continue

        # OK or ZERO branch — same rendering, but skip empty wallets.
        coll = h.get("total_collateral_usd", 0.0) or 0.0
        if coll < 0.01:
            continue
        lines.append(header)
        lines.append(f"    HF: {_fmt_hf(h.get('health_factor'))}")

        coll_sym = h.get("collateral_symbol")
        coll_bal = h.get("collateral_balance") or 0.0
        if coll_sym and coll_bal:
            lines.append(
                f"    Collateral: {coll_bal:.4f} {coll_sym} ({_fmt_usd(h.get('total_collateral_usd'))})"
            )
        else:
            lines.append(f"    Collateral: {_fmt_usd(h.get('total_collateral_usd'))}")

        debt_sym = h.get("debt_symbol")
        debt_bal = h.get("debt_balance") or 0.0
        if debt_sym and debt_bal:
            lines.append(
                f"    Borrowed: {debt_bal:.4f} {debt_sym} ({_fmt_usd(h.get('total_debt_usd'))})"
            )
        else:
            lines.append(f"    Borrowed: {_fmt_usd(h.get('total_debt_usd'))}")

        lines.append(f"    Available borrow: {_fmt_usd(h.get('available_borrows_usd'))}")
        lines.append(f"    LTV: {(h.get('ltv') or 0)*100:.1f}% | LiqThr: {(h.get('current_liquidation_threshold') or 0)*100:.1f}%")

    # ── Bounce Tech leveraged tokens ──
    if bounce_tech is not None:
        bt_positions = []
        for bw in bounce_tech:
            if bw.get("status") != "ok":
                continue
            for p in bw.get("positions", []):
                bt_positions.append(p)

        lines.append("")
        lines.append("BOUNCE TECH (Leveraged Tokens)")
        if bt_positions:
            bt_total = 0.0
            for p in bt_positions:
                direction = "🟢 LONG" if p.get("is_long") else "🔴 SHORT"
                asset = p.get("asset", "?")
                lev = p.get("leverage", "?")
                val = p.get("value_usd", 0.0)
                bt_total += val
                lines.append(f"  {direction} {asset} {lev} — {_fmt_usd(val)}")
            lines.append(f"  Total BT: {_fmt_usd(bt_total)}")
        else:
            lines.append("  INACTIVE — no open positions")

    # ── Trades cerrados últimas 24h (agrupados por classify_fill) ──
    if recent_fills:
        lines.append("")
        lines.append("TRADES CERRADOS (24h)")

        # Bucket fills by classification tag
        grouped: dict[str, list[dict[str, Any]]] = {}
        total_pnl = 0.0
        total_fees = 0.0
        for f in recent_fills:
            label = f.get("_wallet_label", "")
            tag = classify_fill(f, wallet_label=label)
            grouped.setdefault(tag, []).append(f)
            total_pnl += f.get("closedPnl", 0) or 0
            total_fees += f.get("fee", 0) or 0

        # Render order (primary categories first, then alpha)
        primary_order = ["Core DCA", "Basket trade", "HL perp"]
        ordered_tags = [t for t in primary_order if t in grouped] + \
                       [t for t in sorted(grouped.keys()) if t not in primary_order]

        from datetime import datetime as _dt, timezone as _tz

        for tag in ordered_tags:
            fills = grouped[tag]
            sub_pnl = sum(f.get("closedPnl", 0) or 0 for f in fills)
            sub_notional = sum((f.get("sz", 0) or 0) * (f.get("px", 0) or 0) for f in fills)
            # Aggregate by coin/side for compact subtotals inside Core DCA / Basket
            by_coin: dict[str, dict[str, float]] = {}
            for f in fills:
                coin = f.get("coin", "?")
                side = f.get("side", "?").upper()
                key = f"{side} {coin}"
                agg = by_coin.setdefault(key, {"sz": 0.0, "notional": 0.0, "count": 0, "last_px": 0.0})
                agg["sz"] += f.get("sz", 0) or 0
                agg["notional"] += (f.get("sz", 0) or 0) * (f.get("px", 0) or 0)
                agg["count"] += 1
                agg["last_px"] = f.get("px", 0) or 0

            lines.append(f"  [{tag}]  {len(fills)} fill(s) · PnL: {_fmt_usd(sub_pnl)} · Notional: {_fmt_usd(sub_notional)}")

            # Per-fill detail (top 8, rest collapsed)
            for f in fills[:8]:
                coin = f.get("coin", "?")
                side = f.get("side", "?").upper()
                sz = f.get("sz", 0) or 0
                px = f.get("px", 0) or 0
                pnl = f.get("closedPnl", 0) or 0
                icon = "🟢" if pnl >= 0 else ("🔴" if pnl < 0 else "⚪")
                ts = f.get("time")
                time_str = ""
                if ts:
                    time_str = _dt.fromtimestamp(ts / 1000, tz=_tz.utc).strftime("%d %b %H:%M")
                # For spot fills pnl is usually 0 — show notional instead
                pnl_str = f"PnL {_fmt_usd(pnl)}" if pnl != 0 else f"Notional {_fmt_usd(sz*px)}"
                lines.append(
                    f"    {icon} {side} {coin} {sz:.4f} @ ${px:,.4f} | {pnl_str} | {time_str}"
                )
            if len(fills) > 8:
                lines.append(f"    … +{len(fills)-8} more fills in this group")

        lines.append(
            f"  TOTAL PnL: {_fmt_usd(total_pnl)} | Fees: {_fmt_usd(total_fees)} | Net: {_fmt_usd(total_pnl - total_fees)}"
        )

    return "\n".join(lines)


def format_hf(hyperlend: list[dict[str, Any]] | dict[str, Any]) -> str:
    """Format HF for /hf command — supports both list and single dict."""
    hl_list = hyperlend if isinstance(hyperlend, list) else [hyperlend]

    hl_list = sorted(hl_list,
                     key=lambda hl: (hl.get("data", {}).get("total_collateral_usd") or 0)
                     if hl.get("status") == "ok" else 0,
                     reverse=True)

    parts: list[str] = []
    for hl in hl_list:
        if hl.get("status") != "ok":
            parts.append(f"❌ HyperLend: {hl.get('error','error')}")
            continue
        h = hl["data"]
        # R-HF-RENDER: respect hf_status from auto.hyperlend_reader
        hf_status = (hl.get("hf_status") or "OK").upper()

        if hf_status == "UNKNOWN":
            label = h.get("label") or hl.get("label") or "—"
            last_hf = h.get("last_known_hf")
            last_age = h.get("age_seconds")
            last_coll = h.get("last_known_collateral_usd")
            last_debt = h.get("last_known_debt_usd")
            if last_hf is None and not last_coll and not last_debt:
                parts.append(
                    f"⚠️ [{label}] HyperLend offline — no cached read available"
                )
            else:
                hf_str = _fmt_hf_loose(last_hf)
                age_str = _hl_age_label(last_age)
                cached_block = (
                    f"⚠️ [{label}] RPC rate-limited — last known HF: {hf_str} "
                    f"(cached {age_str} ago)"
                )
                if last_coll is not None and last_coll > 0:
                    cached_block += f"\n  Last cached Collateral: {_fmt_usd(last_coll)}"
                if last_debt is not None and last_debt > 0:
                    cached_block += f"\n  Last cached Borrowed: {_fmt_usd(last_debt)}"
                parts.append(cached_block)
            continue

        coll = h.get("total_collateral_usd", 0.0) or 0.0
        if coll < 0.01:
            continue

        hf = h.get("health_factor")
        icon = "🟢"
        # Defensive: NaN must NOT silently keep the green icon. Treat NaN
        # the same as the UNKNOWN branch above (this should be unreachable
        # because hf_status would be UNKNOWN, but defense-in-depth).
        try:
            _hf_is_nan = math.isnan(float(hf)) if hf is not None else False
        except (TypeError, ValueError):
            _hf_is_nan = False
        if _hf_is_nan:
            icon = "⚠️"
        elif hf is not None and not math.isinf(hf):
            # Operational rule: <1.00 real liquidation, <1.10 action, <1.15 monitor,
            # 1.10–1.20 normal operational (DO NOT alert), >1.20 comfortable.
            if hf < 1.10:
                icon = "🚨"
            elif hf < 1.15:
                icon = "⚠️"

        label = h.get("label") or hl.get("label") or "—"
        coll_sym = h.get("collateral_symbol")
        coll_bal = h.get("collateral_balance") or 0.0
        debt_sym = h.get("debt_symbol")
        debt_bal = h.get("debt_balance") or 0.0

        coll_str = (
            f"{coll_bal:.4f} {coll_sym} ({_fmt_usd(h.get('total_collateral_usd'))})"
            if coll_sym and coll_bal
            else _fmt_usd(h.get("total_collateral_usd"))
        )

        debt_str = (
            f"{debt_bal:.4f} {debt_sym} ({_fmt_usd(h.get('total_debt_usd'))})"
            if debt_sym and debt_bal
            else _fmt_usd(h.get("total_debt_usd"))
        )

        parts.append(
            f"{icon} [{label}] HF: {_fmt_hf(hf)}\n"
            f"  Collateral: {coll_str}\n"
            f"  Borrowed: {debt_str}\n"
            f"  Available: {_fmt_usd(h.get('available_borrows_usd'))}"
        )

    return "\n".join(parts) if parts else "— No active HyperLend positions"


def compile_raw_data(
    portfolio: list[dict[str, Any]] | None,
    hyperlend: list[dict[str, Any]] | dict[str, Any] | None,
    market: dict[str, Any] | None,
    unlocks: dict[str, Any] | None,
    telegram_intel: dict[str, Any] | None,
    bounce_tech: list[dict[str, Any]] | None = None,
) -> str:
    """Build the user message that we feed to Claude with all raw data."""
    import json

    now = datetime.now(timezone.utc).isoformat()

    bt = bounce_tech
    if not bt and isinstance(telegram_intel, dict) and "bounce_tech" in telegram_intel:
        bt = telegram_intel.pop("bounce_tech", None)

    # ── R-REPORTE-LIVE (2026-06-03): freshness + venue-truth scrub ──
    # FIX 1: when the flywheel is migrated to Portfolio Margin (default),
    # HyperLend is CLOSED — do NOT feed stale collateral/HF/debt to the LLM
    # as if it were a live position. Replace the raw HyperLend blob with an
    # explicit "deprecated/closed" marker so the model cannot reason on it.
    try:
        from config import FLYWHEEL_DEPRECATED as _FLY_DEP_RAW
    except Exception:  # noqa: BLE001
        _FLY_DEP_RAW = True
    if _FLY_DEP_RAW:
        hyperlend_payload: Any = {
            "status": "deprecated_closed",
            "note": (
                "Flywheel HyperLend CERRADO — fondo migrado 100% a HyperLiquid "
                "Portfolio Margin. Cualquier colateral/HF/deuda de HyperLend es "
                "CACHE STALE de wallets cerradas: NO contar como posición viva, "
                "NO reportar HF de HyperLend, NO incluir en equity. El core del "
                "fondo es HYPE spot como colateral cross en PM (ver bloque PM)."
            ),
        }
    else:
        hyperlend_payload = hyperlend or {}

    # FIX 1 (general freshness): annotate any wallet that fell back to cache
    # or whose data is older than 6h so it is never presented as live state.
    try:
        from auto.freshness import annotate_portfolio_freshness
        portfolio_clean = annotate_portfolio_freshness(portfolio)
    except Exception:  # noqa: BLE001
        portfolio_clean = portfolio or []

    blob = {
        "timestamp_utc": now,
        "portfolio": portfolio_clean or [],
        "hyperlend": hyperlend_payload,
        "market": market or {},
        "unlocks": unlocks or {},
        "telegram_intel": telegram_intel or {},
        "bounce_tech": bt or [],
    }
    pretty = json.dumps(blob, ensure_ascii=False, indent=2, default=str)

    # FIX 2: classify each open position by real structure and inject the
    # block ABOVE the raw data so the LLM tags before writing any "acción
    # sugerida" (CYCLE-ACCUMULATION must never get a bearish close suggestion).
    classification_block = ""
    try:
        from modules.position_classifier import (
            classify_portfolio,
            build_classification_block,
        )
        classification_block = build_classification_block(
            classify_portfolio(portfolio, market)
        )
    except Exception:  # noqa: BLE001
        classification_block = ""

    return (
        (classification_block + "\n" if classification_block else "")
        + "RAW DATA (timestamp UTC " + now + "):\n\n"
        "```json\n" + pretty + "\n```\n\n"
        "Generate the report following the system prompt format. "
        "No filler, specific numbers, actionable conclusions."
    )
