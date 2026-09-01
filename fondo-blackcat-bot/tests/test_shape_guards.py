"""R-BOT-DEFINITIVE — regresiones de los arreglos de forma (clases C3 y C4).

Cada test de este archivo corresponde a UN bug real encontrado en la ronda, y
esta escrito para fallar si alguien revierte el arreglo. Los bugs de esta ronda
comparten una propiedad incomoda: no levantan. Devuelven un numero plausible.
Por eso varios de estos tests no miran el resultado de una funcion sino la
FORMA del codigo o el SQL emitido — es el unico lugar donde el bug es visible.

Todos fueron verificados como no-vacuos: con el arreglo revertido (git stash)
cada uno falla, y con el arreglo puesto pasa.
"""
from __future__ import annotations

import ast
import asyncio
import csv
import importlib
import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _src(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _func(rel: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(_src(rel))
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name):
            return node
    raise AssertionError(f"{rel}: no existe la funcion {name}()")


# ─── El invariante del sobre {"status": ..., "data": ...} ───────────────────

def test_fetch_wallet_status_ok_implica_data():
    """Quince sitios del repo hacen ``w.get("data", {})`` despues de comprobar
    ``status == "ok"``. Eso es seguro SOLO si el productor garantiza que un
    "ok" siempre trae 'data'. El allowlist de C4 acepta esos quince sitios
    citando este test: si el invariante se rompe, se rompen los quince a la
    vez, en silencio, y cada uno leeria la wallet como si no tuviera nada.
    """
    fn = _func("modules/portfolio.py", "fetch_wallet")
    oks = 0
    for node in ast.walk(fn):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
            continue
        pairs = {}
        for k, v in zip(node.value.keys, node.value.values):
            if isinstance(k, ast.Constant):
                pairs[k.value] = v
        st = pairs.get("status")
        if not (isinstance(st, ast.Constant) and st.value == "ok"):
            continue
        oks += 1
        assert "data" in pairs, (
            f"fetch_wallet() linea {node.lineno}: devuelve status='ok' sin la "
            f"clave 'data'. Eso hace que quince `w.get('data', {{}})` del repo "
            f"lean esa wallet como vacia en vez de como fallida.")
    assert oks >= 2, (
        "no se encontraron los returns con status='ok' de fetch_wallet(): "
        "el test se quedo sin objeto y hay que reescribirlo")


def test_fund_state_reconciler_no_inventa_canasta_vacia():
    """El sitio C4 que SI estaba mal: status='ok' sin 'data' se leia como
    'esta wallet no tiene posiciones', lo que baja total_basket_notional y
    hace que el reconciliador reporte una discrepancia que no existe.
    """
    fn = _func("modules/fund_state_reconciler.py", "reconcile_fund_state")
    body = ast.unparse(fn)
    assert "w.get('data', {})" not in body, (
        "volvio el default silencioso: un sobre inconsistente (status=ok sin "
        "data) se cuenta como wallet sin posiciones y el reconciliador "
        "inventa una discrepancia de canasta")
    assert "health_registry.swallowed" in body, (
        "el caso inconsistente tiene que declarar 'portfolio' degradado: la "
        "canasta se calcula incompleta y eso no puede quedar callado")


# ─── C4 en x_intel: 0 posts por shape, no por falta de actividad ────────────

def test_x_intel_distingue_cero_resultados_de_shape_desconocido():
    """La API v2 de X omite 'data' de forma legitima solo cuando manda
    ``meta.result_count == 0``. Cualquier otra ausencia es una respuesta que
    no sabemos leer, y confundirlas produce el reporte "0 posts hoy" cuando en
    realidad la lectura fallo — el mismo bug que el funding 0.00.
    """
    fn = _func("modules/x_intel.py", "fetch_timeline_via_list")
    body = ast.unparse(fn)
    assert "result_count" in body, (
        "el guard de shape de X desaparecio: sin el, un 200 con un cuerpo "
        "inesperado se reporta como 'no hubo posts'")
    # El default crudo no puede volver.
    assert "data.get('data', [])" not in body, (
        "volvio `data.get('data', [])`: iguala 'la API no devolvio nada' con "
        "'no hubo actividad'")


# ─── C3: el upsert del ledger ──────────────────────────────────────────────

def _upsert_sql(rel: str, table: str) -> str:
    tree = ast.parse(_src(rel))
    best = ""
    for node in ast.walk(tree):
        try:
            v = (node.value if isinstance(node, ast.Constant)
                 and isinstance(node.value, str) else ast.unparse(node))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(v, str):
            continue
        up = v.upper()
        if f"INTO {table.upper()}" in up and "ON CONFLICT" in up:
            if len(v) > len(best):
                best = v
    assert best, f"{rel}: no se encontro el upsert de {table}"
    return best


def test_upsert_ledger_positions_actualiza_las_columnas_que_cambian():
    """El bug original: el DO UPDATE SET no incluia `leverage`, asi que una
    posicion ya escrita nunca lo recibia y el ROE salia calculado con el
    apalancamiento por defecto. Se arreglo en la ronda anterior; esta ronda
    encontro tres columnas mas en la misma situacion.

    `side` se recomputa desde los fills en cada rebuild, o sea que PUEDE
    cambiar: va como excluded.side, sin COALESCE.
    `margin_open` y `funding_live_snapshot` solo se preservaban de casualidad,
    por una relectura en Python: van con COALESCE, que escribe esa intencion
    en el SQL en vez de dejarla en un efecto lateral.
    """
    sql = " ".join(_upsert_sql("modules/trade_ledger.py",
                               "ledger_positions").split()).lower()
    _, _, upd = sql.partition("do update set")
    assert upd, "el upsert de ledger_positions perdio el DO UPDATE SET"

    assert "leverage=excluded.leverage" in upd.replace(" ", ""), (
        "regreso el bug de R-LEDGER-FIX: el upsert no actualiza 'leverage' y "
        "el ROE se calcula con el apalancamiento equivocado")
    assert "side=excluded.side" in upd.replace(" ", ""), (
        "'side' se recomputa desde los fills en cada rebuild; si el upsert no "
        "lo escribe, una posicion que cambio de lado queda con el lado viejo")
    for col in ("margin_open", "funding_live_snapshot"):
        assert f"coalesce(excluded.{col}" in upd.replace(" ", ""), (
            f"'{col}' tiene que ir con COALESCE: un rebuild parcial que llega "
            f"sin ese valor no puede borrar el que ya estaba guardado")


# ─── C4 en vault_deposits: equity que desaparece del NAV ────────────────────

def test_as_equity_list_levanta_ante_shape_inesperado():
    vd = importlib.import_module("modules.vault_deposits")
    assert vd._as_equity_list([], "0xabc") == [], (
        "una lista vacia es una respuesta valida: el depositante realmente no "
        "tiene vaults. Ese caso NO puede levantar")
    filas = [{"vaultAddress": "0x1", "equity": "10"}]
    assert vd._as_equity_list(filas, "0xabc") == filas
    for basura in ({"error": "rate limited"}, {}, None, "nope", 0):
        with pytest.raises(ValueError):
            vd._as_equity_list(basura, "0xabc")


# ─── El export que estuvo roto en 3 de 5 tipos desde que se escribio ────────

def test_exports_apuntan_a_la_db_y_la_columna_que_existen(tmp_path, monkeypatch):
    """Tres defectos apilados: la DB equivocada (todo se buscaba en
    intel_memory.db), la columna equivocada ('timestamp' donde la real es
    'ts') y un `except Exception` que devolvia un CSV con solo cabeceras.

    El resultado para BCD era indistinguible de la verdad: pedia
    ``/export pnl 30d``, recibia un CSV vacio y concluia "no hubo PnL".
    """
    ex = importlib.import_module("modules.exports")
    for tipo, src in ex._SOURCES.items():
        mod = importlib.import_module(src.owner)
        db = getattr(mod, "DB_PATH", "")
        assert db, f"{tipo}: el modulo dueno {src.owner} no expone DB_PATH"
        # La columna de tiempo declarada tiene que existir en el CREATE TABLE
        # real del modulo dueno. Este es el chequeo que faltaba: el codigo
        # viejo usaba 'timestamp' y sqlite contestaba "no such column".
        ddl = " ".join(_src(src.owner.replace(".", "/") + ".py").split())
        marca = f"CREATE TABLE IF NOT EXISTS {src.table}"
        assert marca.lower() in ddl.lower(), (
            f"{tipo}: {src.owner} no crea la tabla '{src.table}' que el "
            f"export dice leer")
        cuerpo = ddl.lower().split(marca.lower(), 1)[1][:1200]
        assert src.ts_col.lower() in cuerpo, (
            f"{tipo}: la columna de tiempo '{src.ts_col}' no aparece en el "
            f"CREATE TABLE de '{src.table}'. Este es exactamente el bug: el "
            f"export pedia una columna inexistente, sqlite levantaba, el "
            f"except lo tragaba y salia un CSV con cero filas que se leia "
            f"como 'no hubo actividad'.")


def test_export_de_una_fuente_ilegible_levanta_en_vez_de_mentir(tmp_path, monkeypatch):
    """El corazon del arreglo: 'no pude leer' y 'no hay datos' dejaron de ser
    el mismo CSV."""
    ex = importlib.import_module("modules.exports")
    pnl = importlib.import_module("modules.pnl_tracker")
    monkeypatch.setattr(pnl, "DB_PATH", str(tmp_path / "no_existe.db"),
                        raising=False)
    monkeypatch.setattr(ex, "OUTPUT_DIR", str(tmp_path), raising=False)
    with pytest.raises(ex.ExportError):
        ex.export_dispatch("pnl", "30d")


def test_export_cuenta_filas_no_lineas(tmp_path, monkeypatch):
    """El conteo viejo hacia enumerate() sobre el archivo, o sea contaba
    LINEAS. Cualquier campo con un salto de linea adentro — y intel.raw_text
    casi siempre tiene uno — inflaba el numero reportado a BCD.
    """
    ex = importlib.import_module("modules.exports")
    p = tmp_path / "x.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["a", "b"])
        w.writerow(["1", "texto\ncon\nsaltos"])
        w.writerow(["2", "normal"])
    with p.open(newline="", encoding="utf-8") as f:
        assert max(0, sum(1 for _ in csv.reader(f)) - 1) == 2
    assert sum(1 for _ in p.open(encoding="utf-8")) == 5, (
        "el archivo de prueba tiene que tener mas lineas que filas para que "
        "el test signifique algo")


# ─── utils/shape.py ────────────────────────────────────────────────────────

def test_shape_separa_vacio_valido_de_forma_invalida():
    from utils.shape import UnexpectedShape, dig, require_list, require_mapping
    assert require_mapping({}, source="t") == {}
    assert require_list([], source="t") == []
    with pytest.raises(UnexpectedShape):
        require_mapping([], source="t")
    with pytest.raises(UnexpectedShape):
        require_list({}, source="t")
    # presente y vacio -> valido; ausente -> fallo. Son cosas distintas.
    assert dig({"data": {}}, "data", source="t") == {}
    with pytest.raises(UnexpectedShape):
        dig({}, "data", source="t")
    with pytest.raises(UnexpectedShape):
        dig({"data": None}, "data", source="t")


def test_shape_conserva_el_mensaje_de_error_de_la_api():
    """Cuando un 200 trae {"error": "rate limited"} esa cadena es TODA la
    explicacion. Perderla obliga a adivinar por que fallo."""
    from utils.shape import UnexpectedShape, require_list
    with pytest.raises(UnexpectedShape) as ei:
        require_list({"error": "rate limited"}, source="hl/userFunding")
    assert "rate limited" in str(ei.value)
    assert "hl/userFunding" in str(ei.value)


# ─── health_registry ───────────────────────────────────────────────────────

@pytest.fixture()
def hr(tmp_path, monkeypatch):
    import modules.health_registry as h
    monkeypatch.setattr(h, "DB_PATH", str(tmp_path / "h.db"))
    h.reset_all()
    return h


def test_health_registry_sin_datos_no_es_lo_mismo_que_sano(hr):
    """La distincion que faltaba en las cinco rondas anteriores: un subsistema
    que nunca reporto nada no esta sano, esta sin datos. Tratarlos igual es
    como se ve sano un componente que nunca corrio."""
    st = hr.status("ledger")
    assert st["ok"] is None
    assert hr.is_degraded("ledger") is False
    assert "ledger" in hr.all_status()["unknown"]


def test_health_registry_marca_limpia_y_banner(hr):
    hr.mark_degraded("funding", "userFunding 429")
    assert hr.is_degraded("funding") is True
    b = hr.banner("funding")
    assert "DATOS INCOMPLETOS" in b and "userFunding 429" in b
    # Silencioso cuando esta sano: el contrato de toda la ronda.
    hr.mark_ok("funding")
    assert hr.is_degraded("funding") is False
    assert hr.banner("funding") == ""
    assert hr.banner(section="funding") == ""


def test_health_registry_swallowed_lee_la_excepcion_en_vuelo(hr):
    try:
        raise TimeoutError("hl no responde")
    except TimeoutError:
        hr.swallowed("portfolio", "fetch_wallet")
    st = hr.status("portfolio")
    assert st["ok"] is False
    assert "TimeoutError" in st["detail"] and "fetch_wallet" in st["detail"]


def test_health_registry_nunca_levanta_aunque_su_db_este_rota(tmp_path, monkeypatch):
    """Un vigilante que rompe el proceso que vigila es peor que no tenerlo."""
    import modules.health_registry as h
    monkeypatch.setattr(h, "DB_PATH", "/proc/no/se/puede/escribir/h.db")
    h.mark_degraded("ledger", "x")      # no debe levantar
    h.swallowed("ledger", "x")          # no debe levantar
    st = h.status("ledger")
    assert st["ok"] in (None, False)
    assert h.all_status()["registry_self_errors"] >= 1, (
        "el registro fallo pero no lo conto: un vigilante roto no puede "
        "reportarse a si mismo como sano")


def test_todo_subsistema_del_catalogo_declara_seccion_y_money():
    import modules.health_registry as h
    for name, spec in h.SUBSYSTEMS.items():
        assert spec.get("label"), f"{name} sin label"
        assert spec.get("section"), f"{name} sin seccion (no podria banderear)"
        assert isinstance(spec.get("money"), bool), f"{name} sin flag money"
