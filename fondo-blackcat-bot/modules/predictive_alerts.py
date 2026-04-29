"""Round 18 — Predictive (trend-based) risk alerts.

Records snapshots of critical metrics (HF flywheel, HYPE price, UETH APY)
to a small SQLite table, then computes a linear slope over the last N
samples. If the slope is downward and the current value crosses the
critical threshold within HORIZON_HOURS, fire an edge-triggered alert.

Edge-triggered = if the metric has already alerted in the same direction
within COOLDOWN_HOURS, suppress duplicates.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from config import DATA_DIR, TELEGRAM_CHAT_ID

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "predictive_alerts.db")

HORIZON_HOURS = float(os.getenv("PREDICTIVE_ALERTS_HORIZON_HOURS", "24"))
MIN_SAMPLES = int(os.getenv("PREDICTIVE_ALERTS_MIN_SAMPLES", "6"))
COOLDOWN_HOURS = float(os.getenv("PREDICTIVE_ALERTS_COOLDOWN_HOURS", "6"))


def is_enabled() -> bool:
    return os.getenv("PREDICTIVE_ALERTS_ENABLED", "true").strip().lower() != "false"


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute(
        """CREATE TABLE IF NOT EXISTS metric_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            metric TEXT NOT NULL,
            value REAL NOT NULL
        )"""
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_metric_ts ON metric_history(metric, ts_utc DESC)"
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS metric_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            metric TEXT NOT NULL,
            direction TEXT NOT NULL,
            slope REAL NOT NULL,
            projected REAL NOT NULL,
            hours_to_critical REAL
        )"""
    )
    return c


def _record_sample(metric: str, value: float) -> None:
    if value is None:
        return
    with _conn() as c:
        c.execute(
            "INSERT INTO metric_history(ts_utc,metric,value) VALUES (?,?,?)",
            (datetime.now(timezone.utc).isoformat(), metric, float(value)),
        )
        # purge >7d
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        c.execute(
            "DELETE FROM metric_history WHERE metric=? AND ts_utc<?",
            (metric, cutoff),
        )


def _load_history(metric: str, hours: int = 24) -> list[tuple[datetime, float]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT ts_utc,value FROM metric_history WHERE metric=? AND ts_utc>=? "
            "ORDER BY ts_utc ASC",
            (metric, cutoff),
        ).fetchall()
    out: list[tuple[datetime, float]] = []
    for r in rows:
        try:
            out.append((datetime.fromisoformat(r[0]), float(r[1])))
        except Exception:
            continue
    return out


def _compute_slope_per_hour(history: list[tuple[datetime, float]]) -> float:
    if len(history) < 2:
        return 0.0
    t0 = history[0][0]
    xs = [(t - t0).total_seconds() / 3600.0 for t, _ in history]
    ys = [v for _, v in history]
    n = len(xs)
    sx = sum(xs)
    sy = sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0
    return (n * sxy - sx * sy) / denom


def _last_alert_ts(metric: str, direction: str) -> datetime | None:
    with _conn() as c:
        row = c.execute(
            "SELECT ts_utc FROM metric_alerts WHERE metric=? AND direction=? "
            "ORDER BY id DESC LIMIT 1",
            (metric, direction),
        ).fetchone()
    if not row:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except Exception:
        return None


def _record_alert(metric: str, direction: str, slope: float, projected: float, hours: float | None) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO metric_alerts(ts_utc,metric,direction,slope,projected,hours_to_critical) "
            "VALUES (?,?,?,?,?,?)",
            (
                datetime.now(timezone.utc).isoformat(),
                metric,
                direction,
                slope,
                projected,
                hours,
            ),
        )


METRIC_CONFIGS: dict[str, dict[str, Any]] = {
    "hf_flywheel": {
        "label": "HF flywheel",
        "critical": 1.10,
        "watch_below": True,  # alert when descending toward critical
        "fmt": "{:.3f}",
    },
    "hype_price": {
        "label": "HYPE price (USD)",
        "critical": 36.0,
        "watch_below": True,
        "fmt": "${:.2f}",
    },
    "ueth_apy": {
        "label": "UETH borrow APY",
        "critical": 10.0,
        "watch_below": False,  # alert when rising toward critical
        "fmt": "{:.2f}%",
    },
}


async def _sample_hf_flywheel() -> float | None:
    try:
        from modules.hyperlend import fetch_all_hyperlend
        hl = await fetch_all_hyperlend()
        if isinstance(hl, list):
            hfs = []
            for e in hl:
                if not isinstance(e, dict):
                    continue
                hf = e.get("hf") or e.get("health_factor")
                if isinstance(hf, (int, float)) and 0 < hf < 1000:
                    hfs.append(float(hf))
            if hfs:
                return max(hfs)
    except Exception:
        log.exception("predictive_alerts: HF sample failed")
    return None


async def _sample_hype_price() -> float | None:
    try:
        from modules.portfolio import get_spot_price
        return await get_spot_price("HYPE")
    except Exception:
        log.exception("predictive_alerts: HYPE price sample failed")
        return None


async def _sample_ueth_apy() -> float | None:
    try:
        from modules.hyperlend import get_borrow_apy
        apy = await get_borrow_apy("UETH")
        if apy is None:
            return None
        return float(apy) * 100.0  # convert to %
    except Exception:
        log.exception("predictive_alerts: UETH APY sample failed")
        return None


SAMPLERS = {
    "hf_flywheel": _sample_hf_flywheel,
    "hype_price": _sample_hype_price,
    "ueth_apy": _sample_ueth_apy,
}


async def analyze_trends(bot=None) -> list[dict[str, Any]]:
    """Sample, persist, project. Fire alerts on edge-triggered crossings."""
    if not is_enabled():
        return []
    triggered: list[dict[str, Any]] = []

    for metric, cfg in METRIC_CONFIGS.items():
        sampler = SAMPLERS.get(metric)
        if sampler is None:
            continue
        try:
            value = await sampler()
        except Exception:
            log.exception("predictive_alerts: sampler %s failed", metric)
            value = None
        if value is None:
            continue

        _record_sample(metric, value)
        history = _load_history(metric, hours=24)
        if len(history) < MIN_SAMPLES:
            continue

        slope = _compute_slope_per_hour(history)
        critical = float(cfg["critical"])
        watch_below = bool(cfg["watch_below"])
        projected = value + slope * HORIZON_HOURS

        if watch_below:
            descending = slope < -1e-9
            if not descending or value <= critical:
                continue
            hours_to_critical = (value - critical) / abs(slope)
            direction = "DOWN"
        else:
            ascending = slope > 1e-9
            if not ascending or value >= critical:
                continue
            hours_to_critical = (critical - value) / slope
            direction = "UP"

        if hours_to_critical > HORIZON_HOURS:
            continue

        # cooldown
        last_ts = _last_alert_ts(metric, direction)
        if last_ts:
            elapsed = (datetime.now(timezone.utc) - last_ts).total_seconds() / 3600.0
            if elapsed < COOLDOWN_HOURS:
                continue

        _record_alert(metric, direction, slope, projected, hours_to_critical)
        triggered.append(
            {
                "metric": metric,
                "label": cfg["label"],
                "direction": direction,
                "current": value,
                "slope_per_hour": slope,
                "projected_24h": projected,
                "critical": critical,
                "hours_to_critical": hours_to_critical,
            }
        )

        if bot is not None and TELEGRAM_CHAT_ID:
            msg = format_alert(triggered[-1])
            try:
                from utils.telegram import send_bot_message
                await send_bot_message(bot, TELEGRAM_CHAT_ID, msg)
            except Exception:
                log.exception("predictive_alerts: send failed for %s", metric)
    return triggered


def format_alert(t: dict[str, Any]) -> str:
    fmt = METRIC_CONFIGS[t["metric"]]["fmt"]
    return (
        f"\U0001f4c9 TENDENCIA {t['direction']} \u2014 {t['label']}\n"
        f"Actual: {fmt.format(t['current'])}\n"
        f"Slope: {t['slope_per_hour']:+.4f}/hr\n"
        f"Proyección {HORIZON_HOURS:.0f}h: {fmt.format(t['projected_24h'])}\n"
        f"Cruce zona crítica ({fmt.format(t['critical'])}): "
        f"en {t['hours_to_critical']:.1f}h\n\n"
        "\u26a0\ufe0f Acción preventiva sugerida — ver /reporte para contexto."
    )


async def scheduled_check(application=None) -> None:
    bot = application.bot if application is not None else None
    try:
        await analyze_trends(bot=bot)
    except Exception:
        log.exception("predictive_alerts scheduled_check failed")
