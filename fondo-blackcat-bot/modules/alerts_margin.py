"""R-BOT-DEFINITIVE WI-3 — margin-stress alert redesign (anti-spam + honest copy).

What it kills (live 2026-06-10): 6 identical "MARGIN STRESS" alerts in one
night, every 30 minutes, with the WRONG copy ("Buffer to liquidation") for a
metric (perp margin used / perp equity) that — above 100% — only means the
account cannot OPEN new positions. It is NOT liquidation proximity.

Two channels, both SQLite edge-triggered with band-transition + cooldown:

  1. MARGIN-USED channel ("Perp margin used vs perp equity"):
     bands <90 / 90-100 / 100-110 / >110 (%). Alert ONLY on a band TRANSITION
     or on worsening by > 5 percentage points since the LAST SENT alert.
     Minimum cooldown 6h per band. No liquidation language, ever.

  2. REAL-RISK channel (the actual liquidation axis):
     * PM aave-HF crossing DOWN through 1.30 (info), 1.20 (observación),
       1.10 (acción) — fed ONLY by compute_pm_state.
     * Any single position's liq distance crossing BELOW 12% and below 8% —
       fed by live position data.
     Same transition + cooldown logic.

NEVER raises from public functions. State table: ``margin_alert_state``.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Any

log = logging.getLogger(__name__)

try:
    from config import DATA_DIR
except Exception:  # noqa: BLE001
    DATA_DIR = os.getenv("DATA_DIR", "/tmp")

DB_PATH = os.path.join(DATA_DIR, "margin_alerts.db")

COOLDOWN_SEC = float(os.getenv("MARGIN_ALERT_COOLDOWN_HOURS", "6") or 6) * 3600.0
WORSEN_PP = float(os.getenv("MARGIN_ALERT_WORSEN_PP", "5") or 5)
# R-MARGIN-STRESS-HOTFIX: iso-only informational line cooldown (24h, persisted).
ISO_INFO_COOLDOWN_SEC = float(
    os.getenv("MARGIN_ISO_INFO_COOLDOWN_HOURS", "24") or 24
) * 3600.0

# Margin-used bands (ratio in %): index = severity (higher = worse).
_BANDS = ((0.0, 90.0), (90.0, 100.0), (100.0, 110.0), (110.0, float("inf")))
_BAND_LABELS = ("<90%", "90-100%", "100-110%", ">110%")

# aave-HF thresholds (descending severity when crossing DOWN).
HF_THRESHOLDS = ((1.30, "info"), (1.20, "observación"), (1.10, "acción"))
# Per-position liq distance thresholds (%) when crossing BELOW.
LIQ_DIST_THRESHOLDS = (12.0, 8.0)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS margin_alert_state (
            key TEXT PRIMARY KEY,
            band INTEGER,
            value REAL,
            sent_at REAL
        )
        """
    )
    return conn


def _get_state(key: str) -> tuple[int | None, float | None, float | None]:
    try:
        conn = _conn()
        try:
            cur = conn.execute(
                "SELECT band, value, sent_at FROM margin_alert_state WHERE key=?",
                (key,),
            )
            row = cur.fetchone()
            return (row[0], row[1], row[2]) if row else (None, None, None)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return (None, None, None)


def _set_state(key: str, band: int, value: float, sent_at: float) -> None:
    try:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO margin_alert_state (key, band, value, sent_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET band=excluded.band, "
                "value=excluded.value, sent_at=excluded.sent_at",
                (key, int(band), float(value), float(sent_at)),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        log.exception("margin_alert_state write failed for %s", key)


def margin_used_band(ratio_pct: float) -> int:
    """Band index for the used/equity ratio (%). 0=<90 … 3=>110."""
    try:
        r = float(ratio_pct)
    except (TypeError, ValueError):
        return 0
    for i, (lo, hi) in enumerate(_BANDS):
        if lo <= r < hi:
            return i
    return len(_BANDS) - 1


def count_cross_positions(positions: list[dict[str, Any]] | None) -> int:
    """Number of open perp legs in CROSS margin mode.

    ``leverage_type`` is read LIVE from HL (R-PM-MARGIN-MODE-FIX); a leg is
    isolated ONLY when HL says so explicitly. Missing/unknown mode counts as
    cross (conservative: never suppresses the alert on ambiguous data).
    """
    n = 0
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        try:
            sz = abs(float(p.get("size") or p.get("szi") or 0.0))
        except (TypeError, ValueError):
            sz = 0.0
        if sz <= 0:
            continue
        if str(p.get("leverage_type") or "").lower() != "isolated":
            n += 1
    return n


def perp_cross_utilization(
    wallet_data: dict[str, Any] | None,
) -> tuple[float | None, int]:
    """(util_pct, n_cross) for the /reporte PM panel info line.

    R-NOISE-CUT (2026-06-16): the perp-cross-margin-used vs cross-equity ratio
    is NOT a risk metric — under unified Portfolio Margin the HYPE collateral
    governs liquidation via the aave-HF. Its only real information is that at/
    over 100% the perp cross sub-account cannot OPEN new positions. That single
    fact now lives in the PM panel as an informational line, never as a push.

    Returns ``(None, n_cross)`` when the ratio is not applicable: zero cross
    perp legs (structurally N/A), missing/stale cross fields (NEVER fall back to
    the blended marginSummary), or non-positive cross equity. NEVER raises.
    """
    d = wallet_data or {}
    positions = d.get("positions") or []
    n_cross = count_cross_positions(positions)
    if n_cross == 0:
        return None, 0
    if "cross_margin_used" not in d:
        return None, n_cross  # stale/absent cross data — never blend
    try:
        eq = float(d.get("cross_account_value") or 0.0)
        used = float(d.get("cross_margin_used") or 0.0)
    except (TypeError, ValueError):
        return None, n_cross
    if eq <= 0.0:
        return None, n_cross
    return used / eq * 100.0, n_cross


def format_perp_cross_util_line(util_pct: float) -> str:
    """One INFORMATIONAL panel line for perp cross utilization (never a push).

    Honest copy: at/over 100% only BLOCKS opening new perp legs; it is NOT
    liquidation proximity (that is the aave-HF / liq price shown above)."""
    state = (
        "al/sobre 100% bloquea ABRIR nuevas patas perp"
        if util_pct >= 100.0
        else "head-room para abrir nuevas patas perp"
    )
    return (
        f"├─ Perp cross utilization: {util_pct:.1f}% — {state} "
        "(informativo; el riesgo de liquidación lo mide el aave-HF/liq price, "
        "no esta métrica)."
    )


def format_margin_stress_alert(ident: str, ratio_pct: float, used: float, eq: float) -> str:
    """R-MARGIN-STRESS-HOTFIX mandated copy — CROSS metric, no liquidation
    language. Only used when cross perp positions actually exist."""
    band = margin_used_band(ratio_pct)
    label = _BAND_LABELS[band]
    return (
        f"🚨 MARGIN STRESS — {ident}\n"
        f"Perp cross margin used vs cross equity = {ratio_pct:.1f}%. "
        "Above 100% blocks NEW positions. Liquidation risk is tracked per "
        "position and in the PM panel, not by this metric.\n"
        f"cross_margin_used=${used:,.0f} · cross_equity=${eq:,.0f} · "
        f"banda {label}."
    )


def format_iso_only_info(ident: str) -> str:
    """One-time informational line on transition into iso-only state."""
    return (
        f"ℹ️ PERP MARGIN — {ident}\n"
        "Perp USDC fully allocated to isolated margins. New positions blocked "
        "until margin is freed. NOT a liquidation risk.\n"
        "(Cuenta sin posiciones cross: la métrica de stress de margen no "
        "aplica — el riesgo de liquidación de cada pata aislada se sigue por "
        "su liq price y en el panel PM.)"
    )


def evaluate_iso_only_transition(
    key: str,
    iso_only: bool,
    *,
    now: float | None = None,
) -> bool:
    """Fire the informational line ONLY on state transition INTO iso-only.

    Persisted in SQLite (``isoonly:{key}``) so restarts do not re-fire.
    Cooldown ``ISO_INFO_COOLDOWN_SEC`` (24h default). NEVER raises.
    """
    try:
        now = now or time.time()
        skey = f"isoonly:{key}"
        band = 1 if iso_only else 0
        last_band, _lv, last_sent = _get_state(skey)
        if not iso_only:
            if last_band != 0:
                # Transition out of iso-only — re-arm silently, keep sent_at.
                _set_state(skey, 0, 0.0, float(last_sent or 0.0))
            return False
        if last_band == 1:
            return False  # already known iso-only → silence
        cooled = (now - float(last_sent or 0.0)) >= ISO_INFO_COOLDOWN_SEC
        _set_state(skey, band, 1.0, now if cooled else float(last_sent or 0.0))
        return cooled
    except Exception:  # noqa: BLE001
        log.exception("evaluate_iso_only_transition failed for %s", key)
        return False


def format_margin_used_alert(ident: str, ratio_pct: float, used: float, eq: float) -> str:
    """LEGACY formatter (blended metric) — kept for compatibility/tests only.
    Production routes via :func:`format_margin_stress_alert` (cross-only).
    Honest copy: utilization metric, NOT liquidation proximity."""
    band = margin_used_band(ratio_pct)
    label = _BAND_LABELS[band]
    over_note = (
        "Por encima de 100% SOLO bloquea ABRIR posiciones nuevas — "
        "NO es proximidad de liquidación."
        if ratio_pct >= 100.0
        else "Es una métrica de utilización, NO de proximidad de liquidación."
    )
    return (
        f"📊 PERP MARGIN USED vs PERP EQUITY — {ident}\n"
        f"Ratio: {ratio_pct:.1f}% (banda {label}) · "
        f"margin_used=${used:,.0f} · equity=${eq:,.0f}.\n"
        f"{over_note}\n"
        "El riesgo real de liquidación se mide con el aave-HF / liq price del "
        "panel PM (canal separado)."
    )


def evaluate_margin_used(
    key: str,
    ratio_pct: float,
    *,
    now: float | None = None,
) -> tuple[bool, int]:
    """Decide whether the margin-used channel should fire for ``key``.

    Fires ONLY on (a) band transition vs last-sent band, or (b) worsening by
    more than WORSEN_PP percentage points since the last SENT alert — and in
    both cases never more than once per COOLDOWN_SEC per band. Returns
    ``(should_send, band)`` and persists state when it fires. NEVER raises.
    """
    try:
        now = now or time.time()
        band = margin_used_band(ratio_pct)
        last_band, last_value, last_sent = _get_state(f"used:{key}")
        # Per-band cooldown clock (WI-3: "minimum cooldown 6 hours PER BAND").
        _bb, _bv, band_last_sent = _get_state(f"used:{key}:b{band}")
        band_cooled = (now - float(band_last_sent or 0.0)) >= COOLDOWN_SEC

        def _fire() -> tuple[bool, int]:
            _set_state(f"used:{key}", band, ratio_pct, now)
            _set_state(f"used:{key}:b{band}", band, ratio_pct, now)
            return True, band

        if last_band is None:
            # First observation: only alert when already at/above the 90% band.
            if band >= 1 and band_cooled:
                return _fire()
            _set_state(f"used:{key}", band, ratio_pct, 0.0)
            return False, band
        worsened = (
            last_value is not None and (ratio_pct - float(last_value)) > WORSEN_PP
        )
        # Improving transitions (down-band) update state silently.
        if band < int(last_band):
            _set_state(f"used:{key}", band, ratio_pct, float(last_sent or 0.0))
            return False, band
        transition_up = band > int(last_band)
        if band >= 1 and (transition_up or worsened) and band_cooled:
            return _fire()
        return False, band
    except Exception:  # noqa: BLE001
        log.exception("evaluate_margin_used failed for %s", key)
        return False, 0


# ─── Real-risk channel ──────────────────────────────────────────────────────


def _hf_band(aave_hf: float) -> int:
    """0 = safe (≥1.30), 1 = <1.30, 2 = <1.20, 3 = <1.10."""
    try:
        h = float(aave_hf)
    except (TypeError, ValueError):
        return 0
    if h <= 0:
        return 0
    if h < 1.10:
        return 3
    if h < 1.20:
        return 2
    if h < 1.30:
        return 1
    return 0


def evaluate_pm_hf(aave_hf: float, *, now: float | None = None) -> tuple[bool, str]:
    """Real-risk alert on aave-HF crossing DOWN 1.30/1.20/1.10. NEVER raises."""
    try:
        now = now or time.time()
        band = _hf_band(aave_hf)
        last_band, _lv, last_sent = _get_state("pm_hf")
        if last_band is None:
            _set_state("pm_hf", band, aave_hf, now if band >= 1 else 0.0)
            if band >= 1:
                return True, _hf_message(aave_hf, band)
            return False, ""
        if band > int(last_band):
            # Worsened across a threshold — fire (cooldown per band).
            cooled = (now - float(last_sent or 0.0)) >= COOLDOWN_SEC
            if cooled or band > int(last_band):
                _set_state("pm_hf", band, aave_hf, now)
                return True, _hf_message(aave_hf, band)
        elif band < int(last_band):
            # Recovered — re-arm silently.
            _set_state("pm_hf", band, aave_hf, float(last_sent or 0.0))
        return False, ""
    except Exception:  # noqa: BLE001
        log.exception("evaluate_pm_hf failed")
        return False, ""


def _hf_message(aave_hf: float, band: int) -> str:
    tier = {1: ("ℹ️", "INFO — cruce de 1.30"),
            2: ("🟠", "OBSERVACIÓN — cruce de 1.20"),
            3: ("🔴", "ACCIÓN — cruce de 1.10")}.get(band, ("ℹ️", "INFO"))
    return (
        f"{tier[0]} RIESGO PM REAL — aave-HF {aave_hf:.2f} ({tier[1]})\n"
        "Métrica de distancia REAL a liquidación (compute_pm_state, misma "
        "fuente que el panel). Playbook del fondo: si <1.10, cerrar patas "
        "GANADORAS del basket a USDC y repagar — NUNCA vender HYPE."
    )


def evaluate_position_liq_distance(
    coin: str,
    dist_pct: float,
    *,
    now: float | None = None,
) -> tuple[bool, str]:
    """Real-risk alert: a position's liq distance crossing below 12% / 8%."""
    try:
        now = now or time.time()
        try:
            d = float(dist_pct)
        except (TypeError, ValueError):
            return False, ""
        band = 0
        if d < LIQ_DIST_THRESHOLDS[1]:
            band = 2
        elif d < LIQ_DIST_THRESHOLDS[0]:
            band = 1
        key = f"liqdist:{(coin or '?').upper()}"
        last_band, _lv, last_sent = _get_state(key)
        if last_band is None:
            _set_state(key, band, d, now if band >= 1 else 0.0)
            if band >= 1:
                return True, _liq_dist_message(coin, d, band)
            return False, ""
        if band > int(last_band):
            _set_state(key, band, d, now)
            return True, _liq_dist_message(coin, d, band)
        if band < int(last_band):
            cooled = (now - float(last_sent or 0.0)) >= COOLDOWN_SEC
            if cooled:
                _set_state(key, band, d, float(last_sent or 0.0))
        return False, ""
    except Exception:  # noqa: BLE001
        log.exception("evaluate_position_liq_distance failed for %s", coin)
        return False, ""


def _liq_dist_message(coin: str, dist_pct: float, band: int) -> str:
    sev = "🔴 <8%" if band >= 2 else "🟠 <12%"
    return (
        f"{sev} DISTANCIA A LIQ — {str(coin).upper()}: {dist_pct:.1f}% del precio "
        "actual a su liq price (dato vivo de la posición). Revisar margen/SL "
        "de esa pata. MANUAL REVIEW — el bot nunca ejecuta."
    )


# ─── Orchestrator (called from the alerts cycle) ────────────────────────────


async def run_margin_alerts(bot, wallets: list[dict[str, Any]] | None) -> int:
    """REAL-RISK perp/PM pager. Returns alerts sent.

    R-NOISE-CUT (2026-06-16): the MARGIN STRESS channel (perp cross margin used
    vs cross equity) is REMOVED from paging entirely. Under wallet 0xc7ae's
    unified Portfolio Margin almost all capital is HYPE spot collateral
    cross-margining everything, so the perp cross sub-account holds thin equity
    and the ratio sits at ~100% as its NORMAL resting state — not a stress
    event. It carried no actionable risk information (it is NOT liquidation
    proximity), so it fired every few hours with nothing to act on. Its only
    real datum (perp cross at/over 100% blocks opening NEW positions) now lives
    in the /reporte PM panel as a single INFORMATIONAL line — never a push.

    What remains here is the REAL-RISK channel and ONLY that:
      * PM aave-HF crossing DOWN 1.30/1.20/1.10 (fed by compute_pm_state — the
        same source as the panel), and
      * any single open position's live liq distance crossing below 12% / 8%.
    Real perp/PM liquidation risk is governed by the HYPE collateral via the
    aave-HF; that is what this channel pages. NEVER raises.
    """
    sent = 0
    try:
        from config import TELEGRAM_CHAT_ID
        from utils.telegram import send_bot_message
    except Exception:  # noqa: BLE001
        return 0
    if not TELEGRAM_CHAT_ID:
        return 0

    # ── Channel: real risk (aave-HF + per-position liq distance) ──
    try:
        from modules.pm_context import select_primary_pm_state
        pm = select_primary_pm_state(wallets, None)
        if pm is not None and pm.debt_usd > 1.0 and pm.aave_hf > 0:
            should, msg = evaluate_pm_hf(pm.aave_hf)
            if should and msg:
                try:
                    await send_bot_message(bot, TELEGRAM_CHAT_ID, msg)
                    sent += 1
                except Exception:  # noqa: BLE001
                    log.exception("pm-hf alert send failed")
    except Exception:  # noqa: BLE001
        log.exception("pm-hf channel failed (non-fatal)")

    try:
        for w in wallets or []:
            if not isinstance(w, dict) or w.get("status") != "ok":
                continue
            d = w.get("data") or {}
            for p in d.get("positions") or []:
                coin = p.get("coin") or "?"
                try:
                    liq = float(p.get("liq_px") or 0.0)
                except (TypeError, ValueError):
                    liq = 0.0
                if liq <= 0:
                    continue
                # Mark price: derive from entry+upnl unavailable here — use
                # notional/size as the live mark (positionValue / |szi|).
                try:
                    sz = abs(float(p.get("size") or p.get("szi") or 0.0))
                    ntl = abs(float(p.get("notional_usd") or p.get("positionValue") or 0.0))
                    mark = (ntl / sz) if sz > 0 else 0.0
                except (TypeError, ValueError, ZeroDivisionError):
                    mark = 0.0
                if mark <= 0:
                    continue
                dist_pct = abs(mark - liq) / mark * 100.0
                should, msg = evaluate_position_liq_distance(coin, dist_pct)
                if should and msg:
                    try:
                        await send_bot_message(bot, TELEGRAM_CHAT_ID, msg)
                        sent += 1
                    except Exception:  # noqa: BLE001
                        log.exception("liq-distance alert send failed")
    except Exception:  # noqa: BLE001
        log.exception("liq-distance channel failed (non-fatal)")

    return sent
