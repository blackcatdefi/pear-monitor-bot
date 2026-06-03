"""R-REPORTE-LIVE (2026-06-03) — Position classifier for /reporte.

Tags every OPEN perp position by its REAL on-chain structure BEFORE any
"acción sugerida" is written, so the analysis layer never recommends closing
a cycle-accumulation DCA leg on bearish-environment grounds.

Two buckets, derived per-run from fetched position + open-order data (no
stored labels, no market-environment heuristics):

  • CYCLE_ACCUMULATION — an ISOLATED-margin perp with NO SL/TP attached AND
    laddered limit orders stacked on the accumulation side (below price for a
    LONG, above price for a SHORT). The drawdown IS the thesis. NEVER suggest
    closing/reducing on bearish / capitulation / CVD / downtrend grounds.
    Only flag when (a) liq distance compresses < CYCLE_LIQ_COMPRESS_PCT, or
    (b) funding turns materially costly. Report: distance to liq, whether the
    next lower laddered limit is about to fill, and vault margin top-up need
    near the lowest funded tranche.

  • TACTICAL — anything with a SL/TP attached, or no accumulation ladder, or
    part of an active basket. Normal close-on-thesis-break logic applies.

Robustness: NEVER raises. Missing order data → the position cannot be proven
to be a cycle accumulation, so it is tagged TACTICAL but flagged
``orders_unavailable=True`` so the prompt block tells the LLM to treat an
isolated, no-SL/TP long conservatively (do not suggest closing it blindly when
order visibility is degraded).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

CYCLE = "CYCLE_ACCUMULATION"
TACTICAL = "TACTICAL"

# ── Tunables (baked defaults — overridable via env, no redeploy needed) ──
def _f_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _liq_compress_pct() -> float:
    """Liq-distance threshold (%) below which a cycle leg must be flagged."""
    return _f_env("CYCLE_LIQ_COMPRESS_PCT", 8.0)


def _funding_material_apr() -> float:
    """Annualised funding (%) above which funding is 'materially costly'."""
    return _f_env("CYCLE_FUNDING_MATERIAL_APR", 50.0)


def _ladder_near_pct() -> float:
    """A laddered limit within this % of price is 'about to fill'."""
    return _f_env("CYCLE_LADDER_NEAR_PCT", 3.0)


@dataclass
class PositionTag:
    coin: str
    side: str                       # LONG | SHORT
    bucket: str                     # CYCLE_ACCUMULATION | TACTICAL
    tag_es: str                     # Spanish label shown in the report
    margin_mode: str                # isolated | cross | ?
    has_sl_tp: bool
    ladder_count: int               # laddered limits on the accumulation side
    nearest_ladder_px: float | None
    lowest_ladder_px: float | None
    liq_px: float | None
    liq_distance_pct: float | None
    funding_apr: float | None
    flags: list[str] = field(default_factory=list)
    orders_unavailable: bool = False
    notional_usd: float = 0.0
    entry_px: float | None = None
    mark_px: float | None = None


def _to_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _orders_for_coin(open_orders: list[dict[str, Any]], coin: str) -> list[dict[str, Any]]:
    c = (coin or "").upper()
    return [o for o in (open_orders or []) if (o.get("coin") or "").upper() == c]


def classify_position(
    position: dict[str, Any],
    open_orders: list[dict[str, Any]] | None,
    mark_px: float | None,
    *,
    orders_available: bool = True,
    funding_apr: float | None = None,
) -> PositionTag:
    """Classify ONE open position from its real attributes. NEVER raises."""
    coin = position.get("coin", "?")
    size = _to_float(position.get("size") or position.get("szi")) or 0.0
    side = position.get("side") or ("LONG" if size > 0 else "SHORT")
    is_long = side.upper() == "LONG"
    margin_mode = (position.get("leverage_type") or position.get("margin_mode") or "?").lower()
    liq_px = _to_float(position.get("liq_px") or position.get("liquidationPx"))
    entry_px = _to_float(position.get("entry_px"))
    notional = abs(_to_float(position.get("notional_usd") or position.get("positionValue")) or 0.0)

    coin_orders = _orders_for_coin(open_orders or [], coin)
    has_sl_tp = any(o.get("is_sl_tp") for o in coin_orders)

    # Resting (non-trigger, non-reduce-only) limit orders on the accumulation
    # side: below price for a LONG (BUY), above price for a SHORT (SELL).
    ladder_pxs: list[float] = []
    for o in coin_orders:
        if o.get("is_trigger") or o.get("reduce_only") or o.get("is_sl_tp"):
            continue
        opx = _to_float(o.get("limit_px"))
        if opx is None or opx <= 0:
            continue
        oside = (o.get("side") or "").upper()
        if mark_px and mark_px > 0:
            if is_long and oside == "BUY" and opx < mark_px:
                ladder_pxs.append(opx)
            elif (not is_long) and oside == "SELL" and opx > mark_px:
                ladder_pxs.append(opx)
        else:
            # No mark price: accept by side alone (degraded but directional).
            if is_long and oside == "BUY":
                ladder_pxs.append(opx)
            elif (not is_long) and oside == "SELL":
                ladder_pxs.append(opx)

    ladder_pxs.sort(reverse=is_long)  # for a long: highest (nearest) first
    ladder_count = len(ladder_pxs)
    nearest_ladder_px = ladder_pxs[0] if ladder_pxs else None
    lowest_ladder_px = (min(ladder_pxs) if is_long else max(ladder_pxs)) if ladder_pxs else None

    # Liquidation distance.
    liq_distance_pct: float | None = None
    if liq_px and mark_px and mark_px > 0:
        liq_distance_pct = abs(mark_px - liq_px) / mark_px * 100.0

    orders_unavailable = not orders_available

    # ── Bucket decision ──
    is_isolated = margin_mode == "isolated"
    is_cycle = is_isolated and (not has_sl_tp) and ladder_count >= 1 and orders_available

    flags: list[str] = []
    if is_cycle:
        bucket = CYCLE
        tag_es = "ACUMULACIÓN CICLO (DCA piso, NO cerrar)"
        # Only two legitimate flag conditions for a cycle leg.
        if liq_distance_pct is not None and liq_distance_pct < _liq_compress_pct():
            flags.append(
                f"⚠️ LIQ COMPRIMIDA: distancia a liq {liq_distance_pct:.1f}% "
                f"(< {_liq_compress_pct():.0f}%) — evaluar top-up de margen"
            )
        if funding_apr is not None and funding_apr > _funding_material_apr():
            flags.append(
                f"⚠️ FUNDING CARO: {funding_apr:.0f}% anual "
                f"(> {_funding_material_apr():.0f}%) — costo de mantener sube"
            )
        # Informational: is the next lower laddered limit about to fill?
        if nearest_ladder_px and mark_px and mark_px > 0:
            gap_pct = abs(mark_px - nearest_ladder_px) / mark_px * 100.0
            if gap_pct <= _ladder_near_pct():
                flags.append(
                    f"📥 Próxima tranche DCA por llenarse: ${nearest_ladder_px:,.4f} "
                    f"(a {gap_pct:.1f}% del precio)"
                )
    else:
        bucket = TACTICAL
        tag_es = "TÁCTICA (cierre por ruptura de tesis aplica)"
        if orders_unavailable and is_isolated and not has_sl_tp:
            flags.append(
                "❔ Órdenes no visibles este run — NO sugerir cierre a ciegas; "
                "podría ser ACUMULACIÓN CICLO sin confirmar"
            )

    return PositionTag(
        coin=coin,
        side=side.upper(),
        bucket=bucket,
        tag_es=tag_es,
        margin_mode=margin_mode,
        has_sl_tp=has_sl_tp,
        ladder_count=ladder_count,
        nearest_ladder_px=nearest_ladder_px,
        lowest_ladder_px=lowest_ladder_px,
        liq_px=liq_px,
        liq_distance_pct=liq_distance_pct,
        funding_apr=funding_apr,
        flags=flags,
        orders_unavailable=orders_unavailable,
        notional_usd=notional,
        entry_px=entry_px,
        mark_px=mark_px,
    )


def _price_for(coin: str, prices: dict[str, Any] | None) -> float | None:
    if not prices:
        return None
    c = (coin or "").upper()
    v = prices.get(c)
    if isinstance(v, dict):
        v = v.get("price")
    return _to_float(v)


def _build_price_map(market: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(market, dict):
        return {}
    data = market.get("data") or {}
    if isinstance(data, dict) and data:
        return data
    return market.get("prices") or {}


def classify_portfolio(
    portfolio: list[dict[str, Any]] | None,
    market: dict[str, Any] | None = None,
) -> list[PositionTag]:
    """Classify every open position across all fund wallets. NEVER raises."""
    if os.getenv("POSITION_CLASSIFIER_ENABLED", "true").lower() != "true":
        return []
    out: list[PositionTag] = []
    prices = _build_price_map(market)
    for w in portfolio or []:
        if not isinstance(w, dict) or w.get("status") != "ok":
            continue
        d = w.get("data") or {}
        # Orders availability: a freshly-fetched OK wallet has the key; a
        # stale-cache fallback (older cache) may not — treat absent as
        # unavailable so we never falsely "confirm" a ladder from nothing.
        orders_available = ("open_orders" in d) and not w.get("stale")
        open_orders = d.get("open_orders") or []
        for p in d.get("positions") or []:
            try:
                mark = _price_for(p.get("coin", ""), prices)
                tag = classify_position(
                    p, open_orders, mark, orders_available=orders_available
                )
                out.append(tag)
            except Exception:  # noqa: BLE001
                log.exception("classify_position failed for %r", p.get("coin"))
    return out


def cycle_coins(tags: list[PositionTag]) -> set[str]:
    """Set of coins (upper) currently tagged CYCLE_ACCUMULATION."""
    return {t.coin.upper() for t in tags if t.bucket == CYCLE}


def build_classification_block(tags: list[PositionTag]) -> str:
    """Machine-readable Spanish block for LLM injection + report rendering.

    Returns "" when there are no open positions to classify (so /reporte
    doesn't show an empty section).
    """
    if not tags:
        return ""
    lines = [
        "═══════ CLASIFICACIÓN DE POSICIONES (on-chain, este run) ═══════",
        "Cada posición está tageada por su ESTRUCTURA REAL (margin mode, "
        "SL/TP, órdenes límite escalonadas), NO por el entorno de mercado.",
        "REGLA DURA: para las tageadas ACUMULACIÓN CICLO, NUNCA sugerir "
        "cerrar/reducir por entorno bearish / capitulación / CVD / downtrend. "
        "El drawdown ES la tesis. Solo flaggear si la distancia a liq se "
        "comprime o el funding se vuelve caro.",
        "",
    ]
    for t in tags:
        icon = "🟢" if t.bucket == CYCLE else "⚙️"
        head = f"{icon} {t.side} {t.coin} — {t.tag_es}"
        lines.append(head)
        detail = (
            f"   margin={t.margin_mode} · SL/TP={'sí' if t.has_sl_tp else 'no'} "
            f"· ladder={t.ladder_count}"
        )
        if t.notional_usd:
            detail += f" · notional=${t.notional_usd:,.0f}"
        lines.append(detail)
        if t.bucket == CYCLE:
            if t.liq_distance_pct is not None:
                lines.append(f"   distancia a liq: {t.liq_distance_pct:.1f}%")
            if t.lowest_ladder_px:
                lines.append(
                    f"   tranche DCA más baja fondeada: ${t.lowest_ladder_px:,.4f}"
                )
        for fl in t.flags:
            lines.append(f"   {fl}")
    lines.append("═══════ FIN CLASIFICACIÓN DE POSICIONES ═══════")
    lines.append("")
    return "\n".join(lines)
