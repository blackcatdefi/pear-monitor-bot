"""LMEC Bear Invalidation Triggers — auto-monitored on each /reporte and /tesis.

R-BOT-TERMINOLOGY-UNIFY (2026-05-07) — Bug #5.

LMEC's bear-thesis convicción should drop only when the four classical
invalidation conditions converge (or get dangerously close). The /tesis
output and the LLM prompt now surface each condition explicitly so the
co-gestor never has to remember to evaluate them by hand.

Conditions (BCD-defined, 2026-05-07):

    1. BTC breaks ATH ($97-98K range).
    2. Weekly MACD on positive territory.
    3. Weekly RSI > 70.
    4. 50-week MA (~$95K) broken with sustained force for 2-3 weeks.

Data inputs
-----------
* BTC spot price + ATH come from the live ``market`` dict produced by
  ``modules.market.fetch_market_data``.
* Weekly MACD / weekly RSI / 50-week MA come from env vars BCD updates
  manually from TradingView (the bot has no first-class TA feed yet).
  The env-var contract is intentionally simple so BCD can /env-update
  in 5 seconds when the weekly close ticks.

Env vars (all optional — missing => "unknown"):

    LMEC_BTC_ATH_USD           default 98000   (top of the ATH band)
    LMEC_MACD_WEEKLY_POSITIVE  "true"|"false"  (weekly MACD line above 0)
    LMEC_RSI_WEEKLY            number          (weekly RSI 14)
    LMEC_MA50W_USD             number          (current 50-week MA value)
    LMEC_MA50W_BROKEN_WEEKS    int             (consecutive weeks with
                                                 close above the MA50w)

Public API
----------
    evaluate_lmec_triggers(market: dict | None = None) -> dict
        {
            "ts_utc": iso,
            "btc_price_usd": float | None,
            "conditions": [
                {"id": "btc_above_ath", "name": "...", "status": "VALIDA"|
                 "NEUTRO"|"INVALIDA"|"UNKNOWN", "detail": "..."},
                ...
            ],
            "any_triggered": bool,
            "all_triggered": bool,
            "triggered_count": int,
        }

    format_lmec_block(result: dict | None = None) -> str
        Pretty Telegram-flavoured block for /tesis and /reporte. Calls
        evaluate_lmec_triggers() with no market arg if ``result`` is None.

Status semantics (per condition)
--------------------------------
* VALIDA   — bear thesis IS invalidated by this leg (e.g. BTC > ATH).
* NEUTRO   — leg is close (within tolerance) but not yet broken.
* INVALIDA — bear thesis still intact on this leg (e.g. BTC < ATH).
* UNKNOWN  — input missing or unparseable.

Aggregation rules
-----------------
* ``any_triggered`` is True if ≥1 leg returns ``VALIDA``.
* ``all_triggered`` is True only when all four legs return ``VALIDA``.
* The /reporte path uses ``any_triggered`` to surface a critical alert
  ("convicción debe bajar — revisar tesis") and lets the LLM decide the
  exact action.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


def _env_float(name: str, default: float | None = None) -> float | None:
    raw = os.getenv(name, "")
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int | None = None) -> int | None:
    raw = os.getenv(name, "")
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _env_bool_optional(name: str) -> bool | None:
    """Return True/False if the env var is explicitly set, else None."""
    raw = os.getenv(name, "")
    if raw is None or str(raw).strip() == "":
        return None
    return str(raw).strip().lower() in ("true", "1", "yes", "y", "on")


def _manual_lmec_inputs() -> dict[str, object]:
    """P1.9: BCD's persisted /setlmec inputs (MACD/RSI/MA50w). Never raises."""
    try:
        from modules.lmec_state import get_manual_inputs
        return get_manual_inputs()
    except Exception:  # noqa: BLE001
        return {}


def _btc_price_from_market(market: dict[str, Any] | None) -> float | None:
    """Best-effort BTC spot extraction shared with other modules."""
    if not isinstance(market, dict):
        return None
    # market may be either {data: {prices: {...}}}, {prices: {...}}, or a
    # plain {coin: {price: x}} dict — accept all three layouts.
    candidates: list[dict[str, Any]] = []
    if isinstance(market.get("data"), dict):
        candidates.append(market["data"])
    candidates.append(market)
    for blob in candidates:
        prices = blob.get("prices") if isinstance(blob, dict) else None
        if isinstance(prices, dict):
            entry = prices.get("BTC") or {}
            for key in ("price_usd", "price", "usd"):
                v = entry.get(key) if isinstance(entry, dict) else None
                if v is not None:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        continue
        # Fall-through: top-level {BTC: {price: x}}
        entry = blob.get("BTC") if isinstance(blob, dict) else None
        if isinstance(entry, dict):
            for key in ("price_usd", "price", "usd"):
                v = entry.get(key)
                if v is not None:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        continue
    return None


def evaluate_lmec_triggers(market: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compute the 4 LMEC invalidation legs.

    Each leg returns ``status`` ∈ {VALIDA, NEUTRO, INVALIDA, UNKNOWN}
    and a one-line ``detail`` ready to render to Telegram.

    R-BOT-FEEDS-EXPAND (2026-05-07): TraderMap.io overrides take
    precedence over the LMEC_* env vars when present, so updating
    ``TRADERMAP_BTC_RSI`` / ``TRADERMAP_BTC_MACD`` / ``TRADERMAP_BTC_MA50W``
    in Railway propagates to legs 2 / 3 / 4 automatically. The override
    is best-effort — if the TraderMap module fails to import the function
    silently degrades back to the LMEC_* env vars.

    R-BOT-LMEC-AUTOFEED (2026-05-07): Leg-4 weeks-broken counter is
    auto-managed via ``modules.lmec_state.update_weeks_counter`` —
    increments by 1 on every NEW ISO-week tick where BTC > MA50w,
    resets when BTC drops back below MA. The legacy
    ``LMEC_MA50W_BROKEN_WEEKS`` env var still wins as a manual override
    when the counter has not been warmed yet, and a self-heal banner
    is emitted in the result dict when TraderMap has failed
    consecutively for ≥ ``LMEC_TRADERMAP_FAILURE_THRESHOLD`` cycles.
    """
    btc_price = _btc_price_from_market(market)
    btc_ath = _env_float("LMEC_BTC_ATH_USD", 98000.0) or 98000.0
    btc_neutral_band_pct = _env_float("LMEC_BTC_NEUTRAL_BAND_PCT", 2.0) or 2.0

    # TraderMap overrides for indicator-driven legs (2 / 3 / 4).
    # R-BOT-LMEC-AUTOFEED: read via tradermap_validator so we honour the
    # self-heal failure streak (skip overrides when scraper is unhealthy).
    autofeed_enabled = (os.getenv("LMEC_AUTOFEED_ENABLED", "true").strip().lower()
                        not in {"false", "0", "no", "off"})
    tm_over: dict[str, Any] = {}
    tradermap_unhealthy = False
    try:
        from modules.lmec_state import is_tradermap_unhealthy

        tradermap_unhealthy = bool(is_tradermap_unhealthy())
    except Exception:  # noqa: BLE001
        tradermap_unhealthy = False
    if autofeed_enabled and not tradermap_unhealthy:
        try:
            from modules.tradermap_validator import (
                get_indicator_overrides_safely,
            )

            tm_over = get_indicator_overrides_safely()
        except Exception:  # noqa: BLE001
            try:
                from modules.tradermap import tradermap_indicator_overrides

                tm_over = tradermap_indicator_overrides() or {}
            except Exception:  # noqa: BLE001
                tm_over = {}

    conditions: list[dict[str, Any]] = []

    # ── 1. BTC above ATH ─────────────────────────────────────────────
    if btc_price is None:
        conditions.append({
            "id": "btc_above_ath",
            "name": "BTC rompe ATH ~$97-98K",
            "status": "UNKNOWN",
            "detail": "BTC price feed unavailable",
        })
    else:
        gap_pct = (btc_price - btc_ath) / btc_ath * 100.0
        if btc_price >= btc_ath:
            status = "VALIDA"
            detail = f"BTC ${btc_price:,.0f} ≥ ATH ${btc_ath:,.0f} (+{gap_pct:.2f}%)"
        elif gap_pct >= -btc_neutral_band_pct:
            status = "NEUTRO"
            detail = (
                f"BTC ${btc_price:,.0f} a {gap_pct:+.2f}% del ATH "
                f"${btc_ath:,.0f} (banda neutra {btc_neutral_band_pct:.1f}%)"
            )
        else:
            status = "INVALIDA"
            detail = f"BTC ${btc_price:,.0f} < ATH ${btc_ath:,.0f} ({gap_pct:+.2f}%)"
        conditions.append({
            "id": "btc_above_ath",
            "name": "BTC rompe ATH ~$97-98K",
            "status": status,
            "detail": detail,
        })

    # ── 2. Weekly MACD positive ──────────────────────────────────────
    # Precedence: TraderMap override > manual /setlmec input > LMEC env var.
    _manual = _manual_lmec_inputs()
    if "macd_weekly_positive" in tm_over:
        macd_pos = bool(tm_over["macd_weekly_positive"])
    elif _manual.get("macd_weekly_positive") is not None:
        macd_pos = bool(_manual["macd_weekly_positive"])
    else:
        macd_pos = _env_bool_optional("LMEC_MACD_WEEKLY_POSITIVE")
    if macd_pos is None:
        conditions.append({
            "id": "macd_weekly_positive",
            "name": "MACD semanal terreno positivo",
            "status": "AWAITING_BCD",
            "detail": "⏳ esperando input de BCD (MACD semanal vía TradingView) — usá /setlmec macd <pos|neg>",
        })
    else:
        conditions.append({
            "id": "macd_weekly_positive",
            "name": "MACD semanal terreno positivo",
            "status": "VALIDA" if macd_pos else "INVALIDA",
            "detail": (
                "MACD weekly > 0 (bull crossover)"
                if macd_pos
                else "MACD weekly ≤ 0 (bear/neutro)"
            ),
        })

    # ── 3. Weekly RSI > 70 ───────────────────────────────────────────
    # TraderMap override > LMEC env var.
    if "rsi_weekly" in tm_over:
        try:
            rsi: float | None = float(tm_over["rsi_weekly"])
        except (TypeError, ValueError):
            rsi = None
    elif _manual.get("rsi_weekly") is not None:
        try:
            rsi = float(_manual["rsi_weekly"])
        except (TypeError, ValueError):
            rsi = None
    else:
        rsi = _env_float("LMEC_RSI_WEEKLY", None)
    if rsi is None:
        rsi = _env_float("LMEC_RSI_WEEKLY", None)
    rsi_neutral_band = _env_float("LMEC_RSI_NEUTRAL_BAND", 5.0) or 5.0
    if rsi is None:
        conditions.append({
            "id": "rsi_weekly_above_70",
            "name": "RSI semanal > 70",
            "status": "AWAITING_BCD",
            "detail": "⏳ esperando input de BCD (RSI semanal vía TradingView) — usá /setlmec rsi <valor>",
        })
    else:
        if rsi > 70.0:
            status = "VALIDA"
            detail = f"RSI weekly {rsi:.1f} > 70 (overheated)"
        elif rsi >= 70.0 - rsi_neutral_band:
            status = "NEUTRO"
            detail = (
                f"RSI weekly {rsi:.1f} cerca de 70 (banda {rsi_neutral_band:.0f})"
            )
        else:
            status = "INVALIDA"
            detail = f"RSI weekly {rsi:.1f} ≤ 70-{rsi_neutral_band:.0f}"
        conditions.append({
            "id": "rsi_weekly_above_70",
            "name": "RSI semanal > 70",
            "status": status,
            "detail": detail,
        })

    # ── 4. 50-week MA broken with sustained force ────────────────────
    # TraderMap override > LMEC env var (only the MA value — weeks-broken
    # is now AUTO-MANAGED via lmec_state.update_weeks_counter, with the
    # legacy LMEC_MA50W_BROKEN_WEEKS env var as manual override).
    # Precedence: TraderMap override > manual /setlmec input > LMEC env var.
    if "ma50w" in tm_over:
        try:
            ma50w: float | None = float(tm_over["ma50w"])
        except (TypeError, ValueError):
            ma50w = None
    elif _manual.get("ma50w_usd") is not None:
        try:
            ma50w = float(_manual["ma50w_usd"])
        except (TypeError, ValueError):
            ma50w = None
    else:
        ma50w = _env_float("LMEC_MA50W_USD", None)
    if ma50w is None:
        ma50w = _env_float("LMEC_MA50W_USD", None)

    # R-BOT-LMEC-AUTOFEED: auto-managed counter (lmec_state.json on Railway Volume).
    # Manual env-var override still wins so BCD can force a value if needed.
    counter_weeks: int | None = None
    if autofeed_enabled and btc_price is not None and ma50w is not None:
        try:
            from modules.lmec_state import update_weeks_counter

            new_state = update_weeks_counter(btc_price, ma50w)
            counter_weeks = int(new_state.get("ma50w_consecutive_weeks", 0))
        except Exception:  # noqa: BLE001
            log.exception("lmec_triggers: weeks counter update failed (non-fatal)")
            counter_weeks = None
    env_weeks = _env_int("LMEC_MA50W_BROKEN_WEEKS", None)
    weeks_broken = env_weeks if env_weeks is not None else counter_weeks
    sustained_min_weeks = (
        _env_int("LMEC_MA50W_BROKEN_THRESHOLD_WEEKS", None)
        or _env_int("LMEC_MA50W_SUSTAINED_WEEKS", 2)
        or 2
    )
    if ma50w is None or weeks_broken is None or btc_price is None:
        # P1.9: if the only thing missing is BCD's MA50w value, this is an
        # AWAITING_BCD state (clean) rather than a generic UNKNOWN error.
        if ma50w is None:
            detail = ("⏳ esperando input de BCD (MA50w semanal vía TradingView) "
                      "— usá /setlmec ma50w <valor>")
            status_ma = "AWAITING_BCD"
        else:
            detail = "Inputs incompletos — falta el feed de BTC o el contador de semanas"
            status_ma = "UNKNOWN"
        conditions.append({
            "id": "ma50w_broken_sustained",
            "name": "MA50w rota con fuerza sostenida 2-3 semanas",
            "status": status_ma,
            "detail": detail,
        })
    else:
        gap_ma_pct = (btc_price - ma50w) / ma50w * 100.0
        if weeks_broken >= sustained_min_weeks and btc_price > ma50w:
            status = "VALIDA"
            detail = (
                f"BTC ${btc_price:,.0f} > MA50w ${ma50w:,.0f} ({gap_ma_pct:+.2f}%) "
                f"sostenido {weeks_broken}w (≥{sustained_min_weeks}w)"
            )
        elif btc_price > ma50w:
            status = "NEUTRO"
            detail = (
                f"BTC > MA50w pero solo {weeks_broken}w (necesita ≥{sustained_min_weeks}w)"
            )
        else:
            status = "INVALIDA"
            detail = (
                f"BTC ${btc_price:,.0f} ≤ MA50w ${ma50w:,.0f} ({gap_ma_pct:+.2f}%)"
            )
        conditions.append({
            "id": "ma50w_broken_sustained",
            "name": "MA50w rota con fuerza sostenida 2-3 semanas",
            "status": status,
            "detail": detail,
        })

    triggered = sum(1 for c in conditions if c["status"] == "VALIDA")

    # R-BOT-LMEC-AUTOFEED: surface scraper health + indicator data source.
    if tm_over and not tradermap_unhealthy:
        data_source = "tradermap"
    elif tradermap_unhealthy:
        data_source = "env (tradermap unhealthy)"
    else:
        data_source = "env"

    result = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "btc_price_usd": btc_price,
        "conditions": conditions,
        "any_triggered": triggered >= 1,
        "all_triggered": triggered == len(conditions) and len(conditions) > 0,
        "triggered_count": triggered,
        "total": len(conditions),
        "data_source": data_source,
        "tradermap_unhealthy": tradermap_unhealthy,
        "autofeed_enabled": autofeed_enabled,
    }

    # Persist the leg snapshot so the weekly scheduler can detect flips.
    flips: list[str] = []
    try:
        from modules.lmec_state import record_legs_snapshot

        snap = record_legs_snapshot(conditions)
        flips = list(snap.get("flips") or [])
    except Exception:  # noqa: BLE001
        log.exception("lmec_triggers: legs snapshot persistence failed (non-fatal)")
    result["flips"] = flips

    log.info(
        "lmec_triggers: triggered=%d/%d any=%s all=%s flips=%s source=%s",
        result["triggered_count"],
        result["total"],
        result["any_triggered"],
        result["all_triggered"],
        result["flips"],
        result["data_source"],
    )
    return result


def format_lmec_block(result: dict[str, Any] | None = None) -> str:
    """Telegram-flavoured rendering of the LMEC bear invalidation triggers."""
    if result is None:
        result = evaluate_lmec_triggers()

    lines: list[str] = []
    triggered = int(result.get("triggered_count") or 0)
    total = int(result.get("total") or 0)
    if result.get("all_triggered"):
        header_icon = "🚨"
    elif triggered >= 1:
        header_icon = "⚠️"
    else:
        header_icon = "🟢"
    data_source = str(result.get("data_source") or "env")
    lines.append(
        f"🎯 LMEC BEAR INVALIDATION TRIGGERS — {triggered}/{total} (data: {data_source})"
    )
    if result.get("tradermap_unhealthy"):
        lines.append(
            "⚠️ TraderMap scraping failed, using env fallback"
        )
    lines.append(
        f"{header_icon} Cuando ≥1 condición pasa a ✅ VALIDA, la convicción bear debe BAJAR."
    )
    icon_map = {
        "VALIDA": "✅",
        "NEUTRO": "⚠️",
        "INVALIDA": "🔴",
        "UNKNOWN": "❓",
    }
    for c in result.get("conditions") or []:
        icon = icon_map.get(c.get("status", "UNKNOWN"), "❓")
        lines.append(f"  {icon} {c.get('name', '?')}: {c.get('detail', '?')}")
    if triggered >= 1:
        lines.append("")
        lines.append(
            "📉 ACCIÓN SUGERIDA: revisar tesis bear — convicción global -1 a -2 puntos "
            "según cuántas legs estén ✅. Si all_triggered → SALIR de SHORTs y "
            "rotar a LONG core (BTC/HYPE)."
        )
    elif total > 0:
        lines.append("")
        lines.append(
            f"→ All {total} INVALIDA = bear thesis intact, convicción 9/10"
        )
    return "\n".join(lines)


def format_lmec_status(result: dict[str, Any] | None = None) -> str:
    """Verbose Telegram block for the /lmec_status command.

    Returns source actual (tradermap/env), last successful pull,
    valores de cada leg, and persisted state (weeks counter +
    last flip + scraper health).
    """
    if result is None:
        result = evaluate_lmec_triggers()
    try:
        from modules.lmec_state import status_summary

        st = status_summary()
    except Exception:  # noqa: BLE001
        st = {}

    lines: list[str] = []
    lines.append("🔬 /lmec_status — bear-invalidation telemetry")
    lines.append("")
    lines.append(format_lmec_block(result))
    lines.append("")
    lines.append("── Persisted state (lmec_state.json) ──")
    lines.append(f"  weeks counter (Leg 4): {st.get('ma50w_consecutive_weeks', 0)}")
    if st.get("ma50w_first_break_iso"):
        lines.append(
            f"  streak started: {st.get('ma50w_first_break_iso')} "
            f"(ISO week {st.get('last_iso_week') or '?'})"
        )
    lines.append(f"  BTC < MA50w on last check: {st.get('last_btc_below_ma', False)}")
    lines.append(f"  last_check_iso: {st.get('last_check_iso') or '—'}")
    lines.append("")
    lines.append("── TraderMap health ──")
    streak = int(st.get("tradermap_failure_streak", 0) or 0)
    threshold = int((st.get("thresholds") or {}).get("tradermap_failure", 3))
    health_icon = "🔴" if streak >= threshold else ("🟡" if streak > 0 else "🟢")
    lines.append(
        f"  {health_icon} consecutive failures: {streak} / {threshold} (threshold)"
    )
    lines.append(f"  active source: {result.get('data_source', '?')}")
    lines.append(f"  autofeed_enabled: {result.get('autofeed_enabled', True)}")
    lines.append("")
    lines.append("── Last flip ──")
    if st.get("last_flip_iso"):
        lines.append(f"  at: {st.get('last_flip_iso')}")
        lines.append(f"  legs: {', '.join(st.get('last_flip_legs') or []) or '—'}")
    else:
        lines.append("  (no flips recorded yet)")
    return "\n".join(lines)


def detect_and_alert_flips(
    market: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a fresh evaluation and return ``{result, flips, alert_text}``.

    Pure function — does NOT send Telegram messages itself. The bot
    scheduler is responsible for delivering the alert. ``alert_text`` is
    only populated when at least one leg flipped from non-VALIDA to
    VALIDA on this evaluation.
    """
    res = evaluate_lmec_triggers(market)
    flips = list(res.get("flips") or [])
    alert_text = ""
    if flips:
        flipped_names: list[str] = []
        for c in res.get("conditions") or []:
            if isinstance(c, dict) and c.get("id") in flips:
                flipped_names.append(
                    f"  • {c.get('name', c.get('id'))}: {c.get('detail', '')}"
                )
        alert_text = "\n".join(
            [
                "🚨 LMEC TRIGGER FLIP — leg(s) became ✅ VALIDA",
                "",
                *flipped_names,
                "",
                f"State: {res.get('triggered_count', 0)}/{res.get('total', 0)} "
                f"VALIDA — convicción bear debe BAJAR.",
            ]
        )
    return {"result": res, "flips": flips, "alert_text": alert_text}
