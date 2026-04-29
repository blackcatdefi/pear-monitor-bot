"""Trade del Ciclo — BTC LONG 10x en Blofin (manual tracking).

Blofin does NOT expose a public portfolio API, so the bot stores the last
known state (margin, entry, size, mark) in a JSON file and updates it via
/ciclo_update from Telegram.

State file: DATA_DIR/cycle_trade.json
Schema:
{
  "active": true|false,
  "last_update_utc": ISO,
  "entry_avg": 77200.0,
  "size_btc": 0.0065,
  "margin_usd": 500.0,
  "mark_px": 77300.0,
  "leverage": 10,
  "dca_completed": ["entry"],  // ENTRY / ADD_1 / ADD_2 / ADD_3
  "notes": "optional free text"
}
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from config import DATA_DIR

log = logging.getLogger(__name__)

STATE_FILE = os.path.join(DATA_DIR, "cycle_trade.json")

# DCA plan (BTC prices + margin chunks)
DCA_PLAN = [
    {"key": "ENTRY", "trigger": 77_000.0, "margin_usd": 500.0, "desc": "Entry inicial ~BTC $77K"},
    {"key": "ADD_1", "trigger": 70_000.0, "margin_usd": 500.0, "desc": "ADD 1 @ BTC $70K"},
    {"key": "ADD_2", "trigger": 63_000.0, "margin_usd": 750.0, "desc": "ADD 2 @ BTC $63K"},
    {"key": "ADD_3", "trigger": 55_000.0, "margin_usd": 1000.0, "desc": "ADD 3 @ BTC $55K"},
]

TOTAL_DEPLOYABLE = sum(s["margin_usd"] for s in DCA_PLAN)  # 2750
LIQ_TARGET_RANGE = (45_000.0, 50_000.0)
TP_PARTIAL = 130_000.0
TP_MAIN = 150_000.0


def _load() -> dict[str, Any]:
    if not os.path.isfile(STATE_FILE):
        return {"active": False}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {"active": False}


def _save(state: dict[str, Any]) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        log.exception("cycle_trade save failed")


def get_state() -> dict[str, Any]:
    """Read current Trade del Ciclo state. Always returns a dict."""
    s = _load()
    # Normalize defaults
    s.setdefault("active", False)
    s.setdefault("entry_avg", 0.0)
    s.setdefault("size_btc", 0.0)
    s.setdefault("margin_usd", 0.0)
    s.setdefault("mark_px", 0.0)
    s.setdefault("leverage", 10)
    s.setdefault("dca_completed", [])
    s.setdefault("last_update_utc", None)
    s.setdefault("notes", "")
    return s


def _parse_kv_args(raw_args: list[str]) -> dict[str, str]:
    """Parse key=value args from a Telegram command."""
    out: dict[str, str] = {}
    for token in raw_args:
        if "=" in token:
            k, v = token.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


def update_from_args(raw_args: list[str]) -> tuple[dict[str, Any], str]:
    """Parse /ciclo_update args and persist. Returns (new_state, human_summary).

    Supported keys: margin, entry, size, mark, leverage, notes, active (true/false),
    close (closes position), dca_add (marks a DCA leg completed: ENTRY/ADD_1/...).
    """
    kv = _parse_kv_args(raw_args)
    if not kv:
        raise ValueError(
            "Uso: /ciclo_update margin=500 entry=77200 size=0.0065 mark=77300 [leverage=10] [dca_add=ADD_1] [notes=\"...\"]\n"
            "Cierre: /ciclo_update close=true"
        )

    state = get_state()

    # Handle closure shortcut
    if kv.get("close", "").lower() in ("true", "1", "yes"):
        state["active"] = False
        state["margin_usd"] = 0.0
        state["size_btc"] = 0.0
        state["last_update_utc"] = datetime.now(timezone.utc).isoformat()
        _save(state)
        return state, "✅ Trade del Ciclo marcado como CERRADO."

    def _num(key: str, default: float) -> float:
        v = kv.get(key)
        if v is None:
            return default
        try:
            return float(v)
        except ValueError as exc:
            raise ValueError(f"{key} debe ser numérico: {exc}") from None

    state["margin_usd"] = _num("margin", float(state.get("margin_usd") or 0.0))
    state["entry_avg"] = _num("entry", float(state.get("entry_avg") or 0.0))
    state["size_btc"] = _num("size", float(state.get("size_btc") or 0.0))
    state["mark_px"] = _num("mark", float(state.get("mark_px") or 0.0))
    if "leverage" in kv:
        try:
            state["leverage"] = int(float(kv["leverage"]))
        except ValueError:
            pass
    if "notes" in kv:
        state["notes"] = kv["notes"]
    if "active" in kv:
        state["active"] = kv["active"].lower() in ("true", "1", "yes")
    else:
        # Auto: if margin>0 and size>0, mark active
        state["active"] = state["margin_usd"] > 0.0 and state["size_btc"] > 0.0

    if "dca_add" in kv:
        leg = kv["dca_add"].upper()
        if leg not in (s["key"] for s in DCA_PLAN):
            raise ValueError(f"dca_add debe ser uno de {[s['key'] for s in DCA_PLAN]}")
        completed = set(state.get("dca_completed") or [])
        completed.add(leg)
        state["dca_completed"] = sorted(completed, key=lambda k: next(i for i, s in enumerate(DCA_PLAN) if s["key"] == k))
    elif state["active"] and state["margin_usd"] > 0 and "ENTRY" not in (state.get("dca_completed") or []):
        # Auto-mark ENTRY if first activation
        state["dca_completed"] = ["ENTRY"]

    state["last_update_utc"] = datetime.now(timezone.utc).isoformat()
    _save(state)

    # Compute derived metrics for summary
    upnl = compute_upnl(state)
    liq = estimated_liq_price(state)
    next_trigger = next_dca_trigger(state)

    sign = "+" if upnl >= 0 else ""
    pct = 0.0
    if state["margin_usd"] > 0:
        pct = upnl / state["margin_usd"] * 100

    summary = (
        "✅ Trade del Ciclo actualizado\n"
        f"Margin: ${state['margin_usd']:,.0f} | Entry: ${state['entry_avg']:,.0f} | Size: {state['size_btc']:.4f} BTC\n"
        f"Mark: ${state['mark_px']:,.0f} | UPnL: {sign}${upnl:,.2f} ({sign}{pct:.2f}%)\n"
        f"Liq estimada: ${liq:,.0f} (a {state['leverage']}x)\n"
    )
    if next_trigger:
        summary += (
            f"Próximo DCA trigger: BTC ${next_trigger['trigger']:,.0f} → +${next_trigger['margin_usd']:,.0f} margin"
        )
    else:
        summary += "DCA plan completo (todos los adds marcados)."

    return state, summary


# ─── Derived metrics ────────────────────────────────────────────────────────
def compute_upnl(state: dict[str, Any]) -> float:
    """UPnL USD = size * (mark - entry). No funding, no fees — close enough."""
    size = float(state.get("size_btc") or 0.0)
    mark = float(state.get("mark_px") or 0.0)
    entry = float(state.get("entry_avg") or 0.0)
    if size <= 0 or mark <= 0 or entry <= 0:
        return 0.0
    return size * (mark - entry)


def estimated_liq_price(state: dict[str, Any]) -> float:
    """Very rough liq estimate for a LONG at given leverage.

    liq ≈ entry * (1 - 1/leverage + maintenance_margin_rate).
    We assume maintenance ~0.5%, standard-ish for major exchanges.
    Approximate only — exchange-specific haircut/funding will shift it.
    """
    entry = float(state.get("entry_avg") or 0.0)
    lev = max(1, int(state.get("leverage") or 10))
    if entry <= 0:
        return 0.0
    maintenance = 0.005
    return max(0.0, entry * (1 - 1 / lev + maintenance))


def next_dca_trigger(state: dict[str, Any]) -> dict[str, Any] | None:
    """Return next uncompleted DCA leg."""
    completed = set(state.get("dca_completed") or [])
    for leg in DCA_PLAN:
        if leg["key"] not in completed:
            return leg
    return None


def dca_progress_lines(state: dict[str, Any]) -> list[str]:
    """Return a list of '[✅ ENTRY]' / '[⏳ ADD 1]' style progress lines."""
    completed = set(state.get("dca_completed") or [])
    out: list[str] = []
    for leg in DCA_PLAN:
        icon = "✅" if leg["key"] in completed else "⏳"
        status = "DONE" if leg["key"] in completed else "pending"
        out.append(
            f"  [{icon} {leg['key']}] BTC ${leg['trigger']:,.0f} → ${leg['margin_usd']:,.0f} margin ({status})"
        )
    return out


def status_label(state: dict[str, Any]) -> str:
    if not state.get("active"):
        return "INACTIVO"
    if state.get("margin_usd", 0) == 0:
        return "PENDIENTE (esperando bonus 5 días)"
    return "ACTIVO"


# ─── Text formatters for /ciclo and /posiciones ─────────────────────────────
def format_status_short(state: dict[str, Any]) -> str:
    """Short 3-5 line block used INSIDE /posiciones and /reporte 'POSICIONES'."""
    if not state.get("active") and state.get("margin_usd", 0) == 0 and not state.get("dca_completed"):
        return (
            "TRADE DEL CICLO (Blofin - BTC LONG)\n"
            "  Status: INACTIVO — sin posición abierta\n"
            "  (usar /ciclo_update para registrar entrada)"
        )

    upnl = compute_upnl(state)
    liq = estimated_liq_price(state)
    next_t = next_dca_trigger(state)
    sign = "+" if upnl >= 0 else ""
    pct = (upnl / state["margin_usd"] * 100) if state.get("margin_usd", 0) > 0 else 0.0
    last_upd = state.get("last_update_utc") or "?"
    last_upd_short = last_upd[:16].replace("T", " ") if last_upd else "?"

    lines = [
        "TRADE DEL CICLO (Blofin - BTC LONG)",
        f"  Status: {status_label(state)}",
        f"  Last update: {last_upd_short} UTC",
        f"  Entry avg: ${state.get('entry_avg', 0):,.0f} | Mark: ${state.get('mark_px', 0):,.0f}",
        f"  Size: {state.get('size_btc', 0):.4f} BTC | Leverage: {state.get('leverage', 10)}x",
        f"  Margin deployed: ${state.get('margin_usd', 0):,.0f} / ${TOTAL_DEPLOYABLE:,.0f} plan",
        f"  Liq estimada: ${liq:,.0f}",
        f"  UPnL: {sign}${upnl:,.2f} ({sign}{pct:.2f}%)",
    ]
    if next_t:
        lines.append(f"  DCA next trigger: BTC ${next_t['trigger']:,.0f} → +${next_t['margin_usd']:,.0f} margin")
    else:
        lines.append("  DCA plan: COMPLETO")
    return "\n".join(lines)


def format_full_status(state: dict[str, Any]) -> str:
    """Full /ciclo output — position + DCA plan + horizon + TP/liq zones."""
    if not state.get("active") and state.get("margin_usd", 0) == 0:
        return (
            "🎯 TRADE DEL CICLO — Estado\n\n"
            "Posición: INACTIVA (sin entrada registrada)\n\n"
            "Plan DCA (pendiente ejecución):\n"
            + "\n".join(dca_progress_lines(state))
            + f"\n\nTotal deployable: ${TOTAL_DEPLOYABLE:,.0f}\n"
            f"Liq final post-DCA completo (~): ${43_500:,.0f}\n"
            f"Horizonte: 12-18 meses\n"
            f"TP target: ${TP_MAIN:,.0f}+\n\n"
            "Usá /ciclo_update margin=500 entry=77200 size=0.0065 mark=77300 cuando abras la posición."
        )

    upnl = compute_upnl(state)
    liq = estimated_liq_price(state)
    sign = "+" if upnl >= 0 else ""
    pct = (upnl / state["margin_usd"] * 100) if state.get("margin_usd", 0) > 0 else 0.0
    last_upd = state.get("last_update_utc") or "?"
    last_upd_short = last_upd[:16].replace("T", " ") if last_upd else "?"

    lines = [
        "🎯 TRADE DEL CICLO — Estado",
        "",
        f"Status: {status_label(state)} | Last update: {last_upd_short} UTC",
        "",
        "Posición:",
        f"  Entry: ${state.get('entry_avg', 0):,.0f} | Mark: ${state.get('mark_px', 0):,.0f} | Size: {state.get('size_btc', 0):.4f} BTC",
        f"  Leverage: {state.get('leverage', 10)}x | Margin: ${state.get('margin_usd', 0):,.0f} | Liq est: ${liq:,.0f}",
        f"  UPnL: {sign}${upnl:,.2f} ({sign}{pct:.2f}%)",
        "",
        "Plan DCA:",
    ]
    lines.extend(dca_progress_lines(state))
    lines.extend(
        [
            "",
            f"Total deployable: ${TOTAL_DEPLOYABLE:,.0f}",
            f"Liq final post-DCA completo (~): ${43_500:,.0f}",
            f"Horizonte: 12-18 meses",
            f"TP target: ${TP_MAIN:,.0f}+  (parcial ${TP_PARTIAL:,.0f})",
            f"Liq zone (salvar posición): ${LIQ_TARGET_RANGE[0]:,.0f}-${LIQ_TARGET_RANGE[1]:,.0f}",
        ]
    )
    if state.get("notes"):
        lines.append("")
        lines.append(f"Notas: {state['notes']}")
    return "\n".join(lines)
