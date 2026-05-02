"""Snapshot tracker — SQLite persistence for portfolio deltas.

Every time /reporte or /posiciones runs, we:
  1. Build a snapshot dict of equity-per-wallet + HyperLend + Bounce Tech + Trade del Ciclo.
  2. Save it to SQLite (DATA_DIR/snapshots.db).
  3. Expose a helper to compute deltas vs the PREVIOUS snapshot (OFFSET 1).

The delta formatter is rendered verbatim at the bottom of /posiciones and inside
/reporte's "POSICIONES" section.

Table schema:
  CREATE TABLE snapshots(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT NOT NULL,        -- ISO-8601 UTC
      payload TEXT NOT NULL    -- JSON blob (see Snapshot dataclass below)
  );
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from config import DATA_DIR

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "snapshots.db")


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10.0)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts DESC)")
    con.commit()
    return con


# ─── Build a snapshot from live data ────────────────────────────────────────
def build_snapshot(
    wallets: list[dict[str, Any]] | None,
    hyperlend: list[dict[str, Any]] | dict[str, Any] | None,
    bounce_tech: list[dict[str, Any]] | None = None,
    cycle_trade: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize fetched data into a compact snapshot dict.

    Keys:
      ts: ISO UTC
      perps: { "<wallet_short>": {"label": str, "address": str, "equity": float, "upnl": float} }
      hyperlend: { "<wallet_short>": {"label": str, "address": str, "coll_usd": float, "debt_usd": float, "net_usd": float} }
      bounce_tech_total_usd: float
      cycle_trade: { "active": bool, "margin_usd": float, "upnl_usd": float }
      total_fund_usd: float  (perps + hyperlend_net + bt + cycle_margin+upnl)
    """
    ts = datetime.now(timezone.utc).isoformat()

    perps: dict[str, Any] = {}
    total_perp_equity = 0.0
    for w in wallets or []:
        if w.get("status") != "ok":
            continue
        d = w["data"]
        addr = (d.get("wallet") or "").lower()
        if not addr:
            continue
        short = addr[:6]
        eq = float(d.get("account_value") or 0.0)
        upnl = float(d.get("unrealized_pnl_total") or 0.0)
        perps[short] = {
            "label": d.get("label", ""),
            "address": addr,
            "equity": eq,
            "upnl": upnl,
        }
        total_perp_equity += eq

    hl_data: dict[str, Any] = {}
    total_hl_net = 0.0
    hl_list = hyperlend if isinstance(hyperlend, list) else ([hyperlend] if hyperlend else [])
    for hl in hl_list:
        if not hl or hl.get("status") != "ok":
            continue
        h = hl["data"]
        addr = (h.get("wallet") or "").lower()
        if not addr:
            continue
        short = addr[:6]
        coll = float(h.get("total_collateral_usd") or 0.0)
        debt = float(h.get("total_debt_usd") or 0.0)
        net = coll - debt
        if coll < 0.01 and debt < 0.01:
            continue
        hl_data[short] = {
            "label": h.get("label") or hl.get("label") or "",
            "address": addr,
            "coll_usd": coll,
            "debt_usd": debt,
            "net_usd": net,
        }
        total_hl_net += net

    bt_total = 0.0
    for bw in bounce_tech or []:
        if bw.get("status") != "ok":
            continue
        for p in bw.get("positions", []) or []:
            try:
                bt_total += float(p.get("value_usd") or 0.0)
            except (TypeError, ValueError):
                pass

    cycle_margin = 0.0
    cycle_upnl = 0.0
    cycle_active = False
    if cycle_trade:
        cycle_active = bool(cycle_trade.get("active"))
        if cycle_active:
            cycle_margin = float(cycle_trade.get("margin_usd") or 0.0)
            cycle_upnl = float(cycle_trade.get("upnl_usd") or 0.0)

    total_fund = total_perp_equity + total_hl_net + bt_total + cycle_margin + cycle_upnl

    return {
        "ts": ts,
        "perps": perps,
        "hyperlend": hl_data,
        "bounce_tech_total_usd": bt_total,
        "cycle_trade": {
            "active": cycle_active,
            "margin_usd": cycle_margin,
            "upnl_usd": cycle_upnl,
        },
        "total_fund_usd": total_fund,
    }


# ─── Persistence ────────────────────────────────────────────────────────────
def save_snapshot(snap: dict[str, Any]) -> int:
    """Persist a snapshot. Returns row id."""
    try:
        con = _conn()
        cur = con.execute(
            "INSERT INTO snapshots(ts, payload) VALUES (?, ?)",
            (snap.get("ts") or datetime.now(timezone.utc).isoformat(), json.dumps(snap, default=str)),
        )
        con.commit()
        rid = cur.lastrowid or 0
        con.close()
        log.info("snapshot saved id=%s total_fund=%.2f", rid, snap.get("total_fund_usd", 0.0))
        return rid
    except Exception:  # noqa: BLE001
        log.exception("save_snapshot failed")
        return 0


def previous_snapshot() -> dict[str, Any] | None:
    """Return the PENULTIMATE snapshot (one before the most recently saved).

    The caller typically saves a new snapshot first; previous_snapshot() then
    returns the last baseline to compute deltas against.
    """
    try:
        con = _conn()
        cur = con.execute("SELECT payload FROM snapshots ORDER BY id DESC LIMIT 1 OFFSET 1")
        row = cur.fetchone()
        con.close()
        if not row:
            return None
        return json.loads(row[0])
    except Exception:  # noqa: BLE001
        log.exception("previous_snapshot failed")
        return None


def latest_snapshot() -> dict[str, Any] | None:
    """Return the most recently saved snapshot (useful for /reporte replay)."""
    try:
        con = _conn()
        cur = con.execute("SELECT payload FROM snapshots ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        con.close()
        if not row:
            return None
        return json.loads(row[0])
    except Exception:  # noqa: BLE001
        log.exception("latest_snapshot failed")
        return None


# ─── Delta formatter ────────────────────────────────────────────────────────
def _emoji_for_diff(diff: float) -> str:
    if abs(diff) < 0.005:
        return "="
    return "🟢" if diff > 0 else "🔴"


def _fmt_row(label: str, prev: float, curr: float, note: str | None = None) -> str:
    diff = curr - prev
    pct = (diff / prev * 100) if abs(prev) > 1e-6 else 0.0
    emoji = _emoji_for_diff(diff)
    sign = "+" if diff >= 0 else ""
    tag = ""
    if prev == 0.0 and curr > 0:
        tag = " (deposit or open)"
    elif prev > 0 and curr == 0.0:
        tag = " (transfer or close)"
    elif note:
        tag = f" ({note})"
    if abs(prev) < 1e-6:
        return f"  {label}: $0 → ${curr:,.0f} ({emoji} {sign}${diff:,.0f}, n/a%){tag}"
    return f"  {label}: ${prev:,.0f} → ${curr:,.0f} ({emoji} {sign}${diff:,.0f}, {sign}{pct:.2f}%){tag}"


def _age_hours(iso_ts: str) -> float:
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return 0.0
    now = datetime.now(timezone.utc)
    delta = now - dt
    return round(delta.total_seconds() / 3600.0, 1)


def format_delta_block(current: dict[str, Any], previous: dict[str, Any] | None) -> str:
    """Render the 'CHANGES SINCE LAST REPORT' section.

    If `previous` is None: render a 'first run' notice.
    """
    lines: list[str] = []
    lines.append("")
    lines.append("📈 CHANGES SINCE LAST REPORT")
    lines.append("─" * 35)
    if not previous:
        lines.append("(first run — no history)")
        return "\n".join(lines)

    prev_ts = previous.get("ts") or "?"
    age = _age_hours(prev_ts)
    short_prev = prev_ts[:16].replace("T", " ")
    lines.append(f"Baseline: {short_prev} UTC ({age}h ago)")
    lines.append("")

    # Perps per wallet
    lines.append("POR WALLET (equity perps):")
    all_perp_keys = set(current.get("perps", {}).keys()) | set(previous.get("perps", {}).keys())
    for key in sorted(all_perp_keys):
        c = current.get("perps", {}).get(key, {})
        p = previous.get("perps", {}).get(key, {})
        label = c.get("label") or p.get("label") or ""
        short = key
        prev_eq = float(p.get("equity") or 0.0)
        curr_eq = float(c.get("equity") or 0.0)
        row_label = f"{short} {label}"[:32]
        lines.append(_fmt_row(row_label, prev_eq, curr_eq))

    # HyperLend per wallet
    all_hl_keys = set(current.get("hyperlend", {}).keys()) | set(previous.get("hyperlend", {}).keys())
    if all_hl_keys:
        lines.append("")
        lines.append("HYPERLEND (net equity = collateral - debt):")
        for key in sorted(all_hl_keys):
            c = current.get("hyperlend", {}).get(key, {})
            p = previous.get("hyperlend", {}).get(key, {})
            label = c.get("label") or p.get("label") or ""
            row_label = f"{key} {label}"[:32]
            lines.append(_fmt_row(row_label + " net", float(p.get("net_usd") or 0), float(c.get("net_usd") or 0)))
            lines.append(
                _fmt_row(
                    "    collateral",
                    float(p.get("coll_usd") or 0),
                    float(c.get("coll_usd") or 0),
                )
            )
            # Para deuda, 🔴 cuando sube (contra nosotros), 🟢 cuando baja.
            prev_debt = float(p.get("debt_usd") or 0)
            curr_debt = float(c.get("debt_usd") or 0)
            diff = curr_debt - prev_debt
            pct = (diff / prev_debt * 100) if abs(prev_debt) > 1e-6 else 0.0
            # Invertir emoji: subida de deuda = 🔴
            if abs(diff) < 0.005:
                emoji = "="
            else:
                emoji = "🔴" if diff > 0 else "🟢"
            sign = "+" if diff >= 0 else ""
            lines.append(
                f"    debt: ${prev_debt:,.0f} → ${curr_debt:,.0f} ({emoji} {sign}${diff:,.0f}, {sign}{pct:.2f}%) {'(against)' if diff > 0 else ('(favorable)' if diff < 0 else '')}"
            )

    # Bounce Tech
    prev_bt = float(previous.get("bounce_tech_total_usd") or 0)
    curr_bt = float(current.get("bounce_tech_total_usd") or 0)
    if abs(prev_bt) > 0.5 or abs(curr_bt) > 0.5:
        lines.append("")
        lines.append("BOUNCE TECH:")
        note = "position closed" if prev_bt > 0 and curr_bt == 0 else None
        lines.append(_fmt_row("Total BT", prev_bt, curr_bt, note=note))

    # Trade del Ciclo
    prev_cy = previous.get("cycle_trade", {}) or {}
    curr_cy = current.get("cycle_trade", {}) or {}
    if prev_cy.get("active") or curr_cy.get("active"):
        lines.append("")
        lines.append("TRADE DEL CICLO (Blofin):")
        lines.append(_fmt_row("margin", float(prev_cy.get("margin_usd") or 0), float(curr_cy.get("margin_usd") or 0)))
        lines.append(_fmt_row("UPnL", float(prev_cy.get("upnl_usd") or 0), float(curr_cy.get("upnl_usd") or 0)))

    # Total
    prev_total = float(previous.get("total_fund_usd") or 0)
    curr_total = float(current.get("total_fund_usd") or 0)
    diff_total = curr_total - prev_total
    pct_total = (diff_total / prev_total * 100) if abs(prev_total) > 1e-6 else 0.0
    sign = "+" if diff_total >= 0 else ""
    emoji = _emoji_for_diff(diff_total)

    lines.append("")
    lines.append("─" * 35)
    lines.append(f"TOTAL FONDO (perps + HL net + BT + Ciclo):")
    lines.append(f"  Anterior: ${prev_total:,.0f}")
    lines.append(f"  Actual:   ${curr_total:,.0f}")
    lines.append(f"  Cambio:   {emoji} {sign}${diff_total:,.0f} ({sign}{pct_total:.2f}%)")
    lines.append("─" * 35)
    return "\n".join(lines)


# ─── High-level API used by bot.py ──────────────────────────────────────────
def take_and_format(
    wallets: list[dict[str, Any]] | None,
    hyperlend: list[dict[str, Any]] | dict[str, Any] | None,
    bounce_tech: list[dict[str, Any]] | None = None,
    cycle_trade: dict[str, Any] | None = None,
) -> str:
    """Build+persist current snapshot, then return formatted delta block vs previous."""
    current = build_snapshot(wallets, hyperlend, bounce_tech, cycle_trade)
    previous = latest_snapshot()  # get previous BEFORE saving new
    save_snapshot(current)
    return format_delta_block(current, previous)
