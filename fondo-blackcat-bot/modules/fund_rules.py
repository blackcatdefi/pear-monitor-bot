"""R-BOT-DEFINITIVE WI-8 — fund hard rules injected into every LLM prompt.

The 2026-06-10 live run suggested "partial debt repay / add collateral at
HYPE 50", colliding with the fund's playbook. These rules are a CONSTANT
block injected into every FULL ANALYSIS and tesis prompt, plus a
post-generation strike filter for any line that still violates them.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

FUND_RULES_BLOCK = """═══════ REGLAS DURAS DEL FONDO (INVIOLABLES — prevalecen sobre cualquier análisis) ═══════
1. HYPE SPOT ES ANCLA CONGELADA: NUNCA se vende, NUNCA sugieras venderlo, NUNCA
   sugieras repagar deuda vendiendo HYPE. No existe escenario en el que el
   reporte proponga tocar el HYPE spot.
2. PLAYBOOK DE REPAGO (solo cuando aave-HF < 1.10): cerrar patas GANADORAS del
   basket a USDC y repagar con eso. La única otra fuente es capital externo.
   Jamás vendiendo HYPE.
3. EL PnL SE EVALÚA A NIVEL LIBRO (book), nunca por pata individual. Tener 1-2
   patas apretadas (squeezed) por libro es NORMAL y no amerita acción.
4. NUNCA sugieras cerrar el basket ni las posiciones de acumulación por entorno
   bearish/bullish. SOLO una ruptura de tesis (LMEC) cierra el basket.
5. ZEC está en blocklist PERMANENTE: no proponerlo en NINGUNA dirección
   (ni long, ni short, ni DCA, ni "re-entrada").
6. "Margin used vs equity" NO es proximidad de liquidación: >100% solo bloquea
   ABRIR posiciones nuevas. La proximidad de liquidación real es el aave-HF /
   liq price pre-calculados (bloque PM).
7. Semántica del signo de funding por celda: VERDE = la posición COBRA funding,
   ROJO = la posición PAGA. Marcar costo de funding SOLO cuando la posición PAGA.
═══════ FIN REGLAS DURAS DEL FONDO ═══════"""


def build_fund_rules_block() -> str:
    """Constant rules block. NEVER raises."""
    return FUND_RULES_BLOCK


# ─── Post-generation strike filter (extends report_consistency pattern) ─────

# Each entry: (compiled pattern, reason tag). A line matching ANY pattern is
# struck from the final output and logged.
_SELL_VERBS = r"(vender|vend[ée]|venta|liquidar|sell(?:ing)?|deshacerse|desprenderse)"
_FORBIDDEN_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Sell HYPE in any phrasing (incl. repay-by-selling-HYPE).
    (re.compile(rf"\b{_SELL_VERBS}\b[^.\n]*\bHYPE\b", re.IGNORECASE), "sell_hype"),
    (re.compile(rf"\bHYPE\b[^.\n]*\b{_SELL_VERBS}\b", re.IGNORECASE), "sell_hype"),
    # Repay debt using HYPE (convert/swap HYPE to USDC to repay).
    (re.compile(r"\brepag\w+[^.\n]*\bHYPE\b", re.IGNORECASE), "repay_with_hype"),
    (re.compile(r"\bHYPE\b[^.\n]*\brepag\w+", re.IGNORECASE), "repay_with_hype"),
    (re.compile(r"\b(convertir|swap(?:ear)?)\b[^.\n]*\bHYPE\b[^.\n]*\b(USDC|USDH|deuda)\b",
                re.IGNORECASE), "repay_with_hype"),
    # Reopen / propose ZEC in any direction.
    (re.compile(r"\b(abrir|reabrir|long|short|entrar|comprar|acumular|DCA|re-?entrada)\b"
                r"[^.\n]*\bZEC\b", re.IGNORECASE), "zec_proposal"),
    (re.compile(r"\bZEC\b[^.\n]*\b(abrir|reabrir|long|short|entrar|comprar|acumular|DCA|"
                r"re-?entrada)\b", re.IGNORECASE), "zec_proposal"),
    # Close the basket on environment grounds.
    (re.compile(r"\b(cerrar|cierre|reducir|desarmar|close|unwind|exit)\b[^.\n]*\bbasket\b"
                r"[^.\n]*\b(bearish|bajista|bullish|alcista|entorno|environment|mercado|"
                r"macro|risk[\s-]?off|risk[\s-]?on)\b", re.IGNORECASE), "close_basket_env"),
    (re.compile(r"\b(bearish|bajista|bullish|alcista|entorno|environment|risk[\s-]?off)\b"
                r"[^.\n]*\b(cerrar|cierre|reducir|desarmar|close|unwind|exit)\b[^.\n]*"
                r"\bbasket\b", re.IGNORECASE), "close_basket_env"),
]

# Lines that are quoting/affirming the RULE itself must never be struck.
_RULE_AFFIRM_RE = re.compile(
    r"(nunca|jam[aá]s|prohibido|no\s+(?:se\s+)?(?:vender|vende|propon)|regla|blocklist|"
    r"never|forbidden|do\s+not|don'?t)",
    re.IGNORECASE,
)


def strike_forbidden_lines(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Strike any output line proposing a forbidden action.

    Returns ``(clean_text, [(reason, line), ...])``. Lines that merely AFFIRM
    the rule ("NUNCA vender HYPE") are kept. NEVER raises.
    """
    try:
        out: list[str] = []
        struck: list[tuple[str, str]] = []
        for raw in (text or "").splitlines():
            stripped = raw.strip()
            if stripped:
                hit = None
                for pat, reason in _FORBIDDEN_PATTERNS:
                    if pat.search(stripped):
                        hit = reason
                        break
                if hit and not _RULE_AFFIRM_RE.search(stripped):
                    struck.append((hit, stripped))
                    log.warning("fund_rules strike [%s]: %s", hit, stripped[:160])
                    continue
            out.append(raw)
        clean = "\n".join(out)
        if struck:
            clean += (
                "\n\n⚠️ FILTRO REGLAS DEL FONDO (auto): se removieron "
                f"{len(struck)} línea(s) que violaban reglas duras "
                "(vender HYPE / repagar con HYPE / ZEC / cerrar basket por entorno)."
            )
        return clean, struck
    except Exception:  # noqa: BLE001
        log.exception("strike_forbidden_lines failed — returning original")
        return text, []
