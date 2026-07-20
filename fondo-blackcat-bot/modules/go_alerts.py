"""R-SIGNAL-DIET (2026-07-20) — 5/5 GO entry alerts.

La ÚNICA señal proactiva de trading que el bot empuja: cuando un nombre CRUZA
a 5/5 GO en el screener R-SCREEN (mismo code path que /unlockcheck), en
cualquiera de los dos lados (SHORT = ``is_go_candidate``; LONG =
``long.flag``). Corre cada 60 min (GO_ALERTS_INTERVAL_MIN) y diffea el set GO
actual vs el anterior — SOLO empuja entrantes nuevos.

Anti-spam:
  (a) cooldown — un token que sale de 5/5 y re-entra solo re-alerta si estuvo
      fuera ≥6h (GO_ALERT_COOLDOWN_HOURS);
  (b) >5 entrantes nuevos en un run → UN solo mensaje agrupado (regime flip);
  (c) fallo del screener → log silencioso; push de error SOLO al 3er fallo
      consecutivo (y una única vez hasta el próximo éxito).

Cost guard: usa ``compute_screen_cached`` (TTL 10 min, advance_state=False —
pure read; el z-persistence lo avanza EXCLUSIVAMENTE ``_unlock_monitor_job``).

Primer run sin estado previo = seeding silencioso del baseline (los GOs ya
vigentes no son "entrantes nuevos"; empujarlos en cada boot sería spam).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from config import DATA_DIR, TELEGRAM_CHAT_ID
from utils.telegram import send_bot_message

log = logging.getLogger(__name__)

STATE_FILE = os.path.join(DATA_DIR, "go_alert_state.json")

COOLDOWN_HOURS = float(os.getenv("GO_ALERT_COOLDOWN_HOURS", "6"))
GROUP_THRESHOLD = int(os.getenv("GO_ALERT_GROUP_THRESHOLD", "5"))
FAILURE_PUSH_AFTER = 3  # consecutive failures before ONE error push


# ─── state ───────────────────────────────────────────────────────────────────

def _load_state() -> dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as exc:  # noqa: BLE001
        log.warning("go_alerts: could not save state: %s", exc)


# ─── pure diff logic (unit-tested) ───────────────────────────────────────────

def diff_go_set(
    current: set[str],
    state: dict[str, Any],
    now: float,
    *,
    cooldown_hours: float = COOLDOWN_HOURS,
) -> tuple[list[str], dict[str, Any]]:
    """Diff ``current`` GO keys (``"SHORT:TICKER"`` / ``"LONG:TICKER"``) vs the
    persisted state. Returns ``(to_alert, new_state)``.

    Rules:
      - state empty ("tokens" missing) → SEED baseline, alert nothing.
      - key newly in GO and never seen → alert.
      - key re-enters after leaving: alert only if it stayed OUT ≥ cooldown.
      - key leaves GO → record ``left_at`` (edge for the cooldown clock).
    Pure function — no I/O, no clock reads."""
    tokens: dict[str, Any] = dict(state.get("tokens", {}))
    seeded = bool(state.get("seeded"))
    cooldown_sec = cooldown_hours * 3600.0

    to_alert: list[str] = []
    if not seeded:
        # First ever run: baseline seeding, zero pushes.
        tokens = {k: {"in_go": True, "left_at": None, "last_alert": None}
                  for k in sorted(current)}
        return [], {"seeded": True, "tokens": tokens,
                    "consecutive_failures": 0}

    for key in sorted(current):
        rec = tokens.get(key)
        if rec is None:
            # Brand-new entrant.
            tokens[key] = {"in_go": True, "left_at": None, "last_alert": now}
            to_alert.append(key)
        elif not rec.get("in_go"):
            # Re-entry: cooldown gate.
            left_at = rec.get("left_at")
            out_long_enough = (
                left_at is None or (now - float(left_at)) >= cooldown_sec
            )
            rec["in_go"] = True
            if out_long_enough:
                rec["last_alert"] = now
                to_alert.append(key)
            tokens[key] = rec
        # else: still in GO — nothing to do.

    for key, rec in tokens.items():
        if key not in current and rec.get("in_go"):
            rec["in_go"] = False
            rec["left_at"] = now

    return to_alert, {
        "seeded": True,
        "tokens": tokens,
        "consecutive_failures": int(state.get("consecutive_failures", 0)),
    }


def extract_go_keys(res: Any) -> tuple[set[str], dict[str, Any]]:
    """(go_keys, row_by_key) from a ScreenResult. SHORT = is_go_candidate
    (5/5 non-squeeze); LONG = long.flag (long_context bucket)."""
    keys: set[str] = set()
    rows: dict[str, Any] = {}
    for r in getattr(res, "ranked", []) or []:
        if getattr(r, "is_go_candidate", False):
            k = f"SHORT:{r.ticker}"
            keys.add(k)
            rows[k] = r
    for r in getattr(res, "long_context", []) or []:
        k = f"LONG:{r.ticker}"
        keys.add(k)
        rows[k] = r
    return keys, rows


# ─── rendering ───────────────────────────────────────────────────────────────

def _side_header(key: str) -> str:
    side, _, ticker = key.partition(":")
    icon = "\U0001f534" if side == "SHORT" else "\U0001f7e2"
    return f"{icon} {side} {ticker} — NUEVO 5/5 GO"


def _grouped_line(key: str, row: Any) -> str:
    side, _, ticker = key.partition(":")
    g = getattr(row, "gate", None)
    z = getattr(g, "z", None) if g else None
    h = getattr(g, "hurst", None) if g else None
    z_s = f"z={z:+.2f}" if isinstance(z, (int, float)) else "z=n/d"
    h_s = f"H={h:.2f}" if isinstance(h, (int, float)) else "H=n/d"
    return f"  {'🔴' if side == 'SHORT' else '🟢'} {side} {ticker} · {z_s} · {h_s}"


async def render_alert_message(to_alert: list[str], rows: dict[str, Any]) -> str:
    """ONE message per run. ≤GROUP_THRESHOLD entrantes → bloque telemetry
    compacto por token (mismo formato que /telemetry). Más → agrupado 1 línea
    por token (regime flip, no vale la pena el detalle individual)."""
    n = len(to_alert)
    if n > GROUP_THRESHOLD:
        lines = [f"\U0001f6a8 REGIME FLIP — {n} NUEVOS 5/5 GO en un run",
                 "(agrupado anti-spam; detalle: /unlockcheck)", ""]
        lines += [_grouped_line(k, rows.get(k)) for k in to_alert]
        return "\n".join(lines)

    # Detailed compact blocks (reuse R-TELEMETRY verbatim).
    lines = [f"\U0001f3af NUEVO 5/5 GO ({n}) — confirmá AiPear + tu decisión", ""]
    try:
        from modules.telemetry import (
            _safe_build_from_row,
            fetch_ctx_map,
            format_token_compact,
        )
        ctx_map = await fetch_ctx_map()
        cache: dict[str, Any] = {}
        for key in to_alert:
            lines.append(_side_header(key))
            row = rows.get(key)
            if row is None:
                lines.append("  (row no disponible)")
                continue
            t = await _safe_build_from_row(row, ctx_map, cache)
            lines.append(format_token_compact(t))
            lines.append("")
    except Exception:  # noqa: BLE001
        log.exception("go_alerts: telemetry render failed — falling back to compact lines")
        lines += [_grouped_line(k, rows.get(k)) for k in to_alert]
    return "\n".join(lines).rstrip()


# ─── scheduler entrypoint ────────────────────────────────────────────────────

async def run_go_alert_cycle(bot) -> int:
    """One full cycle: screen (cached) → diff → push new entrants. Returns
    number of alert messages sent (0 or 1 — always ONE message per run).
    NEVER raises (failure counter + 3-strike error push handled inside)."""
    state = _load_state()
    try:
        from modules.universal_screener import compute_screen_cached
        res = await compute_screen_cached()
        current, rows = extract_go_keys(res)
    except Exception:  # noqa: BLE001
        log.exception("go_alerts: screener run failed (silent unless 3 consecutive)")
        fails = int(state.get("consecutive_failures", 0)) + 1
        state["consecutive_failures"] = fails
        _save_state(state)
        if fails == FAILURE_PUSH_AFTER and TELEGRAM_CHAT_ID:
            await send_bot_message(
                bot, TELEGRAM_CHAT_ID,
                f"\u26a0\ufe0f GO alerts: screener fall\u00f3 {fails} runs consecutivos "
                "\u2014 revisar /errors y logs Railway.",
            )
        return 0

    now = time.time()
    to_alert, new_state = diff_go_set(current, state, now)
    new_state["consecutive_failures"] = 0  # success resets the strike counter
    _save_state(new_state)

    log.info(
        "go_alerts OK — go_set=%d new_entrants=%d seeded=%s",
        len(current), len(to_alert), bool(state.get("seeded")),
    )
    if not to_alert:
        return 0
    msg = await render_alert_message(to_alert, rows)
    if TELEGRAM_CHAT_ID:
        await send_bot_message(bot, TELEGRAM_CHAT_ID, msg)
    return 1
