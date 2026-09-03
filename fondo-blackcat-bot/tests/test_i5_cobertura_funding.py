"""R-I5-COBERTURA (2026-09-03) — I5 afirmaba una causa que nunca verifico.

EL DEFECTO
==========
La linea que salia por /diagnostico era:

    27 ciclo(s) de mas de 1h con funding_net = 0.00 exacto (el mas largo,
    20h). El funding de HL se acredita por hora: un cero exacto es un dato
    que falta, no un mercado tranquilo.

La primera oracion es un hecho contado. La segunda es una CONJETURA impresa
con formato de hallazgo — la misma familia de bug que este proyecto ya
persiguio en "falta GITHUB_TOKEN y/o GITHUB_REPO" y en "rancios 4".

El chequeo miraba dos columnas de ledger_positions y de ahi concluia sobre el
estado de ledger_funding, una tabla que no leia. Existe una tercera
posibilidad que no es "mercado tranquilo" ni "bug": que el ciclo sea ANTERIOR
a la primera acreditacion que tenemos guardada. userFillsByTime y userFunding
alcanzan horizontes distintos, asi que el ledger reconstruye ciclos viejos
desde fills que si llegan, con funding que no llega.

POR QUE IMPORTA Y NO ES UNA SUTILEZA
====================================
Esos ciclos no se pueden reparar. Ningun resync los va a llenar, porque el
dato no existe de nuestro lado. O sea: una cruz roja PERMANENTE sobre filas
que nadie puede tocar. Una falla que no se puede cerrar entrena a ignorar el
panel entero — es literalmente lo que hacia la linea de redeploy en Railway
antes de R-RAILWAY-VARS, y se corrigio por la misma razon.

Y al reves: el bug real queda TAPADO. Si de verdad se abre un agujero de
funding adentro del tramo con datos, se suma a un contador que ya venia en 27
y no lo nota nadie.

LO QUE ESTOS TESTS FIJAN
========================
Que el chequeo distinga los tres casos LEYENDO ledger_funding, y que solo dos
sean violaciones. El tercero se reporta como nota, porque descartarlo en
silencio manda a la proxima ronda a investigar lo mismo desde cero.
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
W2 = "0x00bb6858a1f2d4e3c5b7a9086f4c2d1e3a5b7c90"
H = 3_600_000
DIA = 86_400_000
T0 = 1_700_000_000_000


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    import modules.trade_ledger as tl
    monkeypatch.setattr(tl, "DB_PATH", str(tmp_path / "ledger.db"))
    tl._conn().close()

    def _open():
        c = sqlite3.connect(str(tmp_path / "ledger.db"))
        c.row_factory = sqlite3.Row
        return c
    return _open


def _pos(con, wallet=W, **kw):
    """Ciclo de 4h con la formula CERRADA (100 - 2 + funding = net).

    Que I1 cierre no es decorativo: si I1 saltara tambien, un test de I5 que
    solo mira "hay alguna violacion" pasaria por el motivo equivocado.
    """
    fn = kw.pop("funding_net", 0.0)
    d = dict(wallet=wallet, coin="BTC", side="LONG", open_ts=T0,
             close_ts=T0 + 4 * H, avg_entry=100.0, avg_exit=110.0,
             max_size=10.0, notional_open=1000.0, margin_open=200.0,
             leverage=5.0, leverage_source="derived", open_fills=1,
             close_fills=1, fees_total=2.0, funding_net=fn,
             gross_pnl=100.0, net_pnl=98.0 + fn,
             roe_pct=100.0 * (98.0 + fn) / 200.0, cycle_tag=None)
    d.update(kw)
    cols = ",".join(d)
    con.execute(f"INSERT INTO ledger_positions ({cols}) "
                f"VALUES ({','.join('?' * len(d))})", tuple(d.values()))
    con.commit()
    return d


def _fills_del_ciclo(con, d):
    """Los dos fills que sostienen el ciclo de ``_pos``: fees 1+1=2, gross 100.

    Hacen falta cuando el test llama a ``run_all``, que ademas de los
    invariantes corre el recomputo independiente. Sin fills salta R1 ("el
    ciclo existe pero no hay ni un fill en su intervalo") y el test pasaria o
    fallaria por un motivo que no es el que dice medir.
    """
    for tid, (t, fee, closed) in enumerate((
            (int(d["open_ts"]), 1.0, 0.0),
            (int(d["close_ts"]), 1.0, 100.0))):
        con.execute(
            "INSERT INTO ledger_fills (wallet,tid,coin,side,px,sz,time,fee,"
            "closed_pnl) VALUES (?,?,?,?,?,?,?,?,?)",
            (d["wallet"], tid, d["coin"], "B", 100.0, 1.0, t, fee, closed))
    con.commit()


def _fund(con, t, usdc=-1.5, wallet=W, coin="BTC"):
    con.execute("INSERT INTO ledger_funding (wallet,time,coin,usdc) "
                "VALUES (?,?,?,?)", (wallet, t, coin, usdc))
    con.commit()


def _inv(vs):
    return [v.invariant for v in vs]


def _hay(vs, frag):
    return any(frag in v.invariant for v in vs)


# ─── 1. los dos casos que SI son violaciones ────────────────────────────────

def test_wallet_sin_ninguna_acreditacion_grita_y_lo_dice_asi(ledger):
    """La forma pura del bug D1: userFunding se tragaba y quedaban ceros."""
    con = ledger()
    _pos(con, close_ts=T0 + 40 * H)
    con.close()

    vs, notas = li.check_con_notas()
    assert _hay(vs, "sin ninguna acreditacion"), (
        f"con CERO filas en ledger_funding el 0.00 es el bug, no un limite "
        f"del horizonte: {_inv(vs)}")
    assert not _hay(vs, "I1"), "el caso esta armado para que la formula cierre"
    assert not notas, "esto no es una nota, es una falla"


def test_la_violacion_nombra_la_wallet_porque_es_donde_se_arregla(ledger):
    con = ledger()
    _pos(con, close_ts=T0 + 40 * H)
    con.close()
    v = next(v for v in li.check_invariants()
             if "sin ninguna acreditacion" in v.invariant)
    assert W[:8] in v.detail
    assert v.row.get("wallet") == W


def test_cero_rodeado_de_acreditaciones_es_un_agujero_real(ledger):
    """El caso que el chequeo viejo TAPABA.

    Hay funding de antes y de despues del ciclo, y del ciclo no. Eso no lo
    explica ningun horizonte de la API: es un hueco. Antes se sumaba al mismo
    contador que ya venia en 27 por otro motivo, asi que no lo notaba nadie.
    """
    con = ledger()
    _pos(con, open_ts=T0, close_ts=T0 + 40 * H)
    _fund(con, T0 - 5 * DIA)          # cobertura empieza ANTES del ciclo
    _fund(con, T0 + 60 * H)           # y termina DESPUES
    con.close()

    vs, notas = li.check_con_notas()
    assert _hay(vs, "tramo mudo"), (
        f"un cero rodeado de acreditaciones es un agujero y tiene que gritar: "
        f"{_inv(vs)}")
    assert not notas


# ─── 2. el caso que NO es una violacion ─────────────────────────────────────

def test_ciclo_anterior_al_horizonte_del_funding_no_es_violacion(ledger):
    """Los fills llegan mas atras que el funding. Ese cero no es reparable.

    Marcarlo en rojo es prometer un arreglo que no existe, y una cruz que no
    se puede cerrar termina apagando el panel entero.
    """
    con = ledger()
    _pos(con, open_ts=T0, close_ts=T0 + 40 * H)
    _fund(con, T0 + 100 * DIA)        # la PRIMERA acreditacion es posterior
    con.close()

    vs, notas = li.check_con_notas()
    assert not any("I5" in v.invariant for v in vs), (
        f"un ciclo anterior a la primera acreditacion guardada no prueba "
        f"ningun bug: {_inv(vs)}")
    assert notas, "tampoco puede desaparecer en silencio"
    assert "no cuenta como violacion" in notas[0]


def test_la_nota_dice_desde_cuando_hay_datos(ledger):
    """Sin la fecha del borde la nota es tan conjetural como lo que reemplaza:
    es el unico numero que permite chequearla contra la realidad."""
    con = ledger()
    _pos(con, open_ts=T0, close_ts=T0 + 40 * H)
    _fund(con, 1_735_689_600_000)     # 2025-01-01T00:00:00Z
    con.close()

    notas = li.check_con_notas()[1]
    assert "2025-01-01" in notas[0], notas


def test_ciclo_a_caballo_del_borde_tampoco_se_acusa(ledger):
    """Abre antes de que empiece la cobertura y cierra despues.

    Parte de su funding es genuinamente inalcanzable, asi que no se puede
    afirmar que falte por un bug. Ante la duda, nota — no cruz roja.
    """
    con = ledger()
    _pos(con, open_ts=T0, close_ts=T0 + 40 * H)
    _fund(con, T0 + 20 * H)           # la cobertura arranca DENTRO del ciclo
    con.close()

    vs, notas = li.check_con_notas()
    assert not any("I5" in v.invariant for v in vs), _inv(vs)
    assert notas


def test_un_ciclo_con_funding_real_no_aparece_por_ningun_lado(ledger):
    con = ledger()
    _pos(con, funding_net=-4.25, close_ts=T0 + 40 * H)
    _fund(con, T0 + 2 * H, -4.25)
    con.close()
    vs, notas = li.check_con_notas()
    assert not any("I5" in v.invariant for v in vs), _inv(vs)
    assert not notas


def test_ciclo_corto_sigue_sin_contar(ledger):
    """20 minutos pueden no cruzar ninguna hora de funding."""
    con = ledger()
    _pos(con, open_ts=T0, close_ts=T0 + 20 * 60_000)
    con.close()
    vs, notas = li.check_con_notas()
    assert not any("I5" in v.invariant for v in vs)
    assert not notas


# ─── 3. la ventana es por wallet, no por moneda ─────────────────────────────

def test_la_ventana_no_se_parte_por_moneda(ledger):
    """El funding se trae con UN cursor por wallet, para todas las monedas.

    Si la ventana se calculara por (wallet, coin), cualquier moneda sin
    acreditaciones propias caeria siempre en "fuera de cobertura" y el
    agujero real quedaria excusado para siempre. Aca ETH no tiene ni una fila
    de funding, pero la wallet si — y el ciclo de ETH cae adentro de ese
    tramo, asi que es un agujero.
    """
    con = ledger()
    _pos(con, coin="ETH", open_ts=T0, close_ts=T0 + 40 * H)
    _fund(con, T0 - DIA, coin="BTC")
    _fund(con, T0 + 60 * H, coin="BTC")
    con.close()

    vs = li.check_invariants()
    assert _hay(vs, "tramo mudo"), _inv(vs)


def test_cada_wallet_se_juzga_con_su_propia_ventana(ledger):
    """Una wallet con cobertura no puede excusar a otra que no tiene ninguna."""
    con = ledger()
    _pos(con, wallet=W, open_ts=T0, close_ts=T0 + 40 * H)
    _fund(con, T0 - DIA, wallet=W)
    _fund(con, T0 + 60 * H, wallet=W)
    _pos(con, wallet=W2, open_ts=T0, close_ts=T0 + 40 * H)
    con.close()

    vs = li.check_invariants()
    assert _hay(vs, "tramo mudo"), _inv(vs)
    sin = [v for v in vs if "wallet sin ninguna acreditacion" in v.invariant]
    assert len(sin) == 1 and sin[0].row.get("wallet") == W2, _inv(vs)


# ─── 3bis. la FORMA del agujero (R-I5-FORMA) ────────────────────────────────
#
# Produccion contesto que los 27 ciclos caen enteros adentro de la ventana, o
# sea que la hipotesis del horizonte era falsa para esas filas. Pero "adentro
# de la ventana" es una etiqueta sobre el MIN/MAX de la wallet: no dice nada
# sobre que hay adentro del intervalo del ciclo. Tres causas distintas caian
# todas en ese mismo cartel, y cada una se arregla en otro archivo.

def test_si_hay_acreditaciones_de_su_moneda_no_falta_ningun_dato(ledger):
    """El falso positivo mas caro: acreditaciones que EXISTEN y suman cero.

    Una posicion chica paga polvo que redondea a 0.00. R4 ya coteja guardado
    contra recomputo, asi que el numero esta bien calculado. Llamarlo agujero
    manda a buscar un dato que esta ahi, y ningun resync lo iba a callar.
    """
    con = ledger()
    _pos(con, open_ts=T0, close_ts=T0 + 40 * H)
    _fund(con, T0 - DIA)                       # ventana abre antes
    _fund(con, T0 + 10 * H, usdc=0.0)          # ADENTRO, y suma cero
    _fund(con, T0 + 20 * H, usdc=0.0)
    _fund(con, T0 + 60 * H)                    # y cierra despues
    con.close()

    vs, notas = li.check_con_notas()
    assert not any("I5" in v.invariant for v in vs), (
        f"las filas estan y suman cero: no falta ningun dato: {_inv(vs)}")
    assert any("polvo de redondeo" in n for n in notas), notas


def test_sin_filas_de_su_moneda_pero_con_otras_acusa_al_nombre(ledger):
    """La wallet SI cobraba en esa franja, pero para otra moneda.

    Eso descarta la ingesta: el paginado trajo esas horas. Lo que no cuadra es
    el nombre con el que cada lado guarda la moneda. Mandarlo al paginado
    seria releer bien lo que ya esta bien leido.
    """
    con = ledger()
    _pos(con, coin="ETH", open_ts=T0, close_ts=T0 + 40 * H)
    _fund(con, T0 - DIA, coin="BTC")
    _fund(con, T0 + 10 * H, coin="BTC")        # ADENTRO, otra moneda
    _fund(con, T0 + 20 * H, coin="SOL")
    _fund(con, T0 + 60 * H, coin="BTC")
    con.close()

    vs = li.check_invariants()
    v = next((v for v in vs if "cobrando en el mismo rato" in v.invariant), None)
    assert v is not None, (
        f"con acreditaciones de otras monedas adentro del intervalo el "
        f"sospechoso es el nombre, no el paginado: {_inv(vs)}")
    assert not _hay(vs, "tramo mudo"), (
        f"un tramo con acreditaciones no es mudo: {_inv(vs)}")
    assert "BTC" in v.detail and "SOL" in v.detail, (
        f"sin las otras monedas la violacion no se puede cotejar: {v.detail}")
    assert v.row.get("coin_ciclo") == "ETH"


def test_sin_ninguna_fila_de_la_wallet_acusa_al_paginado(ledger):
    con = ledger()
    _pos(con, open_ts=T0, close_ts=T0 + 40 * H)
    _fund(con, T0 - DIA)
    _fund(con, T0 + 60 * H)
    con.close()

    vs = li.check_invariants()
    v = next((v for v in vs if "tramo mudo" in v.invariant), None)
    assert v is not None, _inv(vs)
    assert "userFunding" in v.detail, (
        f"tiene que nombrar donde se arregla: {v.detail}")


def test_el_silencio_se_mide_entre_borde_y_borde_no_por_el_ciclo(ledger):
    """El largo del ciclo dice cuanto del hueco VIMOS; el hueco real va de la
    ultima acreditacion previa a la primera posterior. Reportar el del ciclo
    subestima el problema justo cuando se lo compara contra el paginado."""
    con = ledger()
    _pos(con, open_ts=T0, close_ts=T0 + 40 * H)   # el ciclo dura 40h
    _fund(con, T0 - 10 * H)
    _fund(con, T0 + 70 * H)                        # el silencio dura 80h
    con.close()

    v = next(v for v in li.check_invariants() if "tramo mudo" in v.invariant)
    assert v.row.get("hueco_h") == 80.0, v.row
    assert "80h" in v.detail, v.detail


def test_sin_borde_posterior_no_se_inventa_un_numero(ledger):
    """Guarda defensiva de ``_silencio_horas``, probada como unidad A PROPOSITO.

    El primer intento de este test armaba una DB y miraba las violaciones. No
    media nada: por como esta definido el bucket "dentro" (``MIN <= open`` y
    ``close <= MAX``) los dos bordes SIEMPRE existen cuando se llega aca —
    ``close <= MAX`` garantiza una fila en ``close`` o despues. O sea que el
    camino sin borde es inalcanzable desde el llamador de hoy, y el test
    pasaba con la guarda rota. Una mutacion que devolvia 99.0 no lo hacia
    fallar; por eso se reescribio contra el helper.

    La guarda igual se queda: protege contra que un futuro toque al predicado
    de "dentro" convierta un borde ausente en un largo inventado, que despues
    se lee como evidencia contra el paginado.
    """
    con = ledger()
    _fund(con, T0 - DIA)              # hay borde previo, no posterior
    assert li._silencio_horas(con, W, T0, T0 + 40 * H) == 0.0
    assert li._silencio_horas(con, W2, T0, T0 + 40 * H) == 0.0   # ni uno
    con.close()


def test_el_bucket_mudo_solo_mira_la_wallet_del_ciclo(ledger):
    """Acreditaciones de OTRA wallet en el mismo rato no tapan el silencio."""
    con = ledger()
    _pos(con, wallet=W, open_ts=T0, close_ts=T0 + 40 * H)
    _fund(con, T0 - DIA, wallet=W)
    _fund(con, T0 + 60 * H, wallet=W)
    _fund(con, T0 + 20 * H, wallet=W2)     # adentro, pero de otra wallet
    con.close()

    vs = li.check_invariants()
    assert _hay(vs, "tramo mudo"), (
        f"el funding de otra wallet no es evidencia sobre esta: {_inv(vs)}")
    assert not _hay(vs, "cobrando en el mismo rato"), _inv(vs)


def test_las_tres_formas_conviven_sin_pisarse(ledger):
    """Guarda contra el arreglo de mas: colapsar los buckets en el primero que
    matchea volveria a dar un solo cartel para tres causas."""
    con = ledger()
    # redondeo (BTC en W): acreditaciones propias que suman cero
    _pos(con, coin="BTC", open_ts=T0, close_ts=T0 + 40 * H)
    _fund(con, T0 - DIA, coin="BTC")
    _fund(con, T0 + 5 * H, usdc=0.0, coin="BTC")
    # nombre (ETH en W): sin filas propias, con filas de BTC adentro
    _pos(con, coin="ETH", open_ts=T0 + 100 * H, close_ts=T0 + 140 * H)
    _fund(con, T0 + 110 * H, coin="BTC")
    # mudo (SOL en W): ni una fila de la wallet en el intervalo
    _pos(con, coin="SOL", open_ts=T0 + 200 * H, close_ts=T0 + 240 * H)
    _fund(con, T0 + 300 * H, coin="BTC")
    con.close()

    vs, notas = li.check_con_notas()
    assert _hay(vs, "cobrando en el mismo rato"), _inv(vs)
    assert _hay(vs, "tramo mudo"), _inv(vs)
    assert any("polvo de redondeo" in n for n in notas), notas


# ─── 4. las notas no se disfrazan de fallas ─────────────────────────────────

def test_una_nota_no_suma_al_total_ni_pone_ok_en_falso(ledger):
    """Si la nota contara como violacion, todo el arreglo seria cosmetico:
    el bloque seguiria diciendo "hay un problema" por un limite conocido."""
    con = ledger()
    _fills_del_ciclo(con, _pos(con, open_ts=T0, close_ts=T0 + 40 * H))
    _fund(con, T0 + 100 * DIA)
    con.close()

    out = li.run_all()
    assert out["notas"], "la nota tiene que viajar hasta /diagnostico"
    assert out["total"] == 0, out
    assert out["ok"] is True, out


def test_el_diagnostico_muestra_la_nota_y_no_como_violacion(ledger):
    from modules import diagnostics as diag

    con = ledger()
    _fills_del_ciclo(con, _pos(con, open_ts=T0, close_ts=T0 + 40 * H))
    _fund(con, T0 + 100 * DIA)
    con.close()

    d = {"invariantes": li.run_all()}
    texto = diag.format_diagnosis(d)
    linea = next(l for l in texto.split("\n") if "no cuenta como violacion" in l)
    assert "\u2139" in linea, f"la nota se ve igual que una violacion: {linea!r}"
    assert "\u2022" not in linea

    assert not [p for p in diag.detectar_problemas(d) if "invariante" in p], (
        "una nota no puede entrar en el resumen de *Problemas*: ahi es donde "
        "se decide si el bot esta sano")


# ─── 5. el envoltorio viejo sigue existiendo ────────────────────────────────

def test_check_invariants_sigue_devolviendo_una_lista(ledger):
    """Hay llamadores y tests que esperan list[Violation]. Si esto se rompe,
    se rompe en silencio: una tupla tambien es iterable y tambien es truthy."""
    con = ledger()
    _pos(con, funding_net=-1.0, close_ts=T0 + 40 * H)
    _fund(con, T0 + 2 * H, -1.0)
    con.close()

    out = li.check_invariants()
    assert isinstance(out, list)
    assert all(isinstance(v, li.Violation) for v in out)
