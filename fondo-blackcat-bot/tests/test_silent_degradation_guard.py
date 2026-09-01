"""R-BOT-DEFINITIVE — el test que hace imposible la regresion de la ronda.

Las siete fallas que motivaron esta ronda tenian una sola cosa en comun: NO
LEVANTABAN. Una suite de 1300 tests no atrapo ninguna porque todas devolvian un
valor plausible — 0.00 de funding, 0 posts, media serie de cierres, un ROE
inflado. Testear el comportamiento no alcanzaba: habia que testear la FORMA del
codigo, porque la forma era el bug.

Este archivo hace eso. No verifica que una funcion devuelva lo correcto (de eso
se ocupan los otros 1300); verifica que en el camino del dinero no exista un
`except` capaz de fabricar un numero sin decirlo.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

from silent_degradation_scan import (  # noqa: E402
    MONEY_PATH, load_allowlist, scan_repo,
)


# ─── El guardian ────────────────────────────────────────────────────────────

def test_ningun_swallow_del_money_path_queda_silencioso():
    """Invariante central de R-BOT-DEFINITIVE.

    Todo handler del money path que produzca un valor plausible tiene que estar
    instrumentado (declara la degradacion en health_registry) o aceptado por
    escrito en el allowlist. No hay tercera opcion.

    Si este test falla, alguien agrego un default silencioso. La correccion NO
    es agregarlo al allowlist sin pensar: es decidir si ese default puede
    corromper un numero. Si puede, se instrumenta o se levanta. Si no puede,
    se acepta CON la razon escrita, que es lo que obliga a pensarlo.
    """
    allow = load_allowlist()
    unlisted = [f for f in scan_repo(money_only=True)
                if not f.instrumented and f.key not in allow]
    assert not unlisted, (
        "Swallows nuevos y silenciosos en el money path:\n"
        + "\n".join(f"  {f.as_row()}\n    key: {f.key}" for f in unlisted)
    )


def test_toda_entrada_del_allowlist_tiene_una_razon_de_verdad():
    """Un allowlist con razones vacias o de tramite no sirve de nada."""
    for key, reason in load_allowlist().items():
        assert isinstance(reason, str), f"{key}: la razon no es texto"
        assert len(reason.strip()) >= 60, (
            f"{key}: la razon tiene {len(reason.strip())} caracteres. "
            f"Si no se puede explicar en una frase completa por que ese "
            f"default no miente, el swallow no se acepta: se arregla."
        )


def test_el_allowlist_no_acumula_entradas_muertas():
    """Entradas que ya no corresponden a ningun handler real.

    Sin esto el allowlist crece para siempre y deja de reflejar el codigo: una
    entrada vieja podria estar tapando un handler nuevo con la misma clave.
    """
    keys_reales = {f.key for f in scan_repo(money_only=True)}
    muertas = [k for k in load_allowlist() if k not in keys_reales]
    assert not muertas, (
        "Entradas del allowlist sin handler correspondiente (borrarlas):\n"
        + "\n".join(f"  {k}" for k in muertas)
    )


def test_el_scanner_reconoce_la_instrumentacion():
    """Anti-vacuidad del guardian.

    Si `instrumented` fuera siempre True el guardian pasaria sin mirar nada.
    Se comprueba contra el codigo real: tiene que haber instrumentados Y
    no instrumentados.
    """
    money = scan_repo(money_only=True)
    assert money, "el scanner no encontro nada: esta roto"
    ins = [f for f in money if f.instrumented]
    no_ins = [f for f in money if not f.instrumented]
    assert ins, "ningun handler instrumentado: la instrumentacion se perdio"
    assert no_ins, "todos instrumentados: el flag esta hardcodeado en True"


def test_money_path_cubre_los_archivos_que_producen_numeros():
    """El guardian solo vale sobre los archivos que vigila.

    Si alguien saca un archivo de MONEY_PATH, el guardian deja de mirarlo sin
    fallar. Se fijan los que no pueden salir nunca.
    """
    imprescindibles = {
        "modules/trade_ledger.py",       # el ledger: NET, ROE, funding
        "modules/portfolio.py",          # posiciones y equity
        "modules/portfolio_margin.py",   # estado de portfolio margin
        "modules/funding_tracker.py",    # funding, el bug de 0.00
        "modules/vault_deposits.py",     # equity de vaults dentro del NAV
        "modules/market.py",             # precios
    }
    faltan = imprescindibles - set(MONEY_PATH)
    assert not faltan, f"salieron del money path: {sorted(faltan)}"


def test_el_allowlist_es_json_valido_y_documentado():
    p = REPO / "tools" / "silent_degradation_allowlist.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "accepted" in data
    assert data.get("_doc"), "el allowlist tiene que explicar su propia regla"


# ─── Las cuatro clases de bug probadas (tools/bug_class_scan.py) ─────────────

@pytest.mark.parametrize("clase", ["C1", "C3"])
def test_las_clases_de_bug_ya_probadas_siguen_en_cero(clase):
    """C1 (timestamp mixto) y C3 (upsert incompleto) tienen que dar 0.

    C2 y C4 no se fijan en cero: C2 reporta fallbacks legitimos y C4 tiene
    hallazgos que son navegacion de JSON normal. Esos dos se revisan a mano
    por ronda. C1 y C3 si son binarios: o hay un formato mixto comparado como
    texto, o no lo hay.
    """
    r = subprocess.run(
        [sys.executable, "tools/bug_class_scan.py", "--class", clase],
        cwd=REPO, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith("TOTAL 0"), (
        f"clase {clase} volvio a aparecer:\n{r.stdout}")
