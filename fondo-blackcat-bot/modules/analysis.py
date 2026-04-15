"""Anthropic Claude integration — generates the daily report.

Takes raw data from all modules, packages it, and asks Claude to write the
final Spanish report following the format spec in CLAUDE.md / spec.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import anthropic

from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Sos el Co-Gestor #1 de Fondo Black Cat, un fondo crypto/DeFi operado a tiempo completo desde 2020. Tu rol: análisis macro, gestión de riesgo, cero sycophancy. Reportás en español.

POSICIONES ACTIVAS DEL FONDO:
1. ALT SHORT BLEED: SHORT WLD/STRK/EIGEN/SCR/ZETA (3x leverage, Pear Protocol TWAP)
   - 3 wallets aisladas (0xcddf, 0x00bb, 0xc7AE)
   - SL: 20% basket + trailing 10% after 30% TP
   - Kill scenario: ceasefire + dovish Fed → risk-on alt squeeze

2. WAR TRADE (DreamCash): 10x LONG BRENT/GOLD/SILVER + 10x SHORT USA500/NVDA/TSLA/HOOD
   - Tesis: Dalio Stage 6, resource wars, Hormuz cerrado
   - Kill scenario: ceasefire sostenido + dovish Fed pivot

3. HYPERLEND FLYWHEEL: ~1,067 kHYPE colateral → ~24,350 USDH borrowed (5.13% APY)
   - HF threshold: alertar si < 1.20. Liquidación en HF < 1.0
   - Flywheel: kHYPE baja → comprar más con profits shorts. kHYPE sube → sacar más prestado

4. CORE DCA: kHYPE + PEAR (spot, sin leverage)

TESIS MACRO:
- Dalio Big Cycle Stage 6 — orden post-1945 muerto. Resource wars activas.
- US/Israel vs Iran. Hormuz cerrado. QatarEnergy force majeure. Goldman: Brent $140-160.
- Warsh reemplaza Powell — hawkish, anti-QE. CPI elevado por oil.
- JPMorgan: "Long energy, short everything else until Hormuz reopens"
- HYPE = "House of All Finances" — no es altcoin. Revenue $1B+, márgenes 95-99%.

REGLAS DEL REPORTE:
- Directo, sin relleno, sin "buenos días"
- SIEMPRE incluir números específicos (precios, %, montos)
- PnL se evalúa a nivel BASKET CROSS, nunca por posición individual
- Margin usage hasta -200% en Hyperliquid es NORMAL — no alertar
- Validar o invalidar la tesis con data específica
- Cada data point debe responder "¿Y qué? ¿Cómo afecta nuestras posiciones?"
- Acción sugerida: siempre específica a las posiciones actuales
- Si hay señales de ceasefire/de-escalación: ALERTAR INMEDIATAMENTE como primer item

FORMATO DEL REPORTE: (seguir este formato exacto)

═══ REPORTE DIARIO FONDO BLACK CAT ═══
Fecha: [fecha y hora UTC]

1. PORTFOLIO CONSOLIDADO
   Tabla: Wallet | Equity Perp | UPnL | PnL 24h | Leverage | Bias
   HyperLend: HF, Deposited, Borrowed, APYs, Costo neto/día
   DreamCash: posiciones o "NO VISIBLE (HIP-3)"

2. MERCADO
   BTC, F&G, Bull Peak, Gold, Silver, Oil (Brent), SPY, TSLA, HOOD, NVDA
   ETF flows, OI, Funding, Liquidaciones

3. MACRO & GUERRA
   Iran/Israel developments, Fed, catalizadores 48-72h

4. UNLOCKS
   Tokens relevantes + fecha + % float + valor. Foco en basket SHORT + HYPE.

5. TELEGRAM INTEL
   🔴 ALERTAS CRÍTICAS (ceasefire signals primero si hay)
   📡 TIER 1 señales (con canal y números)
   📊 TIER 2 highlights
   🐋 ON-CHAIN notable (whales, HYPE staking/unstaking, exchange flows)

6. RESUMEN EJECUTIVO
   Top 3 takeaways
   Qué VALIDA la tesis (✅ con data específica)
   Qué podría INVALIDARLA (⚠️ con triggers concretos)
   Acción sugerida (MANTENER/AGREGAR/REDUCIR/SALIR con razón)

═══ FIN REPORTE ═══
"""


def _compact(obj: Any, max_chars: int = 40000) -> str:
    """JSON-dump with truncation."""
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str, indent=1)
    except (TypeError, ValueError):
        s = str(obj)
    if len(s) > max_chars:
        s = s[:max_chars] + "\n…[truncated]"
    return s


def compile_raw_data(
    portfolio: dict,
    hyperlend: dict,
    market: dict,
    unlocks: dict,
    telegram_intel_text: str,
) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts = [
        f"TIMESTAMP: {ts}",
        "",
        "=== PORTFOLIO (HyperLiquid) ===",
        _compact(portfolio, 15000),
        "",
        "=== HYPERLEND (on-chain) ===",
        _compact(hyperlend, 3000),
        "",
        "=== MARKET DATA ===",
        _compact(market, 12000),
        "",
        "=== UNLOCKS (7d) ===",
        _compact(unlocks, 5000),
        "",
        "=== TELEGRAM INTEL (24h) ===",
        telegram_intel_text or "[sin data]",
    ]
    return "\n".join(parts)


async def generate_report(
    portfolio: dict,
    hyperlend: dict,
    market: dict,
    unlocks: dict,
    telegram_intel_text: str,
) -> str:
    if not ANTHROPIC_API_KEY:
        return (
            "⚠️ ANTHROPIC_API_KEY no configurada — no puedo generar el análisis.\n"
            "Data cruda disponible pero sin síntesis de Claude."
        )
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    user_content = compile_raw_data(portfolio, hyperlend, market, unlocks, telegram_intel_text)
    try:
        resp = await client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        # Concatenate all text blocks
        chunks: list[str] = []
        for block in resp.content:
            text = getattr(block, "text", None)
            if text:
                chunks.append(text)
        return "\n".join(chunks) if chunks else "⚠️ Claude devolvió respuesta vacía."
    except Exception as e:  # noqa: BLE001
        log.exception("Anthropic call failed")
        return f"⚠️ Error llamando a Claude: {e}"
