#!/usr/bin/env python3
"""R-BOT-DEFINITIVE (2026-09-01) — inventario de degradacion silenciosa.

POR QUE EXISTE
==============
Las ultimas cinco rondas destaparon, cada una, un componente que corria
DEGRADADO EN SILENCIO y desde afuera se veia sano:

  * Gmail decia "mandado a papelera" mientras archivaba (imaplib no levanta
    excepcion ante un IMAP NO)
  * los numeros de secuencia IMAP se corrian a mitad del loop y el COPY
    pegaba en el mensaje equivocado
  * un 429 de userFunding se capturaba, se logueaba y se convertia en
    funding 0.00 en TODAS las patas
  * una wallet que fallaba el sync desaparecia y se llevaba medio track record
  * el upsert del ledger nunca escribia leverage, asi que corregir el default
    no habria tocado una sola fila guardada
  * rapidfuzz faltaba en requirements y el dedup de integridad caia a un
    matcher mas debil sin decir nada
  * x_api_calls comparaba un timestamp con espacio contra un corte isoformat,
    asi que el panel de costos reportaba cero el dia frontera

Ninguno LEVANTA. Todos producen un numero plausible y equivocado. Una suite de
1300 tests no atrapo ni uno, porque los tests verifican que las cosas no
exploten. Este scanner busca la FORMA del bug, no el bug.

QUE DETECTA
===========
Un handler de excepcion (o un bloque try/except) cuyo camino de error produce
un VALOR PLAUSIBLE en lugar de propagar o marcar degradacion:

  * ``return []`` / ``{}`` / ``0`` / ``0.0`` / ``""`` / ``None`` / ``False``
  * ``return`` pelado dentro de una funcion que en el camino feliz devuelve algo
  * asignar un default a la variable que se termina devolviendo
  * ``pass`` cuando el flujo sigue y usa datos parciales

y ademas la amplitud de lo que traga: ``except Exception`` / ``except:`` es
mucho peor que ``except TimeoutError``, porque se come tambien el KeyError que
delata un cambio de shape en la respuesta.

COMO SE USA
===========
    python3 tools/silent_degradation_scan.py            # inventario completo
    python3 tools/silent_degradation_scan.py --money     # solo money path
    python3 tools/silent_degradation_scan.py --json      # para el test guard

El test ``tests/test_silent_degradation_guard.py`` corre esto y falla si
aparece un swallow NUEVO en un modulo del money path. La lista de swallows
aceptados vive en ``tools/silent_degradation_allowlist.json``, cada uno con su
razon escrita. Agregar uno nuevo obliga a justificarlo por escrito.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# ─── Money path ──────────────────────────────────────────────────────────────
# Modulos donde un default silencioso corrompe un numero que BCD lee o una
# decision que el bot toma. Un swallow aca NUNCA es una degradacion aceptable:
# o levanta, o marca el subsistema degradado via modules.health_registry.
MONEY_PATH = {
    "modules/trade_ledger.py",
    "modules/portfolio.py",
    "modules/portfolio_margin.py",
    "modules/pnl_tracker.py",
    "modules/pnl_extended.py",
    "modules/hl_client.py",
    "modules/hl_prices.py",
    "modules/hl_borrow_lend.py",
    "modules/market.py",
    "modules/funding_tracker.py",
    "modules/pm_context.py",
    "modules/spot_index.py",
    "modules/hype_acquisition.py",
    "modules/vault_deposits.py",
    "modules/vault_history.py",
    "modules/performance_attribution.py",
    "modules/fund_state_reconciler.py",
    "modules/integrity_reconcile.py",
    "modules/cost_tracker.py",
    "modules/intel_memory.py",
    "modules/x_store.py",
    "auto/capital_calc.py",
    "auto/fund_state_v2.py",
    "auto/price_cache.py",
    "fund_state.py",
}

SKIP_DIRS = {".git", "__pycache__", "tests", "node_modules", ".venv", "venv"}

# Valores que un handler puede devolver y que el llamador no puede distinguir
# de un resultado legitimo.
PLAUSIBLE_CONSTANTS = ("[]", "{}", "0", "0.0", "''", '""', "None", "False",
                       "()", "set()", "list()", "dict()", "0.00")


@dataclass
class Finding:
    file: str
    line: int
    func: str
    catches: str          # "Exception", "BARE", "OSError", ...
    swallows: str         # que produce el camino de error
    money_path: bool
    severity: str         # critical | high | low
    key: str              # identidad estable para el allowlist
    # True cuando el handler llama health_registry.swallowed(). El default se
    # sigue devolviendo y el flujo no cambia, pero el subsistema queda marcado
    # degradado y eso aparece en /health, en /diagnostico y como banner en la
    # seccion del reporte que depende de el. Deja de ser SILENCIOSO, que es lo
    # unico prohibido: un swallow instrumentado es una degradacion declarada,
    # no un numero inventado. Por eso no necesita entrada en el allowlist.
    instrumented: bool = False

    def as_row(self) -> str:
        tag = "MONEY" if self.money_path else "cosmetic"
        mark = " [instrumentado]" if self.instrumented else ""
        return (f"{self.file}:{self.line}  [{tag}/{self.severity}]  "
                f"{self.func}()  except {self.catches} -> "
                f"{self.swallows}{mark}")


# Un `except (TypeError, ValueError)` alrededor de un float()/int() sobre UN
# valor es una guarda de coercion legitima: convierte basura de entrada en
# None/0, y el llamador ya sabe que ese campo puede faltar. No es lo mismo que
# tragarse un timeout de red y devolver [] como si la API hubiera dicho "no hay
# nada". Solo lo segundo corrompe un numero.
NARROW = {"TypeError", "ValueError", "KeyError", "IndexError", "AttributeError",
          "ZeroDivisionError", "OverflowError", "ArithmeticError",
          "json.JSONDecodeError", "JSONDecodeError", "UnicodeDecodeError",
          "StopIteration", "ImportError", "ModuleNotFoundError"}


def _severity(catches: str, swallow: str) -> str:
    names = {n.strip() for n in catches.strip("()").split(",") if n.strip()}
    narrow = bool(names) and names <= NARROW
    if narrow:
        return "low"
    if swallow.startswith("return"):
        return "critical"
    return "high"


def _exc_name(handler: ast.ExceptHandler) -> str:
    if handler.type is None:
        return "BARE"
    try:
        return ast.unparse(handler.type)
    except Exception:  # noqa: BLE001
        return "?"


def _is_broad(name: str) -> bool:
    return name in ("BARE", "Exception", "BaseException") or name.startswith("(")


def _describe_return(node: ast.Return) -> str | None:
    """Devuelve la descripcion del valor plausible, o None si no lo es."""
    if node.value is None:
        return "return (implicito None)"
    try:
        src = ast.unparse(node.value)
    except Exception:  # noqa: BLE001
        return None
    stripped = src.strip()
    if stripped in PLAUSIBLE_CONSTANTS:
        return f"return {stripped}"
    # dict/list literales vacios o llenos de ceros: {"a": 0, "b": 0.0}
    if isinstance(node.value, (ast.Dict, ast.List, ast.Tuple)):
        elts = (node.value.values if isinstance(node.value, ast.Dict)
                else node.value.elts)
        if not elts:
            return f"return {stripped[:60]}"
        if all(isinstance(e, ast.Constant)
               and (e.value in (0, 0.0, "", None, False) or e.value == [])
               for e in elts):
            return f"return {stripped[:60]} (todo ceros/vacios)"
    return None


def _handler_findings(fn_name: str, rel: str,
                      handler: ast.ExceptHandler) -> list[Finding]:
    out: list[Finding] = []
    catches = _exc_name(handler)
    swallow: str | None = None
    for node in ast.walk(handler):
        if isinstance(node, ast.Return):
            desc = _describe_return(node)
            if desc:
                swallow = desc
                break
    if swallow is None:
        # `pass` / solo log: el flujo SIGUE con datos parciales.
        body = [n for n in handler.body if not isinstance(n, ast.Expr)]
        only_pass = all(isinstance(n, ast.Pass) for n in body) if body else True
        logs_only = all(
            isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
            for n in handler.body)
        if only_pass and not handler.body:
            swallow = "pass (sigue con datos parciales)"
        elif only_pass:
            swallow = "pass (sigue con datos parciales)"
        elif logs_only:
            swallow = "solo loguea (sigue con datos parciales)"
    if swallow is None:
        return out
    out.append(Finding(
        file=rel, line=handler.lineno, func=fn_name, catches=catches,
        swallows=swallow, money_path=rel in MONEY_PATH,
        severity=_severity(catches, swallow),
        key=f"{rel}::{fn_name}::{catches}::{swallow.split('(')[0].strip()}",
        instrumented=_is_instrumented(handler),
    ))
    return out


def _is_instrumented(handler: ast.ExceptHandler) -> bool:
    """True si el handler declara su degradacion via health_registry.

    Se busca la llamada en TODO el cuerpo del handler, no solo en la primera
    sentencia, para que siga valiendo si alguien reordena o envuelve la linea.
    """
    for node in ast.walk(handler):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "swallowed"):
            return True
    return False


def scan_file(path: Path) -> list[Finding]:
    rel = str(path.relative_to(REPO))
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    findings: list[Finding] = []
    stack: list[str] = []

    class V(ast.NodeVisitor):
        def visit_FunctionDef(self, node):  # noqa: N802
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

        def visit_ExceptHandler(self, node):  # noqa: N802
            fn = stack[-1] if stack else "<module>"
            findings.extend(_handler_findings(fn, rel, node))
            self.generic_visit(node)

    V().visit(tree)
    return findings


def scan_repo(money_only: bool = False) -> list[Finding]:
    out: list[Finding] = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in sorted(files):
            if not f.endswith(".py"):
                continue
            out.extend(scan_file(Path(root) / f))
    if money_only:
        out = [f for f in out if f.money_path]
    return sorted(out, key=lambda f: (not f.money_path, f.file, f.line))


def load_allowlist() -> dict[str, str]:
    p = REPO / "tools" / "silent_degradation_allowlist.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8")).get("accepted", {})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--money", action="store_true", help="solo money path")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--critical", action="store_true",
                    help="solo severity critical")
    ap.add_argument("--unlisted", action="store_true",
                    help="solo money path NO presente en el allowlist")
    args = ap.parse_args()

    findings = scan_repo(money_only=args.money or args.unlisted or args.critical)
    if args.critical:
        findings = [f for f in findings if f.severity == "critical"]
    if args.unlisted:
        # El invariante que vigila el test guardian: en el money path, TODO
        # swallow esta o instrumentado (declara su degradacion) o aceptado por
        # escrito en el allowlist. Lo que sobra es un default silencioso nuevo.
        allow = load_allowlist()
        findings = [f for f in findings
                    if f.key not in allow and not f.instrumented]

    if args.json:
        print(json.dumps([asdict(f) for f in findings], indent=2,
                         ensure_ascii=False))
        return 0

    money = [f for f in findings if f.money_path]
    cosm = [f for f in findings if not f.money_path]
    print(f"=== MONEY PATH ({len(money)}) ===")
    for f in money:
        print(" ", f.as_row())
    if not args.money and not args.unlisted:
        print(f"\n=== COSMETICO ({len(cosm)}) ===")
        for f in cosm:
            print(" ", f.as_row())
    print(f"\nTOTAL {len(findings)}  money={len(money)}  cosmetico={len(cosm)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
