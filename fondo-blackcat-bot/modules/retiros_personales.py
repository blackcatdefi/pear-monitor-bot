"""R-RONDA-OCT #1b — personal withdrawals from the PM wallet (RETIRO PERSONAL).

BCD's method for taking money out of the fund without touching the HYPE
collateral: borrow USDC on Portfolio Margin → buy HYPE spot with it →
withdraw that HYPE to HyperEVM. On-chain (0xc7ae, verified 2026-10-15 against
``userFillsByTime`` + ``userNonFundingLedgerUpdates``):

    2026-09-21 15:12  spot buy  11.64 HYPE @ 94.434  ($1,099.2, pair @107)
    2026-09-21 15:13  send      11.64 HYPE → 0x2222…2222 (HyperEVM bridge)
    2026-10-14 14:15  spot buy  12.53 HYPE @ 79.767  ($999.5)
    2026-10-14 14:16  send      12.53 HYPE → 0x2222…2222

What HL exposes and what it does not: the ledger shows the HYPE **send** to
the bridge system address and the fills show the HYPE **buy**; the USDC
**borrow** itself is NOT a ledger event (it is a negative spot balance that
grows). So a RETIRO PERSONAL is detected as *buy → bridge send within a
window* on the primary wallet, and the "USDC prestado" column is the USDC
notional of the matched buys (+ fee) — the amount the borrow grew by to fund
them. That is stated in the readout, not hidden.

Rules this ledger enforces:
  * each event is stored ONCE (UNIQUE on the send hash) with date, USDC
    borrowed, HYPE bought, HYPE withdrawn, buy price and the fill ids;
  * it is NOT a fund trade: PPC, track record and thesis never read it, and
    ``hype_acquisition`` EXCLUDES the matched buy fills so the PPC of the
    stack is not moved by a withdrawal (``excluded_fill_keys``);
  * the HYPE stack line shows the cumulative HYPE withdrawn, so the
    collateral count never pretends the stack is intact;
  * a bridge send with NO matching buy is still recorded (``kind =
    withdraw_only``): it is still an outflow of the stack.

Money path: every ``except`` either raises or calls ``health_registry.swallowed``.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from config import DATA_DIR, PM_PRIMARY_WALLET
from modules import health_registry

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "intel_memory.db")
SUBSYSTEM = "retiros"

# HL system address that receives spot→HyperEVM transfers.
BRIDGE_ADDR = "0x2222222222222222222222222222222222222222"
# A buy counts for a send when it happened within this window BEFORE it.
MATCH_WINDOW_MS = int(float(os.getenv("RETIROS_MATCH_WINDOW_HOURS", "24") or 24) * 3600 * 1000)
# First scan looks back this far (covers the 21-sep and 14-oct events).
BACKFILL_DAYS = int(os.getenv("RETIROS_BACKFILL_DAYS", "120") or 120)
# |bought − withdrawn| / withdrawn tolerance to call it the same event.
QTY_TOL = float(os.getenv("RETIROS_QTY_TOL", "0.05") or 0.05)

KIND_BORROW_BUY_WITHDRAW = "borrow_buy_withdraw"
KIND_WITHDRAW_ONLY = "withdraw_only"

META_SCAN_CURSOR = "retiros_scan_cursor_ms"
META_REPORT_CURSOR = "retiros_report_cursor_ms"


# ─── storage ────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10.0)
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS retiros_personales (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet          TEXT    NOT NULL,
            send_hash       TEXT    NOT NULL UNIQUE,
            ts_ms           INTEGER NOT NULL,
            kind            TEXT    NOT NULL,
            usdc_borrowed   REAL    NOT NULL DEFAULT 0,
            hype_bought     REAL    NOT NULL DEFAULT 0,
            hype_withdrawn  REAL    NOT NULL DEFAULT 0,
            buy_px          REAL,
            fee_usdc        REAL    NOT NULL DEFAULT 0,
            fill_keys       TEXT    NOT NULL DEFAULT '[]',
            destino         TEXT,
            created_at      TEXT    NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS retiros_meta (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    con.commit()
    return con


def _meta_get(key: str) -> str | None:
    con = _conn()
    try:
        row = con.execute("SELECT value FROM retiros_meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None
    finally:
        con.close()


def _meta_set(key: str, value: str) -> None:
    con = _conn()
    try:
        con.execute(
            "INSERT INTO retiros_meta (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    finally:
        con.close()


# ─── fetch ──────────────────────────────────────────────────────────────────

def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fetch_ledger_updates(wallet: str, start_ms: int) -> list[dict[str, Any]]:
    from modules.hl_client import post_info_sync
    data = post_info_sync({
        "type": "userNonFundingLedgerUpdates", "user": wallet, "startTime": int(start_ms),
    }, ttl=60)
    if not isinstance(data, list):
        raise ValueError("userNonFundingLedgerUpdates: shape inesperado")
    return [u for u in data if isinstance(u, dict)]


def _fetch_fills(wallet: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    from modules.hl_client import post_info_sync
    data = post_info_sync({
        "type": "userFillsByTime", "user": wallet,
        "startTime": int(start_ms), "endTime": int(end_ms),
    }, ttl=60)
    if not isinstance(data, list):
        raise ValueError("userFillsByTime: shape inesperado")
    return [f for f in data if isinstance(f, dict)]


def _spot_map() -> dict[str, str]:
    from modules.hl_client import post_info_sync
    from modules.spot_index import build_spot_index_map
    return build_spot_index_map(post_info_sync({"type": "spotMeta"})) or {}


def _is_hype_spot(coin: Any, spot_map: dict[str, str]) -> bool:
    s = str(coin or "")
    return s.upper() == "HYPE" or spot_map.get(s, "").upper() == "HYPE"


def bridge_sends(updates: list[dict[str, Any]], wallet: str) -> list[dict[str, Any]]:
    """HYPE ``send`` entries from ``wallet`` to the HyperEVM bridge address."""
    w = wallet.lower()
    out = []
    for u in updates:
        d = u.get("delta") or {}
        if d.get("type") != "send":
            continue
        if str(d.get("token", "")).upper() != "HYPE":
            continue
        if str(d.get("destination", "")).lower() != BRIDGE_ADDR:
            continue
        if str(d.get("user", "")).lower() != w:
            continue
        amt = _f(d.get("amount"))
        ts = int(u.get("time") or 0)
        if not amt or amt <= 0 or ts <= 0:
            continue
        out.append({"hash": str(u.get("hash") or f"{w}:{ts}"), "ts_ms": ts,
                    "amount": amt, "usdc_value": _f(d.get("usdcValue")),
                    "destination": str(d.get("destination"))})
    return out


def fill_key(f: dict[str, Any]) -> str:
    tid = f.get("tid")
    if tid is not None:
        return f"tid:{tid}"
    return f"oid:{f.get('oid')}:{f.get('time')}:{f.get('px')}:{f.get('sz')}"


def match_buys(send: dict[str, Any], fills: list[dict[str, Any]],
               spot_map: dict[str, str], *, used: set[str]) -> dict[str, Any]:
    """Pair one bridge send with the HYPE spot BUYS in the window before it.

    Returns ``{kind, hype_bought, usdc, px, fee_usdc, keys}``; ``kind`` is
    ``borrow_buy_withdraw`` when the bought quantity explains the withdrawn
    one within QTY_TOL, else ``withdraw_only``. Fills already used by an
    earlier send are skipped (``used``).
    """
    t1 = int(send["ts_ms"])
    t0 = t1 - MATCH_WINDOW_MS
    qty = 0.0
    notional = 0.0
    fee = 0.0
    keys: list[str] = []
    for f in sorted(fills, key=lambda x: int(x.get("time") or 0)):
        t = int(f.get("time") or 0)
        if t < t0 or t > t1:
            continue
        if not _is_hype_spot(f.get("coin"), spot_map):
            continue
        side = str(f.get("side") or "").upper()
        is_buy = side == "B" or str(f.get("dir") or "").lower().startswith("buy")
        if not is_buy:
            continue
        k = fill_key(f)
        if k in used:
            continue
        sz, px = _f(f.get("sz")) or 0.0, _f(f.get("px")) or 0.0
        if sz <= 0 or px <= 0:
            continue
        qty += sz
        notional += sz * px
        fv = _f(f.get("fee")) or 0.0
        fee += fv * px if str(f.get("feeToken") or "").upper() == "HYPE" else fv
        keys.append(k)
    withdrawn = float(send["amount"])
    if qty > 0 and abs(qty - withdrawn) / withdrawn <= QTY_TOL:
        used.update(keys)
        return {"kind": KIND_BORROW_BUY_WITHDRAW, "hype_bought": qty, "usdc": notional,
                "px": notional / qty, "fee_usdc": fee, "keys": keys}
    return {"kind": KIND_WITHDRAW_ONLY, "hype_bought": 0.0, "usdc": 0.0,
            "px": None, "fee_usdc": 0.0, "keys": []}


# ─── scan ───────────────────────────────────────────────────────────────────

@health_registry.tracked("retiros", "scan")
def scan(wallet: str | None = None, *, now_ms: int | None = None) -> dict[str, Any]:
    """Detect new RETIRO PERSONAL events since the persisted scan cursor and
    store them. Returns ``{ok, new, seen, wallet, from_ms}``. Raises on
    fetch failure (the ``tracked`` decorator marks the subsystem degraded and
    the caller decides); never stores a partial event.
    """
    w = (wallet or PM_PRIMARY_WALLET or "").lower()
    if not w:
        return {"ok": False, "new": 0, "seen": 0, "wallet": "", "reason": "sin wallet primaria"}
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    cur = _meta_get(f"{META_SCAN_CURSOR}:{w}")
    if cur:
        from_ms = max(0, int(cur) - MATCH_WINDOW_MS)
    else:
        from_ms = now_ms - BACKFILL_DAYS * 86_400_000
    updates = _fetch_ledger_updates(w, from_ms)
    sends = [s for s in bridge_sends(updates, w) if s["ts_ms"] >= from_ms]
    con = _conn()
    try:
        known = {r["send_hash"] for r in con.execute(
            "SELECT send_hash FROM retiros_personales WHERE wallet=?", (w,))}
        used: set[str] = set()
        for r in con.execute("SELECT fill_keys FROM retiros_personales WHERE wallet=?", (w,)):
            try:
                used.update(json.loads(r["fill_keys"] or "[]"))
            except (TypeError, ValueError):
                health_registry.swallowed(SUBSYSTEM, "scan: fill_keys ilegibles")
    finally:
        con.close()
    fresh = [s for s in sends if s["hash"] not in known]
    new = 0
    max_ts = int(cur) if cur else 0
    if fresh:
        spot_map = _spot_map()
        lo = min(s["ts_ms"] for s in fresh) - MATCH_WINDOW_MS
        hi = max(s["ts_ms"] for s in fresh)
        fills = _fetch_fills(w, lo, hi)
        con = _conn()
        try:
            for s in sorted(fresh, key=lambda x: x["ts_ms"]):
                m = match_buys(s, fills, spot_map, used=used)
                con.execute(
                    "INSERT OR IGNORE INTO retiros_personales (wallet, send_hash, ts_ms, kind, "
                    "usdc_borrowed, hype_bought, hype_withdrawn, buy_px, fee_usdc, fill_keys, "
                    "destino, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (w, s["hash"], int(s["ts_ms"]), m["kind"],
                     float(m["usdc"]) + float(m["fee_usdc"]), float(m["hype_bought"]),
                     float(s["amount"]), m["px"], float(m["fee_usdc"]),
                     json.dumps(m["keys"]), s["destination"],
                     datetime.now(timezone.utc).isoformat()),
                )
                new += 1
                log.info("RETIRO PERSONAL detectado %s: %s HYPE → HyperEVM, %s USDC (%s)",
                         datetime.fromtimestamp(s["ts_ms"] / 1000, tz=timezone.utc).isoformat(),
                         s["amount"], round(m["usdc"], 2), m["kind"])
            con.commit()
        finally:
            con.close()
    for s in sends:
        max_ts = max(max_ts, int(s["ts_ms"]))
    # Advance the cursor to the newest send seen (or now when none): the next
    # scan re-reads the window before it, dedup is by hash.
    _meta_set(f"{META_SCAN_CURSOR}:{w}", str(max_ts if max_ts > 0 else now_ms))
    return {"ok": True, "new": new, "seen": len(sends), "wallet": w, "from_ms": from_ms}


# ─── queries ────────────────────────────────────────────────────────────────

def rows_between(start_ms: int, end_ms: int, wallet: str | None = None) -> list[dict[str, Any]]:
    """Events with ``ts_ms`` in (start, end]. NEVER raises ([] + degraded)."""
    try:
        con = _conn()
        try:
            q = "SELECT * FROM retiros_personales WHERE ts_ms>? AND ts_ms<=?"
            args: list[Any] = [int(start_ms), int(end_ms)]
            if wallet:
                q += " AND wallet=?"
                args.append(wallet.lower())
            return [dict(r) for r in con.execute(q + " ORDER BY ts_ms", args)]
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        health_registry.swallowed(SUBSYSTEM, f"rows_between: {type(e).__name__}")
        return []


def all_rows(wallet: str | None = None) -> list[dict[str, Any]]:
    return rows_between(0, int(time.time() * 1000) + 1, wallet)


def totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "n": len(rows),
        "usdc_borrowed": float(sum(float(r.get("usdc_borrowed") or 0.0) for r in rows)),
        "hype_bought": float(sum(float(r.get("hype_bought") or 0.0) for r in rows)),
        "hype_withdrawn": float(sum(float(r.get("hype_withdrawn") or 0.0) for r in rows)),
        "last_ts_ms": max((int(r.get("ts_ms") or 0) for r in rows), default=None),
    }


def excluded_fill_keys(wallet: str | None = None) -> set[str]:
    """Fill keys that belong to RETIRO PERSONAL buys: PPC must skip them."""
    out: set[str] = set()
    for r in all_rows(wallet):
        try:
            out.update(json.loads(r.get("fill_keys") or "[]"))
        except (TypeError, ValueError):
            health_registry.swallowed(SUBSYSTEM, "excluded_fill_keys: fill_keys ilegibles")
    return out


def hype_withdrawn_total(wallet: str | None = None) -> float:
    return totals(all_rows(wallet))["hype_withdrawn"]


def usdc_borrowed_between(start_ms: int, end_ms: int, wallet: str | None = None) -> float:
    return totals(rows_between(start_ms, end_ms, wallet))["usdc_borrowed"]


def get_report_cursor() -> int:
    try:
        return int(_meta_get(META_REPORT_CURSOR) or 0)
    except Exception as e:  # noqa: BLE001
        health_registry.swallowed(SUBSYSTEM, f"get_report_cursor: {type(e).__name__}")
        return 0


def set_report_cursor(ts_ms: int) -> None:
    try:
        _meta_set(META_REPORT_CURSOR, str(int(ts_ms)))
    except Exception as e:  # noqa: BLE001
        health_registry.swallowed(SUBSYSTEM, f"set_report_cursor: {type(e).__name__}")


# ─── render ─────────────────────────────────────────────────────────────────

def _fmt_ts(ts_ms: int | None) -> str:
    if not ts_ms:
        return "?"
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "?"


def format_row(r: dict[str, Any]) -> str:
    kind = r.get("kind")
    if kind == KIND_BORROW_BUY_WITHDRAW:
        return (f"  • {_fmt_ts(r.get('ts_ms'))} UTC — RETIRO PERSONAL: "
                f"USDC prestado ≈ ${float(r.get('usdc_borrowed') or 0):,.2f} → "
                f"compra {float(r.get('hype_bought') or 0):.4g} HYPE @ ${float(r.get('buy_px') or 0):,.2f} → "
                f"retiro {float(r.get('hype_withdrawn') or 0):.4g} HYPE a HyperEVM")
    return (f"  • {_fmt_ts(r.get('ts_ms'))} UTC — RETIRO HYPE sin compra previa en 24h: "
            f"{float(r.get('hype_withdrawn') or 0):.4g} HYPE a HyperEVM (sale del stack)")


def render_section(prev_ms: int, now_ms: int, wallet: str | None = None) -> str:
    """/reporte section: events since the last report + cumulative totals."""
    w = (wallet or PM_PRIMARY_WALLET or "").lower()
    recent = rows_between(prev_ms, now_ms, w)
    everything = all_rows(w)
    t = totals(everything)
    lines = ["🏧 RETIROS PERSONALES (borrow → HYPE spot → HyperEVM)"]
    if recent:
        lines.append(f"Desde el ultimo reporte ({_fmt_ts(prev_ms) if prev_ms else 'inicio'}):")
        lines.extend(format_row(r) for r in recent)
    else:
        lines.append("Sin retiros desde el ultimo reporte.")
    if t["n"]:
        lines.append(
            f"Acumulado: {t['n']} retiro{'s' if t['n'] != 1 else ''} · "
            f"USDC prestado ≈ ${t['usdc_borrowed']:,.2f} · HYPE retirado {t['hype_withdrawn']:.4g} "
            f"· ultimo {_fmt_ts(t['last_ts_ms'])} UTC")
    else:
        lines.append("Acumulado: sin retiros registrados en el ledger.")
    lines.append("No son trades del fondo: no tocan PPC, track record ni tesis. "
                 "El borrow no es un evento del ledger de HL: 'USDC prestado' = "
                 "notional de las compras emparejadas (+fee).")
    return "\n".join(lines)


def render_command() -> str:
    """/retiros — the whole ledger."""
    w = (PM_PRIMARY_WALLET or "").lower()
    rows = all_rows(w)
    t = totals(rows)
    lines = [f"🏧 RETIROS PERSONALES — ledger completo [{w[:6]}]"]
    if not rows:
        lines.append("Sin retiros registrados.")
    else:
        lines.extend(format_row(r) for r in rows)
        lines.append(
            f"TOTAL: {t['n']} · USDC prestado ≈ ${t['usdc_borrowed']:,.2f} · "
            f"HYPE comprado {t['hype_bought']:.4g} · HYPE retirado {t['hype_withdrawn']:.4g}")
    lines.append("Deteccion: send de HYPE a 0x2222…2222 (bridge HyperEVM) emparejado con "
                 f"compras spot de HYPE en las {MATCH_WINDOW_MS // 3_600_000}h previas (±{QTY_TOL:.0%}).")
    return "\n".join(lines)


def diagnostics() -> dict[str, Any]:
    """Cheap, no-network block for /diagnostico."""
    try:
        w = (PM_PRIMARY_WALLET or "").lower()
        rows = all_rows(w)
        t = totals(rows)
        cur = _meta_get(f"{META_SCAN_CURSOR}:{w}")
        return {"ok": True, "n": t["n"], "usdc_borrowed": t["usdc_borrowed"],
                "hype_withdrawn": t["hype_withdrawn"], "last_ts_ms": t["last_ts_ms"],
                "scan_cursor_ms": int(cur) if cur else None}
    except Exception as e:  # noqa: BLE001
        health_registry.swallowed(SUBSYSTEM, f"diagnostics: {type(e).__name__}")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:160]}
