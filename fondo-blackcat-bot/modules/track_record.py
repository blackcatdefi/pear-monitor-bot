"""R-BOT-DEFINITIVE Fase 6 — /trackrecord: la serie publica de la canasta.

QUE ES
======
Una linea por CICLO, no por pata: cuanto puso el fondo, cuanto saco, en cuanto
tiempo, y el acumulado. Pensado para pegarlo tal cual afuera sin editar nada.

LA REGLA QUE DEFINE ESTE MODULO
===============================
Un ciclo es lo que dice la columna ``ledger_positions.cycle_tag``. Punto.

Este modulo NO agrupa por fecha, NO infiere ciclos por cercania de cierres y
NO junta patas porque "se ven del mismo dia". El tag se calcula UNA vez, al
reconstruir el ledger (``cluster_cycles``), se guarda en la fila, y todo lo
que se muestre despues LEE esa columna.

La diferencia importa mas de lo que parece. Si el render adivinara los ciclos,
el track record publico cambiaria cada vez que se ajusta la heuristica: el
mismo historico daria numeros distintos segun el dia en que se pidiera, y no
habria forma de saber cual estuvo mal. Un track record que se puede reescribir
solo no es un track record.

Corolario incomodo pero necesario: las posiciones con ``cycle_tag`` NULL —
operaciones sueltas, no canasta — se muestran aparte, con nombre y todo, y
entran al total all-time. No se las mete en el ciclo mas cercano para que la
tabla quede prolija. Meter una pata en un ciclo que no es suyo es exactamente
la clase de numero plausible y falso que esta ronda existe para eliminar.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


def _fecha(ts_ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc)\
            .strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return "?"


def _agg(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Suma un conjunto de patas. NET = gross - fees + funding, siempre.

    El NET se suma de la columna ``net_pnl`` (que ya es la formula aplicada
    fila por fila) Y se recalcula de sus componentes. Si los dos no coinciden,
    se dice: es la misma comprobacion que hacen los invariantes de la Fase 3,
    puesta donde el numero se PUBLICA. Un track record que no cierra consigo
    mismo no se muestra como si cerrara.
    """
    gross = sum(float(r.get("gross_pnl") or 0) for r in rows)
    fees = sum(float(r.get("fees_total") or 0) for r in rows)
    fund = sum(float(r.get("funding_net") or 0) for r in rows)
    net = sum(float(r.get("net_pnl") or 0) for r in rows)
    margen = sum(float(r.get("margin_open") or 0) for r in rows)
    net_formula = gross - abs(fees) + fund
    return {
        "patas": len(rows),
        "gross": gross,
        "fees": abs(fees),
        "funding": fund,
        "net": net,
        "margen": margen,
        "roe_pct": (100.0 * net / margen) if margen > 0 else None,
        "descuadre": abs(net - net_formula),
        "cuadra": abs(net - net_formula) <= max(0.01, abs(net) * 0.001),
        "apertura": min((int(r["open_ts"]) for r in rows), default=0),
        "cierre": max((int(r["close_ts"]) for r in rows), default=0),
        "wallets": sorted({str(r.get("wallet") or "") for r in rows}),
        "monedas": sorted({str(r.get("coin") or "") for r in rows}),
    }


def build_track_record() -> dict[str, Any]:
    """Serie de ciclos leida de la columna cycle_tag. No infiere nada."""
    from modules.trade_ledger import _conn, degraded_wallets

    con = _conn()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM ledger_positions ORDER BY close_ts")]
    finally:
        con.close()

    con_tag = [r for r in rows if r.get("cycle_tag")]
    sin_tag = [r for r in rows if not r.get("cycle_tag")]

    por_tag: dict[str, list[dict[str, Any]]] = {}
    for r in con_tag:
        por_tag.setdefault(str(r["cycle_tag"]), []).append(r)

    ciclos = []
    for tag, trs in por_tag.items():
        c = _agg(trs)
        c["tag"] = tag
        # Split por wallet DENTRO del ciclo: el combinado es el numero
        # publicable, pero sin el desglose no se puede auditar de donde sale.
        por_wallet = {}
        for r in trs:
            por_wallet.setdefault(str(r["wallet"]), []).append(r)
        c["por_wallet"] = {w: _agg(v) for w, v in por_wallet.items()}
        c["duracion_ms"] = max(0, c["cierre"] - c["apertura"])
        ciclos.append(c)
    ciclos.sort(key=lambda c: c["cierre"])

    total = _agg(rows)
    total["ciclos"] = len(ciclos)
    total["patas_sueltas"] = len(sin_tag)

    # Salud: si el ledger esta incompleto, el track record TAMBIEN lo esta, y
    # eso viaja pegado al dato en vez de quedar en otra pantalla.
    try:
        degradadas = degraded_wallets()
    except Exception as exc:  # noqa: BLE001
        log.warning("track_record: no se pudo leer sync_health: %s", exc)
        degradadas = []

    return {
        "ciclos": ciclos,
        "sueltas": [dict(_agg([r]), coin=r.get("coin"), wallet=r.get("wallet"),
                         side=r.get("side")) for r in sin_tag],
        "total": total,
        "wallets_degradadas": [str(h.get("wallet") or "") for h in degradadas],
        "descuadres": [c["tag"] for c in ciclos if not c["cuadra"]],
        "generado_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _usd(v: float | None) -> str:
    from modules.trade_ledger import _fmt_usd
    return _fmt_usd(v)


def format_track_record(tr: dict[str, Any] | None = None,
                        *, incluir_sueltas: bool = True) -> str:
    from modules.trade_ledger import _fmt_dur, wallet_label

    tr = tr or build_track_record()
    L: list[str] = ["\U0001f4ca *TRACK RECORD — canasta Black Cat*"]

    # El banner va ARRIBA del numero, no abajo: si el ledger esta incompleto,
    # quien copie esto tiene que enterarse antes de leer el total.
    if tr["wallets_degradadas"]:
        wl = ", ".join(wallet_label(w) for w in tr["wallets_degradadas"])
        L.append(f"\u26a0\ufe0f LEDGER INCOMPLETO ({wl}) \u2014 los numeros de "
                 f"abajo NO son definitivos. Ver /diagnostico.")
    if tr["descuadres"]:
        L.append(f"\u26a0\ufe0f {len(tr['descuadres'])} ciclo(s) donde el NET "
                 f"guardado no coincide con gross-fees+funding: "
                 f"{', '.join(tr['descuadres'][:3])}")

    if not tr["ciclos"] and not tr["sueltas"]:
        L.append("Todavia no hay ciclos cerrados en el ledger.")
        return "\n".join(L)

    L.append("")
    acumulado = 0.0
    for c in tr["ciclos"]:
        acumulado += c["net"]
        roe = f"{c['roe_pct']:+.1f}%" if c["roe_pct"] is not None else "n/d"
        L.append(f"\u25cf *{c['tag']}* \u2014 {_fecha(c['apertura'])} \u2192 "
                 f"{_fecha(c['cierre'])} ({_fmt_dur(c['duracion_ms'])})")
        L.append(f"    {c['patas']} patas: {', '.join(c['monedas'][:8])}")
        if len(c["por_wallet"]) > 1:
            for w, a in sorted(c["por_wallet"].items()):
                L.append(f"    \u2500 {wallet_label(w)}: NET {_usd(a['net'])}"
                         + (f" ({a['roe_pct']:+.1f}% s/margen)"
                            if a["roe_pct"] is not None else ""))
        L.append(f"    *NET {_usd(c['net'])}* ({roe} sobre margen) \u00b7 "
                 f"gross {_usd(c['gross'])} \u00b7 fees {_usd(-c['fees'])} \u00b7 "
                 f"funding {_usd(c['funding'])}")
        L.append(f"    acumulado: {_usd(acumulado)}")

    if incluir_sueltas and tr["sueltas"]:
        L.append("")
        L.append(f"*Operaciones sueltas* (fuera de canasta, {len(tr['sueltas'])})")
        # No se las mete en ningun ciclo: sumarlas a la fuerza a la canasta
        # inflaria o deprimiria un numero que se publica.
        neto_sueltas = sum(s["net"] for s in tr["sueltas"])
        L.append(f"    NET conjunto {_usd(neto_sueltas)}")

    t = tr["total"]
    L.append("")
    L.append(f"*TOTAL ALL-TIME* \u2014 {t['ciclos']} ciclo(s) + "
             f"{t['patas_sueltas']} suelta(s), {t['patas']} patas")
    L.append(f"  NET {_usd(t['net'])} \u00b7 gross {_usd(t['gross'])} \u00b7 "
             f"fees {_usd(-t['fees'])} \u00b7 funding {_usd(t['funding'])}")
    if t["roe_pct"] is not None:
        L.append(f"  {t['roe_pct']:+.1f}% sobre margen acumulado "
                 f"({_usd(t['margen'])})")
    L.append("")
    L.append("NET = gross - fees + funding (+ = cobrado / - = pagado). "
             "Ciclos segun el tag guardado en el ledger, no inferidos por fecha.")
    return "\n".join(L)
