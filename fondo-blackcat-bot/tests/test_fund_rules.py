"""R-BOT-DEFINITIVE WI-8 — fund rules injection + poisoned-draft strike tests."""
from __future__ import annotations

from modules.fund_rules import (
    FUND_RULES_BLOCK,
    build_fund_rules_block,
    strike_forbidden_lines,
)


def test_rules_block_covers_every_hard_rule():
    b = build_fund_rules_block()
    assert b == FUND_RULES_BLOCK
    assert "ANCLA CONGELADA" in b                      # HYPE never sold
    assert "aave-HF < 1.10" in b                       # repay playbook
    assert "nivel LIBRO" in b.upper() or "nivel libro" in b.lower()
    assert "LMEC" in b                                 # only thesis break closes
    assert "ZEC" in b and "PERMANENTE" in b
    assert "bloquea" in b and "ABRIR posiciones nuevas" in b
    assert "VERDE" in b and "PAGA" in b                # funding sign semantics


def test_strike_sell_hype():
    draft = (
        "Resumen del día.\n"
        "Sugerencia: vender 200 HYPE para reducir exposición.\n"
        "Fin."
    )
    clean, struck = strike_forbidden_lines(draft)
    assert len(struck) == 1 and struck[0][0] == "sell_hype"
    assert "vender 200 HYPE" not in clean
    assert "Resumen del día." in clean and "Fin." in clean


def test_strike_repay_with_hype():
    draft = "PRIORIDAD: repagar la deuda USDC usando parte del HYPE spot."
    clean, struck = strike_forbidden_lines(draft)
    assert any(r == "repay_with_hype" or r == "sell_hype" for r, _ in struck)
    assert "repagar la deuda USDC usando parte del HYPE" not in clean


def test_strike_zec_reopen():
    draft = "Oportunidad: abrir short ZEC tras el rebote."
    clean, struck = strike_forbidden_lines(draft)
    assert struck and struck[0][0] == "zec_proposal"
    assert "ZEC" not in clean.split("FILTRO")[0]


def test_strike_close_basket_on_environment():
    draft = "Dado el entorno bearish, considerar cerrar el basket esta semana."
    clean, struck = strike_forbidden_lines(draft)
    assert struck and struck[0][0] == "close_basket_env"
    assert "cerrar el basket" not in clean.split("FILTRO")[0]


def test_rule_affirmations_are_kept():
    draft = (
        "Regla: NUNCA vender HYPE — es ancla congelada.\n"
        "ZEC sigue en blocklist permanente, no proponerlo.\n"
        "El basket solo se cierra por ruptura de tesis (LMEC), nunca por entorno."
    )
    clean, struck = strike_forbidden_lines(draft)
    assert struck == []
    assert clean == draft


def test_clean_draft_untouched():
    draft = "Todo en orden. Mantener hedge. aave-HF 1.57 saludable."
    clean, struck = strike_forbidden_lines(draft)
    assert clean == draft and struck == []


def test_strike_appends_note_when_removing():
    clean, struck = strike_forbidden_lines("vender HYPE ya")
    assert struck
    assert "FILTRO REGLAS DEL FONDO" in clean


def test_analysis_wiring_present():
    """The strike filter runs in the post-generation pass of analysis.py."""
    import inspect
    import modules.analysis as analysis
    src = inspect.getsource(analysis)
    assert "strike_forbidden_lines" in src


def test_consistency_extension_via_compile_raw_data():
    """Every FULL ANALYSIS / tesis prompt receives the rules block."""
    from templates.formatters import compile_raw_data
    out = compile_raw_data([], [], {}, None, None)
    assert "REGLAS DURAS DEL FONDO" in out
    assert out.index("REGLAS DURAS") < out.index("RAW DATA")
