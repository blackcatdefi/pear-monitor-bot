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
* ROE% = NET / margin, margin = notional_at_open / leverage. Leverage is
  taken LIVE from the open-position snapshot when the close is caught in
  real time; for backfilled history HL fills carry no leverage, so the
  fund's standard basket leverage (env LEDGER_ASSUMED_LEVERAGE, default 5)
  is assumed and flagged via leverage_source='assumed'.
* Cycle grouping (public track record): positions on the SAME wallet whose
  lifecycles OPEN within a 15-minute window cluster into one basket cycle,
  tagged ``ciclo YYYY-MM-DD`` (suffix #2, #3… for repeated same-day
  cycles). Closed cycles render with subtotals; when several wallets ran a
  same-tag cycle, a combined total is rendered.
* API horizon: HL serves only the most recent 10,000 fills per wallet
  (2,000 per page). The backfill goes as far as that horizon allows and the
  ledger states the horizon honestly instead of fabricating older cycles.

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

# Fund standard leverage assumed for BACKFILLED closes (fills carry none).
ASSUMED_LEVERAGE = float(os.getenv("LEDGER_ASSUMED_LEVERAGE", "5") or 5)

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

async def _fetch_fills_paged(wallet: str, start_ms: int) -> list[dict[str, Any]]:
    from modules.portfolio import user_fills_by_time
    out: list[dict[str, Any]] = []
    cursor = max(0, start_ms)
    for _ in range(MAX_PAGES):
        page = await user_fills_by_time(wallet, cursor)
        if not isinstance(page, list) or not page:
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
    from modules.portfolio import _info  # same info endpoint, keyless
    out: list[dict[str, Any]] = []
    cursor = max(0, start_ms)
    for _ in range(MAX_PAGES * 4):
        try:
            page = await _info({"type": "userFunding", "user": wallet, "startTime": cursor})
        except Exception as exc:  # noqa: BLE001
            log.warning("userFunding fetch failed for %s: %s", wallet, exc)
            break
        if not isinstance(page, list) or not page:
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
    con = _conn()
    try:
        n = 0
        for e in entries:
            d = e.get("delta") or {}
            if str(d.get("type", "")) != "funding":
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
            except (TypeError, ValueError):
                continue
        con.commit()
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
                "SELECT leverage, leverage_source, funding_live_snapshot "
                "FROM ledger_positions WHERE wallet=? AND coin=? AND open_ts=?",
                (wallet, p["coin"], p["open_ts"]),
            ).fetchone()
            lev = None
            lev_src = "assumed"
            f_live = None
            if prev is not None and prev["leverage"] and prev["leverage_source"] == "live":
                lev, lev_src, f_live = float(prev["leverage"]), "live", prev["funding_live_snapshot"]
            if lev is None:
                lev = ASSUMED_LEVERAGE
            margin = (p["notional_open"] / lev) if lev and lev > 0 and p["notional_open"] > 0 else None
            roe = (net / margin * 100.0) if margin and margin > 0 else None
            f_delta = (funding - float(f_live)) if f_live is not None else None
            if f_delta is not None and abs(f_delta) > max(0.01, abs(funding) * 0.05):
                log.info(
                    "LEDGER funding delta %s %s: api=%.4f live_snap=%.4f delta=%.4f (API wins)",
                    wallet[:6], p["coin"], funding, float(f_live), f_delta,
                )
            con.execute(
                "INSERT INTO ledger_positions (wallet, coin, side, open_ts, close_ts,"
                " avg_entry, avg_exit, max_size, notional_open, leverage,"
                " leverage_source, open_fills, close_fills, fees_total, funding_net,"
                " funding_live_snapshot, funding_delta, gross_pnl, net_pnl, roe_pct,"
                " cycle_tag) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(wallet, coin, open_ts) DO UPDATE SET "
                " close_ts=excluded.close_ts, avg_entry=excluded.avg_entry,"
                " avg_exit=excluded.avg_exit, max_size=excluded.max_size,"
                " notional_open=excluded.notional_open,"
                " open_fills=excluded.open_fills, close_fills=excluded.close_fills,"
                " fees_total=excluded.fees_total, funding_net=excluded.funding_net,"
                " funding_delta=excluded.funding_delta, gross_pnl=excluded.gross_pnl,"
                " net_pnl=excluded.net_pnl, roe_pct=excluded.roe_pct,"
                " cycle_tag=excluded.cycle_tag",
                (
                    wallet, p["coin"], p["side"], p["open_ts"], p["close_ts"],
                    p["avg_entry"], p["avg_exit"], p["max_size"], p["notional_open"],
                    lev, lev_src, p["open_fills"], p["close_fills"], p["fees_total"],
                    funding, f_live, f_delta, p["gross_pnl"], net, roe, p["cycle_tag"],
                ),
            )
            n += 1
        con.commit()
        return n
    finally:
        con.close()


async def sync_wallet(wallet: str) -> int:
    """Incremental sync: pull new fills + funding since the per-wallet
    cursor (first call = full backfill to the HL horizon), then rebuild the
    wallet's closed lifecycles. Returns # of closed positions upserted."""
    wallet = wallet.lower()
    fills_cur = int(_meta_get(f"fills_cursor_ms:{wallet}") or 0)
    fund_cur = int(_meta_get(f"funding_cursor_ms:{wallet}") or 0)
    fills = await _fetch_fills_paged(wallet, max(0, fills_cur - 1))
    funding = await _fetch_funding_paged(wallet, max(0, fund_cur - 1))
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
    return n


async def sync_all() -> None:
    """Sync every ledger wallet (serialized behind one lock — safe to call
    from /reporte, /cierres and the alert loop concurrently)."""
    async with _lock():
        for w in ledger_wallets():
            try:
                await sync_wallet(w)
            except Exception:  # noqa: BLE001
                log.exception("LEDGER sync failed for %s (non-fatal)", w[:6])
        try:
            ensure_report_cursor_seeded()
        except Exception:  # noqa: BLE001
            log.exception("LEDGER cursor seeding failed (non-fatal)")


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
        lev_val = float(lev) if lev else None
        margin = (pos["notional_open"] / lev_val) if lev_val and pos["notional_open"] else None
        roe = (pos["net_pnl"] / margin * 100.0) if margin else pos.get("roe_pct")
        con.execute(
            "UPDATE ledger_positions SET leverage=COALESCE(?, leverage),"
            " leverage_source=CASE WHEN ? IS NOT NULL THEN 'live' ELSE leverage_source END,"
            " funding_live_snapshot=?, funding_delta=?, roe_pct=COALESCE(?, roe_pct)"
            " WHERE id=?",
            (lev_val, lev_val, f_live, f_delta, roe, pos["id"]),
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
    return "NET = gross - fees + funding (+ = cobrado / - = pagado)"


def format_position_line(p: dict[str, Any]) -> str:
    """Per-closed-position economics line (Part 2.2)."""
    icon = "\U0001f7e2" if (p.get("net_pnl") or 0) >= 0 else "\U0001f534"
    fund = float(p.get("funding_net") or 0)
    fund_lbl = "cobra" if fund > 0 else ("paga" if fund < 0 else "0")
    roe = p.get("roe_pct")
    lev_note = "" if p.get("leverage_source") == "live" else "~"
    roe_str = f"{lev_note}{roe:+.1f}%" if roe is not None else "n/d"
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
        header = "\U0001f4d2 LEDGER DE CIERRES"
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
