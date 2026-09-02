"""R-BOT-DEFINITIVE Fase 6 — tests de /trackrecord.

Lo que se protege aca es una sola propiedad, y es la que hace que un track
record sea un track record: los ciclos salen de la columna `cycle_tag` que
esta GUARDADA en la fila, no de mirar las fechas al momento de renderizar.

Si se infirieran por fecha, el mismo historico daria numeros distintos cada
vez que alguien toque la heuristica de agrupacion — y no habria manera de
saber cual de las dos versiones estuvo mal. Un numero publico que se puede
reescribir solo no sirve para nada.

Los tests de abajo construyen casos donde la fecha y el tag NO coinciden a
proposito: dos ciclos abiertos el mismo dia, un ciclo que cruza dias, patas
sueltas entre medio. Si el render adivinara, fallarian.
"""
from __future__ import annotations

import importlib
import sqlite3

import pytest

DIA = 86_400_000


@pytest.fixture()
def tr(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import config
    importlib.reload(config)
    import modules.trade_ledger as tl
    importlib.reload(tl)
    import modules.track_record as trm
    importlib.reload(trm)
    tl._conn().close()          # crea el esquema
    return tl, trm


def _insertar(tl, *, wallet="0xaaa", coin="BTC", open_ts, close_ts,
              gross=0.0, fees=0.0, funding=0.0, margen=1000.0, tag=None,
              side="LONG"):
    net = gross - abs(fees) + funding
    con = tl._conn()
    try:
        con.execute(
            "INSERT INTO ledger_positions (wallet, coin, side, open_ts,"
            " close_ts, avg_entry, avg_exit, max_size, notional_open,"
            " margin_open, leverage, fees_total, funding_net, gross_pnl,"
            " net_pnl, roe_pct, cycle_tag)"
            " VALUES (?,?,?,?,?,1,1,1,?,?,3,?,?,?,?,?,?)",
            (wallet, coin, side, open_ts, close_ts, margen * 3, margen,
             abs(fees), funding, gross, net,
             (100.0 * net / margen) if margen else None, tag))
        con.commit()
    finally:
        con.close()


# ── LA propiedad ───────────────────────────────────────────────────────────
def test_los_ciclos_salen_del_tag_guardado_y_no_de_las_fechas(tr):
    """Dos ciclos DISTINTOS abiertos y cerrados el MISMO dia.

    Cualquier agrupacion por fecha los fusionaria en uno solo y publicaria un
    NET que nunca existio. El tag guardado dice que son dos.
    """
    tl, trm = tr
    base = 1_700_000_000_000
    _insertar(tl, coin="BTC", open_ts=base, close_ts=base + 3600_000,
              gross=100, fees=10, tag="ciclo 2023-11-14")
    _insertar(tl, coin="ETH", open_ts=base + 60_000, close_ts=base + 3600_000,
              gross=50, fees=5, tag="ciclo 2023-11-14")
    _insertar(tl, coin="SOL", open_ts=base + 120_000, close_ts=base + 7200_000,
              gross=20, fees=2, tag="ciclo 2023-11-14 #2")
    _insertar(tl, coin="HYPE", open_ts=base + 180_000, close_ts=base + 7200_000,
              gross=30, fees=3, tag="ciclo 2023-11-14 #2")

    out = trm.build_track_record()
    tags = [c["tag"] for c in out["ciclos"]]
    assert tags == ["ciclo 2023-11-14", "ciclo 2023-11-14 #2"], (
        "se fusionaron dos ciclos del mismo dia: el render esta agrupando por "
        "fecha en vez de leer cycle_tag")
    assert out["ciclos"][0]["patas"] == 2
    assert out["ciclos"][1]["patas"] == 2


def test_un_ciclo_que_cruza_varios_dias_sigue_siendo_uno(tr):
    """El espejo del test anterior: agrupar por fecha tambien PARTIRIA un
    ciclo largo en varios. El tag lo mantiene entero."""
    tl, trm = tr
    base = 1_700_000_000_000
    for i, coin in enumerate(("BTC", "ETH", "SOL")):
        _insertar(tl, coin=coin, open_ts=base + i * DIA,
                  close_ts=base + 9 * DIA, gross=100, fees=10,
                  tag="ciclo largo")
    out = trm.build_track_record()
    assert len(out["ciclos"]) == 1
    assert out["ciclos"][0]["patas"] == 3
    assert out["ciclos"][0]["duracion_ms"] == 9 * DIA


def test_las_patas_sueltas_no_se_meten_en_el_ciclo_mas_cercano(tr):
    """Una operacion sin tag no es canasta. Meterla en el ciclo vecino para
    que la tabla quede prolija seria publicar un numero falso."""
    tl, trm = tr
    base = 1_700_000_000_000
    _insertar(tl, coin="BTC", open_ts=base, close_ts=base + DIA,
              gross=100, fees=10, tag="ciclo A")
    _insertar(tl, coin="ETH", open_ts=base, close_ts=base + DIA,
              gross=100, fees=10, tag="ciclo A")
    _insertar(tl, coin="DOGE", open_ts=base + 1000, close_ts=base + DIA,
              gross=-500, fees=10, tag=None)

    out = trm.build_track_record()
    assert len(out["ciclos"]) == 1
    assert out["ciclos"][0]["patas"] == 2
    assert out["ciclos"][0]["net"] == pytest.approx(180.0)
    assert len(out["sueltas"]) == 1
    # ...pero SI entra en el total all-time: no se la esconde.
    assert out["total"]["net"] == pytest.approx(180.0 - 510.0)
    texto = trm.format_track_record(out)
    assert "sueltas" in texto.lower()


# ── la formula ─────────────────────────────────────────────────────────────
def test_net_es_gross_menos_fees_mas_funding(tr):
    tl, trm = tr
    base = 1_700_000_000_000
    _insertar(tl, open_ts=base, close_ts=base + DIA,
              gross=1000, fees=120, funding=-30, tag="ciclo X")
    _insertar(tl, coin="ETH", open_ts=base, close_ts=base + DIA,
              gross=-200, fees=40, funding=90, tag="ciclo X")
    c = trm.build_track_record()["ciclos"][0]
    assert c["gross"] == pytest.approx(800.0)
    assert c["fees"] == pytest.approx(160.0)
    assert c["funding"] == pytest.approx(60.0)
    assert c["net"] == pytest.approx(800.0 - 160.0 + 60.0)
    assert c["cuadra"] is True


def test_un_net_que_no_cuadra_se_denuncia_en_vez_de_publicarse(tr):
    """Si la fila guardada dice un NET que no es gross-fees+funding, el track
    record lo dice ARRIBA del numero. No se corrige en silencio ni se publica
    como si nada: las dos cosas serian inventar un dato."""
    tl, trm = tr
    base = 1_700_000_000_000
    _insertar(tl, open_ts=base, close_ts=base + DIA, gross=100, fees=10,
              tag="ciclo roto")
    _insertar(tl, coin="ETH", open_ts=base, close_ts=base + DIA, gross=100,
              fees=10, tag="ciclo roto")
    con = tl._conn()
    con.execute("UPDATE ledger_positions SET net_pnl=99999 WHERE coin='ETH'")
    con.commit()
    con.close()

    out = trm.build_track_record()
    assert out["descuadres"] == ["ciclo roto"]
    texto = trm.format_track_record(out)
    assert "no coincide" in texto
    assert texto.index("no coincide") < texto.index("TOTAL ALL-TIME"), (
        "la advertencia tiene que ir ANTES del total, no despues: quien copie "
        "el numero tiene que verla primero")


# ── split por wallet y totales ─────────────────────────────────────────────
def test_un_ciclo_en_dos_wallets_muestra_el_split_y_el_combinado(tr):
    tl, trm = tr
    base = 1_700_000_000_000
    _insertar(tl, wallet="0xaaa", coin="BTC", open_ts=base,
              close_ts=base + DIA, gross=300, fees=20, tag="ciclo dual")
    _insertar(tl, wallet="0xbbb", coin="ETH", open_ts=base,
              close_ts=base + DIA, gross=100, fees=10, tag="ciclo dual")
    c = trm.build_track_record()["ciclos"][0]
    assert set(c["por_wallet"]) == {"0xaaa", "0xbbb"}
    assert c["por_wallet"]["0xaaa"]["net"] == pytest.approx(280.0)
    assert c["por_wallet"]["0xbbb"]["net"] == pytest.approx(90.0)
    assert c["net"] == pytest.approx(370.0)


def test_el_acumulado_avanza_ciclo_a_ciclo_y_cierra_en_el_total(tr):
    tl, trm = tr
    base = 1_700_000_000_000
    for i, (tag, g) in enumerate((("ciclo 1", 100), ("ciclo 2", -40),
                                  ("ciclo 3", 250))):
        for coin in ("BTC", "ETH"):
            _insertar(tl, coin=coin, open_ts=base + i * 10 * DIA,
                      close_ts=base + (i * 10 + 5) * DIA, gross=g / 2, tag=tag)
    out = trm.build_track_record()
    assert [c["tag"] for c in out["ciclos"]] == ["ciclo 1", "ciclo 2", "ciclo 3"]
    assert out["total"]["net"] == pytest.approx(310.0)
    assert out["total"]["ciclos"] == 3
    texto = trm.format_track_record(out)
    assert "TOTAL ALL-TIME" in texto
    assert "3 ciclo(s)" in texto


def test_ledger_incompleto_avisa_arriba_de_todo(tr):
    """Si el ledger esta degradado, el track record TAMBIEN lo esta. El aviso
    viaja pegado al dato, no en otra pantalla que nadie va a abrir."""
    tl, trm = tr
    base = 1_700_000_000_000
    _insertar(tl, open_ts=base, close_ts=base + DIA, gross=100, tag="ciclo A")
    _insertar(tl, coin="ETH", open_ts=base, close_ts=base + DIA, gross=100,
              tag="ciclo A")
    tl.set_sync_health("0xaaa", ok=False, fills_ok=False,
                       detail="HL rate limit 429")
    texto = trm.format_track_record()
    assert "LEDGER INCOMPLETO" in texto
    assert texto.index("LEDGER INCOMPLETO") < texto.index("ciclo A")


def test_sin_ciclos_lo_dice_en_vez_de_mostrar_cero(tr):
    _tl, trm = tr
    texto = trm.format_track_record()
    assert "Todavia no hay ciclos" in texto
    assert "TOTAL ALL-TIME" not in texto, (
        "un total de $0.00 sin operaciones se lee como una perdida neutra, "
        "no como 'no hay datos'")


def test_el_comando_esta_registrado_con_su_handler():
    import bot
    from commands_registry import COMMANDS
    cmd = next((c for c in COMMANDS if c.command == "trackrecord"), None)
    assert cmd is not None, "/trackrecord no figura en el registro"
    assert hasattr(bot, cmd.handler_name)
    assert bot.HANDLER_MAP.get("trackrecord") is not None, (
        "el comando esta declarado pero no se registra ningun handler: "
        "en Telegram no haria nada")
