"""R-BOT-DEFINITIVE Fase 3 — invariantes del money path y recomputo independiente.

POR QUE ESTE MODULO EXISTE
==========================
El ledger tiene 1300 tests y aun asi paso un deploy entero reportando funding
0.00 en todas las patas. La razon es que los tests verifican que
``rebuild_wallet_positions`` haga lo que ``rebuild_wallet_positions`` dice que
hace. Si la funcion se equivoca de forma consistente, el test se equivoca con
ella. **Un ledger que solo coincide consigo mismo no prueba nada.**

Este modulo agrega las dos unicas cosas que si prueban algo:

1. **INVARIANTES** (``check_invariants``). Propiedades que tienen que valer
   sobre las filas GUARDADAS, sin importar como se calcularon. No miran el
   codigo: miran el resultado. Si NET != gross - fees + funding en una fila,
   algo esta mal aunque todos los tests pasen.

2. **RECOMPUTO INDEPENDIENTE** (``recompute_from_fills``). Reconstruye los
   agregados desde ``ledger_fills`` y ``ledger_funding`` por un camino
   DISTINTO al de ``rebuild_wallet_positions`` — otra consulta, otro orden,
   otra forma de agrupar — y compara. Dos implementaciones distintas que
   coinciden es evidencia; una implementacion que coincide consigo misma no.

Ambas son de solo lectura. No corrigen nada: reportan. Corregir en silencio
seria repetir el error que motivo la ronda.

LA FORMULA
==========
    NET = gross - fees + funding          (funding > 0 = cobrado)
    margin = notional_open / leverage
    ROE = NET / margin

Todo lo que se chequea aca sale de esas tres lineas.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Tolerancias. Son de redondeo, no de "mas o menos": un centavo por fila y
# medio punto de ROE. Si hace falta aflojarlas para que pase, no es que la
# tolerancia sea chica — es que hay un bug.
TOL_USD = 0.01
TOL_ROE_PCT = 0.5
TOL_QTY = 1e-6


@dataclass
class Violation:
    """Un invariante roto, con la fila que lo rompe."""
    invariant: str
    detail: str
    row: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - formato
        return f"[{self.invariant}] {self.detail}"


def _conn():
    """Conexion al ledger resuelta AHORA, no al importar.

    Importante para los tests: leen DB_PATH del modulo en el momento de la
    llamada, asi que monkeypatchearlo funciona. Si se cacheara la ruta al
    importar, este modulo miraria siempre la DB de produccion.
    """
    from modules.trade_ledger import _conn as ledger_conn
    return ledger_conn()


def _tables(con) -> set[str]:
    return {r["name"] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


# ─── 3.1 INVARIANTES ────────────────────────────────────────────────────────

def check_invariants(limit: int = 200) -> list[Violation]:
    """Verifica los invariantes del money path sobre las filas guardadas.

    Devuelve la lista de violaciones (vacia = todo cierra). NUNCA levanta por
    un problema de datos; si el propio chequeo no puede correr, eso SI se
    reporta como violacion, porque "no pude verificar" no es "esta bien".
    """
    out: list[Violation] = []
    try:
        con = _conn()
    except Exception as exc:  # noqa: BLE001
        return [Violation("acceso", f"no se pudo abrir el ledger: {exc}"[:200])]
    try:
        tabs = _tables(con)
        if "ledger_positions" not in tabs:
            return [Violation("acceso", "no existe ledger_positions")]

        rows = [dict(r) for r in con.execute(
            "SELECT * FROM ledger_positions ORDER BY close_ts DESC LIMIT ?",
            (limit,))]

        # I1 — NET = gross - fees + funding, en TODA fila guardada.
        # Este es el invariante que habria hecho imposible el bug de funding
        # 0.00: con funding en cero la igualdad seguia cerrando, pero el
        # invariante I5 (abajo) mira el otro lado del mismo hecho.
        for r in rows:
            esperado = (_f(r["gross_pnl"]) - _f(r["fees_total"])
                        + _f(r["funding_net"]))
            if abs(esperado - _f(r["net_pnl"])) > TOL_USD:
                out.append(Violation(
                    "I1 NET=gross-fees+funding",
                    f"{r['coin']} {r['wallet'][:8]} close={r['close_ts']}: "
                    f"guardado {_f(r['net_pnl']):.4f}, calculado "
                    f"{esperado:.4f} (gross {_f(r['gross_pnl']):.4f} - fees "
                    f"{_f(r['fees_total']):.4f} + funding "
                    f"{_f(r['funding_net']):.4f})", r))

        # I2 — ROE = NET / margin, y margin = notional_open / leverage.
        for r in rows:
            lev = _f(r.get("leverage"))
            if lev <= 0:
                continue
            margin = _f(r.get("margin_open")) or (_f(r["notional_open"]) / lev)
            if margin <= 0:
                continue
            esperado = 100.0 * _f(r["net_pnl"]) / margin
            guardado = r.get("roe_pct")
            if guardado is None:
                continue
            if abs(esperado - _f(guardado)) > TOL_ROE_PCT:
                out.append(Violation(
                    "I2 ROE=NET/margin",
                    f"{r['coin']} {r['wallet'][:8]}: guardado "
                    f"{_f(guardado):.2f}%, calculado {esperado:.2f}% "
                    f"(margin {margin:.2f}, lev {lev:g})", r))

        # I3 — no hay cierre sin al menos un fill de apertura y uno de cierre.
        # Una posicion cerrada que dice tener 0 fills de apertura significa
        # que el rebuild invento el ciclo o que se perdieron fills; en los dos
        # casos el avg_entry es ficticio y el gross tambien.
        for r in rows:
            if int(r.get("open_fills") or 0) < 1 or int(r.get("close_fills") or 0) < 1:
                out.append(Violation(
                    "I3 ciclo con fills de ambos lados",
                    f"{r['coin']} {r['wallet'][:8]} close={r['close_ts']}: "
                    f"open_fills={r.get('open_fills')} "
                    f"close_fills={r.get('close_fills')}", r))

        # I4 — no hay ciclos duplicados. La UNIQUE(wallet, coin, open_ts) ya
        # lo impide a nivel schema, pero un ciclo partido en dos con open_ts
        # a un milisegundo de distancia la esquiva. Se buscan solapamientos
        # reales: dos ciclos del mismo par cuyos intervalos se pisan.
        for w, c, a, b in _solapamientos(con):
            out.append(Violation(
                "I4 ciclos que se solapan",
                f"{c} {w[:8]}: [{a['open_ts']}..{a['close_ts']}] se pisa con "
                f"[{b['open_ts']}..{b['close_ts']}] — el mismo fill puede "
                f"estar contado en los dos", {"a": a, "b": b}))

        # I5 — una posicion de perp abierta mas de una hora TIENE funding.
        # El funding de HL se acredita cada hora: un ciclo de 40 horas con
        # funding exactamente 0.00 no es un mercado tranquilo, es el dato que
        # falta. Este es el invariante que habria gritado durante el deploy
        # que reporto 0.00 en todas las patas.
        sospechosos = [
            r for r in rows
            if _f(r["funding_net"]) == 0.0
            and (int(r["close_ts"]) - int(r["open_ts"])) > 3_600_000
        ]
        if sospechosos:
            horas = max((int(r["close_ts"]) - int(r["open_ts"])) / 3_600_000
                        for r in sospechosos)
            out.append(Violation(
                "I5 funding exactamente cero en ciclo largo",
                f"{len(sospechosos)} ciclo(s) de mas de 1h con funding_net "
                f"= 0.00 exacto (el mas largo, {horas:.0f}h). El funding de "
                f"HL se acredita por hora: un cero exacto es un dato que "
                f"falta, no un mercado tranquilo.",
                {"n": len(sospechosos),
                 "ejemplos": [f"{r['coin']}@{r['close_ts']}"
                              for r in sospechosos[:5]]}))

        # I6 — la ventana de reporte no se solapa ni deja huecos.
        out.extend(_check_cursores(con))

    except Exception as exc:  # noqa: BLE001
        out.append(Violation("chequeo", f"el verificador fallo: {exc}"[:200]))
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass
    return out


def _f(v: Any) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _solapamientos(con) -> list[tuple[str, str, dict, dict]]:
    """Pares de ciclos del mismo (wallet, coin) cuyos intervalos se pisan."""
    filas = [dict(r) for r in con.execute(
        "SELECT wallet, coin, open_ts, close_ts FROM ledger_positions "
        "ORDER BY wallet, coin, open_ts")]
    out = []
    por_par: dict[tuple[str, str], list[dict]] = {}
    for r in filas:
        por_par.setdefault((r["wallet"], r["coin"]), []).append(r)
    for (w, c), lst in por_par.items():
        for prev, cur in zip(lst, lst[1:]):
            if int(cur["open_ts"]) < int(prev["close_ts"]):
                out.append((w, c, prev, cur))
    return out


def _check_cursores(con) -> list[Violation]:
    """I6 — los cursores de sincronizacion no pueden retroceder.

    Si un cursor retrocede, la proxima ventana se solapa con la anterior y el
    mismo funding se suma dos veces. Si salta hacia adelante sin haber leido,
    queda un hueco y ese funding no se suma nunca. Los dos casos producen un
    NET equivocado sin ningun error.
    """
    out: list[Violation] = []
    if "ledger_meta" not in _tables(con):
        return out
    try:
        metas = {r["key"]: r["value"] for r in con.execute(
            "SELECT key, value FROM ledger_meta")}
    except Exception:  # noqa: BLE001
        return out
    for key, val in metas.items():
        if not key.startswith("cursor_"):
            continue
        try:
            cur = int(val)
        except (TypeError, ValueError):
            out.append(Violation("I6 cursor no numerico",
                                 f"{key}={val!r}"))
            continue
        if cur < 0:
            out.append(Violation("I6 cursor negativo", f"{key}={cur}"))
            continue
        # Un cursor por delante del ultimo dato leido significa que se
        # avanzo sin haber guardado: ese tramo no se lee nunca mas.
        wallet = key[len("cursor_"):].split("_")[0].lower()
        try:
            row = con.execute(
                "SELECT MAX(time) m FROM ledger_fills WHERE wallet=?",
                (wallet,)).fetchone()
        except Exception:  # noqa: BLE001
            continue
        ultimo = int(row["m"] or 0) if row else 0
        # 8 dias de margen: el cursor avanza con el horizonte de la API aunque
        # no haya operado, asi que solo se marca un adelanto claramente raro.
        if ultimo and cur > ultimo + 8 * 86_400_000:
            out.append(Violation(
                "I6 cursor adelantado sin datos",
                f"{key}={cur} pero el ultimo fill guardado es {ultimo} "
                f"({(cur - ultimo) / 86_400_000:.1f} dias de hueco)"))
    return out


# ─── 3.2 RECOMPUTO INDEPENDIENTE ────────────────────────────────────────────

def recompute_from_fills(limit: int = 200) -> list[Violation]:
    """Reconstruye los agregados desde los fills por OTRO camino y compara.

    Deliberadamente NO reutiliza nada de ``rebuild_wallet_positions``: no
    importa sus helpers, no comparte su recorrido y agrupa con SQL en vez de
    con un bucle de estado en Python. Esa independencia es todo el valor del
    chequeo. Si algun dia alguien "simplifica" esto llamando a la funcion del
    ledger, el chequeo pasa a ser un espejo y deja de probar nada — por eso
    lo dice el docstring y lo fija un test.

    Se comparan tres agregados por ciclo:
      * fees_total  = suma de fee de los fills del intervalo
      * funding_net = suma de usdc de ledger_funding en el intervalo
      * gross_pnl   = suma de closed_pnl de los fills de cierre del intervalo
    """
    out: list[Violation] = []
    try:
        con = _conn()
    except Exception as exc:  # noqa: BLE001
        return [Violation("acceso", f"no se pudo abrir el ledger: {exc}"[:200])]
    try:
        tabs = _tables(con)
        for t in ("ledger_positions", "ledger_fills", "ledger_funding"):
            if t not in tabs:
                return [Violation("acceso", f"no existe {t}")]

        ciclos = [dict(r) for r in con.execute(
            "SELECT * FROM ledger_positions ORDER BY close_ts DESC LIMIT ?",
            (limit,))]
        for c in ciclos:
            w, coin = c["wallet"], c["coin"]
            a, b = int(c["open_ts"]), int(c["close_ts"])

            # Agregacion por SQL puro sobre el intervalo cerrado del ciclo.
            f = con.execute(
                "SELECT COALESCE(SUM(fee),0) fees, "
                "       COALESCE(SUM(closed_pnl),0) gross, "
                "       COUNT(*) n "
                "FROM ledger_fills "
                "WHERE wallet=? AND coin=? AND time BETWEEN ? AND ?",
                (w, coin, a, b)).fetchone()
            fu = con.execute(
                "SELECT COALESCE(SUM(usdc),0) fund, COUNT(*) n "
                "FROM ledger_funding "
                "WHERE wallet=? AND coin=? AND time BETWEEN ? AND ?",
                (w, coin, a, b)).fetchone()

            ident = f"{coin} {w[:8]} [{a}..{b}]"
            if int(f["n"] or 0) == 0:
                out.append(Violation(
                    "R1 ciclo sin fills en su propio intervalo",
                    f"{ident}: el ciclo existe en ledger_positions pero no "
                    f"hay ni un fill entre su apertura y su cierre. Los "
                    f"numeros de esa fila no salen de ningun dato.", c))
                continue

            if abs(_f(f["fees"]) - _f(c["fees_total"])) > TOL_USD:
                out.append(Violation(
                    "R2 fees recomputadas != guardadas",
                    f"{ident}: guardado {_f(c['fees_total']):.4f}, "
                    f"recomputado {_f(f['fees']):.4f} sobre {f['n']} fills", c))

            if abs(_f(f["gross"]) - _f(c["gross_pnl"])) > TOL_USD:
                out.append(Violation(
                    "R3 gross recomputado != guardado",
                    f"{ident}: guardado {_f(c['gross_pnl']):.4f}, "
                    f"recomputado {_f(f['gross']):.4f}", c))

            # El funding es el que se rompio: si hay filas de funding en el
            # intervalo y el ciclo guarda 0.00, el recomputo lo canta.
            if abs(_f(fu["fund"]) - _f(c["funding_net"])) > TOL_USD:
                out.append(Violation(
                    "R4 funding recomputado != guardado",
                    f"{ident}: guardado {_f(c['funding_net']):.4f}, "
                    f"recomputado {_f(fu['fund']):.4f} sobre {fu['n']} "
                    f"acreditaciones", c))
    except Exception as exc:  # noqa: BLE001
        out.append(Violation("recomputo", f"el recomputo fallo: {exc}"[:200]))
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass
    return out


# ─── API para /diagnostico y el self-test ───────────────────────────────────

def run_all(limit: int = 200) -> dict[str, Any]:
    """Corre invariantes + recomputo. Devuelve un resumen serializable."""
    inv = check_invariants(limit)
    rec = recompute_from_fills(limit)
    return {
        "ok": not inv and not rec,
        "invariantes": [str(v) for v in inv],
        "recomputo": [str(v) for v in rec],
        "total": len(inv) + len(rec),
        "limite_filas": limit,
    }
