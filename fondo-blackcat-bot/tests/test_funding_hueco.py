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
    assert any("ni una fila nueva" in n for n in notas), notas


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


def test_una_tabla_de_pruebas_SIN_la_columna_nuevas_no_excusa_nada(tmp_path):
    """UNITARIO por el mismo motivo que el de arriba, y con el mismo riesgo.

    ``_conn()`` migra la tabla en cada conexion, asi que desde el llamador
    vivo esta rama no se alcanza y un test de integracion pasaria por el lado
    equivocado sin medir nada. La conexion entra por parametro, o sea que la
    rama existe y tiene que decir lo correcto.

    Y lo correcto es no excusar. Sin la columna no hay una sola medicion de
    filas nuevas: leer esa ausencia como "ninguna prueba trajo nada" bajaria
    los ciclos a nota por no tener el dato, que es la version mas directa de
    decir verde por no haber mirado.
    """
    con = sqlite3.connect(str(tmp_path / "sin_columna.db"))
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE ledger_funding_probe (wallet TEXT NOT NULL, "
                "a INTEGER NOT NULL, b INTEGER NOT NULL, probed_at TEXT NOT "
                "NULL, found INTEGER NOT NULL DEFAULT 0, "
                "PRIMARY KEY (wallet, a, b))")
    # Una prueba que ABARCA el ciclo y que con el criterio viejo (found=0) lo
    # excusaria. Sin la columna no se puede saber si trajo algo, asi que no.
    con.execute("INSERT INTO ledger_funding_probe VALUES (?,?,?,?,?)",
                (W, T0 - 5 * H, T0 + 30 * H, "2026-09-02T00:00:00+00:00", 0))
    con.commit()
    assert "ledger_funding_probe" in li._tables(con)

    entrada = [({"wallet": W, "open_ts": T0, "close_ts": T0 + 20 * H,
                 "coin": "BTC"}, 24.0)]
    quedan, probados = li._sacar_probados_vacios(con, entrada)
    con.close()

    assert probados == 0, "excuso un ciclo con una prueba que nunca se midio"
    assert quedan == entrada, "se comio el ciclo en vez de dejarlo en rojo"


def test_una_prueba_de_OTRA_wallet_no_excusa_a_esta(tl, db):
    con = db()
    _pos(con, wallet=W)
    _fund(con, T0 - 2 * H, wallet=W)
    _fund(con, T0 + 22 * H, wallet=W)
    con.close()
    tl._anotar_probe(W2, T0 - 5 * H, T0 + 30 * H, 0, 0)

    vs = li.check_invariants()
    assert any("tramo mudo" in v.invariant for v in vs), [v.invariant for v in vs]


def test_una_prueba_que_trajo_filas_NUEVAS_no_excusa_nada(tl, db):
    """Si entraron filas, la reparacion hizo algo. Que el ciclo siga mudo es
    un problema distinto, y taparlo con la prueba lo esconderia."""
    con = db()
    _pos(con, wallet=W)
    _fund(con, T0 - 2 * H, wallet=W)
    _fund(con, T0 + 22 * H, wallet=W)
    con.close()
    tl._anotar_probe(W, T0 - 5 * H, T0 + 30 * H, 7, 3)

    vs = li.check_invariants()
    assert any("tramo mudo" in v.invariant for v in vs), [v.invariant for v in vs]


def test_una_prueba_con_ECO_pero_sin_NOVEDAD_si_excusa(tl, db):
    """R-FUNDING-NOVEDAD — ESTE ES EL CASO QUE PRODUCCION TENIA ROTO.

    El test anterior decia "found>0 no excusa nada" y con eso alcanzaba,
    porque yo suponia que found=0 era alcanzable. No lo es: la ventana se le
    pide a HL acotada por acreditaciones que YA tenemos, asi que HL siempre
    devuelve al menos esos bordes. ``found`` mide el eco del pedido, no el
    resultado.

    Consecuencia real, medida en produccion: ``pruebas 14 (vacias 0) · filas
    traidas 564`` con la violacion intacta. Ninguna prueba podia bajar a nota
    nunca, asi que la I5 quedaba en rojo permanente aunque el dato no exista
    del lado de HL. La cruz que no se puede cerrar.

    Lo que decide es ``nuevas``: se volvio a pedir y no entro NADA.
    """
    con = db()
    _pos(con, wallet=W)
    _fund(con, T0 - 2 * H, wallet=W)
    _fund(con, T0 + 22 * H, wallet=W)
    con.close()
    tl._anotar_probe(W, T0 - 5 * H, T0 + 30 * H, 7, 0)

    vs, notas = li.check_con_notas()
    assert not any("tramo mudo" in v.invariant for v in vs), (
        f"HL contesto y no trajo nada nuevo: no hay resync que lo arregle, "
        f"dejarlo en rojo es una cruz que nadie puede cerrar — "
        f"{[v.invariant for v in vs]}")
    assert any("ni una fila nueva" in n for n in notas), notas


def test_una_prueba_vieja_SIN_MEDIR_no_excusa_nada(tl, db):
    """La migracion deja ``nuevas=-1`` en las pruebas anteriores al cambio.

    -1 es NO MEDIDO y no es 0. Leerlo como 0 daria por probado irreparable un
    tramo con una medicion que nunca se tomo, y encima lo haria de golpe sobre
    las 14 pruebas que ya hay en la base de produccion: 27 ciclos pasando de
    violacion a nota sin que nadie haya mirado nada. Se vuelven a probar.
    """
    con = db()
    _pos(con, wallet=W)
    _fund(con, T0 - 2 * H, wallet=W)
    _fund(con, T0 + 22 * H, wallet=W)
    con.execute(
        "INSERT INTO ledger_funding_probe (wallet, a, b, probed_at, found, "
        "nuevas) VALUES (?,?,?,?,?,?)",
        (W, T0 - 5 * H, T0 + 30 * H, "2026-09-03T00:00:00+00:00", 7, -1))
    con.commit()
    con.close()

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


# ─── 5. el tope de la corrida ───────────────────────────────────────────────
#
# R-FUNDING-NOVEDAD: el tope era 3 huecos por wallet por corrida, y el
# comentario que lo justificaba decia que lo que sobra "queda para la proxima".
# Lo escribi suponiendo syncs frecuentes. _ledger_sync_job corre a los 2
# minutos del arranque y despues cada LEDGER_SYNC_HOURS (6): "la proxima" son
# seis horas, y 33 huecos a 3 por corrida son medio dia con un numero de plata
# mal en el reporte. Ademas el tope contaba la variable que no se paga: el
# rate limit lo gasta la PAGINA, no el hueco.

def _n_huecos(con, n):
    """``n`` silencios separados, de una pagina cada uno."""
    for i in range(n):
        _pos(con, coin=f"C{i}", open_ts=T0 + i * 100 * H,
             close_ts=T0 + i * 100 * H + 20 * H)
        _fund(con, T0 + i * 100 * H - 2 * H)
        _fund(con, T0 + i * 100 * H + 22 * H)


def test_el_corte_de_la_corrida_lo_manda_el_presupuesto_de_PAGINAS(tl, db,
                                                                   monkeypatch):
    """Lo que se paga es la pagina, no el hueco.

    Con el tope puesto en huecos, un hueco de una pagina y uno de doce contaban
    igual — o sea que el numero que protegia el presupuesto no tenia relacion
    con el presupuesto. Aca se piden 10 huecos con un techo de 4 paginas: se
    tienen que probar 4 y parar, no 10.
    """
    con = db()
    _n_huecos(con, 10)
    con.close()

    vistos: list[dict] = []
    import modules.portfolio as pf
    monkeypatch.setattr(pf, "_info", _hl([[]] * 30, vistos))

    r = asyncio.run(tl.backfill_funding_gaps(W, page_budget=4))
    assert r["probed"] == 4, r
    assert r["pages"] == 4, r
    assert len(vistos) == 4, vistos


def test_el_tope_por_defecto_ya_no_deja_33_huecos_para_medio_dia(tl, db,
                                                                 monkeypatch):
    """El caso de produccion: 33 pendientes tienen que drenar en UNA corrida.

    Con el tope viejo de 3 por wallet y un sync cada 6h eran ~13 horas. El
    default nuevo esta dimensionado contra esa cadencia, no contra la
    frecuencia que yo suponia cuando escribi el 3.
    """
    con = db()
    _n_huecos(con, 12)
    con.close()

    vistos: list[dict] = []
    import modules.portfolio as pf
    monkeypatch.setattr(pf, "_info", _hl([[]] * 40, vistos))

    r = asyncio.run(tl.backfill_funding_gaps(W))
    assert r["probed"] == 12, (
        f"con el default nuevo los 12 tramos entran en una corrida: {r}")


def test_entre_huecos_tambien_se_PAUSA(tl, db, monkeypatch):
    """El pausado por pagina no alcanzaba y subir el tope lo volvia grave.

    ``_fetch_funding_window`` duerme entre paginas de UNA ventana, pero cada
    ventana arranca en ``i=0``, asi que N huecos de una pagina salian como N
    pedidos consecutivos sin ninguna espera. Con tope 3 era una rafaga chica;
    con el tope nuevo seria la reparacion disparando el 429 que vino a evitar.
    """
    con = db()
    _n_huecos(con, 5)
    con.close()

    monkeypatch.setattr(tl, "PAGE_PAUSE_SEC", 0.01)
    esperas: list[float] = []
    real = asyncio.sleep

    async def _spy(s, *a, **k):
        esperas.append(s)
        return await real(0)

    monkeypatch.setattr(tl.asyncio, "sleep", _spy)
    import modules.portfolio as pf
    monkeypatch.setattr(pf, "_info", _hl([[]] * 20, [])) 

    r = asyncio.run(tl.backfill_funding_gaps(W))
    assert r["probed"] == 5, r
    assert len(esperas) == 4, (
        f"5 tramos de una pagina = 4 pausas entre medio; hubo {esperas}")
    assert all(s > 0 for s in esperas), esperas


# ─── 6. el eco no es novedad ────────────────────────────────────────────────

def test_HL_que_devuelve_SOLO_lo_que_ya_teniamos_cuenta_como_sin_novedad(
        tl, db, monkeypatch):
    """EL CASO DE PRODUCCION, de punta a punta.

    La ventana se pide acotada por los bordes, o sea por acreditaciones que ya
    estan guardadas. HL las devuelve —tiene que devolverlas, estan adentro del
    rango— y ``found`` sube. Pero no entro nada: el silencio del medio sigue
    igual de mudo. Eso es "ya se pidio y no hay nada mas", no "la reparacion
    esta trayendo datos".

    Produccion lo mostro como ``pruebas 14 (vacias 0) · filas traidas 564`` y
    yo lo lei como avance. Este test fija la lectura correcta en los dos
    lugares donde importa: el tramo deja de repedirse y el ciclo baja a nota.
    """
    con = db()
    _pos(con)
    _fund(con, T0 - 2 * H)
    _fund(con, T0 + 22 * H)
    con.close()

    eco = [_entrada(T0 - 2 * H, -1.5), _entrada(T0 + 22 * H, -1.5)]
    import modules.portfolio as pf
    monkeypatch.setattr(pf, "_info", _hl([eco], []))

    r = asyncio.run(tl.backfill_funding_gaps(W))
    assert r["probed"] == 1 and r["rows"] == 0, (
        f"HL devolvio 2 filas y las 2 ya estaban: no entro ninguna — {r}")

    con = db()
    p = con.execute("SELECT found, nuevas FROM ledger_funding_probe").fetchone()
    con.close()
    assert (p["found"], p["nuevas"]) == (2, 0), (
        f"found es el eco del pedido, nuevas es el resultado: {tuple(p)}")

    assert tl.funding_gaps(W) == [], "se volveria a pedir lo mismo cada sync"
    vs, notas = li.check_con_notas()
    assert not any("tramo mudo" in v.invariant for v in vs), [v.invariant for v in vs]
    assert any("ni una fila nueva" in n for n in notas), notas


def test_la_migracion_deja_las_pruebas_viejas_en_NO_MEDIDO(tmp_path,
                                                           monkeypatch):
    """Una prueba de antes del cambio no puede nacer diciendo "sin novedad".

    En la base de produccion hay 14 pruebas con found>0 y sin medicion de
    filas nuevas. Si la migracion las pusiera en 0, los 27 ciclos pasarian de
    violacion a nota de golpe, sin que nadie haya medido nada: el panel se
    pondria verde por una columna con default mal elegido. -1 es NO MEDIDO y
    se vuelve a probar.
    """
    ruta = tmp_path / "vieja.db"
    con = sqlite3.connect(str(ruta))
    con.execute("CREATE TABLE ledger_funding_probe (wallet TEXT NOT NULL, "
                "a INTEGER NOT NULL, b INTEGER NOT NULL, probed_at TEXT NOT "
                "NULL, found INTEGER NOT NULL DEFAULT 0, "
                "PRIMARY KEY (wallet, a, b))")
    con.execute("INSERT INTO ledger_funding_probe VALUES (?,?,?,?,?)",
                (W, T0, T0 + H, "2026-09-02T00:00:00+00:00", 564))
    con.commit()
    con.close()

    import modules.trade_ledger as _tl
    monkeypatch.setattr(_tl, "DB_PATH", str(ruta))
    c = _tl._conn()
    try:
        fila = c.execute("SELECT found, nuevas FROM ledger_funding_probe"
                         ).fetchone()
    finally:
        c.close()
    assert fila["found"] == 564, "la migracion no puede perder lo que ya habia"
    assert fila["nuevas"] == -1, (
        f"NO MEDIDO, no cero: {fila['nuevas']} excusaria 27 ciclos sin prueba")
