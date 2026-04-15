"""Estructura canónica de la sección de Telegram Intel en el reporte.

Este módulo solo define constantes/plantillas — la generación final la hace Claude.
"""

INTEL_SECTION_HEADER = """5. TELEGRAM INTEL
   🔴 ALERTAS CRÍTICAS (ceasefire/de-escalación signals primero si hay)
   📡 TIER 1 señales (con canal y números)
   📊 TIER 2 highlights
   🐋 ON-CHAIN notable (whales, HYPE staking/unstaking, exchange flows)
"""

CEASEFIRE_KEYWORDS = [
    "ceasefire", "alto el fuego", "cease fire", "tregua",
    "de-escalation", "descalation", "withdrawal", "retirada",
    "truce", "peace deal", "acuerdo de paz",
    "hormuz reopen", "hormuz abre", "hormuz open",
    "negotiation", "negociación", "diplomatic",
]
