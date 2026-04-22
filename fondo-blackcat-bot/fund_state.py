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
TRADE_DEL_CICLO_PLATFORM = "blofin"
TRADE_DEL_CICLO_LEVERAGE = 10       # 10x leverage, NOT 3x
TRADE_DEL_CICLO_LAST_ENTRY = 75298.70  # USD — manually set by BCD
TRADE_DEL_CICLO_LAST_UPDATE = "2026-04-20T22:00:00Z"
TRADE_DEL_CICLO_BLOFIN_BALANCE_USD = 2234.0  # ~$1K manual + $1K copy-trading
TRADE_DEL_CICLO_NOTE = (
    "Trade del Ciclo vive en Blofin (sin API pública). El bot NO tiene acceso "
    "a esta posición en tiempo real. Al reportar Trade del Ciclo, citar el "
    "valor conocido de la última actualización manual y declararlo como "
    "'último dato confirmado por BCD'. NO inventar entry/leverage/liq price. "
    "Si hace >24h sin update manual, marcar explícitamente: "
    "'Trade del Ciclo (Blofin, gestionado fuera del bot) — última lectura "
    "manual: pendiente de update por BCD.'"
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


def classify_fill(fill: dict, wallet_label: str = "") -> str:
    """Return a classification label for a recent fill.

    Rules:
      - Spot buy/sell of KHYPE/PEAR/BTC/HYPE → 'Core DCA'
      - Perp on basket tokens (WLD/STRK/...)  → 'Basket trade'
      - Anything else → wallet label (fallback)
    """
    coin = (fill.get("coin") or "").upper()
    side = (fill.get("side") or "").upper()
    # Hyperliquid spot fills use side 'B' or 'A' (buy/ask); perp uses LONG/SHORT
    is_spot_like = side in ("B", "A", "BUY", "SELL")
    if coin in CORE_DCA_SPOT_TOKENS and is_spot_like:
        return "Core DCA"
    if coin in BASKET_PERP_TOKENS:
        return "Basket trade"
    return wallet_label or "?"
