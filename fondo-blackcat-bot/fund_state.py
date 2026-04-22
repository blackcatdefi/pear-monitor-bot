"""Fondo Black Cat — manually-curated fund state.

Constants that live outside env vars because they describe operational state
that Claude/Sonnet must know about when producing reports but cannot derive
from on-chain data alone. Update this file by hand when positions change
(trade del ciclo platform/entry, basket open/close, etc.).

Imported by templates/system_prompt.py to inject authoritative context into
the LLM prompt, and by modules/alerts.py / templates/formatters.py for
presentation-time decisions.
"""
from __future__ import annotations

# ─── HF thresholds (duplicated here for self-contained import) ─────────────
# Actual values live in config.py — kept here as documentation for Sonnet.
HF_LIQUIDATION = 1.00  # Real HyperLend liquidation threshold
HF_CRITICAL = 1.10     # Fund operative action threshold (trigger action)
HF_WARN = 1.15         # Fund operative monitoring threshold (trigger monitoreo)

# ─── Trade del Ciclo (Blofin, managed outside bot) ─────────────────────────
# BCD edita este bloque a mano cuando el trade abre/cierra. Todas las constantes
# abajo se leen por templates/system_prompt.py + templates/formatters.py.
TRADE_DEL_CICLO_STATUS = "CLOSED"   # "OPEN" | "CLOSED" — BCD edita
TRADE_DEL_CICLO_PLATFORM = "blofin"
TRADE_DEL_CICLO_LEVERAGE = 10        # 10x leverage
TRADE_DEL_CICLO_LAST_ENTRY = 75298.70  # USD — último entry cuando estaba OPEN
TRADE_DEL_CICLO_LAST_UPDATE = "2026-04-22T15:00:00Z"
TRADE_DEL_CICLO_BLOFIN_BALANCE_USD = 2800.0  # USDT disponibles post-close (22 abr)

# Realized PnL + close timestamp (only meaningful when STATUS == "CLOSED")
TRADE_DEL_CICLO_PNL_REALIZED = 577.39     # USD consolidado 3 días
TRADE_DEL_CICLO_LAST_CLOSE = "2026-04-22T15:00:00Z"
BLOFIN_BALANCE_AVAILABLE = 2800.0         # USDT disponibles para próxima entrada

TRADE_DEL_CICLO_NOTE = (
    "Trade del Ciclo vive en Blofin (sin API pública). El bot NO lee esta "
    "posición en tiempo real. Estado actual (manual): CERRADO el "
    "2026-04-22 ~15:00 UTC con +$577.39 realizado. Copy-trading descopiado. "
    "Balance Blofin disponible ~$2,800 USDT esperando nueva entrada. "
    "NO reportar UPnL estimado cuando STATUS == 'CLOSED'. "
    "Al reabrir: BCD debe setear TRADE_DEL_CICLO_STATUS='OPEN', actualizar "
    "LAST_ENTRY, LAST_UPDATE, y vaciar LAST_CLOSE."
)

# ─── Basket SHORT status (Alt Short Bleed) ─────────────────────────────────
# Update when a new basket is opened or the current one closes.
BASKET_STATUS: dict[str, object] = {
    "active": False,
    "last_basket": "v4",
    "last_basket_result_net_usd": 290.20,  # NET +$290.20
    "last_basket_closed": "2026-04-20T22:45:00Z",
    "next_basket": "v5 pending capital",
}

# Wallets that historically held basket positions. Any dust (<$1) on these
# wallets while BASKET_STATUS["active"] is False is residual — NOT a position.
ALT_SHORT_BLEED_WALLETS = [
    "0x00bb",  # partial prefix — matched case-insensitively
    "0xc7ae",
    "0xcddf",
]

BASKET_NOTE = (
    "Las wallets de Alt Short Bleed (0x00bb, 0xc7AE, 0xcddf) están IDLE "
    "desde 2026-04-20 22:45 UTC — basket v4 se cerró con NET +$290.20. "
    "Cualquier valor spot <$1 en esas wallets es dust residual, NO posición "
    "activa ni estructura separada. v5 EN PAUSA hasta nueva orden. NO "
    "interpretar account_value=0 como 'posiciones Pear Protocol TWAP en "
    "contratos separados'."
)

# ─── Basket v5 operational plan (PENDING_CAPITAL) ──────────────────────────
# BCD edita este bloque a mano cuando el plan del v5 cambia o se deploya.
# El LLM lo lee desde templates/system_prompt.build_fund_state_block().
BASKET_V5_STATUS = "PENDING_CAPITAL"   # IDLE | PENDING_CAPITAL | DEPLOYING | ACTIVE | CLOSED
BASKET_V5_PLAN: dict[str, object] = {
    "capital_target_usdt": 3050,
    "deploy_eta": "2026-04-24 to 2026-04-28",
    "source": "blofin_withdrawal_post_bonus",
    "leverage_max": "3x",
    "notional_target_usdt": 9150,
    "logic": (
        "Hedge natural: alts caen más fuerte que HYPE en crash. Gains del "
        "basket financian DCA kHYPE o repago UETH."
    ),
    "triggers_close": [
        "BTC toca $64K–$65K (BCD BUY LIMIT)",
        "HYPE toca $28K o $24.5K (BCD BUY LIMIT)",
        "Ceasefire permanente + Hormuz reabierto físicamente",
    ],
    "bonus_blofin_unlock": "2026-04-24",  # $250 USDT libera
}

# ─── Flywheel HyperLend — by-design pair trade ─────────────────────────────
FLYWHEEL_NOTE = (
    "El flywheel HyperLend es un pair trade INTENCIONAL LONG kHYPE / SHORT "
    "ETH. La exposición direccional NO es un riesgo — es la tesis. Solo "
    "alertar si: (a) HF < 1.10, (b) UETH utilization > 90% (riesgo liquidez "
    "pool), (c) APY borrow UETH > 6% (costo del pair trade se hace "
    "insostenible). ETH outperform HYPE NO es alerta — es el caso adverso "
    "intrínseco de la estrategia, no un bug."
)

# ─── Trade classifier — distinguish Core DCA vs Basket trades ──────────────
CORE_DCA_SPOT_TOKENS = ["KHYPE", "PEAR", "BTC", "HYPE"]
BASKET_PERP_TOKENS = ["WLD", "STRK", "ZRO", "AVAX", "ENA", "EIGEN", "SCR", "ZETA"]
# Stablecoins legs of a spot fill (USDC/USDH/USDT0/DAI). If a fill is the
# stablecoin leg of a pair, it is still part of the same Core DCA transaction.
STABLE_TOKENS = {"USDC", "USDH", "USDT", "USDT0", "DAI"}


def classify_fill(fill: dict, wallet_label: str = "") -> str:
    """Return a classification label for a recent fill.

    Disambiguation: Hyperliquid fills expose a `dir` field.
      - Spot fills:  dir == "Buy" | "Sell"          (coin is "@N" or token symbol)
      - Perp fills:  dir contains "Long" | "Short"   (e.g. "Open Long", "Close Short")
    We check `dir` first so BTC/HYPE perps aren't misclassified as Core DCA spot.

    Rules (in order):
      1. Perp LONG/SHORT on basket tokens → 'Basket trade'.
      2. Perp LONG/SHORT on anything else → 'HL perp' (flywheel/hedge).
      3. Spot '@N' index or CORE_DCA_SPOT_TOKENS → 'Core DCA'.
      4. Spot stablecoin leg (USDC/USDH/USDT0/DAI) with spot-like side → 'Core DCA'.
      5. Fallback → wallet_label or '?'.
    """
    coin_raw = (fill.get("coin") or "").strip()
    coin = coin_raw.upper()
    side = (fill.get("side") or "").upper()
    dir_ = (fill.get("dir") or "").lower()
    is_perp = bool(dir_) and ("long" in dir_ or "short" in dir_)
    is_spot_like = side in ("B", "A", "BUY", "SELL") and not is_perp

    # Rule 1 + 2: perp first
    if is_perp:
        if coin in BASKET_PERP_TOKENS:
            return "Basket trade"
        return "HL perp"
    # Rule 3: HL spot (coin starts with "@N" is spot-pair index; the fund only uses spot for DCA)
    if coin_raw.startswith("@") and is_spot_like:
        return "Core DCA"
    if coin in CORE_DCA_SPOT_TOKENS and is_spot_like:
        return "Core DCA"
    # Rule 4: stable leg of a spot pair
    if coin in STABLE_TOKENS and is_spot_like:
        return "Core DCA"
    # Rule 5: basket tokens without dir (shouldn't happen but safe)
    if coin in BASKET_PERP_TOKENS:
        return "Basket trade"
    return wallet_label or "?"
