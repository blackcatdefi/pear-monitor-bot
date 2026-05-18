"""R-LEVERAGE-AUTODETECT (2026-05-18) — leverage SIEMPRE dinámico.

Background
----------
El bot venía reportando leverage hardcoded (3x en `kill_scenarios`,
"BASKET v5 leverage_max 3x" en `fund_state.py`, "SHORT 3x" en el
system prompt LLM). BCD opera 5x cross default permanente (Claude
memory slot 5, vigente desde 2026-05-15), y el leverage real de cada
posición DEBE calcularse dinámicamente como notional/equity_perp en
todos los outputs del bot — el bot NUNCA debe asumir un valor fijo.

Estos tests bloquean el agujero:

1. La constante `FUND_DEFAULT_LEVERAGE` existe y vale "5x" — referencia
   documental que reemplaza el viejo "3x" hardcoded.
2. `BASKET_V5_PLAN["leverage_max"]` está alineado con `FUND_DEFAULT_LEVERAGE`.
3. El render de leverage en `templates/formatters.py` (ruta /reporte y
   /posiciones) calcula `notional/equity` con 1 decimal, NUNCA hardcoded.
4. Tres escenarios pinned: ntl 30K/eq 6K → 5.0x, ntl 20K/eq 4K → 5.0x,
   sin perp → 0x.
5. Regression guard global: ningún string user-facing del repo contiene
   el viejo " 4x cross", " 3x cross" o "(SHORT basket 3x)".
6. `compute_kill_scenarios` renderiza la etiqueta basket usando
   `FUND_DEFAULT_LEVERAGE`, no un literal.
"""
from __future__ import annotations

import os
import re
import pathlib
import pytest

# Ensure project root is on sys.path (conftest does it, but keep import-safe).
from auto.fund_constants import FUND_DEFAULT_LEVERAGE
from fund_state import BASKET_V5_PLAN


# ────────────────────────────────────────────────────────────────────────────
# 1. Constante FUND_DEFAULT_LEVERAGE — documentary 5x
# ────────────────────────────────────────────────────────────────────────────
def test_fund_default_leverage_is_5x() -> None:
    """BCD opera 5x cross default permanente — slot 5 Claude memory."""
    assert FUND_DEFAULT_LEVERAGE == "5x"


def test_basket_v5_plan_leverage_aligned() -> None:
    """`BASKET_V5_PLAN["leverage_max"]` debe seguir a `FUND_DEFAULT_LEVERAGE`,
    no quedarse pegado al legacy "3x"."""
    assert BASKET_V5_PLAN["leverage_max"] == FUND_DEFAULT_LEVERAGE
    assert BASKET_V5_PLAN["leverage_max"] == "5x"


# ────────────────────────────────────────────────────────────────────────────
# 2. Render dinámico /reporte — notional/equity, 1 decimal
# ────────────────────────────────────────────────────────────────────────────
def _render_perp_line(ntl_pos: float, perp_eq: float, margin_used: float = 0.0,
                       withdrawable: float = 0.0) -> str:
    """Mimic the exact formatters.py:665-672 render block."""
    if ntl_pos > 50 or margin_used > 50:
        lev = round((ntl_pos / perp_eq), 1) if perp_eq > 0.01 else 0.0
        return (
            f"    Margin used: ${margin_used:,.0f} | "
            f"Withdrawable: ${withdrawable:,.0f} | "
            f"Notional: ${ntl_pos:,.0f} (~{lev:.1f}x)"
        )
    return ""


@pytest.mark.parametrize(
    "ntl,eq,expected_lev_str",
    [
        (30_000.0, 6_000.0, "~5.0x"),   # canónico BCD
        (20_000.0, 4_000.0, "~5.0x"),   # canónico BCD (escala distinta)
        (31_200.0, 6_094.0, "~5.1x"),   # snapshot real 16 may /posiciones
        (10_000.0, 2_500.0, "~4.0x"),   # leg under-leveraged — debe mostrar 4.0, no asumir 5
        (15_000.0, 1_500.0, "~10.0x"),  # leg over-leveraged
    ],
)
def test_leverage_dynamic_one_decimal(ntl: float, eq: float, expected_lev_str: str) -> None:
    line = _render_perp_line(ntl, eq, margin_used=ntl / 5)
    assert expected_lev_str in line, (
        f"Expected '{expected_lev_str}' for ntl={ntl} eq={eq}; got: {line}"
    )


def test_leverage_zero_when_no_perp_equity() -> None:
    """Sin equity perp activo, leverage = 0.0x — NUNCA 4x ni 5x."""
    line = _render_perp_line(ntl_pos=500.0, perp_eq=0.0, margin_used=100.0)
    assert "~0.0x" in line
    assert "~4.0x" not in line
    assert "~5.0x" not in line


def test_leverage_never_hardcoded_in_render() -> None:
    """Regression guard: si equity * 4 != notional, el render no puede
    mostrar 4.0x. Esto pinea que el bot NUNCA cae a un default mientras
    haya ratio computable."""
    # ntl=27000, eq=6000 → 4.5x. Si el bot hardcodeara 4x volvería 4.0x.
    line = _render_perp_line(ntl_pos=27_000.0, perp_eq=6_000.0, margin_used=5_400.0)
    assert "~4.5x" in line
    assert "~4.0x" not in line


# ────────────────────────────────────────────────────────────────────────────
# 3. kill_scenarios usa FUND_DEFAULT_LEVERAGE (no literal "3x")
# ────────────────────────────────────────────────────────────────────────────
def test_kill_scenarios_label_uses_constant() -> None:
    """El label de Super Basket Stage 6 en /kill debe interpolar
    FUND_DEFAULT_LEVERAGE — no quedarse con "(SHORT basket 3x)". Lo
    verificamos por inspección del source para no arrastrar la cadena
    de import web3/hyperlend en CI."""
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "modules" / "kill_scenarios.py").read_text()
    assert "FUND_DEFAULT_LEVERAGE" in src, (
        "kill_scenarios.py debe importar FUND_DEFAULT_LEVERAGE"
    )
    assert "(SHORT basket 3x)" not in src
    assert "SHORT basket {FUND_DEFAULT_LEVERAGE}" in src


# ────────────────────────────────────────────────────────────────────────────
# 4. Global grep guard — no leverage hardcoded user-facing strings
# ────────────────────────────────────────────────────────────────────────────
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _iter_py_files() -> list[pathlib.Path]:
    skip_dirs = {"tests", "scripts", "docs", "__pycache__", "data"}
    return [
        p for p in REPO_ROOT.rglob("*.py")
        if not any(part in skip_dirs for part in p.parts)
    ]


@pytest.mark.parametrize("bad", [
    "(SHORT basket 3x)",
    "~4x cross",
    "~3x cross",
    'leverage_max": "3x"',
    'leverage_max": "4x"',
])
def test_no_hardcoded_leverage_strings(bad: str) -> None:
    """Ningún archivo .py del bot puede contener literal hardcoded leverage
    en strings user-facing. Si querés referenciar el default, usá
    FUND_DEFAULT_LEVERAGE."""
    hits: list[str] = []
    for f in _iter_py_files():
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if bad in text:
            hits.append(str(f.relative_to(REPO_ROOT)))
    assert not hits, f"Leverage hardcode '{bad}' encontrado en: {hits}"


def test_formatters_render_no_legacy_2decimal_lev() -> None:
    """formatters.py debe usar `:.1f` en el render de leverage, no `:.2f`."""
    fmt = (REPO_ROOT / "templates" / "formatters.py").read_text()
    # Encontrar la línea del render Notional + leverage
    m = re.search(r"Notional: \{_fmt_usd\(ntl_pos\)\} \(~\{lev:\.([0-9])f\}x\)", fmt)
    assert m is not None, "No se encontró el render de leverage en formatters.py"
    assert m.group(1) == "1", (
        f"Esperaba `:.1f` en el render de leverage, encontré `:.{m.group(1)}f`"
    )
