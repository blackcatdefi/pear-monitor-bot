#!/usr/bin/env python3
"""R-BOT-DEFINITIVE (2026-09-01) — cazador de las clases de bug ya probadas.

No busca bugs genericos: busca las CUATRO FORMAS exactas que ya mordieron a
este repo, en todos los lugares donde todavia pueden ocurrir.

C1  TIMESTAMP MIXTO COMPARADO COMO TEXTO
    SQLite escribe ``DEFAULT CURRENT_TIMESTAMP`` como "YYYY-MM-DD HH:MM:SS" y
    Python ``.isoformat()`` produce "YYYY-MM-DDTHH:MM:SS+00:00". En comparacion
    TEXT el espacio (0x20) va ANTES de la 'T' (0x54), asi que toda fila del
    mismo dia que el corte cae por debajo del bound y desaparece.
    Ya paso en x_api_calls: posts_fetched_today() devolvia 0 SIEMPRE.
    Detecta: comparaciones sobre una columna cuyo CREATE TABLE la declara
    DEFAULT CURRENT_TIMESTAMP, sin normalizar (sin replace/substr/date/datetime).

C2  IMPORT OPCIONAL CON FALLBACK MAS DEBIL Y SILENCIOSO
    Ya paso con rapidfuzz: sin el, fuzzy_ratio caia a difflib, que puntua ~58
    donde token_set_ratio da ~85, y los rumores parafraseados dejaban de
    deduplicarse. Nadie se entero porque el fallback no avisa.
    Detecta: try/except ImportError que define un sustituto, y verifica que el
    paquete este en requirements.txt.

C3  ON CONFLICT DO UPDATE QUE OMITE COLUMNAS QUE SI PUEDEN CAMBIAR
    Ya paso en el ledger: el upsert no actualizaba ``leverage``, asi que
    corregir el default de 5x a 3x no habria tocado una sola fila guardada.
    Detecta: columnas presentes en el INSERT y ausentes del SET.

C4  RESPUESTA 200 CON SHAPE INESPERADO TRATADA COMO "NO HAY DATOS"
    Ya paso con userFunding: un 429 se capturaba y se convertia en funding 0.00.
    La variante que queda viva es el 200 con JSON de forma distinta: un
    ``.get("data", [])`` sobre un payload que ahora trae ``{"error": ...}``
    devuelve [] y el llamador lo lee como "no hubo nada".
    Detecta: ``.get(<clave>, [])`` / ``.get(<clave>, {})`` sobre respuestas HTTP
    sin ninguna validacion previa de forma.

Uso:
    python3 tools/bug_class_scan.py            # todo
    python3 tools/bug_class_scan.py --class C1
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}


def py_files(include_tests: bool = False):
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS
                   and (include_tests or d != "tests")]
        for f in sorted(files):
            if f.endswith(".py"):
                yield Path(root) / f


def rel(p: Path) -> str:
    return str(p.relative_to(REPO))


# ─── C1 ─────────────────────────────────────────────────────────────────────

CREATE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\)\s*(?:;|\"\"\"|$)",
    re.IGNORECASE | re.DOTALL)
CURRENT_TS_COL = re.compile(
    r"(\w+)\s+(?:TEXT|TIMESTAMP|DATETIME)[^,\)]*DEFAULT\s+CURRENT_TIMESTAMP",
    re.IGNORECASE)
NORMALISERS = ("replace(", "substr(", "date(", "datetime(", "strftime(",
               "TS_NORM", "ts_norm(", "julianday(")
# Tabla de la que lee la consulta. Se busca en la MISMA sentencia SQL.
FROM_RE = re.compile(r"\b(?:FROM|UPDATE|INTO|JOIN)\s+(\w+)", re.IGNORECASE)


def _current_ts_columns() -> dict[str, set[str]]:
    """Mapa tabla -> columnas declaradas DEFAULT CURRENT_TIMESTAMP.

    Por tabla, NO repo-wide. La primera version de este scanner juntaba los
    nombres de columna de todo el repo en un solo set, asi que un unico
    ``ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP`` (en x_api_calls) volvia
    sospechosa TODA columna llamada 'ts' del proyecto. Reportaba
    cost_tracker.llm_calls.ts (que es INTEGER epoch) y pnl_events.ts (que es
    isoformat puro escrito y leido igual) como bugs. Tres falsos positivos
    sobre cuatro hallazgos: un scanner con esa tasa se ignora, y un scanner
    ignorado no sirve para nada.
    """
    out: dict[str, set[str]] = {}
    for p in py_files():
        txt = p.read_text(encoding="utf-8", errors="replace")
        for m in CREATE_RE.finditer(txt):
            table, body = m.group(1), m.group(2)
            cols = {c.group(1) for c in CURRENT_TS_COL.finditer(body)}
            if cols:
                out.setdefault(table, set()).update(cols)
    return out


def scan_c1() -> list[str]:
    by_table = _current_ts_columns()
    if not by_table:
        return []
    all_cols = {c for cols in by_table.values() for c in cols}
    col_re = re.compile(
        r"(?<![\w.])(" + "|".join(sorted(map(re.escape, all_cols))) +
        r")\s*(>=|<=|>|<|BETWEEN)\s*\?", re.IGNORECASE)
    out: list[str] = []
    for p in py_files():
        # Se mira una ventana de lineas porque el SQL suele partirse en varias
        # constantes concatenadas: el FROM puede estar una o dos lineas arriba.
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines, 1):
            m = col_re.search(line)
            if not m:
                continue
            col = m.group(1)
            window = "\n".join(lines[max(0, i - 6):i + 2])
            if any(n in window for n in NORMALISERS):
                continue
            tables = {t.lower() for t in FROM_RE.findall(window)}
            # Solo es bug si la columna comparada pertenece a una tabla que
            # REALMENTE la declara DEFAULT CURRENT_TIMESTAMP.
            hits = [t for t in tables
                    if col in {c.lower() for c in by_table.get(t, set())}
                    or any(t == k.lower() and col in {c.lower() for c in v}
                           for k, v in by_table.items())]
            if not tables:
                out.append(f"{rel(p)}:{i}  columna '{col}' comparada sin "
                           f"normalizar y sin FROM visible (revisar a mano) "
                           f"-> {line.strip()[:80]}")
                continue
            if not hits:
                continue
            out.append(f"{rel(p)}:{i}  columna '{col}' de la tabla "
                       f"'{hits[0]}' (DEFAULT CURRENT_TIMESTAMP) comparada sin "
                       f"normalizar -> {line.strip()[:80]}")
    return out


# ─── C2 ─────────────────────────────────────────────────────────────────────

def _is_first_party(mod: str) -> bool:
    """True si ``mod`` es un modulo o paquete del propio repo."""
    if (REPO / f"{mod}.py").exists() or (REPO / mod / "__init__.py").exists():
        return True
    for sub in ("modules", "auto", "utils", "tools"):
        if ((REPO / sub / f"{mod}.py").exists()
                or (REPO / sub / mod / "__init__.py").exists()):
            return True
    return mod in sys.builtin_module_names or mod in sys.stdlib_module_names


def scan_c2() -> list[str]:
    reqs = ""
    rq = REPO / "requirements.txt"
    if rq.exists():
        reqs = rq.read_text(encoding="utf-8").lower()
    out: list[str] = []
    for p in py_files():
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            imported: set[str] = set()
            for n in ast.walk(node):
                if isinstance(n, ast.Import):
                    imported |= {a.name.split(".")[0] for a in n.names}
                elif isinstance(n, ast.ImportFrom) and n.module:
                    imported.add(n.module.split(".")[0])
            if not imported:
                continue
            # Un `except Exception` o un except desnudo alrededor de un import
            # atrapa ImportError igual que uno explicito — y ademas es MAS
            # ancho. La primera version de este scanner exigia la palabra
            # "ImportError" en el handler, asi que era ciega justamente al caso
            # que motivo la clase: rapidfuzz en integrity_reconcile.fuzzy_ratio
            # cae por `except Exception`. Un scanner que no encuentra el bug que
            # le dio origen no vale nada.
            def _catches_import(h: ast.ExceptHandler) -> bool:
                if h.type is None:            # except desnudo
                    return True
                names = ast.unparse(h.type)
                return ("ImportError" in names
                        or "ModuleNotFoundError" in names
                        or "Exception" in names
                        or "BaseException" in names)

            catches_import = any(_catches_import(h) for h in node.handlers)
            if not catches_import:
                continue
            # el except define un sustituto?
            has_fallback = any(
                isinstance(n, (ast.Assign, ast.FunctionDef, ast.AsyncFunctionDef))
                for h in node.handlers for n in h.body)
            for mod in sorted(imported):
                if mod in ("modules", "config", "auto", "templates", "utils"):
                    continue
                # Modulos propios del repo: nunca van a estar en
                # requirements.txt, asi que reportarlos como "falta en
                # requirements" es ruido puro. La clase C2 es sobre paquetes
                # de TERCEROS que pueden no estar instalados.
                if _is_first_party(mod):
                    continue
                declared = mod.lower().replace("_", "-") in reqs or mod.lower() in reqs
                if has_fallback and not declared:
                    out.append(
                        f"{rel(p)}:{node.lineno}  '{mod}' importado opcional con "
                        f"fallback y NO esta en requirements.txt")
                elif has_fallback:
                    out.append(
                        f"{rel(p)}:{node.lineno}  '{mod}' tiene fallback silencioso "
                        f"(esta en requirements: el fallback solo corre si el build falla)")
    return out


# ─── C3 ─────────────────────────────────────────────────────────────────────

INSERT_RE = re.compile(
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(\w+)\s*\(([^)]*)\)(.*?)(?=(?:INSERT\s|$))",
    re.IGNORECASE | re.DOTALL)
SET_RE = re.compile(r"ON\s+CONFLICT[^)]*?\)?\s*DO\s+UPDATE\s+SET\s+(.*)",
                    re.IGNORECASE | re.DOTALL)
# columnas que NO tiene sentido re-escribir en un upsert
IMMUTABLE_OK = {"created_at", "created_utc", "first_seen", "first_seen_utc",
                "inserted_at", "id", "rowid"}

# Upserts revisados a mano y aceptados, con la razon escrita. Clave:
# "archivo::tabla::columnas_omitidas". Igual que el allowlist de swallows: si no
# se puede escribir por que la omision no pierde nada, no se acepta, se arregla.
C3_ACCEPTED: dict[str, str] = {
    "modules/sl_validator.py::sl_unreachable_state::alerted,liq_px": (
        "No es una posicion: es una fila centinela con coin='__DIGEST_TS__' que"
        " usa la tabla como almacen de un unico timestamp, guardado en sl_px."
        " liq_px=0 y alerted=1 son literales que nadie lee para esa fila, asi"
        " que omitirlos del SET es un no-op exacto. Agregarlos al UPDATE"
        " tocaria la semantica de alertas de las filas reales sin ningun"
        " beneficio, y las semanticas de alerta no se cambian en esta ronda."
    ),
}


def _sql_strings(path: Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value
            if "INSERT" in v.upper() and "ON CONFLICT" in v.upper():
                out.append((node.lineno, v))
        elif isinstance(node, ast.BinOp) or isinstance(node, ast.JoinedStr):
            try:
                v = ast.unparse(node)
            except Exception:  # noqa: BLE001
                continue
            if "INSERT" in v.upper() and "ON CONFLICT" in v.upper():
                out.append((node.lineno, v))
    return out


def scan_c3() -> list[str]:
    out: list[str] = []
    seen: set[tuple[str, int]] = set()
    for p in py_files():
        for lineno, sql in _sql_strings(p):
            if (rel(p), lineno) in seen:
                continue
            seen.add((rel(p), lineno))
            mi = INSERT_RE.search(sql)
            ms = SET_RE.search(sql)
            if not mi or not ms:
                continue
            table = mi.group(1)
            cols = [c.strip().strip("'\"") for c in mi.group(2).split(",")]
            cols = [c for c in cols if re.fullmatch(r"\w+", c)]
            set_body = ms.group(1)
            set_cols = {m.group(1).lower() for m in
                        re.finditer(r"(\w+)\s*=", set_body)}
            # La columna del ON CONFLICT(...) es la CLAVE del upsert: no
            # actualizarla no es un bug, es la definicion de la operacion.
            mt = re.search(r"ON\s+CONFLICT\s*\(([^)]*)\)", sql, re.IGNORECASE)
            key_cols = set()
            if mt:
                key_cols = {c.strip().strip("'\"").lower()
                            for c in mt.group(1).split(",")}
            missing = [c for c in cols
                       if c.lower() not in set_cols
                       and c.lower() not in IMMUTABLE_OK
                       and c.lower() not in key_cols]
            if not missing:
                continue
            akey = (f"{rel(p)}::{table}::"
                    f"{','.join(sorted(c.lower() for c in missing))}")
            if akey in C3_ACCEPTED:
                continue
            out.append(
                f"{rel(p)}:{lineno}  upsert en '{table}' NO actualiza "
                f"{missing} (estan en el INSERT pero no en el DO UPDATE SET)"
                f"\n      [clave para aceptar: {akey}]")
    return out


# ─── C4 ─────────────────────────────────────────────────────────────────────

HTTP_HINT = re.compile(r"\.json\(\)|resp\.|response\.|r\.json|payload|data\b")

# Sitios C4 revisados a mano y aceptados, con la razon escrita. Clave:
# "archivo::expresion" (sin numero de linea, para que editar alrededor no
# invalide la entrada). Se admite '*' como comodin al final del archivo para
# aceptar un patron identico repetido en un paquete entero.
#
# El criterio para aceptar es UNO: que la ausencia de la clave no pueda
# confundirse con "no hubo datos". Eso pasa en dos situaciones:
#   (a) el diccionario NO es una respuesta HTTP sino un payload que construye
#       el propio modulo, con contrato fijo (falso positivo del scanner, que
#       solo mira el nombre de la variable);
#   (b) la ausencia YA se reporta por otra via antes de llegar al .get().
C4_ACCEPTED: dict[str, str] = {
    "modules/intel30/*::data.get('series', [])": (
        "'data' aca no es la respuesta HTTP: es el payload que arma el propio"
        " modulo intel30, que siempre construye {'series': [...],"
        " '_global_error': ...} o graceful_no_key_payload(), que tambien trae"
        " series=[]. Ademas format_for_telegram() ya chequea _status y"
        " _global_error ANTES de este .get(), asi que el fallo de red se"
        " imprime como aviso y nunca llega aca disfrazado de lista vacia."
    ),
    "modules/llm_router.py::data.get('candidates', [])": (
        "La ausencia no se trata como 'no hay datos': las dos lineas"
        " siguientes son `if not candidates: _track_error(...); return None`."
        " El llamador recibe None, que significa 'el modelo no respondio', y"
        " cae al siguiente proveedor del router. La degradacion ya viaja."
    ),
    "modules/llm_router.py::data.get('usageMetadata', {})": (
        "Solo alimenta el conteo de tokens para el tracker de costos. Si"
        " falta, se cuentan 0 tokens en esa llamada: subestima el gasto, no"
        " corrompe ningun numero del fondo ni ninguna decision. El texto ya"
        " se valido aparte y el error de la llamada se registra igual."
    ),
    "modules/x_intel.py::data.get('includes', {}).get('users', [])": (
        "Se llega aca solo despues del guard de shape agregado en esta ronda,"
        " que ya exige 'data' lista o meta.result_count==0. 'includes' es la"
        " expansion opcional de autores: si falta, users_map queda vacio y el"
        " tweet se guarda con username generico, nunca se pierde el tweet."
    ),
    "modules/x_intel.py::data.get('includes', {})": (
        "Mismo sitio que la entrada anterior (el scanner cuenta el .get()"
        " externo y el interno por separado). Misma razon."
    ),
    "modules/x_intel.py::data.get('meta', {})": (
        "Solo se usa para leer next_token y paginar. Si falta, no hay"
        " siguiente pagina y el bucle corta: se leen menos tweets, pero el"
        " conteo real devuelto por la API queda en _last_fetch_meta y el"
        " reporte lo compara contra lo guardado (R-XSTORE-FIX)."
    ),
    "modules/catalysts.py::(data or {}).get('releases', [])": (
        "Feed de catalizadores (releases de GitHub). No entra en ningun"
        " numero del fondo ni en ninguna decision automatica: alimenta una"
        " seccion informativa del reporte. El fallo de transporte de la misma"
        " llamada esta instrumentado en el except que la envuelve."
    ),
    "modules/catalysts.py::(data or {}).get('release_dates', [])": (
        "Mismo feed, misma llamada, misma razon que 'releases'."
    ),
    "modules/market.py::data.get(gid, {})": (
        "Queda dentro de coingecko_prices(), que en esta ronda paso a exigir"
        " que al menos uno de los gids pedidos este presente (CoinGecko"
        " responde 200 con {'status':{'error_code':429}} en vez de 429). Si"
        " esa exigencia pasa, un gid suelto ausente es un activo que"
        " CoinGecko no cotiza y se omite del mapa, no se valua en cero."
    ),
    # ── familia w.get("data", {}) tras status=="ok" ──────────────────────
    # Todas estas leen el sobre {"status": ..., "data": ...} que producen
    # fetch_wallet() (modules/portfolio.py) y sus equivalentes, SIEMPRE
    # despues de comprobar status=="ok". El productor tiene exactamente tres
    # returns y los dos que dicen "ok" traen 'data'; no existe un camino que
    # produzca status=ok sin data. Ese invariante lo fija por escrito
    # tests/test_shape_guards.py::test_fetch_wallet_status_ok_implica_data,
    # que es lo que convierte estos .get() en seguros en vez de en suerte.
    # El unico sitio de esta familia que NO estaba cubierto por el chequeo de
    # status previo era modules/fund_state_reconciler.py:124, y ese se
    # arreglo en esta ronda (marca 'portfolio' degradado y saltea la wallet).
    "templates/formatters.py::w.get('data', {})": (
        "Sobre status/data de fetch_wallet, leido tras status=='ok'. El"
        " productor garantiza 'data' en ese caso (test que lo fija:"
        " test_fetch_wallet_status_ok_implica_data). Ademas es codigo de"
        " render: no calcula ningun numero, solo imprime lo que recibe."
    ),
    "templates/formatters.py::hl.get('data', {})": (
        "Idem para el sobre de HyperLend, tambien tras status=='ok'."
    ),
    "templates/formatters.py::hl_by_wallet.get(wallet_addr, {})": (
        "hl_by_wallet no es una respuesta HTTP: es un dict local que arma la"
        " propia funcion unas lineas mas arriba, indexado por address. Que"
        " falte una address significa que esa wallet no tiene posicion en"
        " HyperLend, que es informacion correcta y el caso normal."
    ),
    "modules/portfolio_snapshot.py::hl_by_wallet.get(addr, {})": (
        "Mismo dict local armado en la misma funcion. Misma razon."
    ),
    "modules/analysis.py::p.get('data', {})": (
        "Sobre de fetch_wallet leido tras status=='ok'. Ademas analysis.py"
        " arma el prompt del LLM: no produce numeros del fondo."
    ),
    "modules/analysis.py::hl.get('data', {})": (
        "Sobre de HyperLend leido tras status=='ok'. Misma razon."
    ),
    "modules/analysis.py::market.get('data', {})": (
        "Sobre de market leido tras status=='ok'. Misma razon."
    ),
    "modules/analysis.py::data.get('BTC', {})": (
        "Precio de un activo suelto dentro del bloque de market ya validado."
        " Si falta, el prompt dice '?' en vez de un precio inventado, que es"
        " la conducta correcta."
    ),
    "modules/analysis.py::data.get('HYPE', {})": (
        "Idem BTC: imprime '?' en vez de inventar un precio."
    ),
    "modules/basket_killer.py::w.get('data', {})": (
        "Sobre de fetch_wallet leido tras status=='ok' (linea inmediatamente"
        " anterior hace `if w.get('status') != 'ok': continue`), asi que la"
        " garantia del productor aplica. La wallet con status!='ok' se saltea"
        " explicitamente y eso ya se refleja en el estado del subsistema."
    ),
    "modules/intel30/hyperevmscan.py::gas_data.get('result', {})": (
        "Feed informativo de gas de HyperEVM. No entra en ningun numero del"
        " fondo ni en ninguna decision; si el explorer cambia el shape, el"
        " bloque imprime gas vacio en una seccion de contexto. El estado del"
        " feed se sigue aparte por set_source_state()/consecutive_fails."
    ),
    "modules/intel30/treasuries_bundle.py::data.get('data', [])": (
        "Feed macro de treasuries. Misma razon que hyperevmscan: contexto,"
        " no dinero, y el estado del feed se sigue por source_state."
    ),
    "bot.py::application.bot_data.get('validate_issues', [])": (
        "bot_data es el diccionario en memoria de python-telegram-bot, no una"
        " respuesta HTTP. La clave la escribe el propio job de validacion; si"
        " no esta es porque el job todavia no corrio, y eso se lee como 'sin"
        " issues pendientes', que es correcto."
    ),
}


def _c4_accepted(relpath: str, expr: str) -> bool:
    for key in C4_ACCEPTED:
        kfile, _, kexpr = key.partition("::")
        if kexpr != expr:
            continue
        if kfile.endswith("*"):
            if relpath.startswith(kfile[:-1]):
                return True
        elif kfile == relpath:
            return True
    return False


def scan_c4() -> list[str]:
    out: list[str] = []
    for p in py_files():
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        src = p.read_text(encoding="utf-8", errors="replace").splitlines()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and len(node.args) == 2):
                continue
            default = node.args[1]
            if not (isinstance(default, (ast.List, ast.Dict))
                    and not (default.elts if isinstance(default, ast.List)
                             else default.keys)):
                continue
            try:
                recv = ast.unparse(node.func.value)
            except Exception:  # noqa: BLE001
                continue
            line = src[node.lineno - 1] if node.lineno <= len(src) else ""
            if not HTTP_HINT.search(recv) and not HTTP_HINT.search(line):
                continue
            expr = ast.unparse(node)
            if _c4_accepted(rel(p), expr):
                continue
            out.append(f"{rel(p)}:{node.lineno}  {expr[:80]} "
                       f"— un 200 con shape distinto se lee como 'no hay datos'"
                       f"\n      [clave para aceptar: {rel(p)}::{expr}]")
    return out


SCANS = {"C1": ("timestamp mixto comparado como texto", scan_c1),
         "C2": ("import opcional con fallback silencioso", scan_c2),
         "C3": ("upsert que omite columnas mutables", scan_c3),
         "C4": ("200 con shape inesperado = 'no hay datos'", scan_c4)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--class", dest="cls", choices=sorted(SCANS))
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    keys = [a.cls] if a.cls else sorted(SCANS)
    total = 0
    for k in keys:
        title, fn = SCANS[k]
        res = fn()
        total += len(res)
        print(f"\n=== {k}: {title} ({len(res)}) ===")
        for r in res:
            print("  " + r)
    print(f"\nTOTAL {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
