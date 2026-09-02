"""R-BOT-DEFINITIVE Fase 3 — tests de los invariantes y del recomputo.

Cada test siembra a mano una DB con UNA fila mal y comprueba que el chequeo
la encuentra. Es la unica forma de saber que el verificador no esta vacio: un
verificador que siempre devuelve "todo bien" pasa desapercibido para siempre,
que es exactamente como sobrevivio el funding 0.00 durante un deploy entero.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modules import ledger_invariants as li  # noqa: E402

W = "0xc7ae23316b47f7e75f455f53ad37873a18351505"
H = 3_600_000          # una hora en ms


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    """DB de ledger real (mismo schema) con las tablas vacias."""
    import modules.trade_ledger as tl
    monkeypatch.setattr(tl, "DB_PATH", str(tmp_path / "ledger.db"))
    con = tl._conn()
    con.close()

    def _open():
        c = sqlite3.connect(str(tmp_path / "ledger.db"))
        c.row_factory = sqlite3.Row
        return c
    return _open


def _pos(con, **kw):
    d = dict(wallet=W, coin="BTC", side="LONG", open_ts=1_700_000_000_000,
             close_ts=1_700_000_000_000 + 4 * H, avg_entry=100.0, avg_exit=110.0,
             max_size=10.0, notional_open=1000.0, margin_open=200.0,
             leverage=5.0, leverage_source="derived", open_fills=1,
             close_fills=1, fees_total=2.0, funding_net=3.0,
             gross_pnl=100.0, net_pnl=101.0, roe_pct=50.5, cycle_tag=None)
    d.update(kw)
    cols = ",".join(d)
    con.execute(f"INSERT INTO ledger_positions ({cols}) "
                f"VALUES ({','.join('?' * len(d))})", tuple(d.values()))
    con.commit()
    return d


def _fill(con, tid, t, fee=0.0, closed=0.0, coin="BTC"):
    con.execute(
        "INSERT INTO ledger_fills (wallet,tid,coin,side,px,sz,time,fee,"
        "closed_pnl) VALUES (?,?,?,?,?,?,?,?,?)",
        (W, tid, coin, "B", 100.0, 1.0, t, fee, closed))
    con.commit()


def _fund(con, t, usdc, coin="BTC"):
    con.execute("INSERT INTO ledger_funding (wallet,time,coin,usdc) "
                "VALUES (?,?,?,?)", (W, t, coin, usdc))
    con.commit()


def _nombres(vs):
    return {v.invariant.split()[0] for v in vs}


# ─── I1: la formula ─────────────────────────────────────────────────────────

def test_i1_detecta_net_que_no_cierra(ledger):
    con = ledger()
    # gross 100 - fees 2 + funding 3 = 101, pero se guarda 95.
    _pos(con, net_pnl=95.0)
    con.close()
    vs = li.check_invariants()
    assert "I1" in _nombres(vs), (
        "el invariante central de todo el ledger no se verifica: "
        "NET = gross - fees + funding")
    assert "95" in str(vs[0]) and "101" in str(vs[0]), (
        "la violacion tiene que mostrar los dos numeros; sin eso no se puede "
        "arreglar sin abrir la DB a mano")


def test_i1_pasa_cuando_la_formula_cierra(ledger):
    con = ledger()
    _pos(con)              # 100 - 2 + 3 = 101 ✓
    con.close()
    assert "I1" not in _nombres(li.check_invariants())


# ─── I2: ROE ────────────────────────────────────────────────────────────────

def test_i2_detecta_roe_calculado_sobre_el_margen_equivocado(ledger):
    """El bug de R-LEDGER-FIX: el leverage no se escribia en el upsert, asi
    que el ROE salia calculado con el margen del apalancamiento por defecto.
    El numero era plausible y nadie podia notarlo mirando el reporte."""
    con = ledger()
    _pos(con, roe_pct=10.1)     # real: 101/200 = 50.5%
    con.close()
    vs = li.check_invariants()
    assert "I2" in _nombres(vs)


# ─── I3: ciclos sin fills de un lado ────────────────────────────────────────

def test_i3_detecta_cierre_sin_apertura(ledger):
    con = ledger()
    _pos(con, open_fills=0)
    con.close()
    assert "I3" in _nombres(li.check_invariants())


# ─── I4: ciclos solapados ───────────────────────────────────────────────────

def test_i4_detecta_ciclos_que_se_pisan(ledger):
    """Dos ciclos del mismo par cuyos intervalos se solapan: el mismo fill
    entra en los dos y el NET del par queda inflado."""
    con = ledger()
    _pos(con, open_ts=1000, close_ts=5000)
    _pos(con, open_ts=3000, close_ts=8000)      # arranca antes de que cierre
    con.close()
    assert "I4" in _nombres(li.check_invariants())


def test_i4_no_marca_ciclos_consecutivos(ledger):
    con = ledger()
    _pos(con, open_ts=1000, close_ts=5000)
    _pos(con, open_ts=5000, close_ts=9000)      # pegados, no solapados
    con.close()
    assert "I4" not in _nombres(li.check_invariants())


# ─── I5: el bug del funding 0.00 ────────────────────────────────────────────

def test_i5_detecta_funding_cero_exacto_en_ciclo_largo(ledger):
    """EL bug de la ronda anterior, convertido en invariante.

    Un 429 de userFunding se convertia en funding 0.00 en todas las patas y
    el reporte salia igual de prolijo. El funding de HL se acredita por hora:
    un ciclo de 40 horas con 0.00 EXACTO no es un mercado tranquilo.
    """
    con = ledger()
    _pos(con, funding_net=0.0, net_pnl=98.0,   # 100 - 2 + 0, I1 cierra
         open_ts=0, close_ts=40 * H, roe_pct=49.0)
    con.close()
    vs = li.check_invariants()
    assert "I5" in _nombres(vs), (
        "el ledger volveria a poder reportar funding 0.00 en todas las patas "
        "sin que nada se queje")
    assert "I1" not in _nombres(vs), (
        "el caso esta armado para que la formula CIERRE: si I1 tambien salta, "
        "el test no esta probando lo que dice probar")


def test_i5_no_marca_ciclo_corto_sin_funding(ledger):
    """Un ciclo de 20 minutos puede no cruzar ninguna hora de funding."""
    con = ledger()
    _pos(con, funding_net=0.0, net_pnl=98.0, open_ts=0,
         close_ts=20 * 60 * 1000, roe_pct=49.0)
    con.close()
    assert "I5" not in _nombres(li.check_invariants())


# ─── I6: cursores ───────────────────────────────────────────────────────────

def test_i6_detecta_cursor_adelantado_sin_datos(ledger):
    """Un cursor que salta hacia adelante sin haber leido deja un hueco: ese
    funding no se lee nunca mas y el NET queda corto para siempre."""
    con = ledger()
    _fill(con, 1, 1_700_000_000_000)
    con.execute("INSERT INTO ledger_meta (key,value,updated_at) VALUES (?,?,?)",
                (f"cursor_{W}", str(1_700_000_000_000 + 30 * 86_400_000), "x"))
    con.commit()
    con.close()
    assert "I6" in _nombres(li.check_invariants())


# ─── 3.2 recomputo independiente ────────────────────────────────────────────

def test_recomputo_detecta_funding_guardado_que_no_existe_en_los_fills(ledger):
    """El caso que ningun test de los 1300 podia atrapar: los agregados
    guardados son internamente consistentes pero no salen de los datos."""
    con = ledger()
    _pos(con, funding_net=3.0, fees_total=2.0, gross_pnl=100.0, net_pnl=101.0)
    _fill(con, 1, 1_700_000_000_000 + H, fee=2.0, closed=100.0)
    _fund(con, 1_700_000_000_000 + H, 25.0)     # la realidad dice 25, no 3
    con.close()
    vs = li.recompute_from_fills()
    assert "R4" in _nombres(vs)
    assert "25" in str(vs[0]) and "3.0" in str(vs[0])


def test_recomputo_detecta_fees_infladas(ledger):
    con = ledger()
    _pos(con, fees_total=50.0, net_pnl=53.0)
    _fill(con, 1, 1_700_000_000_000 + H, fee=2.0, closed=100.0)
    _fund(con, 1_700_000_000_000 + H, 3.0)
    con.close()
    assert "R2" in _nombres(li.recompute_from_fills())


def test_recomputo_detecta_ciclo_sin_ningun_fill(ledger):
    """Una fila de ledger_positions cuyos numeros no salen de ningun dato."""
    con = ledger()
    _pos(con)
    con.close()
    vs = li.recompute_from_fills()
    assert "R1" in _nombres(vs)


def test_recomputo_pasa_cuando_todo_sale_de_los_datos(ledger):
    con = ledger()
    _pos(con, fees_total=2.0, funding_net=3.0, gross_pnl=100.0, net_pnl=101.0)
    _fill(con, 1, 1_700_000_000_000 + H, fee=2.0, closed=100.0)
    _fund(con, 1_700_000_000_000 + 2 * H, 3.0)
    con.close()
    assert li.recompute_from_fills() == []


# ─── Anti-espejo: el recomputo tiene que ser independiente ──────────────────

def test_el_recomputo_no_reusa_el_camino_del_ledger():
    """Todo el valor del chequeo esta en que sea OTRA implementacion.

    Si alguien "simplifica" recompute_from_fills() llamando a las funciones
    de agregacion de trade_ledger, el chequeo pasa a compararse consigo mismo
    y deja de probar nada — pero seguiria pasando en verde, que es la peor
    forma posible de romperse.
    """
    import ast
    src = (REPO / "modules" / "ledger_invariants.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "recompute_from_fills")
    # Se mira el CODIGO, no el docstring: el docstring nombra a
    # rebuild_wallet_positions justamente para decir que no se usa.
    cuerpo = [s for s in fn.body if not (
        isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
        and isinstance(s.value.value, str))]
    body = "\n".join(ast.unparse(s) for s in cuerpo)
    for prohibido in ("rebuild_wallet_positions", "_rebuild", "cycle_pnl",
                      "compute_position"):
        assert prohibido not in body, (
            f"recompute_from_fills() usa {prohibido}() de trade_ledger: dejo "
            f"de ser un recomputo independiente y paso a ser un espejo")
    # Lo unico que puede tomar de trade_ledger es la conexion.
    assert "trade_ledger" not in body, (
        "el recomputo importa trade_ledger directamente; la unica dependencia "
        "permitida es _conn(), que resuelve la ruta de la DB")


def test_run_all_resume_ambos_chequeos(ledger):
    con = ledger()
    _pos(con, net_pnl=95.0)
    con.close()
    r = li.run_all()
    assert r["ok"] is False
    assert r["total"] >= 1
    assert r["invariantes"] and isinstance(r["invariantes"][0], str)
