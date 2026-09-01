#!/usr/bin/env python3
"""R-BOT-DEFINITIVE — instrumenta los swallows criticos del money path.

Inserta ``health_registry.swallowed("<subsistema>", "<func>")`` como PRIMERA
sentencia del cuerpo de cada handler critico detectado por
``silent_degradation_scan.py``. El valor por default se sigue devolviendo — el
flujo no cambia — pero la degradacion deja de ser invisible: aparece en /health,
en /diagnostico y como banner en la seccion del reporte que depende de ese
subsistema.

Se hace con guia de AST (para ubicar el handler con precision) y edicion por
lineas (para no reformatear el archivo entero, que arruinaria el diff y el
blame). Idempotente: si la linea ya esta, no la duplica.

Uso:
    python3 tools/instrument_swallows.py --dry-run
    python3 tools/instrument_swallows.py --apply
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from silent_degradation_scan import REPO, scan_repo  # noqa: E402

# Cada archivo del money path declara a que subsistema del registro pertenece.
FILE_SUBSYSTEM = {
    "modules/trade_ledger.py": "ledger",
    "modules/portfolio.py": "portfolio",
    "modules/portfolio_margin.py": "pm_state",
    "modules/pm_context.py": "pm_state",
    "modules/market.py": "market",
    "modules/funding_tracker.py": "funding",
    "modules/hl_client.py": "portfolio",
    "modules/hl_prices.py": "market",
    "modules/hl_borrow_lend.py": "pm_state",
    "modules/vault_deposits.py": "vault",
    "modules/vault_history.py": "vault",
    "modules/hype_acquisition.py": "ppc",
    "modules/spot_index.py": "market",
    "modules/intel_memory.py": "x_api",
    "modules/x_store.py": "x_api",
    "modules/cost_tracker.py": "cost_tracker",
    "modules/performance_attribution.py": "attribution",
    "modules/integrity_reconcile.py": "integrity",
    "modules/fund_state_reconciler.py": "pm_state",
    "modules/pnl_tracker.py": "attribution",
    "modules/pnl_extended.py": "attribution",
    "auto/capital_calc.py": "pm_state",
    "auto/fund_state_v2.py": "pm_state",
    "auto/price_cache.py": "market",
    "fund_state.py": "pm_state",
}

# Excluidos a proposito, con razon escrita. Ver el allowlist para el detalle.
SKIP_FUNCS = {
    # El registro de salud no puede reportarse a si mismo a traves de si mismo.
    ("modules/health_registry.py", "*"),
    # _alert ya devuelve False y el llamador CUENTA los envios fallidos; ademas
    # instrumentarlo crearia recursion si el envio falla por el propio registro.
    ("modules/trade_ledger.py", "_alert"),
}

# Algunos archivos guardan tablas de subsistemas distintos en una sola DB.
# intel_memory.py es el caso claro: sirve x_api_calls, llm_usage, intel_memory
# y unlock_schedule. Mapearlo entero a "x_api" haria que un insert fallido de
# un unlock reporte "X API degradada", que es una senal FALSA — exactamente el
# tipo de dato enganoso que esta ronda existe para eliminar. El mapeo por
# funcion gana sobre el mapeo por archivo.
FUNC_SUBSYSTEM = {
    ("modules/intel_memory.py", "track_llm_usage"): "cost_tracker",
    ("modules/intel_memory.py", "get_usage_stats"): "cost_tracker",
    ("modules/intel_memory.py", "save_unlock_events"): "intel_feeds",
    ("modules/intel_memory.py", "get_cached_unlocks"): "intel_feeds",
    ("modules/intel_memory.py", "save_intel"): "intel_feeds",
    ("modules/intel_memory.py", "get_recent_intel"): "intel_feeds",
    ("modules/intel_memory.py", "cleanup_old"): "intel_feeds",
}

MARKER = "health_registry.swallowed("
IMPORT_LINE = "from modules import health_registry\n"


def _should_skip(rel: str, func: str) -> bool:
    return (rel, "*") in SKIP_FUNCS or (rel, func) in SKIP_FUNCS


def _handler_first_stmt(path: Path, lineno: int):
    """Devuelve (line_index_0based, indent_str) de la primera sentencia del
    handler que empieza en ``lineno``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.lineno == lineno:
            if not node.body:
                return None
            first = node.body[0]
            return first.lineno - 1, " " * first.col_offset
    return None


def instrument(apply: bool, severities: set[str] | None = None) -> int:
    # critical = el handler RETORNA un default (numero inventado).
    # high     = el handler sigue con datos parciales tras un fallo ancho. Es
    #            igual de peligroso, solo que diferido: una escritura que falla
    #            en silencio (set_since_id, log_llm_call, persist) no corrompe
    #            el numero de HOY, corrompe el de la proxima lectura, cuando ya
    #            nadie relaciona el sintoma con la causa.
    # low      = guarda de coercion estrecha sobre un solo valor: legitima, va
    #            al allowlist con razon escrita en vez de instrumentarse.
    sev = severities or {"critical", "high"}
    findings = [f for f in scan_repo(money_only=True)
                if f.severity in sev
                and not f.instrumented
                and not _should_skip(f.file, f.func)]
    by_file: dict[str, list] = {}
    for f in findings:
        by_file.setdefault(f.file, []).append(f)

    total = 0
    for rel, items in sorted(by_file.items()):
        subsystem = FILE_SUBSYSTEM.get(rel)
        if not subsystem:
            print(f"SKIP {rel}: sin subsistema mapeado")
            continue
        path = REPO / rel
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        inserts: list[tuple[int, str]] = []
        for f in items:
            spot = _handler_first_stmt(path, f.line)
            if spot is None:
                print(f"SKIP {rel}:{f.line}: handler vacio")
                continue
            idx, indent = spot
            # Idempotencia: no duplicar si ya esta instrumentado.
            window = "".join(lines[idx:idx + 4])
            if MARKER in window:
                continue
            sub = FUNC_SUBSYSTEM.get((rel, f.func), subsystem)
            call = (f'{indent}health_registry.swallowed("{sub}", '
                    f'"{f.func}")\n')
            inserts.append((idx, call))
        if not inserts:
            continue
        for idx, call in sorted(inserts, reverse=True):
            lines.insert(idx, call)
        text = "".join(lines)
        if MARKER in text and "import health_registry" not in text:
            text = _add_import(text)
        total += len(inserts)
        print(f"{rel}: +{len(inserts)}")
        if apply:
            path.write_text(text, encoding="utf-8")
    print(f"\nTOTAL instrumentado: {total}")
    return 0


def _add_import(text: str) -> str:
    """Inserta el import despues del ultimo import de nivel superior.

    Import diferido a proposito NO: health_registry no importa nada del bot
    salvo config, asi que no hay ciclo. Se pone arriba para que sea visible.
    """
    lines = text.splitlines(keepends=True)
    last = 0
    depth = 0
    for i, ln in enumerate(lines[:200]):
        s = ln.strip()
        if s.startswith("import ") or s.startswith("from "):
            if not ln.startswith((" ", "\t")):
                last = i
                depth = ln.count("(") - ln.count(")")
        elif depth:
            last = i
            depth += ln.count("(") - ln.count(")")
    lines.insert(last + 1, IMPORT_LINE)
    return "".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    sys.exit(instrument(apply=a.apply))
