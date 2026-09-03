"""R-FUNDING-HUECO (2026-09-03) — el cursor solo avanza, asi que el hueco se fosiliza.

QUE CONTESTO PRODUCCION
=======================
Tres rondas seguidas de /diagnostico sobre los mismos 27 ciclos:

  1. "un cero exacto es un dato que falta"  → conjetura, no verificaba nada.
  2. "cae ENTERO adentro de la ventana"     → cierto, y todavia sin causa.
  3. "tramo mudo, silencio de 24h, ninguna moneda, apunta al paginado"

La tercera es la que se puede accionar: la wallet no tiene NI UNA acreditacion
en todo el intervalo, de ninguna moneda. No es el nombre de la moneda.

EL DEFECTO ESTRUCTURAL
======================
``funding_cursor_ms`` solo avanza. Es lo correcto para ponerse al dia y es
exactamente lo que vuelve PERMANENTE cualquier hueco: cuando el cursor paso
por encima de una franja sin traerla, ningun sync posterior la vuelve a mirar.
El agujero no se cierra con el tiempo, se fosiliza.

Eso es cierto sin importar QUE abrio este hueco puntual — un dia sin correr,
una respuesta rara contada como "no hay datos", un restore. La causa cambia
como se evita el proximo; no cambia como se arregla el que ya esta. Por eso la
reparacion se escribe sin esperar a saberla.

LO QUE ESTOS TESTS FIJAN
========================
Que el hueco se detecte, se vuelva a pedir ACOTADO por los dos extremos, se
pida UNA vez y no 27, no se toque lo que no esta roto, y —lo mas importante—
que un tramo ya reintentado que vuelve vacio DEJE de contar como violacion.
Sin eso la reparacion cambia una cruz que no se puede cerrar por otra igual.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modules import ledger_invariants as li  # noqa: E402

W = "0xc7ae23316b47f7e75f455f53ad37873a18351505"
W2 = "0x00bb6858a1f2d4e3c5b7a9086f4c2d1e3a5b7c90"
H = 3_600_000
DIA = 86_400_000
T0 = 1_700_000_000_000


@pytest.fixture()
def tl(tmp_path, monkeypatch):
    import modules.trade_ledger as _tl
    monkeypatch.setattr(_tl, "DB_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(_tl, "PAGE_PAUSE_SEC", 0.0)
    # NO hace falta patchear ledger_invariants: su _conn() delega en el de
    # trade_ledger, que resuelve DB_PATH en cada llamada. Un patch ahi seria
    # un no-op que sugiere una aislacion que no es la que hay.
    _tl._conn().close()
    return _tl


@pytest.fixture()
def db(tmp_path):
    def _open():
        c = sqlite3.connect(str(tmp_path / "ledger.db"))
        c.row_factory = sqlite3.Row
        return c
    return _open


def _pos(con, wallet=W, **kw):
    fn = kw.pop("funding_net", 0.0)
    d = dict(wallet=wallet, coin="BTC", side="LONG", open_ts=T0,
             close_ts=T0 + 20 * H, avg_entry=100.0, avg_exit=110.0,
             max_size=10.0, notional_open=1000.0, margin_open=200.0,
             leverage=5.0, leverage_source="derived", open_fills=1,
             close_fills=1, fees_total=2.0, funding_net=fn,
             gross_pnl=100.0, net_pnl=98.0 + fn, roe_pct=49.0, cycle_tag=None)
    d.update(kw)
    cols = ",".join(d)
    con.execute(f"INSERT INTO ledger_positions ({cols}) "
                f"VALUES ({','.join('?' * len(d))})", tuple(d.values()))
    con.commit()
    return d


def _fills(con, wallet=W, coin="BTC", a=T0, b=T0 + 20 * H):
    """Los dos fills que sostienen el ciclo: abre 10 y cierra 10.

    Hacen falta cuando el test mira ``funding_net`` DESPUES de reparar:
    ``rebuild_wallet_positions`` reconstruye desde ``ledger_fills``, asi que
    sin fills no hay ciclo que recalcular y la fila guardada no se toca.

    El ``dir`` NO es decorativo: ``_is_perp_fill`` filtra por ahi, y un fill
    con ``dir`` vacio se descarta como si fuera spot. Con ``dir=''`` este
    helper armaba fills que ``reconcile_positions`` devolvia vacio, o sea que
    el test de ``funding_net`` pasaba a no medir nada.
    """
    for tid, (t, side, px, pnl, d) in enumerate((
            (a, "B", 100.0, 0.0, "Open Long"),
            (b, "S", 110.0, 100.0, "Close Long"))):
        con.execute(
            "INSERT INTO ledger_fills (wallet,tid,coin,side,px,sz,time,fee,"
            "closed_pnl,start_position,dir) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (wallet, tid, coin, side, px, 10.0, t, 1.0, pnl, 0.0, d))
    con.commit()


def _fund(con, t, usdc=-1.5, wallet=W, coin="BTC"):
    con.execute("INSERT INTO ledger_funding (wallet,time,coin,usdc) "
                "VALUES (?,?,?,?)", (wallet, t, coin, usdc))
    con.commit()


def _hl(respuestas, registro):
    """``_info`` falso que anota cada payload y contesta lo que se le diga."""
    async def _info(payload):
        registro.append(dict(payload))
        r = respuestas.pop(0) if respuestas else []
        if isinstance(r, Exception):
            raise r
        return r
    return _info


def _entrada(t, usdc=-0.75, coin="BTC"):
    return {"time": t, "delta": {"type": "funding", "coin": coin,
                                 "usdc": str(usdc), "szi": "1.0",
                                 "fundingRate": "0.0001"}}


# ─── 1. detectar el tramo mudo ──────────────────────────────────────────────

def test_un_ciclo_mudo_produce_un_tramo_para_repedir(tl, db):
    con = db()
    _pos(con)                          # 20h abierto, funding 0.00
    _fund(con, T0 - 2 * H)             # ultima previa
    _fund(con, T0 + 22 * H)            # primera posterior
    con.close()

    assert tl.funding_gaps(W) == [(T0 - 2 * H, T0 + 22 * H)], (
        "el pedido se acota con los bordes reales del silencio, no con el "
        "ciclo: el ciclo solo dice cuanto del hueco vimos")


def test_un_ciclo_que_si_tiene_funding_no_se_repide(tl, db):
    """Guarda contra el arreglo de mas: repedir tramos sanos es gastar
    presupuesto de rate limit para confirmar lo que ya esta bien."""
    con = db()
    _pos(con)
    _fund(con, T0 + 5 * H)             # ADENTRO del ciclo
    con.close()
    assert tl.funding_gaps(W) == []


def test_un_ciclo_corto_no_genera_pedido(tl, db):
    con = db()
    _pos(con, close_ts=T0 + 20 * 60_000)
    _fund(con, T0 - 2 * H)
    con.close()
    assert tl.funding_gaps(W) == []


def test_veintisiete_ciclos_en_un_silencio_son_UN_pedido(tl, db):
    """El caso exacto de produccion. Sin fusionar serian 27 relecturas de la
    misma ventana — 27 llamadas de peso 20 para el mismo payload, que es la
    forma mas directa de convertir la reparacion en el proximo 429."""
    con = db()
    for i in range(27):
        _pos(con, coin=f"C{i}", open_ts=T0 + i * 60_000,
             close_ts=T0 + i * 60_000 + 20 * H)
    _fund(con, T0 - 2 * H)
    _fund(con, T0 + 40 * H)
    con.close()

    huecos = tl.funding_gaps(W)
    assert len(huecos) == 1, f"no se fusionaron: {len(huecos)} pedidos"


def test_cada_wallet_pide_lo_suyo(tl, db):
    con = db()
    _pos(con, wallet=W)
    _fund(con, T0 - 2 * H, wallet=W)
    _fund(con, T0 + 22 * H, wallet=W)
    _pos(con, wallet=W2)
    _fund(con, T0 + 5 * H, wallet=W2)   # W2 esta sana
    con.close()

    assert tl.funding_gaps(W)
    assert tl.funding_gaps(W2) == []


# ─── 2. el pedido a HL ──────────────────────────────────────────────────────

def test_el_repedido_va_acotado_por_LOS_DOS_extremos(tl, db, monkeypatch):
    """El paginado normal manda solo startTime y avanza. Aca no se esta
    poniendo al dia: se relee un tramo que ya quedo atras del cursor, asi que
    sin endTime el pedido se traeria toda la historia posterior."""
    con = db()
    _pos(con)
    _fund(con, T0 - 2 * H)
    _fund(con, T0 + 22 * H)
    con.close()

    vistos: list[dict] = []
    import modules.portfolio as pf
    monkeypatch.setattr(pf, "_info", _hl([[_entrada(T0 + 3 * H)]], vistos))

    asyncio.run(tl.backfill_funding_gaps(W))
    assert vistos, "no se le pidio nada a HL"
    p = vistos[0]
    assert p["type"] == "userFunding" and p["user"] == W
    assert p["startTime"] == T0 - 2 * H
    assert p["endTime"] == T0 + 22 * H, f"sin endTime el pedido no acota: {p}"


def test_lo_que_vuelve_se_guarda_y_el_ciclo_se_recalcula(tl, db, monkeypatch):
    con = db()
    _pos(con)
    _fund(con, T0 - 2 * H)
    _fund(con, T0 + 22 * H)
    con.close()

    import modules.portfolio as pf
    monkeypatch.setattr(pf, "_info", _hl(
        [[_entrada(T0 + 3 * H, -0.75), _entrada(T0 + 4 * H, -0.75)]], []))

    r = asyncio.run(tl.backfill_funding_gaps(W))
    assert r["rows"] == 2, r

    con = db()
    n = con.execute("SELECT COUNT(*) c FROM ledger_funding WHERE wallet=? "
                    "AND time BETWEEN ? AND ?",
                    (W, T0, T0 + 20 * H)).fetchone()["c"]
    con.close()
    assert n == 2, "el tramo sigue mudo despues de la reparacion"


def test_el_hueco_reparado_deja_de_ser_violacion(tl, db, monkeypatch):
    """La prueba de que la reparacion sirve para algo: el mismo chequeo que
    gritaba tiene que callarse solo, sin tocarlo a el."""
    con = db()
    _pos(con)
    _fund(con, T0 - 2 * H)
    _fund(con, T0 + 22 * H)
    con.close()

    assert any("tramo mudo" in v.invariant for v in li.check_invariants())

    import modules.portfolio as pf
    monkeypatch.setattr(pf, "_info", _hl([[_entrada(T0 + 3 * H)]], []))
    asyncio.run(tl.backfill_funding_gaps(W))

    vs = li.check_invariants()
    assert not any("tramo mudo" in v.invariant for v in vs), [v.invariant for v in vs]


def test_el_funding_reparado_llega_a_la_fila_del_ciclo(tl, db, monkeypatch):
    """QUE EL CHEQUEO SE CALLE NO ALCANZA.

    I5 lee ``ledger_funding`` directo, asi que se calla apenas entran las
    filas — aunque ``ledger_positions.funding_net`` siga en 0.00. Y ese es el
    numero que va al track record: el P&L quedaria mal con el panel en verde,
    que es peor que el estado del que salimos.

    Una mutacion que borra el ``rebuild_wallet_positions`` del final no hacia
    fallar ningun test de esta ronda. Este es el que la agarra.
    """
    con = db()
    _pos(con)
    _fills(con)
    _fund(con, T0 - 2 * H)
    _fund(con, T0 + 22 * H)
    con.close()

    import modules.portfolio as pf
    monkeypatch.setattr(pf, "_info", _hl(
        [[_entrada(T0 + 3 * H, -0.75), _entrada(T0 + 4 * H, -1.25)]], []))
    asyncio.run(tl.backfill_funding_gaps(W))

    con = db()
    r = con.execute("SELECT funding_net, net_pnl FROM ledger_positions "
                    "WHERE wallet=? AND coin='BTC'", (W,)).fetchone()
    con.close()
    assert r["funding_net"] == pytest.approx(-2.0), (
        f"la fila del ciclo no se recalculo: funding_net={r['funding_net']}")
    assert r["net_pnl"] == pytest.approx(96.0), (
        f"el NET no siguio al funding: {r['net_pnl']}")


# ─── 3. lo irreparable deja de ser cruz roja ────────────────────────────────

def test_un_tramo_repedido_que_vuelve_vacio_baja_a_nota(tl, db, monkeypatch):
    """LA decision de diseno de esta ronda.

    Si HL contesta la ventana y no trae nada, el dato NO EXISTE de su lado.
    Dejarlo en rojo es prometer un arreglo que no hay, y una cruz que no se
    puede cerrar entrena a ignorar el panel entero — el mismo motivo por el
    que se saco la linea de redeploy y el horizonte del funding.
    """
    con = db()
    _pos(con)
    _fund(con, T0 - 2 * H)
    _fund(con, T0 + 22 * H)
    con.close()

    import modules.portfolio as pf
    monkeypatch.setattr(pf, "_info", _hl([[]], []))     # HL: no hay nada
    asyncio.run(tl.backfill_funding_gaps(W))

    vs, notas = li.check_con_notas()
    assert not any("I5" in v.invariant for v in vs), (
        f"ya se probo que el dato no existe: {[v.invariant for v in vs]}")
    assert any("volvieron VACIOS" in n for n in notas), notas


def test_un_tramo_ya_probado_vacio_no_se_vuelve_a_pedir(tl, db, monkeypatch):
    """Repetir el pedido en cada sync es gastar rate limit para volver a
    confirmar lo mismo, en el mismo presupuesto que ya tropezo antes."""
    con = db()
    _pos(con)
    _fund(con, T0 - 2 * H)
    _fund(con, T0 + 22 * H)
    con.close()

    vistos: list[dict] = []
    import modules.portfolio as pf
    monkeypatch.setattr(pf, "_info", _hl([[], []], vistos))

    asyncio.run(tl.backfill_funding_gaps(W))
    assert len(vistos) == 1
    assert tl.funding_gaps(W) == [], "el tramo probado sigue en la cola"
    asyncio.run(tl.backfill_funding_gaps(W))
    assert len(vistos) == 1, f"se volvio a pedir lo ya probado: {vistos}"


def test_sin_ninguna_prueba_hecha_la_violacion_se_mantiene(tl, db):
    """Ante la falta de evidencia se reporta la falla, no se la excusa. Si la
    ausencia de pruebas bajara todo a nota, el chequeo no diria nada nunca."""
    con = db()
    _pos(con)
    _fund(con, T0 - 2 * H)
    _fund(con, T0 + 22 * H)
    con.close()

    vs, notas = li.check_con_notas()
    assert any("tramo mudo" in v.invariant for v in vs), [v.invariant for v in vs]
    assert not any("VACIOS" in n for n in notas)


def test_sin_tabla_de_pruebas_no_se_excusa_nada(tmp_path):
    """UNITARIO A PROPOSITO, y este es el motivo.

    La primera version de este test dropeaba ``ledger_funding_probe`` y
    despues llamaba a ``check_con_notas()``. No probaba nada: ``_conn()`` de
    trade_ledger hace ``CREATE TABLE IF NOT EXISTS`` de todo el esquema en
    cada conexion, asi que la tabla estaba de vuelta —vacia— antes de que el
    chequeo la mirara. El test pasaba por la rama equivocada, y una mutacion
    que convierte "no hay tabla" en "esta todo excusado" no lo hacia fallar.

    Leerlo al reves seria el peor de los dos errores: sin evidencia el panel
    diria verde, y diria verde por no tener una tabla.
    """
    con = sqlite3.connect(str(tmp_path / "vacia.db"))
    con.row_factory = sqlite3.Row
    assert "ledger_funding_probe" not in li._tables(con)

    entrada = [({"wallet": W, "open_ts": T0, "close_ts": T0 + 20 * H,
                 "coin": "BTC"}, 24.0)]
    quedan, probados = li._sacar_probados_vacios(con, entrada)
    con.close()

    assert probados == 0, "excuso un ciclo sin tener una sola prueba hecha"
    assert quedan == entrada, "se comio el ciclo en vez de dejarlo en rojo"


def test_una_prueba_de_OTRA_wallet_no_excusa_a_esta(tl, db):
    con = db()
    _pos(con, wallet=W)
    _fund(con, T0 - 2 * H, wallet=W)
    _fund(con, T0 + 22 * H, wallet=W)
    con.close()
    tl._anotar_probe(W2, T0 - 5 * H, T0 + 30 * H, 0)

    vs = li.check_invariants()
    assert any("tramo mudo" in v.invariant for v in vs), [v.invariant for v in vs]


def test_una_prueba_que_SI_trajo_filas_no_excusa_nada(tl, db):
    """found>0 significa que la reparacion funciono. Si el ciclo igual quedo
    mudo es un problema distinto, y taparlo con la prueba lo esconderia."""
    con = db()
    _pos(con, wallet=W)
    _fund(con, T0 - 2 * H, wallet=W)
    _fund(con, T0 + 22 * H, wallet=W)
    con.close()
    tl._anotar_probe(W, T0 - 5 * H, T0 + 30 * H, 7)

    vs = li.check_invariants()
    assert any("tramo mudo" in v.invariant for v in vs), [v.invariant for v in vs]


# ─── 4. lo que no se traga ──────────────────────────────────────────────────

def test_una_falla_de_red_no_se_convierte_en_tramo_probado(tl, db, monkeypatch):
    """Si un fallo de transporte contara como "probado vacio", una caida
    momentanea de HL borraria la violacion para siempre. Es exactamente el
    default silencioso en el money path que la doctrina prohibe."""
    import httpx
    con = db()
    _pos(con)
    _fund(con, T0 - 2 * H)
    _fund(con, T0 + 22 * H)
    con.close()

    import modules.portfolio as pf
    monkeypatch.setattr(pf, "_info", _hl([httpx.ConnectError("sin ruta")] * 9, []))
    monkeypatch.setattr(tl, "PAGE_MAX_ATTEMPTS", 2)

    with pytest.raises(tl.LedgerSyncError):
        asyncio.run(tl.backfill_funding_gaps(W))

    con = db()
    n = con.execute("SELECT COUNT(*) c FROM ledger_funding_probe").fetchone()["c"]
    con.close()
    assert n == 0, "una falla de red quedo anotada como prueba"
    assert any("tramo mudo" in v.invariant for v in li.check_invariants())


def test_el_backfill_esta_acotado_por_corrida(tl, db, monkeypatch):
    """Cada pagina pesa 20 contra el presupuesto de 1200/min por IP — el mismo
    que ya tropezo. Una reparacion que dispara 429s se convierte en el
    problema que venia a resolver."""
    con = db()
    for i in range(6):
        _pos(con, coin=f"C{i}", open_ts=T0 + i * 100 * H,
             close_ts=T0 + i * 100 * H + 20 * H)
        _fund(con, T0 + i * 100 * H - 2 * H)
        _fund(con, T0 + i * 100 * H + 22 * H)
    con.close()

    vistos: list[dict] = []
    import modules.portfolio as pf
    monkeypatch.setattr(pf, "_info", _hl([[]] * 20, vistos))

    r = asyncio.run(tl.backfill_funding_gaps(W, max_gaps=2))
    assert r["probed"] == 2, r
    assert len(vistos) == 2, vistos


def test_sin_huecos_no_se_le_pide_nada_a_HL(tl, db, monkeypatch):
    con = db()
    _pos(con, funding_net=-3.0)
    _fund(con, T0 + 5 * H, -3.0)
    con.close()

    vistos: list[dict] = []
    import modules.portfolio as pf
    monkeypatch.setattr(pf, "_info", _hl([[]], vistos))

    assert asyncio.run(tl.backfill_funding_gaps(W))["gaps"] == 0
    assert vistos == [], "se gasto una llamada sin nada que reparar"
