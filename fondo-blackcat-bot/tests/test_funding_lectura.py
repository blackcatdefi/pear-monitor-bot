"""R-FUNDING-LECTURA (2026-09-03) — una reparacion que no se puede observar.

QUE PASO
========
La ronda anterior mando R-FUNDING-HUECO a produccion. El /diagnostico
siguiente mostro la MISMA violacion, palabra por palabra: 27 ciclos, el mas
largo de 20h. Y con el panel de entonces no habia forma de saber cual de estas
tres cosas habia pasado:

  a) el backfill todavia no llego a correr,
  b) corrio y fallo — el log.warning queda en Railway, que el panel no lee,
  c) corrio bien y no encontro ningun hueco para pedir.

Las tres se ven IDENTICAS desde afuera: la violacion sigue ahi. Y llevan a
arreglos opuestos: (a) es un problema de scheduler, (b) es un bug en el
pedido, (c) significa que el detector no encuentra lo que el chequeo si ve.

Esa es la misma falla que vengo persiguiendo hace cuatro rondas —una conjetura
con formato de hallazgo— pero esta vez en mi propio codigo: mande un arreglo
sin ningun modo de leer si se ejecuto. Estos tests fijan el readout.

LO QUE FIJAN, EN UNA LINEA
==========================
Que "nunca corrio" NO se pueda leer como "no hay nada que reparar", que un
error se PERSISTA hasta el panel en vez de morir en el log, y que ese error se
LIMPIE solo cuando deja de pasar — una cruz que no se puede cerrar entrena a
ignorar el panel entero, que es la leccion de R-RAILWAY-VARS.
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

W = "0xc7ae23316b47f7e75f455f53ad37873a18351505"
H = 3_600_000
T0 = 1_700_000_000_000


@pytest.fixture()
def tl(tmp_path, monkeypatch):
    import modules.trade_ledger as _tl
    monkeypatch.setattr(_tl, "DB_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(_tl, "PAGE_PAUSE_SEC", 0.0)
    monkeypatch.setattr(_tl, "ledger_wallets", lambda: {W: "fondo"})
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
    d = dict(wallet=wallet, coin="BTC", side="LONG", open_ts=T0,
             close_ts=T0 + 20 * H, avg_entry=100.0, avg_exit=110.0,
             max_size=10.0, notional_open=1000.0, margin_open=200.0,
             leverage=5.0, leverage_source="derived", open_fills=1,
             close_fills=1, fees_total=2.0, funding_net=0.0,
             gross_pnl=100.0, net_pnl=98.0, roe_pct=49.0, cycle_tag=None)
    d.update(kw)
    con.execute(f"INSERT INTO ledger_positions ({','.join(d)}) "
                f"VALUES ({','.join('?' * len(d))})", tuple(d.values()))
    con.commit()


def _fund(con, t, wallet=W, coin="BTC", usdc=-1.5):
    con.execute("INSERT INTO ledger_funding (wallet,time,coin,usdc) "
                "VALUES (?,?,?,?)", (wallet, t, coin, usdc))
    con.commit()


# ─── 1. nunca corrio NO es lo mismo que no hay nada que hacer ───────────────

def test_sin_ninguna_corrida_el_estado_dice_NUNCA(tl):
    """El caso que me dejo ciego. Si esto devolviera algo que se lee como
    "0 huecos, todo bien", un backfill que jamas se engancho al scheduler
    seria indistinguible de uno que ya termino su trabajo."""
    est = tl.funding_repair_status()
    assert est["ultimo_intento"] is None, "invento una corrida que no hubo"
    assert est["horas_desde_intento"] is None


def test_una_corrida_sin_huecos_deja_marca_igual(tl, db):
    """La marca se escribe aunque no haya un solo hueco. Sin esto, el estado
    "corri y no habia nada" seguiria siendo indistinguible de "no corri"."""
    con = db()
    _pos(con, funding_net=-3.0)          # nada roto: no hay hueco que pedir
    con.close()

    asyncio.run(_sync_con_backfill(tl))

    est = tl.funding_repair_status()
    assert est["ultimo_intento"] is not None, \
        "corrio y no dejo rastro: el panel no puede distinguirlo de no correr"
    assert est["pendientes_total"] == 0
    assert est["pruebas"] == 0, "anoto una prueba sin haberle pedido nada a HL"


def test_los_huecos_pendientes_se_cuentan(tl, db):
    con = db()
    _pos(con)
    _fund(con, T0 - 2 * H)
    _fund(con, T0 + 22 * H)
    con.close()

    est = tl.funding_repair_status()
    assert est["pendientes_total"] == 1, \
        f"no vio el hueco que el invariante si ve: {est}"


# ─── 2. el error llega al panel, no se queda en el log de Railway ───────────

async def _sync_con_backfill(tl, falla: Exception | None = None):
    """Corre el ``sync_all`` DE VERDAD, con el sync de wallet mockeado.

    Ojo con la tentacion de replicar el bloque de sync_all aca adentro: un
    test contra una copia del bucle pasa aunque el bucle real se borre. Se
    mockea lo de afuera (red, telegram) y se ejecuta la funcion de produccion.
    """
    async def _fake_backfill(w, max_gaps=3):
        if falla is not None:
            raise falla
        return {"gaps": 0, "probed": 0, "rows": 0}

    async def _fake_sync(w):
        return None

    async def _no_alert(*a, **k):
        return False

    tl_backfill = tl.backfill_funding_gaps
    tl_sync = tl.sync_wallet
    tl_alert = tl._alert
    tl.backfill_funding_gaps = _fake_backfill
    tl.sync_wallet = _fake_sync
    tl._alert = _no_alert
    try:
        return await tl.sync_all()
    finally:
        tl.backfill_funding_gaps = tl_backfill
        tl.sync_wallet = tl_sync
        tl._alert = tl_alert


def test_un_backfill_que_falla_deja_el_error_leible(tl):
    """El log.warning vive en Railway y el panel no lo lee. Sin persistir el
    error, una reparacion que falla en TODAS las corridas se ve exactamente
    igual que una que anda bien: la violacion sigue y nadie sabe por que."""
    asyncio.run(_sync_con_backfill(tl, falla=RuntimeError("HL dijo 429")))

    est = tl.funding_repair_status()
    assert est["ultimo_error"], "el error se perdio en el log"
    assert "429" in est["ultimo_error"], est["ultimo_error"]
    assert est["ultimo_intento"] is not None


def test_cuando_deja_de_fallar_el_error_se_limpia(tl):
    """Una cruz que no se puede cerrar entrena a ignorar el panel entero
    (R-RAILWAY-VARS). El error tiene que irse solo cuando el problema se fue,
    o en dos semanas nadie lee mas este bloque."""
    asyncio.run(_sync_con_backfill(tl, falla=RuntimeError("HL dijo 429")))
    assert tl.funding_repair_status()["ultimo_error"]

    asyncio.run(_sync_con_backfill(tl))

    assert tl.funding_repair_status()["ultimo_error"] is None, \
        "el error viejo sobrevivio a una corrida buena"


# ─── 3. el panel dice cual de los tres estados es ───────────────────────────

def _problemas(rep):
    from modules import diagnostics as dg
    return dg.detectar_problemas({"ledger": {"reparacion_funding": rep}})


def _render(rep):
    from modules import diagnostics as dg
    return dg.format_diagnosis(
        {"ledger": {"sync_por_wallet": {}, "reparacion_funding": rep}})


def test_el_panel_grita_cuando_nunca_se_ejecuto():
    p = _problemas({"ultimo_intento": None})
    assert any("NUNCA se ejecuto" in x for x in p), p
    assert "NUNCA se ejecuto" in _render({"ultimo_intento": None})


def test_el_panel_no_grita_cuando_corrio_bien():
    p = _problemas({"ultimo_intento": "2026-09-03T02:00:00+00:00",
                    "ultimo_error": None})
    assert not any("reparacion de funding" in x or "tramos mudos" in x
                   for x in p), p


def test_el_panel_grita_cuando_la_reparacion_falla():
    rep = {"ultimo_intento": "2026-09-03T02:00:00+00:00",
           "ultimo_error": "0xc7ae: LedgerSyncError: 429"}
    p = _problemas(rep)
    assert any("fallando" in x for x in p), p
    assert "429" in _render(rep)


def test_el_panel_distingue_pendientes_de_pruebas_hechas():
    """Dos numeros distintos que no se pueden colapsar: cuantos huecos quedan
    por pedir, y cuantos ya se pidieron. Uno solo no alcanza para saber si la
    reparacion avanza o gira en el lugar."""
    txt = _render({"ultimo_intento": "2026-09-03T02:00:00+00:00",
                   "horas_desde_intento": 0.2, "pendientes_total": 3,
                   "pruebas": 5, "sin_novedad": 2, "sin_medir": 1,
                   "filas_nuevas": 41, "filas_eco": 564})
    assert "3 hueco(s) pendiente(s)" in txt, txt
    assert "pruebas 5" in txt and "sin novedad 2" in txt, txt
    assert "sin medir 1" in txt, txt
    assert "filas NUEVAS 41" in txt, txt


def test_el_panel_no_confunde_el_ECO_de_HL_con_reparacion():
    """R-FUNDING-NOVEDAD — el defecto que este bloque tuvo una ronda entera.

    El panel imprimia ``filas traidas 564`` y ese 564 era lo que HL devolvio,
    casi todo filas que ya teniamos: la ventana se pide acotada por
    acreditaciones conocidas, asi que el pedido se devuelve a si mismo. Leido
    de corrido decia "la reparacion esta trayendo datos" cuando no habia
    entrado ni una fila. Un numero con formato de hallazgo, en el panel que
    escribi para no tener numeros con formato de hallazgo.

    El numero grande no puede quedar solo ni quedar primero.
    """
    txt = _render({"ultimo_intento": "2026-09-03T02:00:00+00:00",
                   "horas_desde_intento": 0.2, "pendientes_total": 33,
                   "pruebas": 14, "sin_novedad": 0, "sin_medir": 0,
                   "filas_nuevas": 0, "filas_eco": 564})
    assert "filas NUEVAS 0" in txt, txt
    assert "eco HL 564" in txt, txt
    # El eco no puede aparecer sin la novedad al lado: solo, vuelve a leerse
    # como reparacion.
    assert txt.index("filas NUEVAS 0") < txt.index("eco HL 564"), txt


def test_abajo_de_una_hora_el_panel_dice_minutos():
    """``hace 0h`` no distingue "corrio recien" de "corrio hace 50 minutos".

    Con un sync que arranca a los 2 minutos del deploy y despues cada 6 horas,
    esa es justo la diferencia entre dos lecturas de la MISMA corrida y dos
    corridas distintas — o sea entre comparar sus numeros y no poder.
    """
    base = {"ultimo_intento": "2026-09-03T02:00:00+00:00", "pendientes_total": 0,
            "pruebas": 0, "sin_novedad": 0, "filas_nuevas": 0, "filas_eco": 0}
    assert "hace 2min" in _render({**base, "horas_desde_intento": 0.033}), "2min"
    assert "hace 50min" in _render({**base, "horas_desde_intento": 0.833}), "50min"
    assert "hace 6h" in _render({**base, "horas_desde_intento": 6.0}), "6h"
    # Sin dato NO se inventa un cero, que se leeria como "recien corrio".
    assert "hace" not in _render({**base, "horas_desde_intento": None})
