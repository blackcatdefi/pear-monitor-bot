"""R-PMCORE — HyperLiquid Portfolio Margin state for the primary account.

Post-migration the fund holds its core capital as a spot HYPE balance in the
primary wallet (``0xc7ae``). Under Portfolio Margin that spot HYPE IS the
cross collateral — there is no separate "deposit as collateral" step, and the
ONLY borrowable asset is USDC/USDH (UETH borrow no longer exists). This module
derives the live PM state from the wallet's spot balances + perp positions:

* ``collateral_usd``   — value of PM-eligible spot (HYPE + configured assets)
                          at the live HL oracle price.
* ``debt_usd``         — USDC/USDH/USDT0 ACTUALLY borrowed (negative spot
                          balance). This is a real liability, NOT capacity.
* ``capacity_usd``     — borrow capacity = ``PM_HYPE_LTV × collateral`` (0.5×).
* ``available_usd``    — capacity − debt (head-room still borrowable).
* ``ratio``            — debt / capacity (utilisation of borrow capacity).
                          Thresholds: WARN 0.40, STRESS 0.70, LIQ 0.95.
* ``shorts_notional``  — total SHORT perp notional (the basket = the hedge).
* ``naked_long``       — debt drawn but NO shorts open → leveraged long with
                          no hedge (the fund's hard-rule violation).

Robustness: NEVER raises. Missing data → zeros and a CALM status. The HYPE
price is resolved via ``modules.hl_prices`` (keyless oracle) so a CoinGecko
outage never zeroes the collateral.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

try:
    from config import (
        PM_HYPE_LTV,
        PM_WARN_RATIO,
        PM_STRESS_RATIO,
        PM_CRITICAL_RATIO,
        PM_LIQ_RATIO,
        PM_COLLATERAL_ASSETS,
    )
except Exception:  # noqa: BLE001 — importable in isolated tests
    PM_HYPE_LTV = 0.50
    PM_WARN_RATIO = 0.40
    PM_STRESS_RATIO = 0.70
    PM_CRITICAL_RATIO = 0.85
    PM_LIQ_RATIO = 0.95
    PM_COLLATERAL_ASSETS = ["HYPE"]

log = logging.getLogger(__name__)

_STABLES = frozenset({"USDC", "USDH", "USDT", "USDT0", "USDE", "DAI", "USR", "USDHL"})


@dataclass(frozen=True)
class PMState:
    collateral_usd: float
    debt_usd: float
    capacity_usd: float
    available_usd: float
    ratio: float                      # debt / capacity (0..1+)
    status: str                       # CALM | WARN | STRESS | LIQ
    shorts_notional: float
    naked_long: bool
    hype_qty: float
    hype_px: float
    collateral_breakdown: dict[str, float] = field(default_factory=dict)
    has_data: bool = True
    # R-WALLET-FIX (2026-06-06): borrow-capacity health factor and the HYPE
    # price at which the borrow hits the LTV ceiling. HF = (collateral ×
    # PM_HYPE_LTV) / debt — i.e. the fraction of the LTV-allowed capacity still
    # covered by collateral. HF < 1 means the borrow has exceeded the 50% LTV
    # cap (collateral fell or debt grew) and the position is over-extended.
    # ``liq_price`` is debt / (hype_qty × PM_HYPE_LTV): the HYPE oracle price
    # at which collateral × LTV == debt. Both 0.0 when there is no debt.
    health_factor: float = 0.0
    liq_price: float = 0.0
    # R-PM-LIQ (2026-06-06): the cap-50%-LTV guess for liq_price was WRONG — it
    # used the BORROW LTV (0.5) as if it were the maintenance threshold, putting
    # the liquidation price ~50% too high ($60.45 vs the real ~$40.32). The HL
    # Portfolio Margin maintenance threshold is ``0.5 + 0.5 × ltv`` per
    # collateral token (0.75 for a 0.5-LTV asset like HYPE), and the app's
    # "Health Factor %" is borrow UTILISATION (HF_app = capacity/debt), NOT the
    # liquidation point. These fields separate the two framings cleanly:
    #   * ``max_ltv``        — borrow LTV cap used for capacity (HYPE 0.50).
    #   * ``liq_threshold``  — maintenance LTV = 0.5 + 0.5×ltv (HYPE 0.75).
    #   * ``aave_hf``        — REAL risk metric = Σ(value×liq_threshold)/debt.
    #                          ≥1 safe, <1 liquidatable. Drives the risk band.
    #   * ``current_ltv``    — debt / collateral value (informational).
    #   * ``price_buffer_pct`` — % the HYPE oracle can fall before liq.
    #   * ``risk_emoji``/``risk_label`` — aave_HF-driven status (NOT the naked
    #     -long flag, which stays a separate HEDGE-MISSING note).
    #   * ``perp_cross_mm``  — perp cross maintenance margin folded into the
    #     liability ONLY when cross perps share the PM account (live: 0).
    # ``health_factor`` stays HF_app (capacity/debt, "borrow utilization Earn")
    # and ``liq_price`` is now the REAL maintenance liquidation price for HYPE.
    max_ltv: float = 0.0
    liq_threshold: float = 0.0
    aave_hf: float = 0.0
    current_ltv: float = 0.0
    price_buffer_pct: float = 0.0
    risk_emoji: str = "🟢"
    risk_label: str = "SIN DEUDA"
    perp_cross_mm: float = 0.0
    # P1.5: capital reserved by resting (non-trigger, non-reduce-only) limit
    # BUY orders. Loaded limit orders reserve margin/notional in HL, so the
    # borrow head-room (``available_usd``) is NOT freely deployable — this is
    # the dry powder already allocated to the ladder.
    committed_orders_usd: float = 0.0
    committed_orders_count: int = 0


def _f(v: Any) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _classify(ratio: float) -> str:
    if ratio >= PM_LIQ_RATIO:
        return "LIQ"
    if ratio >= PM_STRESS_RATIO:
        return "STRESS"
    if ratio >= PM_WARN_RATIO:
        return "WARN"
    return "CALM"


def _liq_threshold_for_ltv(ltv: float) -> float:
    """HL Portfolio Margin maintenance threshold for a collateral asset.

    The borrow LTV (e.g. 0.50 for HYPE) is the cap at which you can no longer
    borrow MORE; liquidation happens later, at the maintenance threshold
    ``0.5 + 0.5 × ltv`` (0.75 for a 0.5-LTV asset). NEVER raises.
    """
    try:
        lv = float(ltv)
    except (TypeError, ValueError):
        lv = PM_HYPE_LTV
    if lv <= 0:
        lv = PM_HYPE_LTV
    return 0.5 + 0.5 * lv


# R-PM-LIQ aave-style health bands (DRIVEN BY aave_HF, not the borrow ratio):
#   ≥1.30 🟢 SALUDABLE · 1.15-1.30 🟡 WATCH · 1.05-1.15 🟠 ALERTA
#   1.00-1.05 🔴 CRÍTICO · <1.00 ⛔ LIQUIDABLE.
def risk_tier(aave_hf: float, *, has_debt: bool) -> tuple[str, str]:
    """Map an aave-style health factor to (emoji, label). NEVER raises."""
    if not has_debt:
        return "🟢", "SIN DEUDA"
    try:
        h = float(aave_hf)
    except (TypeError, ValueError):
        return "🟢", "SIN DATOS"
    if h <= 0:
        return "🟢", "SIN DATOS"
    if h < 1.00:
        return "⛔", "LIQUIDABLE"
    if h < 1.05:
        return "🔴", "CRÍTICO"
    if h < 1.15:
        return "🟠", "ALERTA"
    if h < 1.30:
        return "🟡", "WATCH"
    return "🟢", "SALUDABLE"


def compute_pm_risk_metrics(
    breakdown: dict[str, float],
    debt: float,
    hype_qty: float,
    hype_px: float,
    *,
    ltv_map: dict[str, float] | None = None,
    perp_cross_mm: float = 0.0,
    min_borrow_offset: float = 20.0,
) -> dict[str, float]:
    """Pure PM risk math. Generic multi-collateral support.

    ``breakdown`` is ``{COIN: usd_value}`` of PM-eligible collateral at oracle.
    Returns the borrow capacity (Σ value×ltv), the liquidation-weighted
    collateral (Σ value×liq_threshold), the aave-style HF, the app-style HF
    (capacity/debt), current LTV, the REAL HYPE maintenance liquidation price
    (others held constant) and the % price buffer. NEVER raises.
    """
    ltv_map = {k.upper(): v for k, v in (ltv_map or {}).items()}
    total_value = 0.0
    borrow_capacity = 0.0
    liq_weighted = 0.0
    hype_liq_threshold = _liq_threshold_for_ltv(ltv_map.get("HYPE", PM_HYPE_LTV))
    hype_value_at_oracle = 0.0
    for coin, value in (breakdown or {}).items():
        v = _f(value)
        if v <= 0:
            continue
        ltv = _f(ltv_map.get(coin.upper(), PM_HYPE_LTV)) or PM_HYPE_LTV
        lt = _liq_threshold_for_ltv(ltv)
        total_value += v
        borrow_capacity += v * ltv
        liq_weighted += v * lt
        if coin.upper() == "HYPE":
            hype_value_at_oracle += v

    debt = _f(debt)
    liability = debt + _f(perp_cross_mm)
    hf_app = (borrow_capacity / debt) if debt > 0 else 0.0
    aave_hf = (liq_weighted / liability) if liability > 0 else 0.0
    current_ltv = (debt / total_value) if total_value > 0 else 0.0

    # REAL HYPE maintenance liquidation price: hold every other collateral
    # constant and solve for the HYPE oracle px at which the liquidation-weighted
    # collateral equals the liability (+ the 20-USDC min-borrow offset).
    other_liq = liq_weighted - hype_value_at_oracle * hype_liq_threshold
    liq_price = 0.0
    if debt > 0 and hype_qty > 0 and hype_liq_threshold > 0:
        target = (liability + _f(min_borrow_offset)) - other_liq
        if target > 0:
            liq_price = target / (hype_liq_threshold * hype_qty)
    buffer_pct = 0.0
    if liq_price > 0 and hype_px > 0:
        buffer_pct = max(0.0, (hype_px - liq_price) / hype_px * 100.0)

    return {
        "borrow_capacity": borrow_capacity,
        "liq_weighted": liq_weighted,
        "hf_app": hf_app,
        "aave_hf": aave_hf,
        "current_ltv": current_ltv,
        "liq_price": liq_price,
        "price_buffer_pct": buffer_pct,
        "max_ltv": _f(ltv_map.get("HYPE", PM_HYPE_LTV)) or PM_HYPE_LTV,
        "liq_threshold": hype_liq_threshold,
    }


def _committed_resting_notional(
    open_orders: list[dict[str, Any]] | None,
) -> tuple[float, int]:
    """Sum the USD notional reserved by resting BUY limit orders.

    P1.5: a resting limit BUY (non-trigger, non-reduce-only) reserves
    capital in HL the moment it is loaded — it is NOT free head-room. SL/TP
    and other reduce-only / trigger orders do NOT commit new capital and are
    excluded. Notional = ``limit_px × size``. NEVER raises.
    """
    total = 0.0
    count = 0
    for o in open_orders or []:
        if not isinstance(o, dict):
            continue
        try:
            side = str(o.get("side") or "").upper()
            is_buy = side in ("BUY", "B")
            if not is_buy:
                continue
            if o.get("is_trigger") or o.get("reduce_only") or o.get("is_sl_tp"):
                continue
            px = float(o.get("limit_px") or o.get("limitPx") or 0.0)
            sz = float(o.get("size") or o.get("sz") or 0.0)
            ntl = px * sz
            if ntl > 0:
                total += ntl
                count += 1
        except (TypeError, ValueError):
            continue
    return total, count


def compute_pm_state(
    spot_balances: list[dict[str, Any]] | None,
    positions: list[dict[str, Any]] | None,
    prices: dict[str, float] | None = None,
    *,
    hype_px: float | None = None,
    open_orders: list[dict[str, Any]] | None = None,
    ltv_map: dict[str, float] | None = None,
    perp_cross_mm: float = 0.0,
    min_borrow_offset: float = 20.0,
) -> PMState:
    """Derive Portfolio Margin state from one wallet's spot + perp data.

    ``prices`` is an optional ``{COIN: usd}`` oracle map; when absent the HYPE
    price is fetched live via ``modules.hl_prices``. NEVER raises.
    """
    spot_balances = spot_balances or []
    positions = positions or []

    # Resolve prices: prefer the passed oracle map, fall back to live fetch.
    prices = dict(prices or {})
    if "HYPE" not in prices:
        try:
            from modules.hl_prices import get_oracle_prices
            for k, v in (get_oracle_prices() or {}).items():
                prices.setdefault(k, v)
        except Exception:  # noqa: BLE001
            pass
    if hype_px is not None and hype_px > 0:
        prices["HYPE"] = hype_px

    pm_assets = {a.upper() for a in (PM_COLLATERAL_ASSETS or ["HYPE"])}

    collateral = 0.0
    debt = 0.0
    hype_qty = 0.0
    breakdown: dict[str, float] = {}
    for sb in spot_balances:
        coin = (sb.get("coin") or "").upper()
        total = _f(sb.get("total"))
        # R-WALLET-FIX (2026-06-06): a Portfolio Margin borrow is reported with
        # a positive ``borrowed`` field (the GROSS liability, e.g. 39,808) AND
        # a smaller-magnitude negative ``total`` (the net spot USDC after the
        # borrowed dollars were swept into the perp account). The ``borrowed``
        # field is authoritative — using the net ``total`` understates the debt
        # by the swept portion (which is already inside perp accountValue), so
        # the KPI under-reports liability. Prefer ``borrowed``; fall back to the
        # negative ``total`` for older payloads that lack the field.
        if coin in _STABLES:
            borrowed = _f(sb.get("borrowed"))
            if borrowed > 0:
                debt += borrowed
            elif total < 0:
                debt += abs(total)  # stables are ~1:1 USD
            continue
        if coin in pm_assets:
            lookup = coin[1:] if coin.startswith("K") and len(coin) > 1 else coin
            px = prices.get(lookup) or prices.get(coin) or 0.0
            val = total * float(px or 0.0)
            if val > 0:
                collateral += val
                breakdown[coin] = breakdown.get(coin, 0.0) + val
            if coin == "HYPE":
                hype_qty += total

    shorts_notional = 0.0
    for p in positions:
        try:
            sz = float(p.get("size") or p.get("szi") or 0.0)
        except (TypeError, ValueError):
            sz = 0.0
        try:
            ntl = abs(float(p.get("notional_usd") or p.get("positionValue") or 0.0))
        except (TypeError, ValueError):
            ntl = 0.0
        if sz < 0:
            shorts_notional += ntl

    capacity = PM_HYPE_LTV * collateral
    available = capacity - debt
    ratio = (debt / capacity) if capacity > 0 else 0.0
    status = _classify(ratio)
    naked_long = debt > 1.0 and shorts_notional < 1.0
    committed_usd, committed_n = _committed_resting_notional(open_orders)

    # R-WALLET-FIX: HF_app (borrow utilisation) = capacity / debt = the HL app's
    # "Health Factor %". KEPT as ``health_factor`` for backward compatibility.
    health_factor = (capacity / debt) if debt > 0 else 0.0
    _hype_px = float(prices.get("HYPE") or 0.0)

    # R-PM-LIQ: the REAL liquidation price uses the MAINTENANCE threshold
    # (0.5 + 0.5×ltv), not the borrow LTV. aave_HF drives the risk band.
    metrics = compute_pm_risk_metrics(
        breakdown,
        debt,
        hype_qty,
        _hype_px,
        ltv_map=ltv_map,
        perp_cross_mm=perp_cross_mm,
        min_borrow_offset=min_borrow_offset,
    )
    liq_price = metrics["liq_price"]
    aave_hf = metrics["aave_hf"]
    current_ltv = metrics["current_ltv"]
    price_buffer_pct = metrics["price_buffer_pct"]
    max_ltv = metrics["max_ltv"]
    liq_threshold = metrics["liq_threshold"]
    risk_emoji, risk_label = risk_tier(aave_hf, has_debt=(debt > 1.0))

    return PMState(
        collateral_usd=collateral,
        debt_usd=debt,
        capacity_usd=capacity,
        available_usd=available,
        ratio=ratio,
        status=status,
        shorts_notional=shorts_notional,
        naked_long=naked_long,
        hype_qty=hype_qty,
        hype_px=_hype_px,
        collateral_breakdown=breakdown,
        has_data=(collateral > 0 or debt > 0 or len(spot_balances) > 0),
        committed_orders_usd=committed_usd,
        committed_orders_count=committed_n,
        health_factor=health_factor,
        liq_price=liq_price,
        max_ltv=max_ltv,
        liq_threshold=liq_threshold,
        aave_hf=aave_hf,
        current_ltv=current_ltv,
        price_buffer_pct=price_buffer_pct,
        risk_emoji=risk_emoji,
        risk_label=risk_label,
        perp_cross_mm=float(perp_cross_mm or 0.0),
    )


def _fmt_usd(v: float) -> str:
    av = abs(v)
    if av >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if av >= 1_000:
        return f"${v / 1_000:.1f}K"
    return f"${v:,.0f}"


_STATUS_EMOJI = {"CALM": "🟢", "WARN": "🟡", "STRESS": "🟠", "LIQ": "🔴"}


def _display_band(ratio: float) -> tuple[str, str]:
    """R-PMALERT 4-level DISPLAY label for the ratio line: CALM/WARN/STRESS/
    LIQ-RISK with the 0.85 pre-liq tier mapped to 🔴. This is the rendering
    label only; ``PMState.status`` (the R-PMCORE classifier) is unchanged and
    still flips to LIQ at 0.95. NEVER raises.
    """
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        return "🟢", "CALM"
    if r >= PM_CRITICAL_RATIO:
        return "🔴", "LIQ-RISK"
    if r >= PM_STRESS_RATIO:
        return "🟠", "STRESS"
    if r >= PM_WARN_RATIO:
        return "🟡", "WARN"
    return "🟢", "CALM"


def format_pm_state_telegram(pm: PMState) -> str:
    """Telegram block for the Portfolio Margin state. NEVER raises."""
    if pm is None or not pm.has_data or pm.collateral_usd <= 0:
        return ""
    # R-PM-RATIO-RELABEL: the HEADLINE is the aave-HF (distance-to-liquidation),
    # coloured GREEN≥1.30 / YELLOW 1.10-1.30 / RED<1.10 — NOT the borrow ratio.
    from modules.pm_panel import (
        headline_color, borrow_utilization_status, explainer_line,
    )
    has_debt = pm.debt_usd > 1.0
    head_emoji = headline_color(pm.aave_hf, has_debt=has_debt)
    head_label = f" {head_emoji} {pm.risk_label}" if has_debt else ""
    lines = [
        f"⚖️ PORTFOLIO MARGIN{head_label} (cuenta primaria — HYPE como colateral cross)"
    ]
    hype_line = f"├─ Colateral: {_fmt_usd(pm.collateral_usd)}"
    if pm.hype_qty > 0 and pm.hype_px > 0:
        hype_line += f"  ({pm.hype_qty:,.1f} HYPE × ${pm.hype_px:,.2f})"
    lines.append(hype_line)
    lines.append(f"├─ Deuda (USDC/USDH borrowed): {_fmt_usd(pm.debt_usd)}")
    lines.append(
        f"├─ Capacidad borrow (LTV {PM_HYPE_LTV:.2f}): {_fmt_usd(pm.capacity_usd)}"
        f"  | head-room borrow: {_fmt_usd(pm.available_usd)}"
    )
    # P1.5: resting limit orders reserve capital — never present head-room as
    # freely deployable "free capital" without showing what's already
    # committed to the ladder.
    if pm.committed_orders_usd > 0:
        lines.append(
            f"├─ Capital comprometido en órdenes resting: "
            f"{_fmt_usd(pm.committed_orders_usd)} ({pm.committed_orders_count} órdenes)"
        )
    # R-PM-RATIO-RELABEL: the borrow ratio is UTILIZATION of the max-borrow cap,
    # NOT a liquidation signal. Rename it, drop the WARN/STRESS/CRÍTICO/LIQ
    # scale, and use non-liquidation status labels (never red, never LIQ-RISK).
    max_borrow_pct = (pm.max_ltv or PM_HYPE_LTV) * 100.0
    util_pct = pm.ratio * 100.0
    util_label, _util_red = borrow_utilization_status(util_pct)
    lines.append(
        f"├─ Borrow utilization (vs {max_borrow_pct:.0f}% max-borrow): "
        f"{util_pct:.1f}%  — {util_label}"
    )
    # R-PM-LIQ: the RISK metric is the aave-style Health factor (uses the
    # MAINTENANCE liq threshold 0.5+0.5×ltv), NOT the borrow utilisation. Surface
    # both framings unambiguously, plus the real liquidation price + buffer.
    if pm.debt_usd > 1.0:
        lines.append(
            f"├─ Health factor (aave, liq-threshold {pm.liq_threshold:.2f}): "
            f"{pm.aave_hf:.2f} {pm.risk_emoji} {pm.risk_label}"
        )
        lines.append(
            f"├─ Utilización borrow (Earn, app HF): {pm.health_factor:.4f} "
            f"(LTV actual {pm.current_ltv*100:.1f}% · máx borrow {pm.max_ltv*100:.0f}% · "
            f"maint {pm.liq_threshold*100:.0f}%)"
        )
        if pm.perp_cross_mm > 0:
            lines.append(
                f"├─ Perp cross maint-margin (en cuenta PM): {_fmt_usd(pm.perp_cross_mm)}"
            )
        if pm.liq_price > 0:
            buf = (
                f" · buffer {pm.price_buffer_pct:.1f}%"
                if pm.price_buffer_pct > 0 else ""
            )
            lines.append(
                f"├─ Liq. price HYPE (maint-LTV {pm.liq_threshold:.2f}): "
                f"${pm.liq_price:,.2f}"
                + (f"  (oracle ${pm.hype_px:,.2f}{buf})" if pm.hype_px > 0 else "")
            )
        # Clarify that over-max-borrow only blocks NEW draws — liquidation is the
        # maintenance-LTV price, not the 100%-utilization point.
        lines.append(explainer_line(pm.liq_price))
    if pm.shorts_notional > 0:
        lines.append(
            f"└─ Hedge (shorts basket): {_fmt_usd(pm.shorts_notional)} notional "
            f"vs colateral {_fmt_usd(pm.collateral_usd)}"
        )
    else:
        lines.append("└─ Hedge (shorts basket): sin shorts abiertos")
    if pm.naked_long:
        lines.append(
            "🚨 WARNING: USDC debt vs HYPE with no shorts open — "
            "naked leveraged long, hedge missing."
        )
    return "\n".join(lines)


def pm_alert(pm: PMState) -> tuple[bool, str]:
    """R-SILENT break-silence decision for the PM ratio.

    Returns ``(should_alert, message)``. Stays SILENT (False) below WARN
    (ratio < PM_WARN_RATIO). Breaks silence at WARN, escalates language at
    STRESS/LIQ. The naked-long condition ALSO breaks silence (hedge missing
    is a hard-rule violation regardless of ratio).
    """
    if pm is None or not pm.has_data:
        return False, ""
    if pm.naked_long:
        return True, (
            "🚨 PORTFOLIO MARGIN — HEDGE MISSING\n"
            f"Deuda {_fmt_usd(pm.debt_usd)} contra HYPE {_fmt_usd(pm.collateral_usd)} "
            "SIN shorts abiertos. Naked leveraged long — falta el hedge del basket."
        )
    if pm.status == "CALM":
        return False, ""
    head = {
        "WARN": "🟡 PORTFOLIO MARGIN — WARN",
        "STRESS": "🟠 PORTFOLIO MARGIN — STRESS (reducir deuda)",
        "LIQ": "🔴 PORTFOLIO MARGIN — LIQUIDACIÓN INMINENTE",
    }.get(pm.status, "🟡 PORTFOLIO MARGIN")
    return True, (
        f"{head}\n"
        f"Margin ratio {pm.ratio*100:.1f}% (deuda {_fmt_usd(pm.debt_usd)} / "
        f"capacidad {_fmt_usd(pm.capacity_usd)}). Colateral HYPE "
        f"{_fmt_usd(pm.collateral_usd)}."
    )
