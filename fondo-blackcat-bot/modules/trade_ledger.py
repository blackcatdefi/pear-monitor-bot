"""R-TRADE-LEDGER (2026-08-20) — permanent closed-trade ledger.

Owner mandate: every closed perp position must be reported automatically
with full economics — entries, exits, fees, funding paid/received and final
NET PnL. The old "TRADES CERRADOS (24h)" section was a rolling fill window
that missed closures between reports and never showed per-position
economics. This module is the single source of truth for realized trades.

Design
------
* Storage: SAME store as the rest of the bot's intel state —
  ``intel_memory.db`` on the Railway volume (inherits the nightly SQLite
  backup from modules.sqlite_backup). Tables:
    - ledger_fills      raw HL perp fills per wallet (dedup by (wallet,tid))
    - ledger_funding    raw HL funding deltas per wallet (userFunding API)
    - ledger_positions  ONE ROW PER CLOSED POSITION LIFECYCLE per wallet
    - ledger_meta       cursors (per-wallet fill/funding cursor + the
                        persistent /reporte cursor) — key/value
    - ledger_open_snap  last live snapshot of each OPEN tracked position
                        (leverage + cumFunding) consumed at close time
* Position reconciliation: fills aggregate into lifecycles — a position
  OPENS when net size leaves zero and CLOSES when net size returns to zero.
  Direction flips split the flipping fill into a close of the old lifecycle
  plus the open of a new one (fees pro-rated by size fraction; closedPnl
  belongs 100% to the closing part — HL only realizes PnL on the closing
  size).
* THE FORMULA (documented here, in code and in every render footer):
      NET = gross realized PnL − fees + funding
  where funding > 0 means the position RECEIVED funding and funding < 0
  means it PAID. Funding comes from the HL ``userFunding`` history endpoint
  attributed to the position's coin over [open_ts, close_ts]. The live
  ``cumFunding.sinceOpen`` snapshot captured at close time is persisted too
  and cross-checked; on mismatch the API history wins and the delta is
  logged (LEDGER funding delta).
* ROE% = NET / margin. Margin is resolved in THIS precedence
  (R-LEDGER-FIX D3):
    1. ``derived``  — margin ACTUALLY posted at open, read from the live
       open-position snapshot (``margin_used``). Leverage is then
       notional_at_open / margin_open, i.e. the real leverage of that leg.
    2. ``live``     — the leverage field reported by HL on the open position
       snapshot, when margin_used is unavailable.
    3. ``assumed``  — env LEDGER_ASSUMED_LEVERAGE (default 3, the fund's
       STANDARD basket leverage) for backfilled history, where HL fills
       carry neither leverage nor margin. Only this case renders the '~'
       marker; derived/live values are exact and carry no marker.
* Cycle grouping (public track record): positions on the SAME wallet whose
  lifecycles OPEN within a 15-minute window cluster into one basket cycle,
  tagged ``ciclo YYYY-MM-DD`` (suffix #2, #3… for repeated same-day
  cycles). Closed cycles render with subtotals; when several wallets ran a
  same-tag cycle, a combined total is rendered.
* API horizon: HL serves only the most recent 10,000 fills per wallet
  (2,000 per page). The backfill goes as far as that horizon allows and the
  ledger states the horizon honestly instead of fabricating older cycles.

R-LEDGER-FIX (2026-08-26) — three production defects fixed
----------------------------------------------------------
D1 FUNDING WAS ZERO IN PRODUCTION. Root cause CONFIRMED against the live
   API: HL rate-limits the info endpoint (weight 20 per userFillsByTime /
   userFunding call, 1200/min per IP). A full ledger sync issues ~16 heavy
   paged calls PER WALLET back to back; the fills pagination burned the
   budget and the very first ``userFunding`` page then 429'd through all
   four hl_client retries and raised. ``_fetch_funding_paged`` caught EVERY
   exception, logged a warning and ``break``ed → empty list → nothing
   stored → ``_funding_for`` summed an empty table → funding 0.00 on every
   leg and NET overstated by the whole carry. Same silent-swallow class as
   the Gmail rounds. Fixed by (a) pacing the paged reads so the budget is
   never exhausted, (b) raising ``LedgerSyncError`` instead of swallowing,
   (c) persisting per-wallet sync health, (d) ONE deduped Telegram alert
   when a wallet with closures in the window has no funding data, and (e) a
   visible banner on the rendered section instead of silent zeros.
D2 THE CHALLENGE WALLET VANISHED. ``sync_all`` wrapped each wallet in a
   bare try/except that logged and continued, so a 429 on wallet #2 (the
   one synced LAST, i.e. the one whose budget was already spent) removed it
   from the report with no trace. Fixed: per-wallet health row, ONE deduped
   alert per failing wallet, a scope line logged every run, an explicit
   alert when the ledger scope collapses to a single wallet, and a render
   banner. The dual-wallet number is the public track record.
D3 ROE WAS INFLATED. Backfilled legs assumed 5x while the fund's baskets
   run at 3x, overstating every ROE by ~67%. Fixed: default assumed
   leverage is now 3, and leverage is DERIVED per position from
   notional_at_open / margin_actually_posted whenever the open snapshot
   captured the margin. Only still-assumed values keep the '~' marker.

GUARDRAIL: read-only on-chain — this module NEVER trades. COMPUTE_PM_STATE
stays 0xc7ae-only; the ledger reads fills for BOTH wallets read-only.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time as _time
from datetime import datetime, timezone
from typing import Any, Callable

from config import DATA_DIR, FUND_WALLETS, PM_PRIMARY_WALLET

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "intel_memory.db")

# HL pagination caps (docs: userFillsByTime ≤2000/page, 10k most recent;
# userFunding ≤500/page).
FILLS_PAGE_CAP = 2000
FUNDING_PAGE_CAP = 500
MAX_PAGES = 12  # 12×2000 > 10k horizon — hard stop, never loops forever

# Basket-cycle clustering window (Part 3): legs opened within 15 minutes on
# the same wallet belong to one cycle.
CYCLE_WINDOW_MS = int(float(os.getenv("LEDGER_CYCLE_WINDOW_MIN", "15") or 15) * 60_000)

# R-LEDGER-FIX D1: pacing between paged HL reads. HL bills every
# userFillsByTime / userFunding call at weight 20 against a 1200/min per-IP
# budget (= 60 heavy calls/min). One full ledger sync issues ~16 of them per
# wallet, and with two wallets back-to-back the burst tripped 429s that the
# old code swallowed. A small pause between pages keeps the whole backfill
# inside the budget; it costs seconds on a background sync and is the
# difference between real funding and silent zeros.
PAGE_PAUSE_SEC = float(os.getenv("LEDGER_PAGE_PAUSE_SEC", "1.1") or 1.1)

# Per-page retry budget on top of hl_client's own retries. A page that still
# fails after this raises LedgerSyncError — it is NEVER swallowed.
PAGE_MAX_ATTEMPTS = int(os.getenv("LEDGER_PAGE_MAX_ATTEMPTS", "3") or 3)

# Fund standard leverage assumed for BACKFILLED closes (fills carry neither
# leverage nor posted margin). R-LEDGER-FIX D3: the fund's baskets run at
# 3x — the old default of 5 overstated every backfilled ROE by ~67%.
ASSUMED_LEVERAGE = float(os.getenv("LEDGER_ASSUMED_LEVERAGE", "3") or 3)

# Sanity band for leverage DERIVED from notional/margin. Outside it the
# division is not trustworthy (dust margin, cross-margin bleed) and the
# ledger falls back instead of printing a fantasy ROE.
DERIVED_LEV_MIN = float(os.getenv("LEDGER_DERIVED_LEV_MIN", "1") or 1)
DERIVED_LEV_MAX = float(os.getenv("LEDGER_DERIVED_LEV_MAX", "50") or 50)

# Schema/semantics version. Bumping it forces a one-shot recompute of every
# stored row on the next sync (used to re-price ROE at the corrected
# assumed leverage without waiting for new fills).
LEDGER_SEMANTICS_VERSION = "2"


class LedgerSyncError(RuntimeError):
    """A ledger HL read failed. Carries whatever was fetched before the
    failure so the caller can still persist partial progress, but the
    failure itself is NEVER silent."""

    def __init__(self, message: str, *, wallet: str = "", kind: str = "",
                 partial: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.wallet = wallet
        self.kind = kind
        self.partial = partial or []

# First-run guard: the report cursor starts at 0, and (0, now] would dump the
# ENTIRE backfilled horizon (months of closures, dozens of Telegram messages)
# into the first /reporte. Instead the cursor is seeded at sync time to the
# most recent closure minus this lookback, so the first section only covers
# the recent window; older history stays reachable via /cierres.
FIRST_RUN_LOOKBACK_MS = int(
    float(os.getenv("LEDGER_FIRST_RUN_LOOKBACK_HOURS", "72") or 72) * 3_600_000
)

# Hard cap on closures RENDERED in one section. Subtotals/totals are always
# computed over the FULL window — only the per-position lines are capped.
SECTION_MAX_ROWS = int(os.getenv("LEDGER_SECTION_MAX_ROWS", "40") or 40)

# Net-size epsilon: HL sizes are decimal strings; treat |net| below this as
# flat (guards float dust from partial-fill arithmetic).
_EPS = 1e-9

_sync_lock: asyncio.Lock | None = None


def _lock() -> asyncio.Lock:
    global _sync_lock
    if _sync_lock is None:
        _sync_lock = asyncio.Lock()
    return _sync_lock


# ─── store ──────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10.0)
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS ledger_fills (
            wallet TEXT NOT NULL,
            tid INTEGER NOT NULL,
            oid INTEGER,
            coin TEXT NOT NULL,
            side TEXT,
            px REAL NOT NULL,
            sz REAL NOT NULL,
            time INTEGER NOT NULL,
            start_position REAL,
            dir TEXT DEFAULT '',
            closed_pnl REAL DEFAULT 0,
            fee REAL DEFAULT 0,
            fee_token TEXT DEFAULT '',
            PRIMARY KEY (wallet, tid)
        )
    """)
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_ledger_fills_wct "
        "ON ledger_fills(wallet, coin, time)"
    )
    con.execute("""
        CREATE TABLE IF NOT EXISTS ledger_funding (
            wallet TEXT NOT NULL,
            time INTEGER NOT NULL,
            coin TEXT NOT NULL,
            usdc REAL NOT NULL,
            szi REAL DEFAULT 0,
            rate REAL DEFAULT 0,
            PRIMARY KEY (wallet, time, coin)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS ledger_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            coin TEXT NOT NULL,
            side TEXT NOT NULL,
            open_ts INTEGER NOT NULL,
            close_ts INTEGER NOT NULL,
            avg_entry REAL NOT NULL,
            avg_exit REAL NOT NULL,
            max_size REAL NOT NULL,
            notional_open REAL NOT NULL,
            margin_open REAL,
            leverage REAL,
            leverage_source TEXT DEFAULT 'assumed',
            open_fills INTEGER DEFAULT 0,
            close_fills INTEGER DEFAULT 0,
            fees_total REAL DEFAULT 0,
            funding_net REAL DEFAULT 0,
            funding_live_snapshot REAL,
            funding_delta REAL,
            gross_pnl REAL DEFAULT 0,
            net_pnl REAL DEFAULT 0,
            roe_pct REAL,
            cycle_tag TEXT,
            UNIQUE (wallet, coin, open_ts)
        )
    """)
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_ledger_pos_close "
        "ON ledger_positions(close_ts)"
    )
    con.execute("""
        CREATE TABLE IF NOT EXISTS ledger_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS ledger_open_snap (
            wallet TEXT NOT NULL,
            coin TEXT NOT NULL,
            szi REAL NOT NULL,
            leverage REAL,
            cum_funding REAL,
            entry_px REAL,
            margin_used REAL,
            snap_ts INTEGER NOT NULL,
            PRIMARY KEY (wallet, coin)
        )
    """)
    # R-LEDGER-FIX D1/D2: per-wallet sync health. A wallet whose HL read
    # failed must NEVER disappear silently — its state is persisted here and
    # surfaced both as a Telegram alert and as a banner on every render.
    con.execute("""
        CREATE TABLE IF NOT EXISTS ledger_sync_health (
            wallet TEXT PRIMARY KEY,
            ok INTEGER NOT NULL DEFAULT 1,
            fills_ok INTEGER NOT NULL DEFAULT 1,
            funding_ok INTEGER NOT NULL DEFAULT 1,
            detail TEXT DEFAULT '',
            updated_at TEXT NOT NULL
        )
    """)
    # Migration for DBs created before R-LEDGER-FIX (Railway volume keeps the
    # old file): add margin_open if the table predates it.
    cols = {r["name"] for r in con.execute("PRAGMA table_info(ledger_positions)")}
    if "margin_open" not in cols:
        con.execute("ALTER TABLE ledger_positions ADD COLUMN margin_open REAL")
    con.commit()
    return con


def _meta_get(key: str) -> str | None:
    con = _conn()
    try:
        row = con.execute("SELECT value FROM ledger_meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None
    finally:
        con.close()


def _meta_set(key: str, value: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    con = _conn()
    try:
        con.execute(
            "INSERT INTO ledger_meta (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, value, now),
        )
        con.commit()
    finally:
        con.close()


# ─── wallets in scope ───────────────────────────────────────────────────────

def ledger_wallets() -> dict[str, str]:
    """Primary PM wallet + every FUND_WALLET_N (+ optional LEDGER_WALLETS
    CSV extra). Read-only scope — never mutates COMPUTE_PM_STATE inputs."""
    out: dict[str, str] = {}
    # H3: the configured FUND_WALLET_N_LABEL is the public track-record
    # wording — it wins. "core" is only the fallback label when the primary
    # PM wallet is not among the FUND_WALLET_N env vars.
    for addr, label in (FUND_WALLETS or {}).items():
        out[addr.lower()] = label
    if PM_PRIMARY_WALLET and PM_PRIMARY_WALLET.startswith("0x"):
        out.setdefault(PM_PRIMARY_WALLET, "core")
    raw = os.getenv("LEDGER_WALLETS", "").strip()
    for part in raw.split(","):
        w = part.strip().lower()
        if w.startswith("0x") and len(w) == 42:
            out.setdefault(w, w[:6])
    return out


def wallet_label(wallet: str) -> str:
    return ledger_wallets().get((wallet or "").lower(), (wallet or "?")[:6])


# ─── R-LEDGER-FIX D1/D2: sync health (never fail silently) ──────────────────

def set_sync_health(wallet: str, *, ok: bool, fills_ok: bool = True,
                    funding_ok: bool = True, detail: str = "") -> None:
    """Persist the outcome of the last sync of ONE wallet."""
    con = _conn()
    try:
        con.execute(
            "INSERT INTO ledger_sync_health (wallet, ok, fills_ok, funding_ok,"
            " detail, updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(wallet) DO UPDATE SET ok=excluded.ok,"
            " fills_ok=excluded.fills_ok, funding_ok=excluded.funding_ok,"
            " detail=excluded.detail, updated_at=excluded.updated_at",
            (wallet.lower(), 1 if ok else 0, 1 if fills_ok else 0,
             1 if funding_ok else 0, detail[:400],
             datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    finally:
        con.close()


def sync_health() -> dict[str, dict[str, Any]]:
    con = _conn()
    try:
        return {r["wallet"]: dict(r)
                for r in con.execute("SELECT * FROM ledger_sync_health")}
    finally:
        con.close()


def degraded_wallets() -> list[dict[str, Any]]:
    """Wallets whose last sync did NOT produce complete data."""
    return [h for h in sync_health().values()
            if not h["ok"] or not h["fills_ok"] or not h["funding_ok"]]


def _health_banner() -> list[str]:
    """Render lines warning that the numbers below are incomplete. Empty
    when every wallet synced cleanly — the ledger stays quiet when healthy
    and LOUD when it is not."""
    bad = degraded_wallets()
    if not bad:
        return []
    lines = ["\u26a0\ufe0f LEDGER INCOMPLETO — los numeros de abajo NO son definitivos:"]
    for h in bad:
        w = str(h["wallet"])
        missing = []
        if not h["fills_ok"]:
            missing.append("fills")
        if not h["funding_ok"]:
            missing.append("funding")
        what = "+".join(missing) if missing else "sync"
        lines.append(f"   [{wallet_label(w)} {w[:6]}] {what} incompleto — "
                     f"{str(h.get('detail') or '')[:120]}")
    lines.append("   Reintenta /cierres cuando HL deje de rate-limitear.")
    return lines


def ledger_diagnostics() -> dict[str, Any]:
    """Read-only ledger state for the PUBLIC /health payload.

    R-LEDGER-FIX post-deploy: production ledger state was unverifiable from
    outside the box, which is why "funding is zero" survived a whole deploy.
    This exposes exactly what proves a boot went clean — schema migration,
    semantics version, leverage provenance histogram, degraded wallets — and
    deliberately NOT the money detail: no wallet addresses, no per-leg rows,
    only per-cycle aggregates. NEVER raises: /health must not 500 because a
    diagnostic did.
    """
    out: dict[str, Any] = {"ok": False, "assumed_leverage": ASSUMED_LEVERAGE}
    try:
        con = _conn()
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"db unavailable: {exc}"[:200]
        return out
    try:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(ledger_positions)")}
        tables = {r["name"] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        out["schema"] = {
            "margin_open": "margin_open" in cols,
            "ledger_sync_health": "ledger_sync_health" in tables,
            "semantics_version": _meta_get("semantics_version"),
            "semantics_expected": LEDGER_SEMANTICS_VERSION,
        }
        out["rows"] = {
            t: int(con.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"])
            for t in ("ledger_fills", "ledger_funding", "ledger_positions")
            if t in tables
        }
        # D1 tell-tale: how many closed legs still carry EXACTLY zero funding.
        # A perp position accrues funding hourly, so a high count here means
        # the carry is missing, not that the market was quiet.
        out["zero_funding_legs"] = int(con.execute(
            "SELECT COUNT(*) c FROM ledger_positions WHERE funding_net=0"
        ).fetchone()["c"]) if "ledger_positions" in tables else None
        out["leverage_sources"] = {
            str(r["leverage_source"] or "none"): int(r["n"])
            for r in con.execute(
                "SELECT leverage_source, COUNT(*) n FROM ledger_positions "
                "GROUP BY leverage_source")
        }
        # Per-cycle aggregates only — the COMBINADO number, no per-leg detail.
        out["last_cycles"] = [
            {"tag": r["cycle_tag"], "legs": int(r["legs"]),
             "wallets": int(r["wallets"]),
             "gross": round(float(r["gross"] or 0), 2),
             "fees": round(float(r["fees"] or 0), 2),
             "funding": round(float(r["funding"] or 0), 2),
             "net": round(float(r["net"] or 0), 2)}
            for r in con.execute(
                "SELECT cycle_tag, COUNT(*) legs, COUNT(DISTINCT wallet) wallets, "
                "SUM(gross_pnl) gross, SUM(fees_total) fees, "
                "SUM(funding_net) funding, SUM(net_pnl) net "
                "FROM ledger_positions WHERE cycle_tag IS NOT NULL "
                "GROUP BY cycle_tag ORDER BY MAX(close_ts) DESC LIMIT 5")
        ]
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:200]
        return out
    finally:
        con.close()
    try:
        scope = ledger_wallets()
        out["scope"] = sorted(scope.values())
        bad = degraded_wallets()
        out["degraded"] = sorted(wallet_label(str(h["wallet"])) for h in bad)
        out["degraded_detail"] = [
            {"wallet": wallet_label(str(h["wallet"])),
             "fills_ok": bool(h["fills_ok"]), "funding_ok": bool(h["funding_ok"]),
             "detail": str(h.get("detail") or "")[:160]}
            for h in bad
        ]
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:200]
        return out
    out["ok"] = (
        bool(out["schema"]["margin_open"])
        and bool(out["schema"]["ledger_sync_health"])
        and out["schema"]["semantics_version"] == LEDGER_SEMANTICS_VERSION
        and not out["degraded"]
    )
    return out


async def _alert(text: str, atype: str, entity: str, state: str,
                 send: Callable | None = None) -> bool:
    """ONE deduped Telegram alert. NEVER raises — an alert that cannot be
    delivered still leaves the health row + the render banner behind."""
    try:
        from modules.alert_dedup import should_emit
        if not should_emit(atype, entity, state, cooldown_hours=6):
            return False
    except Exception:  # noqa: BLE001
        log.exception("LEDGER alert dedup unavailable — emitting anyway")
    try:
        if send is not None:
            res = send(text)
            if asyncio.iscoroutine(res):
                await res
            return True
        from config import TELEGRAM_CHAT_ID
        from utils.telegram import send_bot_message
        from telegram import Bot  # type: ignore
        from config import TELEGRAM_BOT_TOKEN
        if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
            log.error("LEDGER ALERT (no telegram configured): %s", text)
            return False
        await send_bot_message(Bot(TELEGRAM_BOT_TOKEN), TELEGRAM_CHAT_ID, text)
        return True
    except Exception:  # noqa: BLE001
        log.exception("LEDGER alert send failed — health row still records it")
        return False


# ─── perp fill filter + reconciliation (pure — unit-testable) ───────────────

def _is_perp_fill(f: dict[str, Any]) -> bool:
    """Perp fills carry dir 'Open Long'/'Close Short'/'Long > Short'… Spot
    fills say 'Buy'/'Sell' and their coin embeds '/' or '@'."""
    coin = str(f.get("coin", "") or "")
    if "/" in coin or coin.startswith("@"):
        return False
    d = str(f.get("dir", "") or "")
    return ("Long" in d) or ("Short" in d)


def _signed(f: dict[str, Any]) -> float:
    sz = float(f.get("sz", 0) or 0)
    return sz if str(f.get("side", "")).upper() in ("B", "BUY") else -sz


def reconcile_positions(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild CLOSED position lifecycles from raw HL fills of ONE wallet.

    Partial fills aggregate into one lifecycle; a lifecycle closes when the
    net size returns to zero. A direction flip splits the crossing fill:
    the closing fraction (fees pro-rated, closedPnl in full) ends the old
    lifecycle and the remainder opens the new one.

    Returns closed positions sorted by close_ts ascending. Open lifecycles
    (net ≠ 0 at the end of the fill stream) are NOT returned.
    """
    by_coin: dict[str, list[dict[str, Any]]] = {}
    for f in fills:
        if not _is_perp_fill(f):
            continue
        by_coin.setdefault(str(f.get("coin")), []).append(f)

    closed: list[dict[str, Any]] = []
    for coin, cfills in by_coin.items():
        cfills.sort(key=lambda x: (int(x.get("time", 0) or 0), int(x.get("tid", 0) or 0)))
        cur: dict[str, Any] | None = None
        net = 0.0

        def _new_lifecycle(ts: int) -> dict[str, Any]:
            return {
                "coin": coin, "open_ts": ts, "close_ts": 0,
                "entry_sz": 0.0, "entry_notional": 0.0,
                "exit_sz": 0.0, "exit_notional": 0.0,
                "max_size": 0.0, "open_fills": 0, "close_fills": 0,
                "fees_total": 0.0, "gross_pnl": 0.0, "side": "?",
            }

        def _apply(part_sz: float, px: float, fee: float, pnl: float,
                   ts: int, opening: bool, direction: float) -> None:
            nonlocal cur, net
            if opening:
                if cur is None:
                    cur = _new_lifecycle(ts)
                    cur["side"] = "LONG" if direction > 0 else "SHORT"
                cur["entry_sz"] += part_sz
                cur["entry_notional"] += part_sz * px
                cur["open_fills"] += 1
            else:
                if cur is None:  # close without tracked open (horizon cut)
                    cur = _new_lifecycle(ts)
                    cur["side"] = "LONG" if direction < 0 else "SHORT"
                cur["exit_sz"] += part_sz
                cur["exit_notional"] += part_sz * px
                cur["close_fills"] += 1
                cur["gross_pnl"] += pnl
            cur["fees_total"] += fee
            net += direction * part_sz
            cur["max_size"] = max(cur["max_size"], abs(net))
            if not opening and abs(net) < _EPS:
                cur["close_ts"] = ts
                closed.append(cur)
                cur = None
                net = 0.0

        for f in cfills:
            sgn = _signed(f)
            sz = abs(sgn)
            if sz < _EPS:
                continue
            px = float(f.get("px", 0) or 0)
            fee = float(f.get("fee", 0) or 0)
            pnl = float(f.get("closedPnl", 0) or 0)
            ts = int(f.get("time", 0) or 0)
            direction = 1.0 if sgn > 0 else -1.0

            if net > _EPS and direction < 0 and sz > net + _EPS:
                # LONG flip → close `net`, open remainder SHORT
                close_part, open_part = net, sz - net
                fr = close_part / sz
                _apply(close_part, px, fee * fr, pnl, ts, opening=False, direction=-1.0)
                _apply(open_part, px, fee * (1 - fr), 0.0, ts, opening=True, direction=-1.0)
            elif net < -_EPS and direction > 0 and sz > -net + _EPS:
                # SHORT flip → close `-net`, open remainder LONG
                close_part, open_part = -net, sz + net
                fr = close_part / sz
                _apply(close_part, px, fee * fr, pnl, ts, opening=False, direction=1.0)
                _apply(open_part, px, fee * (1 - fr), 0.0, ts, opening=True, direction=1.0)
            else:
                opening = (abs(net) < _EPS) or (net > 0) == (direction > 0)
                _apply(sz, px, fee, pnl, ts, opening=opening, direction=direction)

    for p in closed:
        p["avg_entry"] = (p["entry_notional"] / p["entry_sz"]) if p["entry_sz"] > _EPS else 0.0
        p["avg_exit"] = (p["exit_notional"] / p["exit_sz"]) if p["exit_sz"] > _EPS else 0.0
        p["notional_open"] = p["avg_entry"] * p["max_size"]
    closed.sort(key=lambda x: x["close_ts"])
    return closed


def cluster_cycles(positions: list[dict[str, Any]]) -> None:
    """Part 3 — tag basket cycles IN PLACE. Positions of ONE wallet whose
    open_ts fall within CYCLE_WINDOW_MS of the cycle's first open cluster
    together. ≥2 legs = a cycle (tag ``ciclo YYYY-MM-DD`` [+ #N same-day]).
    Single positions keep cycle_tag None."""
    ordered = sorted(positions, key=lambda p: p["open_ts"])
    clusters: list[list[dict[str, Any]]] = []
    for p in ordered:
        if clusters and p["open_ts"] - clusters[-1][0]["open_ts"] <= CYCLE_WINDOW_MS:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    day_counts: dict[str, int] = {}
    for cl in clusters:
        if len(cl) < 2:
            for p in cl:
                p["cycle_tag"] = None
            continue
        day = datetime.fromtimestamp(cl[0]["open_ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        day_counts[day] = day_counts.get(day, 0) + 1
        tag = f"ciclo {day}" if day_counts[day] == 1 else f"ciclo {day} #{day_counts[day]}"
        for p in cl:
            p["cycle_tag"] = tag


def compute_net(gross: float, fees: float, funding: float) -> float:
    """THE formula: NET = gross − fees + funding (funding>0 = received)."""
    return gross - fees + funding


# ─── HL sync (fills + funding) ──────────────────────────────────────────────

async def _page(fetch: Callable, wallet: str, kind: str, cursor: int,
                page_idx: int, partial: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fetch ONE page with its own bounded retry, then validate the shape.

    R-LEDGER-FIX D1: this is the function that used to swallow everything.
    It now raises ``LedgerSyncError`` — carrying the rows fetched so far —
    on transport failure OR on an unexpected response shape. Silent zeros
    are not an acceptable degradation for a track-record ledger.
    """
    last_exc: Exception | None = None
    for attempt in range(max(1, PAGE_MAX_ATTEMPTS)):
        try:
            page = await fetch()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.warning("LEDGER %s page %d failed for %s (attempt %d/%d): %s",
                        kind, page_idx, wallet[:6], attempt + 1,
                        PAGE_MAX_ATTEMPTS, exc)
            if attempt < PAGE_MAX_ATTEMPTS - 1:
                await asyncio.sleep(PAGE_PAUSE_SEC * (2 ** attempt))
            continue
        if page is None:
            return []
        if not isinstance(page, list):
            # HL answers errors as a dict/string on the same 200 path — that
            # is a FAILURE, not "no data".
            raise LedgerSyncError(
                f"{kind}: unexpected response shape {type(page).__name__} "
                f"({str(page)[:160]})", wallet=wallet, kind=kind, partial=partial)
        return page
    raise LedgerSyncError(
        f"{kind}: page {page_idx} failed after {PAGE_MAX_ATTEMPTS} attempts "
        f"(cursor={cursor}): {last_exc}", wallet=wallet, kind=kind, partial=partial)


async def _fetch_fills_paged(wallet: str, start_ms: int) -> list[dict[str, Any]]:
    from modules.portfolio import user_fills_by_time
    out: list[dict[str, Any]] = []
    cursor = max(0, start_ms)
    for i in range(MAX_PAGES):
        if i:
            await asyncio.sleep(PAGE_PAUSE_SEC)
        page = await _page(lambda c=cursor: user_fills_by_time(wallet, c),
                           wallet, "fills", cursor, i, out)
        if not page:
            break
        out.extend(page)
        if len(page) < FILLS_PAGE_CAP:
            break
        new_cursor = max(int(f.get("time", 0) or 0) for f in page)  # overlap 1ms — PK dedups
        if new_cursor <= cursor:
            # H2: a full page sharing one millisecond would stall the cursor —
            # break explicitly instead of burning MAX_PAGES on the same window.
            log.warning("LEDGER fills cursor stalled at %d for %s — breaking",
                        new_cursor, wallet[:6])
            break
        cursor = new_cursor
    return out


async def _fetch_funding_paged(wallet: str, start_ms: int) -> list[dict[str, Any]]:
    """Paged ``userFunding`` history for one wallet.

    Request shape VERIFIED against the live endpoint (2026-08-26):
    ``{"type": "userFunding", "user": "0x…", "startTime": <epoch_ms>}``
    returns a time-ASCENDING list of ``{"time", "hash", "delta": {"type":
    "funding", "coin", "usdc", "szi", "fundingRate", "nSamples"}}``, capped
    at 500 entries per page. ``delta.usdc`` is a STRING and already carries
    the ledger's sign convention: NEGATIVE = paid, POSITIVE = received —
    no inversion is applied here.
    """
    from modules.portfolio import _info  # same info endpoint, keyless
    out: list[dict[str, Any]] = []
    cursor = max(0, start_ms)
    for i in range(MAX_PAGES * 4):
        if i:
            await asyncio.sleep(PAGE_PAUSE_SEC)
        page = await _page(
            lambda c=cursor: _info({"type": "userFunding", "user": wallet,
                                    "startTime": int(c)}),
            wallet, "funding", cursor, i, out)
        if not page:
            break
        out.extend(page)
        if len(page) < FUNDING_PAGE_CAP:
            break
        new_cursor = max(int(e.get("time", 0) or 0) for e in page)
        if new_cursor <= cursor:  # H2: same stall guard as fills pagination
            log.warning("LEDGER funding cursor stalled at %d for %s — breaking",
                        new_cursor, wallet[:6])
            break
        cursor = new_cursor
    return out


def _store_fills(wallet: str, fills: list[dict[str, Any]]) -> int:
    con = _conn()
    try:
        n = 0
        for f in fills:
            try:
                cur = con.execute(
                    "INSERT OR IGNORE INTO ledger_fills "
                    "(wallet, tid, oid, coin, side, px, sz, time, start_position,"
                    " dir, closed_pnl, fee, fee_token) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        wallet, int(f.get("tid", 0) or 0), int(f.get("oid", 0) or 0),
                        str(f.get("coin", "?")), str(f.get("side", "")),
                        float(f.get("px", 0) or 0), float(f.get("sz", 0) or 0),
                        int(f.get("time", 0) or 0),
                        float(f.get("startPosition", 0) or 0),
                        str(f.get("dir", "") or ""),
                        float(f.get("closedPnl", 0) or 0),
                        float(f.get("fee", 0) or 0),
                        str(f.get("feeToken", "") or ""),
                    ),
                )
                n += cur.rowcount if cur.rowcount > 0 else 0
            except (TypeError, ValueError):
                continue
        con.commit()
        return n
    finally:
        con.close()


def _store_funding(wallet: str, entries: list[dict[str, Any]]) -> int:
    """Persist ``userFunding`` rows. Response parsing verified live: the
    payload nests the economics under ``delta`` and ``usdc`` arrives as a
    STRING. Anything that does not parse is COUNTED and logged — a silently
    dropped funding row is exactly how the carry went missing."""
    con = _conn()
    try:
        n = 0
        skipped = 0
        for e in entries:
            d = e.get("delta") or {}
            if str(d.get("type", "")) != "funding":
                continue
            if d.get("usdc") is None or e.get("time") is None:
                skipped += 1
                continue
            try:
                cur = con.execute(
                    "INSERT OR IGNORE INTO ledger_funding "
                    "(wallet, time, coin, usdc, szi, rate) VALUES (?,?,?,?,?,?)",
                    (
                        wallet, int(e.get("time", 0) or 0), str(d.get("coin", "?")),
                        float(d.get("usdc", 0) or 0), float(d.get("szi", 0) or 0),
                        float(d.get("fundingRate", 0) or 0),
                    ),
                )
                n += cur.rowcount if cur.rowcount > 0 else 0
            except (TypeError, ValueError) as exc:
                skipped += 1
                log.warning("LEDGER funding row unparseable for %s: %s (%s)",
                            wallet[:6], exc, str(e)[:160])
                continue
        con.commit()
        if skipped:
            log.warning("LEDGER funding: %d/%d rows skipped for %s",
                        skipped, len(entries), wallet[:6])
        return n
    finally:
        con.close()


def _funding_for(con: sqlite3.Connection, wallet: str, coin: str,
                 t0: int, t1: int) -> float:
    row = con.execute(
        "SELECT COALESCE(SUM(usdc),0) s FROM ledger_funding "
        "WHERE wallet=? AND coin=? AND time>=? AND time<=?",
        (wallet, coin, t0, t1),
    ).fetchone()
    return float(row["s"] or 0)


def derive_leverage(notional_open: float | None,
                    margin_open: float | None) -> float | None:
    """R-LEDGER-FIX D3 — the REAL leverage of a leg: notional at open over
    the margin actually posted. Returns None when the inputs cannot support
    an honest division, so the caller falls back instead of inventing a
    number. Values outside [DERIVED_LEV_MIN, DERIVED_LEV_MAX] are rejected —
    those come from dust margin or cross-margin bleed, not from a real
    basket leg."""
    try:
        n = float(notional_open or 0)
        m = float(margin_open or 0)
    except (TypeError, ValueError):
        return None
    if n <= 0 or m <= 0:
        return None
    lev = n / m
    if not (DERIVED_LEV_MIN <= lev <= DERIVED_LEV_MAX):
        log.info("LEDGER derived leverage %.2f out of band (notional %.2f / "
                 "margin %.2f) — falling back", lev, n, m)
        return None
    return lev


def resolve_leverage(notional_open: float | None, margin_open: float | None,
                     live_leverage: float | None = None
                     ) -> tuple[float, str]:
    """Leverage + its provenance, in precedence order: derived (from the
    margin actually posted) → live (HL's reported leverage) → assumed."""
    lev = derive_leverage(notional_open, margin_open)
    if lev is not None:
        return lev, "derived"
    try:
        if live_leverage and float(live_leverage) > 0:
            return float(live_leverage), "live"
    except (TypeError, ValueError):
        pass
    return ASSUMED_LEVERAGE, "assumed"


def rebuild_wallet_positions(wallet: str) -> int:
    """Deterministic rebuild of CLOSED lifecycles for one wallet from the
    stored fills (idempotent — upserts by (wallet, coin, open_ts)). Applies
    funding attribution, cycle clustering, NET and ROE. Preserves any
    live-close enrichment (leverage/funding snapshot) already persisted."""
    con = _conn()
    try:
        fills = [dict(r) for r in con.execute(
            "SELECT tid, coin, side, px, sz, time, start_position AS startPosition,"
            " dir, closed_pnl AS closedPnl, fee FROM ledger_fills WHERE wallet=?"
            " ORDER BY time, tid", (wallet,),
        ).fetchall()]
        positions = reconcile_positions(fills)
        cluster_cycles(positions)
        n = 0
        for p in positions:
            funding = _funding_for(con, wallet, p["coin"], p["open_ts"], p["close_ts"])
            net = compute_net(p["gross_pnl"], p["fees_total"], funding)
            prev = con.execute(
                "SELECT leverage, leverage_source, funding_live_snapshot,"
                " margin_open FROM ledger_positions "
                "WHERE wallet=? AND coin=? AND open_ts=?",
                (wallet, p["coin"], p["open_ts"]),
            ).fetchone()
            # R-LEDGER-FIX D3: the margin ACTUALLY posted (captured by the
            # live open snapshot) survives rebuilds and drives the derived
            # leverage. Only when it is unavailable do we fall back to HL's
            # reported live leverage, and only then to the env default.
            f_live = prev["funding_live_snapshot"] if prev is not None else None
            margin_open = prev["margin_open"] if prev is not None else None
            prev_live_lev = (
                float(prev["leverage"])
                if prev is not None and prev["leverage"]
                and prev["leverage_source"] in ("live", "derived") else None
            )
            lev, lev_src = resolve_leverage(
                p["notional_open"], margin_open, prev_live_lev)
            margin = (p["notional_open"] / lev) if lev and lev > 0 and p["notional_open"] > 0 else None
            roe = (net / margin * 100.0) if margin and margin > 0 else None
            f_delta = (funding - float(f_live)) if f_live is not None else None
            if f_delta is not None and abs(f_delta) > max(0.01, abs(funding) * 0.05):
                log.info(
                    "LEDGER funding delta %s %s: api=%.4f live_snap=%.4f delta=%.4f (API wins)",
                    wallet[:6], p["coin"], funding, float(f_live), f_delta,
                )
            # R-LEDGER-FIX D3: leverage/leverage_source/margin_open are now
            # part of the UPDATE set. They were previously insert-only, so a
            # corrected assumed leverage would never have reached the rows
            # already stored — every historical ROE would have stayed
            # inflated forever.
            con.execute(
                "INSERT INTO ledger_positions (wallet, coin, side, open_ts, close_ts,"
                " avg_entry, avg_exit, max_size, notional_open, margin_open, leverage,"
                " leverage_source, open_fills, close_fills, fees_total, funding_net,"
                " funding_live_snapshot, funding_delta, gross_pnl, net_pnl, roe_pct,"
                " cycle_tag) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(wallet, coin, open_ts) DO UPDATE SET "
                " close_ts=excluded.close_ts, avg_entry=excluded.avg_entry,"
                " avg_exit=excluded.avg_exit, max_size=excluded.max_size,"
                " notional_open=excluded.notional_open,"
                " margin_open=excluded.margin_open, leverage=excluded.leverage,"
                " leverage_source=excluded.leverage_source,"
                " open_fills=excluded.open_fills, close_fills=excluded.close_fills,"
                " fees_total=excluded.fees_total, funding_net=excluded.funding_net,"
                " funding_delta=excluded.funding_delta, gross_pnl=excluded.gross_pnl,"
                " net_pnl=excluded.net_pnl, roe_pct=excluded.roe_pct,"
                " cycle_tag=excluded.cycle_tag",
                (
                    wallet, p["coin"], p["side"], p["open_ts"], p["close_ts"],
                    p["avg_entry"], p["avg_exit"], p["max_size"], p["notional_open"],
                    margin_open, lev, lev_src,
                    p["open_fills"], p["close_fills"], p["fees_total"],
                    funding, f_live, f_delta, p["gross_pnl"], net, roe, p["cycle_tag"],
                ),
            )
            n += 1
        con.commit()
        return n
    finally:
        con.close()


def funding_gap(wallet: str, t0: int, t1: int) -> bool:
    """True when the wallet has CLOSED positions inside (t0, t1] but NOT a
    single funding row covering that window. Every perp position accrues
    funding hourly, so "closures but no funding" is not a quiet market — it
    is missing data, and it is exactly what produced the 0.00 carry."""
    con = _conn()
    try:
        pos = con.execute(
            "SELECT COUNT(*) c FROM ledger_positions "
            "WHERE wallet=? AND close_ts>? AND close_ts<=?",
            (wallet.lower(), int(t0), int(t1)),
        ).fetchone()
        if int(pos["c"] or 0) == 0:
            return False
        fund = con.execute(
            "SELECT COUNT(*) c FROM ledger_funding "
            "WHERE wallet=? AND time>? AND time<=?",
            (wallet.lower(), int(t0), int(t1)),
        ).fetchone()
        return int(fund["c"] or 0) == 0
    finally:
        con.close()


async def sync_wallet(wallet: str) -> int:
    """Incremental sync: pull new fills + funding since the per-wallet
    cursor (first call = full backfill to the HL horizon), then rebuild the
    wallet's closed lifecycles. Returns # of closed positions upserted.

    R-LEDGER-FIX D1: a failing HL read no longer degrades to zeros. Whatever
    was fetched before the failure IS persisted (so progress is never lost),
    the wallet is marked unhealthy, and ``LedgerSyncError`` propagates so
    the caller alerts. Only a CLEAN pass marks the wallet healthy.
    """
    wallet = wallet.lower()
    fills_cur = int(_meta_get(f"fills_cursor_ms:{wallet}") or 0)
    fund_cur = int(_meta_get(f"funding_cursor_ms:{wallet}") or 0)

    fills: list[dict[str, Any]] = []
    funding: list[dict[str, Any]] = []
    failure: LedgerSyncError | None = None
    fills_ok = funding_ok = True
    try:
        fills = await _fetch_fills_paged(wallet, max(0, fills_cur - 1))
    except LedgerSyncError as exc:
        fills, fills_ok, failure = list(exc.partial), False, exc
    if failure is None:
        try:
            funding = await _fetch_funding_paged(wallet, max(0, fund_cur - 1))
        except LedgerSyncError as exc:
            funding, funding_ok, failure = list(exc.partial), False, exc

    if fills:
        _store_fills(wallet, fills)
        _meta_set(f"fills_cursor_ms:{wallet}",
                  str(max(int(f.get("time", 0) or 0) for f in fills)))
    if funding:
        _store_funding(wallet, funding)
        _meta_set(f"funding_cursor_ms:{wallet}",
                  str(max(int(e.get("time", 0) or 0) for e in funding)))
    n = await asyncio.to_thread(rebuild_wallet_positions, wallet)
    if not _meta_get(f"backfill_done:{wallet}"):
        _meta_set(f"backfill_done:{wallet}",
                  datetime.now(timezone.utc).isoformat())
        log.info("LEDGER backfill done for %s: %d fills, %d funding, %d closed positions",
                 wallet[:6], len(fills), len(funding), n)

    if failure is not None:
        set_sync_health(wallet, ok=False, fills_ok=fills_ok,
                        funding_ok=funding_ok, detail=str(failure))
        raise failure
    set_sync_health(wallet, ok=True, fills_ok=True, funding_ok=True, detail="")
    return n


async def sync_all(send: Callable | None = None) -> dict[str, Any]:
    """Sync every ledger wallet (serialized behind one lock — safe to call
    from /reporte, /cierres and the alert loop concurrently).

    R-LEDGER-FIX D2: a wallet that fails is no longer swallowed by a bare
    log-and-continue. Its failure is persisted, alerted ONCE (deduped) and
    banner-rendered. The scope itself is logged and checked every run,
    because a challenge wallet missing from ``ledger_wallets()`` produces
    the exact same symptom as a failed sync — a wallet that simply is not
    in the report.
    """
    scope = ledger_wallets()
    log.info("LEDGER scope: %d wallet(s) — %s", len(scope),
             ", ".join(f"{lbl} {w[:6]}" for w, lbl in scope.items()))
    result: dict[str, Any] = {"wallets": list(scope), "ok": [], "failed": {},
                              "alerts": 0}
    if not scope:
        result["alerts"] += int(await _alert(
            "\u26a0\ufe0f LEDGER SIN WALLETS\nledger_wallets() devolvio vacio: "
            "ninguna FUND_WALLET_N ni PM_PRIMARY_WALLET configurada. El ledger "
            "no puede reportar cierres.", "ledger_scope", "scope", "empty", send))
        return result
    if len(scope) < 2:
        # The dual-wallet number IS the public track record. One wallet in
        # scope means the challenge wallet is not configured — say so.
        result["alerts"] += int(await _alert(
            "\u26a0\ufe0f LEDGER SCOPE DEGRADADO\nSolo 1 wallet en el ledger "
            f"({', '.join(scope.values())}). La wallet de reto no esta en "
            "FUND_WALLET_N / LEDGER_WALLETS, asi que sus cierres NO van a "
            "aparecer en el track record.", "ledger_scope", "scope",
            "single_wallet", send))

    async with _lock():
        # R-LEDGER-FIX D3: one-shot re-pricing of rows stored under the old
        # 5x assumption. rebuild_wallet_positions now writes leverage on
        # conflict, so a plain rebuild of every wallet is enough — but it
        # must run even for wallets with no new fills, hence the version gate.
        if _meta_get("semantics_version") != LEDGER_SEMANTICS_VERSION:
            for w in scope:
                try:
                    await asyncio.to_thread(rebuild_wallet_positions, w)
                except Exception:  # noqa: BLE001
                    log.exception("LEDGER recompute failed for %s", w[:6])
            _meta_set("semantics_version", LEDGER_SEMANTICS_VERSION)
            log.info("LEDGER recomputed stored rows at semantics v%s "
                     "(assumed leverage %gx)", LEDGER_SEMANTICS_VERSION,
                     ASSUMED_LEVERAGE)
        for w in scope:
            try:
                await sync_wallet(w)
                result["ok"].append(w)
            except Exception as exc:  # noqa: BLE001
                log.exception("LEDGER sync FAILED for %s", w[:6])
                result["failed"][w] = str(exc)
                set_sync_health(w, ok=False, detail=str(exc))
                result["alerts"] += int(await _alert(
                    f"\u26a0\ufe0f LEDGER SYNC FALLIDO — {wallet_label(w)} {w[:6]}\n"
                    f"{str(exc)[:300]}\n"
                    "Los cierres de esta wallet pueden faltar o quedar con "
                    "funding incompleto en el proximo reporte.",
                    "ledger_sync", w, "failed", send))
        try:
            ensure_report_cursor_seeded()
        except Exception:  # noqa: BLE001
            log.exception("LEDGER cursor seeding failed (non-fatal)")

    # Funding-coverage guard: a wallet that closed positions in the reported
    # window but stored NO funding for it is the D1 signature. One deduped
    # alert, never a silent 0.00.
    now_ms = int(_time.time() * 1000)
    prev_ms = get_report_cursor()
    for w in scope:
        try:
            if funding_gap(w, prev_ms, now_ms):
                set_sync_health(w, ok=False, funding_ok=False,
                                detail="funding vacio con cierres en la ventana")
                result["alerts"] += int(await _alert(
                    f"\u26a0\ufe0f LEDGER FUNDING VACIO — {wallet_label(w)} {w[:6]}\n"
                    "Hay cierres en la ventana pero CERO filas de funding: el "
                    "NET reportado seria gross - fees, sin carry. No tomar los "
                    "numeros como definitivos hasta re-sincronizar.",
                    "ledger_funding", w, "empty", send))
        except Exception:  # noqa: BLE001
            log.exception("LEDGER funding-gap check failed for %s", w[:6])
    return result


# ─── Part 2: persistent /reporte cursor ─────────────────────────────────────

def get_report_cursor() -> int:
    """Epoch-ms of the last rendered /reporte CIERRES section (0 = never)."""
    return int(_meta_get("last_report_ts_ms") or 0)


def set_report_cursor(ts_ms: int) -> None:
    """Advance the cursor after a SUCCESSFUL section send. Also consumes the
    one-shot first-run note (the note is rendered at most once)."""
    _meta_set("last_report_ts_ms", str(int(ts_ms)))
    if _meta_get("first_run_note_pending"):
        _meta_set("first_run_note_pending", "")


def ensure_report_cursor_seeded() -> None:
    """First-run guard: if the report cursor was never set, seed it to
    (most recent closure − LEDGER_FIRST_RUN_LOOKBACK_HOURS) so the first
    /reporte section covers only the recent window instead of dumping the
    whole backfilled horizon. Marks a one-shot note for the renderer."""
    if get_report_cursor() > 0:
        return
    con = _conn()
    try:
        row = con.execute("SELECT MAX(close_ts) m FROM ledger_positions").fetchone()
    finally:
        con.close()
    mx = int(row["m"] or 0) if row is not None else 0
    if mx <= 0:
        return  # empty ledger — nothing to guard yet
    seeded = max(0, mx - FIRST_RUN_LOOKBACK_MS)
    _meta_set("last_report_ts_ms", str(seeded))  # NOT set_report_cursor — keep note
    _meta_set("first_run_note_pending", "1")
    log.info("LEDGER report cursor seeded at %d (first run, lookback %dh)",
             seeded, FIRST_RUN_LOOKBACK_MS // 3_600_000)


def closures_between(start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    """Closed positions with close_ts in (start_ms, end_ms] — the no-gap
    no-dup window contract."""
    con = _conn()
    try:
        rows = con.execute(
            "SELECT * FROM ledger_positions WHERE close_ts>? AND close_ts<=? "
            "ORDER BY wallet, close_ts", (int(start_ms), int(end_ms)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# ─── Part 4: real-time close alerts ─────────────────────────────────────────

def _live_positions_from_wallets(wallets_payload: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """(wallet, coin) → live position dict, from fetch_all_wallets output."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    scope = ledger_wallets()
    for w in wallets_payload or []:
        if not isinstance(w, dict) or w.get("status") != "ok":
            continue
        data = w.get("data") or {}
        addr = str(data.get("wallet", "") or "").lower()
        if addr not in scope:
            continue
        for p in data.get("positions") or []:
            try:
                szi = float(p.get("size", 0) or 0)
            except (TypeError, ValueError):
                continue
            if abs(szi) < _EPS:
                continue
            out[(addr, str(p.get("coin", "?")))] = p
    return out


def _snap_open_positions(live: dict[tuple[str, str], dict[str, Any]], now_ms: int) -> None:
    con = _conn()
    try:
        for (wallet, coin), p in live.items():
            cf = p.get("cum_funding_since_open")
            con.execute(
                "INSERT INTO ledger_open_snap (wallet, coin, szi, leverage,"
                " cum_funding, entry_px, margin_used, snap_ts) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(wallet, coin) DO UPDATE SET szi=excluded.szi,"
                " leverage=excluded.leverage, cum_funding=excluded.cum_funding,"
                " entry_px=excluded.entry_px, margin_used=excluded.margin_used,"
                " snap_ts=excluded.snap_ts",
                (
                    wallet, coin, float(p.get("size", 0) or 0),
                    float(p["leverage"]) if p.get("leverage") else None,
                    float(cf) if cf is not None else None,
                    float(p.get("entry_px", 0) or 0),
                    float(p.get("margin_used", 0) or 0),
                    now_ms,
                ),
            )
        con.commit()
    finally:
        con.close()


def _enrich_close_from_snap(wallet: str, coin: str, snap: dict[str, Any]) -> dict[str, Any] | None:
    """Persist live leverage + funding snapshot into the freshest closed
    lifecycle of (wallet, coin) and recompute ROE. API funding stays the
    NET source of truth; the live snapshot is stored + cross-checked."""
    con = _conn()
    try:
        row = con.execute(
            "SELECT * FROM ledger_positions WHERE wallet=? AND coin=? "
            "ORDER BY close_ts DESC LIMIT 1", (wallet, coin),
        ).fetchone()
        if row is None:
            return None
        pos = dict(row)
        lev = snap.get("leverage")
        cf = snap.get("cum_funding")
        # HL cumFunding.sinceOpen convention: positive = PAID out. Ledger
        # convention: positive = RECEIVED → invert sign.
        f_live = (-float(cf)) if cf is not None else None
        f_api = float(pos["funding_net"] or 0)
        f_delta = (f_api - f_live) if f_live is not None else None
        if f_delta is not None and abs(f_delta) > max(0.01, abs(f_api) * 0.05):
            log.info(
                "LEDGER close funding cross-check %s %s: api=%.4f live=%.4f "
                "delta=%.4f (API wins)", wallet[:6], coin, f_api, f_live, f_delta,
            )
        # R-LEDGER-FIX D3: prefer the margin ACTUALLY posted over HL's
        # reported leverage field. margin_open is persisted so later
        # rebuilds keep deriving the same exact leverage.
        margin_open = snap.get("margin_used")
        try:
            margin_open = float(margin_open) if margin_open else None
        except (TypeError, ValueError):
            margin_open = None
        lev_val, lev_src = resolve_leverage(
            pos["notional_open"], margin_open,
            float(lev) if lev else None,
        )
        margin = (pos["notional_open"] / lev_val) if lev_val and pos["notional_open"] else None
        roe = (pos["net_pnl"] / margin * 100.0) if margin else pos.get("roe_pct")
        con.execute(
            "UPDATE ledger_positions SET margin_open=COALESCE(?, margin_open),"
            " leverage=?, leverage_source=?,"
            " funding_live_snapshot=?, funding_delta=?, roe_pct=COALESCE(?, roe_pct)"
            " WHERE id=?",
            (margin_open, lev_val, lev_src, f_live, f_delta, roe, pos["id"]),
        )
        con.commit()
        upd = con.execute("SELECT * FROM ledger_positions WHERE id=?", (pos["id"],)).fetchone()
        return dict(upd) if upd else pos
    finally:
        con.close()


async def run_close_alerts(bot, wallets_payload: list[dict[str, Any]],
                           chat_id: str | None = None,
                           send: Callable | None = None) -> int:
    """Monitoring-loop hook (Part 4). Detects OPEN→CLOSED transitions of
    tracked positions, syncs the ledger and pushes ONE deduped alert per
    closed position (+ one per fully-closed cycle). Returns alerts sent."""
    from modules.alert_dedup import should_emit
    from config import TELEGRAM_CHAT_ID

    chat_id = chat_id or TELEGRAM_CHAT_ID
    now_ms = int(_time.time() * 1000)
    live = _live_positions_from_wallets(wallets_payload)
    if not wallets_payload:
        return 0

    con = _conn()
    try:
        snaps = {(r["wallet"], r["coin"]): dict(r) for r in con.execute(
            "SELECT * FROM ledger_open_snap").fetchall()}
    finally:
        con.close()

    # Fetch health guard: only treat a tracked position as CLOSED if its
    # wallet payload came back from a LIVE fetch this cycle. NOTE:
    # portfolio.fetch_wallet returns status="ok" WITH stale=True when all
    # retries failed and it fell back to the cache — a stale payload must
    # never drive close detection (a failed OR stale fetch must never fake
    # a close).
    ok_wallets = {
        str((w.get("data") or {}).get("wallet", "")).lower()
        for w in (wallets_payload or [])
        if isinstance(w, dict) and w.get("status") == "ok" and not w.get("stale")
    }

    closed_keys = [
        k for k in snaps
        if k not in live and k[0] in ok_wallets
    ]
    sent = 0
    synced: set[str] = set()
    for wallet, coin in closed_keys:
        try:
            if wallet not in synced:
                async with _lock():
                    await sync_wallet(wallet)
                synced.add(wallet)
            pos = _enrich_close_from_snap(wallet, coin, snaps[(wallet, coin)])
            con = _conn()
            try:
                con.execute("DELETE FROM ledger_open_snap WHERE wallet=? AND coin=?",
                            (wallet, coin))
                con.commit()
            finally:
                con.close()
            if pos is None:
                continue
            akey_entity = f"{wallet}|{coin}|{pos['open_ts']}"
            if should_emit("ledger_close", akey_entity, "closed",
                           cooldown_hours=24 * 365):
                msg = ("\U0001f4b0 POSICION CERRADA\n"
                       + format_position_line(pos) + "\n" + _formula_footer())
                await _send(bot, chat_id, msg, send)
                sent += 1
            # Cycle completion check
            tag = pos.get("cycle_tag")
            if tag and _cycle_fully_closed(wallet, tag):
                if should_emit("ledger_cycle", f"{wallet}|{tag}", "closed",
                               cooldown_hours=24 * 365):
                    await _send(bot, chat_id, render_cycle_subtotal(wallet, tag), send)
                    sent += 1
        except Exception:  # noqa: BLE001
            log.exception("LEDGER close alert failed for %s %s (non-fatal)",
                          wallet[:6], coin)

    _snap_open_positions(live, now_ms)
    return sent


async def _send(bot, chat_id, text: str, send: Callable | None) -> None:
    if send is not None:
        res = send(text)
        if asyncio.iscoroutine(res):
            await res
        return
    await bot.send_message(chat_id=chat_id, text=text)


def _cycle_fully_closed(wallet: str, tag: str) -> bool:
    """A cycle is fully closed when none of its coins still has an open
    snapshot on that wallet."""
    con = _conn()
    try:
        coins = [r["coin"] for r in con.execute(
            "SELECT coin FROM ledger_positions WHERE wallet=? AND cycle_tag=?",
            (wallet, tag)).fetchall()]
        if not coins:
            return False
        q = ",".join("?" for _ in coins)
        row = con.execute(
            f"SELECT COUNT(*) c FROM ledger_open_snap WHERE wallet=? AND coin IN ({q})",
            [wallet, *coins],
        ).fetchone()
        return int(row["c"] or 0) == 0
    finally:
        con.close()


# ─── rendering (Spanish, Telegram plain text) ───────────────────────────────

def _fmt_usd(v: float | None) -> str:
    if v is None:
        return "n/d"
    sign = "+" if v > 0 else ("-" if v < 0 else "")
    return f"{sign}${abs(v):,.2f}"


def _fmt_dur(ms: int) -> str:
    s = max(0, ms // 1000)
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def _fmt_px(v: float) -> str:
    if v >= 1000:
        return f"{v:,.1f}"
    if v >= 1:
        return f"{v:,.4g}"
    return f"{v:.6g}"


def _formula_footer() -> str:
    return (
        "NET = gross - fees + funding (+ = cobrado / - = pagado)\n"
        f"ROE = NET / margen. '~' = leverage asumido {ASSUMED_LEVERAGE:g}x "
        "(basket estandar del fondo); sin '~' = leverage real derivado del "
        "margen posteado."
    )


def format_position_line(p: dict[str, Any]) -> str:
    """Per-closed-position economics line (Part 2.2)."""
    icon = "\U0001f7e2" if (p.get("net_pnl") or 0) >= 0 else "\U0001f534"
    fund = float(p.get("funding_net") or 0)
    fund_lbl = "cobra" if fund > 0 else ("paga" if fund < 0 else "0")
    roe = p.get("roe_pct")
    # R-LEDGER-FIX D3: '~' means "leverage assumed". A leverage DERIVED from
    # the margin actually posted is exact and must NOT be marked, same as a
    # live one.
    lev_note = "" if p.get("leverage_source") in ("live", "derived") else "~"
    lev = p.get("leverage")
    lev_str = f" @{float(lev):g}x" if lev else ""
    roe_str = f"{lev_note}{roe:+.1f}%{lev_str}" if roe is not None else "n/d"
    dur = _fmt_dur(int(p["close_ts"]) - int(p["open_ts"]))
    return (
        f"{icon} {p['coin']} {p['side']} [{wallet_label(p['wallet'])}] "
        f"{_fmt_px(float(p['avg_entry']))}\u2192{_fmt_px(float(p['avg_exit']))} | "
        f"sz {float(p['max_size']):g} | {dur} | "
        f"fees {_fmt_usd(-abs(float(p['fees_total'])))} | "
        f"funding {_fmt_usd(fund)} ({fund_lbl}) | "
        f"NET {_fmt_usd(float(p['net_pnl']))} | ROE {roe_str}"
    )


def _totals_line(rows: list[dict[str, Any]], label: str = "TOTAL") -> str:
    gross = sum(float(r["gross_pnl"] or 0) for r in rows)
    fees = sum(float(r["fees_total"] or 0) for r in rows)
    fund = sum(float(r["funding_net"] or 0) for r in rows)
    net = sum(float(r["net_pnl"] or 0) for r in rows)
    return (f"{label}: {len(rows)} pata(s) | gross {_fmt_usd(gross)} | "
            f"fees {_fmt_usd(-abs(fees))} | funding {_fmt_usd(fund)} | "
            f"NET {_fmt_usd(net)}")


def render_cierres_section(prev_ms: int, now_ms: int) -> str:
    """Part 2 — the /reporte section. Covers exactly (prev_ms, now_ms]."""
    rows = closures_between(prev_ms, now_ms)
    lines = ["\U0001f4b0 CIERRES DESDE EL ULTIMO REPORTE", "\u2500" * 30]
    # R-LEDGER-FIX D1/D2: incomplete data announces itself at the TOP of the
    # section. Silent zeros and silently missing wallets are the two bugs
    # this banner exists to make impossible.
    lines.extend(_health_banner())
    if prev_ms > 0:
        t0 = datetime.fromtimestamp(prev_ms / 1000, tz=timezone.utc).strftime("%d %b %H:%M")
    else:
        t0 = "inicio del ledger"
    t1 = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%d %b %H:%M UTC")
    lines.append(f"Ventana: {t0} \u2192 {t1}")
    if not rows:
        lines.append("Sin cierres desde el ultimo reporte.")
        return "\n".join(lines)

    # Render cap: only the most recent SECTION_MAX_ROWS closures get a line.
    # ALL subtotals and the final total stay computed over the FULL window —
    # totals must never be truncated.
    shown_ids = {
        id(r) for r in sorted(rows, key=lambda x: x["close_ts"], reverse=True)[:SECTION_MAX_ROWS]
    }
    omitted = len(rows) - len(shown_ids)

    by_wallet: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_wallet.setdefault(r["wallet"], []).append(r)
    for wallet, wrows in by_wallet.items():
        lines.append("")
        lines.append(f"[{wallet_label(wallet)} {wallet[:6]}]")
        # cycles first, grouped
        by_tag: dict[str | None, list[dict[str, Any]]] = {}
        for r in wrows:
            by_tag.setdefault(r.get("cycle_tag"), []).append(r)
        for tag, trows in by_tag.items():
            if tag:
                lines.append(f"  \u25cf {tag}")
            for r in sorted(trows, key=lambda x: x["close_ts"]):
                if id(r) in shown_ids:
                    lines.append("  " + format_position_line(r))
            if tag:
                lines.append("  " + _totals_line(trows, label=f"subtotal {tag}"))
        lines.append("  " + _totals_line(wrows, label="subtotal wallet"))

    # R-LEDGER-FIX D2: when a cycle ran on BOTH wallets, the combined number
    # is the one that belongs to the public track record — render it
    # explicitly per cycle instead of leaving it implicit in the grand total.
    tags_multi: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        t = r.get("cycle_tag")
        if t:
            tags_multi.setdefault(t, []).append(r)
    combined = [(t, trs) for t, trs in tags_multi.items()
                if len({x["wallet"] for x in trs}) > 1]
    if combined:
        lines.append("")
        for t, trs in sorted(combined):
            wl = ", ".join(sorted({wallet_label(x["wallet"]) for x in trs}))
            lines.append(_totals_line(trs, label=f"COMBINADO {t} ({wl})"))

    if omitted > 0:
        lines.append("")
        lines.append(f"\u2026 {omitted} cierre(s) mas antiguos omitidos del render "
                     "(incluidos en los totales) — detalle via /cierres")
    lines.append("")
    lines.append(_totals_line(rows))
    if _meta_get("first_run_note_pending"):
        lines.append("Primer reporte con ledger: el historial anterior "
                     "(backfill completo) esta disponible via /cierres")
    lines.append(_formula_footer())
    return "\n".join(lines)


def render_cycle_subtotal(wallet: str, tag: str) -> str:
    con = _conn()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM ledger_positions WHERE wallet=? AND cycle_tag=? "
            "ORDER BY close_ts", (wallet, tag)).fetchall()]
        all_rows = [dict(r) for r in con.execute(
            "SELECT * FROM ledger_positions WHERE cycle_tag=?", (tag,)).fetchall()]
    finally:
        con.close()
    lines = [f"\U0001f3c1 CICLO CERRADO \u2014 {tag} [{wallet_label(wallet)}]"]
    lines.append(_totals_line(rows, label="Ciclo"))
    other_wallets = {r["wallet"] for r in all_rows} - {wallet}
    if other_wallets:
        lines.append(_totals_line(all_rows, label="COMBINADO (ambas wallets)"))
    lines.append(_formula_footer())
    return "\n".join(lines)


def render_cierres_command(arg: str | None = None) -> str:
    """Part 5 — /cierres [N | YYYY-MM-DD | ciclo YYYY-MM-DD[ #N]]."""
    con = _conn()
    try:
        arg = (arg or "").strip()
        header = "\n".join(["\U0001f4d2 LEDGER DE CIERRES", *_health_banner()])
        if arg.lower().startswith("ciclo"):
            tag = arg if arg.lower() != "ciclo" else ""
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM ledger_positions WHERE cycle_tag=? "
                "ORDER BY wallet, close_ts", (tag,)).fetchall()]
            if not rows:
                tags = [r["cycle_tag"] for r in con.execute(
                    "SELECT DISTINCT cycle_tag FROM ledger_positions "
                    "WHERE cycle_tag IS NOT NULL ORDER BY cycle_tag DESC LIMIT 10")]
                return (f"{header}\nCiclo '{tag}' no encontrado.\n"
                        "Ciclos disponibles:\n" + "\n".join(f"  {t}" for t in tags))
            lines = [header, f"Detalle {tag}", "\u2500" * 30]
            by_wallet: dict[str, list[dict[str, Any]]] = {}
            for r in rows:
                by_wallet.setdefault(r["wallet"], []).append(r)
            for wallet, wrows in by_wallet.items():
                lines.append(f"[{wallet_label(wallet)} {wallet[:6]}]")
                for r in wrows:
                    lines.append(format_position_line(r))
                    lines.append(
                        f"    fills: {int(r['open_fills'])} apertura / "
                        f"{int(r['close_fills'])} cierre | notional {_fmt_usd(float(r['notional_open']))}"
                    )
                lines.append(_totals_line(wrows, label="subtotal wallet"))
            if len(by_wallet) > 1:
                lines.append(_totals_line(rows, label="COMBINADO"))
            lines.append(_formula_footer())
            return "\n".join(lines)

        day: str | None = None
        limit = 10
        if arg:
            if len(arg) == 10 and arg[4] == "-" and arg[7] == "-":
                day = arg
            else:
                try:
                    limit = max(1, min(50, int(arg)))
                except ValueError:
                    return (f"{header}\nUso: /cierres [N | YYYY-MM-DD | "
                            "ciclo YYYY-MM-DD]")
        if day:
            t0 = int(datetime.strptime(day, "%Y-%m-%d")
                     .replace(tzinfo=timezone.utc).timestamp() * 1000)
            t1 = t0 + 86_400_000
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM ledger_positions WHERE close_ts>=? AND close_ts<? "
                "ORDER BY close_ts DESC", (t0, t1)).fetchall()]
            sub = f"Cierres del {day} (UTC)"
        else:
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM ledger_positions ORDER BY close_ts DESC LIMIT ?",
                (limit,)).fetchall()]
            sub = f"Ultimos {len(rows)} cierres"

        tot = con.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(net_pnl),0) net,"
            " COALESCE(SUM(fees_total),0) fees, COALESCE(SUM(funding_net),0) fund,"
            " COALESCE(SUM(gross_pnl),0) gross FROM ledger_positions").fetchone()

        lines = [header, sub, "\u2500" * 30]
        if not rows:
            lines.append("Sin cierres registrados.")
        for r in rows:
            ts = datetime.fromtimestamp(r["close_ts"] / 1000, tz=timezone.utc)
            tag = f" \u00b7 {r['cycle_tag']}" if r.get("cycle_tag") else ""
            lines.append(f"{ts.strftime('%d %b %H:%M')}{tag}")
            lines.append(format_position_line(r))
        lines.append("")
        lines.append(
            f"ALL-TIME ({int(tot['c'])} cierres): NET {_fmt_usd(float(tot['net']))} | "
            f"gross {_fmt_usd(float(tot['gross']))} | fees {_fmt_usd(-abs(float(tot['fees'])))} | "
            f"funding {_fmt_usd(float(tot['fund']))}"
        )
        lines.append(_formula_footer())
        return "\n".join(lines)
    finally:
        con.close()
