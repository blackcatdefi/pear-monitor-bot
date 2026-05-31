"""R-VAULTDEP evolution tracking + dashboard vault card regression tests.

Covers:
  1. Daily snapshot persistence (one row per vault) and same-day dedupe.
  2. Evolution line — all-time-only when no prior snapshot exists.
  3. Evolution line — all-time + delta vs the PRIOR-DAY snapshot.
  4. 'ayer' vs explicit-date label for the prior snapshot.
  5. Robustness — never raises on a broken DB / no found deposits.
  6. Dashboard _render_html shows the vault card (label/equity/cost/PnL%)
     when vault_deposits_detail is populated, and the total still folds into
     the TOTAL EQUITY headline.
  7. Dashboard renders cleanly (no card, no crash) with no vault detail.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from modules import vault_history as vh  # noqa: E402


@dataclass
class _Dep:
    """Minimal duck-typed stand-in for modules.vault_deposits.VaultDeposit."""

    label: str
    vault_address: str
    equity_usd: float
    cost_basis_usd: float
    pnl_usd: float
    found: bool = True
    locked_until_ts: int = 0


def _dep(equity: float, cost: float = 5000.0, found: bool = True) -> _Dep:
    return _Dep(
        label="Systemic Strategies HyperGrowth",
        vault_address="0xd6e56265890b76413d1d527eb9b75e334c0c5b42",
        equity_usd=equity,
        cost_basis_usd=cost,
        pnl_usd=equity - cost,
        found=found,
    )


def _day(d: int) -> datetime:
    return datetime(2026, 5, d, 12, 0, tzinfo=timezone.utc)


# ── 1. persistence + same-day dedupe ────────────────────────────────────────

def test_snapshot_persists_and_same_day_dedupes(tmp_path):
    db = str(tmp_path / "vh.db")
    # Two writes the SAME day → 1 row, latest equity wins.
    assert vh.record_vault_snapshot([_dep(5050.0)], now=_day(30), db_path=db) == 1
    assert vh.record_vault_snapshot([_dep(5073.0)], now=_day(30), db_path=db) == 1

    import sqlite3

    with sqlite3.connect(db) as c:
        rows = c.execute(
            "SELECT snap_date, equity_usd FROM vault_snapshots"
        ).fetchall()
    assert len(rows) == 1, "same-day writes must dedupe to one row"
    assert rows[0][0] == "2026-05-30"
    assert abs(rows[0][1] - 5073.0) < 1e-6, "latest same-day equity wins"

    # A different day adds a second row.
    vh.record_vault_snapshot([_dep(5100.0)], now=_day(31), db_path=db)
    with sqlite3.connect(db) as c:
        n = c.execute("SELECT COUNT(*) FROM vault_snapshots").fetchone()[0]
    assert n == 2


def test_unfound_or_zero_equity_not_persisted(tmp_path):
    db = str(tmp_path / "vh.db")
    assert vh.record_vault_snapshot([_dep(5000.0, found=False)], now=_day(30),
                                    db_path=db) == 0
    assert vh.record_vault_snapshot([_dep(0.0)], now=_day(30), db_path=db) == 0


# ── 2. evolution line: all-time only when no prior snapshot ─────────────────

def test_evolution_line_all_time_only_no_prior(tmp_path):
    db = str(tmp_path / "vh.db")
    line = vh.format_vault_evolution_line(_dep(5073.0), now=_day(30), db_path=db)
    assert "all-time" in line
    assert "+$73" in line
    assert "+1.46%" in line  # 73/5000 = 1.46%
    assert "vs" not in line, "no prior snapshot → no 'vs' delta segment"


# ── 3. evolution line: all-time + vs prior-day snapshot ─────────────────────

def test_evolution_line_with_prior_day_delta(tmp_path):
    db = str(tmp_path / "vh.db")
    # Seed yesterday (29th) at $5,061, then read today (30th) at $5,073.
    vh.record_vault_snapshot([_dep(5061.0)], now=_day(29), db_path=db)
    line = vh.format_vault_evolution_line(_dep(5073.0), now=_day(30), db_path=db)
    assert "all-time" in line
    assert "+$12 vs ayer" in line, f"expected '+$12 vs ayer' in: {line}"


def test_prior_label_uses_date_when_gap_gt_one_day(tmp_path):
    db = str(tmp_path / "vh.db")
    vh.record_vault_snapshot([_dep(5061.0)], now=_day(27), db_path=db)
    line = vh.format_vault_evolution_line(_dep(5073.0), now=_day(30), db_path=db)
    assert "vs 2026-05-27" in line, f"expected explicit date in: {line}"
    assert "ayer" not in line


# ── 4. block records baseline + renders, using prior-day delta ──────────────

def test_block_records_then_reads_prior_day(tmp_path):
    db = str(tmp_path / "vh.db")

    class _Res:
        ok = True
        deposits = [_dep(5061.0)]

    # Day 1: no prior → all-time only, and it records the baseline.
    b1 = vh.format_vault_evolution_block(_Res(), now=_day(29), db_path=db)
    _dep_line = [ln for ln in b1.splitlines() if "HyperGrowth" in ln][0]
    assert "all-time" in _dep_line and "vs ayer" not in _dep_line

    class _Res2:
        ok = True
        deposits = [_dep(5073.0)]

    # Day 2: prior-day baseline exists → delta vs ayer.
    b2 = vh.format_vault_evolution_block(_Res2(), now=_day(30), db_path=db)
    assert "+$12 vs ayer" in b2


# ── 5. robustness — never raises ────────────────────────────────────────────

def test_record_never_raises_on_bad_path():
    # An impossible directory must degrade to 0, not raise.
    assert vh.record_vault_snapshot(
        [_dep(5000.0)], db_path="/proc/cannot/write/vh.db"
    ) == 0


def test_block_empty_when_no_found_deposits(tmp_path):
    db = str(tmp_path / "vh.db")

    class _Res:
        ok = True
        deposits = [_dep(5000.0, found=False)]

    assert vh.format_vault_evolution_block(_Res(), db_path=db) == ""
    assert vh.format_vault_evolution_block(None, db_path=db) == ""


# ── 6/7. dashboard vault card render ─────────────────────────────────────────

def _base_state() -> dict:
    """Minimal flat state dict accepted by dashboard._render_html."""
    return {
        "ts": "2026-05-30 12:00 UTC",
        "capital_total": 35_000.0,
        "hl_collateral_total": 0.0,
        "hl_debt_total": 0.0,
        "perp_equity_total": 5_000.0,
        "spot_usd_total": 0.0,
        "spot_stables_total": 0.0,
        "pear_staked_total": 0.0,
        "vault_deposits_total": 5_073.0,
        "vault_deposits_detail": [],
        "upnl_perp_total": 0.0,
        "main_flywheel": {"short": "0xA…F", "hf": 1.4, "collateral_balance": 0.0,
                          "collateral_symbol": "kHYPE", "collateral_usd": 0.0,
                          "debt_balance": 0.0, "debt_symbol": "UETH",
                          "debt_usd": 0.0},
        "secondary_flywheel": {"short": "0xC…B", "hf": 2.0,
                               "collateral_balance": 0.0,
                               "collateral_symbol": "UBTC", "collateral_usd": 0.0,
                               "debt_balance": 0.0, "debt_symbol": "USDT0",
                               "debt_usd": 0.0},
        "basket_state": {"wallets": {}, "summary": {}},
        "basket_positions": [],
        "basket_upnl": 0.0,
        "basket_notional": 0.0,
        "btc": 96_500.0, "eth": 1_800.0, "hype": 28.5,
        "fg_value": 60, "fg_label": "Greed",
        "wallets": [],
        "upcoming": [],
        "snap_age_sec": 10.0,
        "is_fresh": True,
        "last_error": None,
        "cached_prices": {},
        "spot_tokens": [],
    }


def test_dashboard_renders_vault_card_with_breakdown():
    from modules import dashboard as dash

    state = _base_state()
    state["vault_deposits_detail"] = [{
        "label": "Systemic Strategies HyperGrowth",
        "vault_address": "0xd6e5",
        "equity_usd": 5_073.0,
        "cost_basis_usd": 5_000.0,
        "pnl_usd": 73.0,
        "pnl_pct": 1.46,
        "found": True,
        "locked_until_ts": 0,
        "has_prev": True,
        "prev_label": "ayer",
        "delta_prev_usd": 12.0,
        "delta_prev_pct": 0.24,
    }]
    html = dash._render_html(state)
    assert "Vault Deposits (HL)" in html
    assert "Systemic Strategies HyperGrowth" in html
    assert "Cost basis" in html
    assert "1.46%" in html
    assert "vs ayer" in html
    # Total still folds into the TOTAL EQUITY headline.
    assert "TOTAL EQUITY" in html


def test_dashboard_renders_without_vault_detail():
    from modules import dashboard as dash

    html = dash._render_html(_base_state())  # empty detail
    # No card emitted, and the page renders fine.
    assert "<h2>Vault Deposits (HL)</h2>" not in html
    assert "TOTAL EQUITY" in html
