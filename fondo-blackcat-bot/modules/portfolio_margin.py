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
* ``capacity_usd``     — borrow capacity = ``max-borrow LTV × collateral``
  (R-LTV65-QUIET: PM_MAX_BORROW_LTV, default 0.65; liq math uses PM_MAINT_LTV).
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
        PM_MAINT_LTV,
        PM_WARN_RATIO,
        PM_STRESS_RATIO,
        PM_CRITICAL_RATIO,
        PM_LIQ_RATIO,
        PM_COLLATERAL_ASSETS,
    )
except Exception:  # noqa: BLE001 — importable in isolated tests
    PM_HYPE_LTV = 0.65      # R-LTV65-QUIET: HL max-borrow LTV HYPE 0.50 → 0.65
    PM_MAINT_LTV = None     # R-UNIFIED-LIQ: optional manual override ONLY
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
    # R-PM-MARGIN-MODE-FIX (2026-06-07): per-leg margin-mode awareness. The
    # basket is MIXED MARGIN — some legs are CROSS (share this PM pool, fold
    # into ``perp_cross_mm`` and the cross liq math) and some are ISOLATED
    # (walled off, their own margin + own liq price, NEVER in the cross-pool
    # math). ``shorts_notional`` stays the TOTAL hedge (cross + isolated); the
    # split below annotates which portion touches the pool. ``isolated_positions``
    # carries the per-leg ISOLATED report structs (modules.margin_mode.IsolatedLeg).
    cross_shorts_notional: float = 0.0
    isolated_shorts_notional: float = 0.0
    cross_perp_count: int = 0
    isolated_perp_count: int = 0
    isolated_positions: tuple = ()
    # R-BOT-DEFINITIVE WI-7 (2026-06-10) — OUTPUTS ONLY (no math change): the
    # HYPE oracle price at which the aave-HF crosses each observation band,
    # computed on the SAME debt + perp cross maint-margin basis as ``aave_hf``
    # (other collateral held constant). Solves
    #   aave_HF(px) = (other_liq + px × hype_qty × liq_threshold) / liability
    # for px at HF targets 1.30 / 1.20 / 1.10. 0.0 when no debt / no HYPE.
    # These are the ONLY threshold prices the narrative may use — the LLM is
    # forbidden from deriving its own observation zones.
    hype_price_at_hf_130: float = 0.0
    hype_price_at_hf_120: float = 0.0
    hype_price_at_hf_110: float = 0.0
    # R-BOT-DEFINITIVE-2 T1 (2026-07-02): HL PM liquidation TRIGGERS at
    # portfolio_margin_ratio > 0.95, i.e. BEFORE the nominal maintenance point.
    # Effective liquidation LTV = 0.95 × liq_threshold (0.7125 for HYPE 0.75).
    #   * ``liq_price_real``       — HYPE px where ratio hits 0.95 (the REAL
    #                                 liquidation price; higher than nominal).
    #   * ``price_buffer_real_pct`` — % HYPE can fall before REAL liquidation.
    #   * ``hf_at_real_liq``       — aave_HF at the real trigger = 1/0.95 ≈ 1.053.
    # ``liq_price`` above stays the NOMINAL (0.75, HF=1.0) maintenance price.
    liq_price_real: float = 0.0
    price_buffer_real_pct: float = 0.0
    hf_at_real_liq: float = 0.0
    # R-UNIFIED-LIQ (2026-08-11): official HL PM borrow-liquidation model.
    #   * ``liq_price``/``liq_price_real`` — PARTIAL liq price (threshold =
    #     LTV + (1−LTV)/2; fires repeatedly every 3s until healthy).
    #   * ``liq_price_full``  — FULL liq price (LTV + (1−LTV)×2/3; takes all).
    #   * ``full_factor``     — the full-liq threshold factor for HYPE.
    #   * ladder rebuilt from the partial price: observation ×1.20, action
    #     ×1.10, conservative early-warning = partial/0.95 (5% threshold
    #     haircut, fires ABOVE the partial price).
    liq_price_full: float = 0.0
    price_buffer_full_pct: float = 0.0
    full_factor: float = 0.0
    liq_early_price: float = 0.0
    liq_action_price: float = 0.0
    liq_obs_price: float = 0.0


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


def _clean_ltv(ltv: Any) -> float:
    """Sanitise a borrow LTV to (0, 1); fall back to PM_HYPE_LTV/0.65."""
    try:
        v = float(ltv)
        if 0.0 < v < 1.0:
            return v
    except (TypeError, ValueError):
        pass
    try:
        d = float(PM_HYPE_LTV)
        return d if 0.0 < d < 1.0 else 0.65
    except (TypeError, ValueError):
        return 0.65


def borrow_partial_liq_factor(ltv: float) -> float:
    """R-UNIFIED-LIQ — official HL PM PARTIAL borrow-liquidation factor.

    ``LTV + (1 − LTV) × 1/2``. Partial liquidation fires when
    ``debt ≥ collateral_value × factor`` and REPEATS every 3s (with slippage)
    until the account is healthy again. LTV 0.65 → 0.825; LTV 0.50 → 0.75
    (matches the legacy app value — backward consistency). NEVER raises.
    """
    v = _clean_ltv(ltv)
    return v + (1.0 - v) * 0.5


def borrow_full_liq_factor(ltv: float) -> float:
    """R-UNIFIED-LIQ — official HL PM FULL borrow-liquidation factor.

    ``LTV + (1 − LTV) × 2/3``. Full liquidation takes EVERYTHING.
    LTV 0.65 → 0.88333; LTV 0.50 → 0.8333. NEVER raises.
    """
    v = _clean_ltv(ltv)
    return v + (1.0 - v) * (2.0 / 3.0)


def _liq_threshold_for_ltv(
    ltv: float, maint_override: float | None = None
) -> float:
    """HL Portfolio Margin PARTIAL borrow-liquidation threshold for an asset.

    R-UNIFIED-LIQ (2026-08-11): replaces the flat maint-LTV model. The
    threshold DERIVES from the borrow LTV via the official HL formula
    ``LTV + (1 − LTV)/2`` (docs: support/faq/portfolio-margin). Priority:
      1. ``maint_override`` — a LIVE per-asset maintenance value from the HL
         borrow-lend API, when the API reports one.
      2. ``PM_MAINT_LTV`` env — OPTIONAL manual override (None/unset = skip).
      3. The formula (default path).
    NEVER raises.
    """
    try:
        if maint_override is not None:
            mo = float(maint_override)
            if 0.0 < mo < 1.0:
                return mo
    except (TypeError, ValueError):
        pass
    try:
        if PM_MAINT_LTV is not None:
            m = float(PM_MAINT_LTV)
            if 0.0 < m < 1.0:
                return m
    except (TypeError, ValueError):
        pass
    return borrow_partial_liq_factor(ltv)


def compute_borrow_liq_ladder(
    debt: float,
    hype_qty: float,
    ltv: float,
    *,
    maint_override: float | None = None,
) -> dict[str, float]:
    """Pure single-collateral R-UNIFIED-LIQ price ladder (no offsets).

    From the official HL PM borrow-liquidation model:
      * ``partial_price``  = debt / (qty × partial_factor) — partial liq starts
        here and repeats every 3s until healthy.
      * ``full_price``     = debt / (qty × full_factor) — full liq, takes all.
      * ``early_price``    = partial_price / 0.95 — CONSERVATIVE early-warning
        (5% haircut on the partial threshold; fires ABOVE the partial price).
      * ``action_price``   = partial_price × 1.10.
      * ``observation_price`` = partial_price × 1.20.
    Aug-11 snapshot pin (debt 91.6K · 3,006.28 HYPE · LTV 0.65):
    PARTIAL 36.93 · FULL 34.49 · early 38.87 · action 40.62 · obs 44.32.
    All 0.0 when inputs are degenerate. NEVER raises.
    """
    d = _f(debt)
    q = _f(hype_qty)
    pf = _liq_threshold_for_ltv(ltv, maint_override)
    ff = borrow_full_liq_factor(ltv)
    if d <= 0 or q <= 0 or pf <= 0 or ff <= 0:
        return {
            "partial_factor": pf, "full_factor": ff,
            "partial_price": 0.0, "full_price": 0.0,
            "early_price": 0.0, "action_price": 0.0, "observation_price": 0.0,
        }
    partial = d / (q * pf)
    return {
        "partial_factor": pf,
        "full_factor": ff,
        "partial_price": partial,
        "full_price": d / (q * ff),
        "early_price": partial / 0.95,
        "action_price": partial * 1.10,
        "observation_price": partial * 1.20,
    }


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

    # R-UNIFIED-LIQ (2026-08-11) — official HL PM borrow-liquidation model
    # replaces the 0.95-trigger heuristic. The PARTIAL threshold (``liq_price``
    # above, solved on the partial factors) is where liquidation ACTUALLY
    # starts (repeats every 3s until healthy); the FULL threshold (factor
    # ``LTV + (1−LTV)×2/3``) is where HL takes everything. ``liq_price_real``
    # is kept as a backward-compat ALIAS of the partial price (the "real"
    # liquidation start); the aave-HF at that point is exactly 1.0.
    full_weighted = 0.0
    hype_full_factor = borrow_full_liq_factor(ltv_map.get("HYPE", PM_HYPE_LTV))
    for coin, value in (breakdown or {}).items():
        v = _f(value)
        if v <= 0:
            continue
        c_ltv = _f(ltv_map.get(coin.upper(), PM_HYPE_LTV)) or PM_HYPE_LTV
        full_weighted += v * borrow_full_liq_factor(c_ltv)
    other_full = full_weighted - hype_value_at_oracle * hype_full_factor
    liq_price_full = 0.0
    if debt > 0 and hype_qty > 0 and hype_full_factor > 0:
        target_full = (liability + _f(min_borrow_offset)) - other_full
        if target_full > 0:
            liq_price_full = target_full / (hype_full_factor * hype_qty)
    buffer_full_pct = 0.0
    if liq_price_full > 0 and hype_px > 0:
        buffer_full_pct = max(0.0, (hype_px - liq_price_full) / hype_px * 100.0)

    liq_price_real = liq_price
    buffer_real_pct = buffer_pct
    hf_at_real_liq = 1.0

    # R-UNIFIED-LIQ ladder rebuilt FROM the partial price: observation ×1.20,
    # action ×1.10, conservative early-warning = partial threshold with a 5%
    # haircut (→ price / 0.95, fires ABOVE the partial price).
    liq_early_price = (liq_price / 0.95) if liq_price > 0 else 0.0
    liq_action_price = liq_price * 1.10
    liq_obs_price = liq_price * 1.20

    # R-BOT-DEFINITIVE WI-7 — HYPE price at aave-HF thresholds (OUTPUT ONLY,
    # same liability basis as aave_hf: debt + perp cross maint margin; other
    # collateral held constant). px(HF=h) = (h×liability − other_liq) /
    # (hype_qty × liq_threshold).
    def _px_at_hf(h: float) -> float:
        if liability <= 0 or hype_qty <= 0 or hype_liq_threshold <= 0:
            return 0.0
        target = h * liability - other_liq
        return (target / (hype_liq_threshold * hype_qty)) if target > 0 else 0.0

    return {
        "borrow_capacity": borrow_capacity,
        "liq_weighted": liq_weighted,
        "hf_app": hf_app,
        "aave_hf": aave_hf,
        "current_ltv": current_ltv,
        "liq_price": liq_price,
        "price_buffer_pct": buffer_pct,
        "liq_price_real": liq_price_real,
        "price_buffer_real_pct": buffer_real_pct,
        "hf_at_real_liq": hf_at_real_liq,
        "liq_price_full": liq_price_full,
        "price_buffer_full_pct": buffer_full_pct,
        "full_factor": hype_full_factor,
        "liq_early_price": liq_early_price,
        "liq_action_price": liq_action_price,
        "liq_obs_price": liq_obs_price,
        "max_ltv": _f(ltv_map.get("HYPE", PM_HYPE_LTV)) or PM_HYPE_LTV,
        "liq_threshold": hype_liq_threshold,
        "hype_price_at_hf_130": _px_at_hf(1.30),
        "hype_price_at_hf_120": _px_at_hf(1.20),
        "hype_price_at_hf_110": _px_at_hf(1.10),
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
    perp_cross_mm: float | None = None,
    min_borrow_offset: float = 20.0,
) -> PMState:
    """Derive Portfolio Margin state from one wallet's spot + perp data.

    ``prices`` is an optional ``{COIN: usd}`` oracle map; when absent the HYPE
    price is fetched live via ``modules.hl_prices``. NEVER raises.

    R-PM-MARGIN-MODE-FIX: the perp basket is MIXED MARGIN. Only CROSS legs share
    this PM pool; ISOLATED legs are walled off. ``perp_cross_mm`` is the cross
    perp maintenance margin folded into the shared liability:
      * pass an explicit value (e.g. the HL account-level
        ``crossMaintenanceMarginUsed``, which is cross-only by definition) and it
        is honoured exactly; or
      * leave it ``None`` (default) and it is DERIVED from the CROSS legs'
        per-leg maintenance margin (isolated legs never contribute). Fieldless
        synthetic positions derive 0.0, so prior callers are unchanged.
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

    # R-PM-MARGIN-MODE-FIX: split the basket by margin mode (read live, never
    # hardcoded). Only the CROSS legs feed the shared-pool maintenance margin;
    # the ISOLATED legs are walled off and reported separately. The hedge
    # (``shorts_notional``) still counts BOTH so the naked-long guard sees the
    # full hedge, but we surface the cross/isolated split for the framing.
    cross_legs: list[dict[str, Any]] = []
    isolated_legs_struct: tuple = ()
    cross_shorts = 0.0
    isolated_shorts = 0.0
    isolated_count = 0
    cross_count = 0
    try:
        from modules.margin_mode import (
            split_legs as _split_legs,
            build_isolated_legs as _build_iso,
            cross_perp_maint_margin as _cross_mm,
            shorts_notional_split as _shorts_split,
        )
        cross_legs, _iso_raw = _split_legs(positions)
        cross_count = len(cross_legs)
        isolated_count = len(_iso_raw)
        isolated_legs_struct = tuple(_build_iso(positions, prices))
        cross_shorts, isolated_shorts = _shorts_split(positions)
        _derived_cross_mm = _cross_mm(positions)
    except Exception:  # noqa: BLE001 — margin-mode split is best-effort
        _derived_cross_mm = 0.0

    # Effective cross perp maintenance margin folded into the shared liability:
    # honour an explicit value; otherwise derive from the CROSS legs only.
    if perp_cross_mm is None:
        effective_cross_mm = _derived_cross_mm
    else:
        effective_cross_mm = _f(perp_cross_mm)

    naked_long = debt > 1.0 and shorts_notional < 1.0
    committed_usd, committed_n = _committed_resting_notional(open_orders)
    _hype_px = float(prices.get("HYPE") or 0.0)

    # R-LTV65-QUIET: capacity comes from the ltv-map-aware borrow_capacity
    # (Σ value × max-borrow LTV, per-coin live map with PM_MAX_BORROW_LTV
    # fallback). Liquidation math is SEPARATE — it uses PM_MAINT_LTV (0.75),
    # never the borrow LTV. Metrics are computed FIRST so capacity/headroom/
    # utilization all derive from the same LTV basis.
    metrics = compute_pm_risk_metrics(
        breakdown,
        debt,
        hype_qty,
        _hype_px,
        ltv_map=ltv_map,
        perp_cross_mm=effective_cross_mm,
        min_borrow_offset=min_borrow_offset,
    )
    capacity = metrics.get("borrow_capacity", 0.0) or (PM_HYPE_LTV * collateral)
    available = capacity - debt
    ratio = (debt / capacity) if capacity > 0 else 0.0
    status = _classify(ratio)

    # R-WALLET-FIX: HF_app (borrow utilisation) = capacity / debt = the HL app's
    # "Health Factor %". KEPT as ``health_factor`` for backward compatibility.
    health_factor = (capacity / debt) if debt > 0 else 0.0
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
        perp_cross_mm=float(effective_cross_mm or 0.0),
        cross_shorts_notional=cross_shorts,
        isolated_shorts_notional=isolated_shorts,
        cross_perp_count=cross_count,
        isolated_perp_count=isolated_count,
        isolated_positions=isolated_legs_struct,
        hype_price_at_hf_130=metrics.get("hype_price_at_hf_130", 0.0),
        hype_price_at_hf_120=metrics.get("hype_price_at_hf_120", 0.0),
        hype_price_at_hf_110=metrics.get("hype_price_at_hf_110", 0.0),
        liq_price_real=metrics.get("liq_price_real", 0.0),
        price_buffer_real_pct=metrics.get("price_buffer_real_pct", 0.0),
        hf_at_real_liq=metrics.get("hf_at_real_liq", 0.0),
        liq_price_full=metrics.get("liq_price_full", 0.0),
        price_buffer_full_pct=metrics.get("price_buffer_full_pct", 0.0),
        full_factor=metrics.get("full_factor", 0.0),
        liq_early_price=metrics.get("liq_early_price", 0.0),
        liq_action_price=metrics.get("liq_action_price", 0.0),
        liq_obs_price=metrics.get("liq_obs_price", 0.0),
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


def format_pm_state_telegram(
    pm: PMState,
    *,
    perp_cross_util_pct: float | None = None,
    perp_cross_count: int = 0,
) -> str:
    """Telegram block for the Portfolio Margin state. NEVER raises.

    R-NOISE-CUT (2026-06-16): ``perp_cross_util_pct``/``perp_cross_count`` carry
    the perp-cross-margin utilization that used to be a recurring MARGIN STRESS
    push. It is now surfaced here as a single INFORMATIONAL line (only when
    cross perp legs exist and the ratio is available) — at/over 100% it blocks
    opening NEW perp legs, but it is NOT liquidation proximity. Both default to
    no-op so existing callers render unchanged.
    """
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
        f"├─ Capacidad borrow (LTV {(pm.max_ltv or PM_HYPE_LTV):.2f}): {_fmt_usd(pm.capacity_usd)}"
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
    # official PARTIAL liq threshold LTV+(1−LTV)/2, NOT the raw borrow LTV). Surface
    # both framings unambiguously, plus the real liquidation price + buffer.
    if pm.debt_usd > 1.0:
        lines.append(
            f"├─ Health factor (aave, liq-threshold {pm.liq_threshold:.2f}): "
            f"{pm.aave_hf:.2f} {pm.risk_emoji} {pm.risk_label}"
        )
        lines.append(
            f"├─ Utilización borrow (Earn, app HF): {pm.health_factor:.4f} "
            f"(LTV actual {pm.current_ltv*100:.1f}% · máx borrow {pm.max_ltv*100:.0f}% · "
            f"liq parcial {pm.liq_threshold*100:.1f}%)"
        )
        if pm.perp_cross_mm > 0:
            lines.append(
                f"├─ Perp cross maint-margin (en cuenta PM): {_fmt_usd(pm.perp_cross_mm)}"
            )
        # R-UNIFIED-LIQ: BOTH official HL PM borrow-liquidation lines, with
        # buffers. PARTIAL = LTV + (1−LTV)/2 (repeats every 3s until healthy);
        # FULL = LTV + (1−LTV)×2/3 (takes everything).
        if pm.liq_price > 0:
            buf_p = (
                f" · buffer {pm.price_buffer_pct:.1f}%"
                if pm.price_buffer_pct > 0 else ""
            )
            lines.append(
                f"├─ LIQ PARCIAL (factor {pm.liq_threshold:.4f} = LTV+(1−LTV)/2): "
                f"${pm.liq_price:,.2f}"
                + (f"  (oracle ${pm.hype_px:,.2f}{buf_p})" if pm.hype_px > 0 else "")
                + " — repite c/3s hasta sano"
            )
        if pm.liq_price_full > 0:
            buf_f = (
                f" · buffer {pm.price_buffer_full_pct:.1f}%"
                if pm.price_buffer_full_pct > 0 else ""
            )
            lines.append(
                f"├─ LIQ TOTAL (factor {pm.full_factor:.4f} = LTV+(1−LTV)×2/3): "
                f"${pm.liq_price_full:,.2f}{buf_f} — se lleva TODO"
            )
        # R-UNIFIED-LIQ ladder (rebuilt from the PARTIAL price): observation
        # ×1.20 | action ×1.10 | conservative early-warning (partial/0.95).
        if pm.liq_obs_price > 0 or pm.liq_action_price > 0:
            lines.append(
                f"├─ Umbrales HYPE: ${pm.liq_obs_price:,.2f} (observación, parcial×1.20) | "
                f"${pm.liq_action_price:,.2f} (acción, parcial×1.10) | "
                f"${pm.liq_early_price:,.2f} (early-warning conservador, umbral −5%) | "
                f"liq parcial ${pm.liq_price:,.2f}"
            )
        # Clarify that over-max-borrow only blocks NEW draws — liquidation is the
        # real-trigger price (ratio > 0.95), not the 100%-utilization point.
        lines.append(explainer_line(
            pm.liq_price_real if pm.liq_price_real > 0 else pm.liq_price
        ))
    # R-NOISE-CUT (2026-06-16): perp cross utilization, INFORMATIONAL only (the
    # ex-MARGIN-STRESS datum). Rendered only when cross perp legs exist and the
    # ratio is available; at/over 100% it blocks opening NEW perp legs and is
    # NOT a liquidation signal (the aave-HF above governs liquidation risk).
    if perp_cross_count and perp_cross_count > 0 and perp_cross_util_pct is not None:
        try:
            from modules.alerts_margin import format_perp_cross_util_line
            lines.append(format_perp_cross_util_line(float(perp_cross_util_pct)))
        except Exception:  # noqa: BLE001 — panel must never break on this line
            pass
    # R-PM-MARGIN-MODE-FIX: the hedge framing still shows TOTAL short notional
    # (cross + isolated) as the macro hedge of the leveraged HYPE long, but it
    # annotates which portion shares the cross PM pool vs which is walled off.
    iso_legs = pm.isolated_positions or ()
    if pm.shorts_notional > 0:
        hedge_lead = "├─" if iso_legs else "└─"
        split_note = ""
        if pm.isolated_shorts_notional > 0:
            split_note = (
                f" [cross {_fmt_usd(pm.cross_shorts_notional)} en pool · "
                f"isolated {_fmt_usd(pm.isolated_shorts_notional)} walled-off]"
            )
        lines.append(
            f"{hedge_lead} Hedge (shorts basket): {_fmt_usd(pm.shorts_notional)} notional "
            f"vs colateral {_fmt_usd(pm.collateral_usd)}{split_note}"
        )
    else:
        lines.append("└─ Hedge (shorts basket): sin shorts abiertos")

    # ── ISOLATED POSITIONS subsection (walled off from the HYPE cross pool) ──
    if iso_legs:
        lines.append(
            "🧱 ISOLATED POSITIONS (margin walled-off — NO tocan el pool cross "
            "ni el liq price del colateral HYPE):"
        )
        n = len(iso_legs)
        for i, leg in enumerate(iso_legs):
            lead = "   └─" if i == n - 1 else "   ├─"
            upnl_sign = "+" if leg.upnl >= 0 else "−"
            parts = [
                f"{leg.side} {leg.coin}",
                f"notional {_fmt_usd(leg.notional_usd)}",
            ]
            if leg.entry_px > 0:
                parts.append(f"entry ${leg.entry_px:,.2f}")
            if leg.mark_px > 0:
                parts.append(f"mark ${leg.mark_px:,.2f}")
            if leg.isolated_margin > 0:
                parts.append(f"iso-margin {_fmt_usd(leg.isolated_margin)}")
            if leg.liq_px > 0:
                liq_bit = f"liq ${leg.liq_px:,.2f}"
                if leg.distance_to_liq_pct > 0:
                    liq_bit += f" ({leg.distance_to_liq_pct:.1f}% away)"
                parts.append(liq_bit)
            parts.append(f"UPnL {upnl_sign}{_fmt_usd(abs(leg.upnl))}")
            lines.append(f"{lead} " + " · ".join(parts))

    # R-BOT-DEFINITIVE-2 T7: neutral one-liner — the leveraged long without an
    # in-wallet hedge is a KNOWN, owner-approved structure, not an alarm. No
    # siren, no imperative. HF/liq threshold alerts above are untouched.
    if pm.naked_long:
        lines.append(
            "Estructura: long apalancado sin hedge activo (decisión del owner)"
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
