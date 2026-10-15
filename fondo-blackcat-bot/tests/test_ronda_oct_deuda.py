"""R-RONDA-OCT #1 — debt carry (APR / interest) + RETIRO PERSONAL detection.

The PM debt moved 92.9K → 94.2K in 22 days and the bot could not say why.
These tests pin the two halves that explain it and the rules around them:
  * APR is read live from HL (USDC reserve, token 0) and NEVER assumed —
    when HL is unreadable the fallback is labelled CONFIG and the subsystem
    is degraded;
  * interest accrues on the observed debt over elapsed time and is
    persisted, with marks for "since last report" and "since cursor";
  * a RETIRO PERSONAL = HYPE spot buy → HYPE send to the HyperEVM bridge
    within a window, stored once per send hash with date, USDC borrowed,
    HYPE bought, HYPE withdrawn; shown in /reporte and in the delta block as
    "deuda: +X por retiro, +Y por interes";
  * retiros never touch PPC (matched buys are excluded from
    hype_acquisition) and the panel shows the cumulative HYPE withdrawn.

Mutation-checked by scripts/mutation_check_ronda_oct.py.
"""
from __future__ import annotations

import json
import os
import time

import pytest

W = "0xc7ae23316b47f7e75f455f53ad37873a18351505"
BRIDGE = "0x2222222222222222222222222222222222222222"
OTHER = "0x3333333333333333333333333333333333333333"


@pytest.fixture
def dbs(monkeypatch, tmp_path):
    """Point every module at a throwaway intel_memory.db."""
    db = str(tmp_path / "intel_memory.db")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from modules import debt_carry as dc, retiros_personales as rp, health_registry as hr
    monkeypatch.setattr(dc, "DB_PATH", db)
    monkeypatch.setattr(rp, "DB_PATH", db)
    monkeypatch.setattr(hr, "DB_PATH", db)
    monkeypatch.setattr(rp, "PM_PRIMARY_WALLET", W)
    dc._apr_cache.update(ts=0.0, apr=None, source="none", error=None)
    hr.reset_all() if hasattr(hr, "reset_all") else None
    return {"dc": dc, "rp": rp, "hr": hr, "db": db}


# ─── 1a. APR ────────────────────────────────────────────────────────────────

def test_apr_is_read_from_hl_usdc_reserve_token_0(dbs, monkeypatch):
    dc = dbs["dc"]
    seen = {}

    def fake_state(token):
        seen["token"] = token
        return {"borrowYearlyRate": "0.05", "supplyYearlyRate": "0.0349", "ltv": "0.0"}

    monkeypatch.setattr("modules.hl_borrow_lend.get_reserve_state", fake_state)
    info = dc.get_usdc_borrow_apr(force=True)
    assert seen["token"] == 0
    assert info["apr"] == pytest.approx(0.05)
    assert info["source"] == "hl_live"
    assert dc.apr_source_label(info) == "HL live"


def test_apr_percent_form_is_normalised(dbs, monkeypatch):
    dc = dbs["dc"]
    monkeypatch.setattr("modules.hl_borrow_lend.get_reserve_state",
                        lambda t: {"borrowYearlyRate": "7.0"})
    info = dc.get_usdc_borrow_apr(force=True)
    assert info["apr"] == pytest.approx(0.07) and info["source"] == "hl_live"


def test_apr_unreadable_falls_back_to_config_label_and_degrades(dbs, monkeypatch):
    dc, hr = dbs["dc"], dbs["hr"]
    monkeypatch.setattr("modules.hl_borrow_lend.get_reserve_state", lambda t: None)
    info = dc.get_usdc_borrow_apr(force=True)
    assert info["source"] == "config"
    assert info["apr"] == pytest.approx(dc._APR_FALLBACK)
    assert dc.apr_source_label(info).startswith("CONFIG — HL no leido")
    assert hr.is_degraded("debt_carry")
    # ... and the panel line carries the CONFIG label next to the number.
    snap = dc.snapshot(94_200.0)
    line = dc.panel_line(snap)
    assert "CONFIG" in line and "5.00%" in line
    assert snap["ok"] is False


def test_apr_cache_serves_last_live_value_as_hl_cache(dbs, monkeypatch):
    dc = dbs["dc"]
    monkeypatch.setattr("modules.hl_borrow_lend.get_reserve_state",
                        lambda t: {"borrowYearlyRate": "0.07"})
    dc.get_usdc_borrow_apr(force=True)
    monkeypatch.setattr("modules.hl_borrow_lend.get_reserve_state", lambda t: None)
    info = dc.get_usdc_borrow_apr(force=True)
    assert info["source"] == "hl_cache" and info["apr"] == pytest.approx(0.07)
    assert dc.apr_source_label(info).startswith("HL cache")


# ─── 1a. accrual ────────────────────────────────────────────────────────────

def _live(monkeypatch, apr="0.05"):
    monkeypatch.setattr("modules.hl_borrow_lend.get_reserve_state",
                        lambda t: {"borrowYearlyRate": apr})


def test_accrual_is_debt_times_apr_times_elapsed_time(dbs, monkeypatch):
    dc = dbs["dc"]
    _live(monkeypatch)
    t0 = 1_700_000_000_000
    acc = dc.observe(94_200.0, now_ms=t0)
    assert acc["accrued_total"] == pytest.approx(0.0)      # first tick seeds
    acc = dc.observe(94_200.0, now_ms=t0 + 86_400_000, force=True)
    assert acc["accrued_total"] == pytest.approx(94_200.0 * 0.05 / 365.0, rel=1e-6)
    # 22 days on 92.9K at 5% ≈ $280 — the number BCD estimated by hand.
    acc = dc.observe(92_900.0, now_ms=t0 + 86_400_000 * 2, force=True)
    acc = dc.observe(92_900.0, now_ms=t0 + 86_400_000 * 24, force=True)
    assert acc["accrued_total"] == pytest.approx(
        94_200.0 * 0.05 / 365.0 + 94_200.0 * 0.05 / 365.0 + 92_900.0 * 0.05 * 22 / 365.0, rel=1e-6)


def test_accrual_uses_the_debt_observed_at_the_previous_tick(dbs, monkeypatch):
    dc = dbs["dc"]
    _live(monkeypatch)
    t0 = 1_700_000_000_000
    dc.observe(100_000.0, now_ms=t0)
    acc = dc.observe(1.0, now_ms=t0 + 86_400_000 * 10, force=True)
    assert acc["accrued_total"] == pytest.approx(100_000.0 * 0.05 * 10 / 365.0, rel=1e-6)


def test_min_tick_interval_skips_churn_but_force_does_not(dbs, monkeypatch):
    dc = dbs["dc"]
    _live(monkeypatch)
    t0 = 1_700_000_000_000
    dc.observe(50_000.0, now_ms=t0)
    acc = dc.observe(50_000.0, now_ms=t0 + 1000)
    assert int(acc["ticks"]) == 1 and int(acc["last_tick_ms"]) == t0
    acc = dc.observe(50_000.0, now_ms=t0 + 1000, force=True)
    assert int(acc["ticks"]) == 2


def test_marks_split_since_report_and_since_cursor(dbs, monkeypatch):
    dc = dbs["dc"]
    _live(monkeypatch)
    t0 = 1_700_000_000_000
    day = 86_400_000
    dc.observe(73_000.0, now_ms=t0)                       # seeds both marks at 0
    dc.observe(73_000.0, now_ms=t0 + 10 * day, force=True)  # +100
    dc.set_mark(dc.MARK_LAST_REPORT, now_ms=t0 + 10 * day)
    dc.observe(73_000.0, now_ms=t0 + 13 * day, force=True)  # +30
    snap = dc.snapshot(73_000.0, observe_first=False)
    assert snap["since_report"] == pytest.approx(30.0, rel=1e-6)
    assert snap["since_cursor"] == pytest.approx(130.0, rel=1e-6)
    assert snap["per_day"] == pytest.approx(10.0, rel=1e-6)
    dc.reset_cursor(73_000.0)
    assert dc.snapshot(73_000.0, observe_first=False)["since_cursor"] == pytest.approx(0.0)
    line = dc.panel_line(snap)
    assert "APR 5.00% (HL live)" in line and "$10.00/dia" in line
    assert "+$30.00" in line and "+$130.00" in line


def test_gap_longer_than_48h_is_flagged_on_the_line(dbs, monkeypatch):
    dc = dbs["dc"]
    _live(monkeypatch)
    old = int(time.time() * 1000) - 3 * 86_400_000
    dc.observe(10_000.0, now_ms=old)
    snap = dc.snapshot(10_000.0, observe_first=False)
    assert snap["gap_flag"] is True
    assert "hueco" in dc.panel_line(snap)


def test_snapshot_marks_subsystem_ok_when_hl_live(dbs, monkeypatch):
    dc, hr = dbs["dc"], dbs["hr"]
    _live(monkeypatch)
    dc.snapshot(1000.0)
    st = hr.status("debt_carry")
    assert st.get("ok") is True and st.get("last_ok_utc")


# ─── 1b. retiros: detection primitives ─────────────────────────────────────

def _send(ts, amount, *, dest=BRIDGE, token="HYPE", user=W, h=None):
    return {"time": ts, "hash": h or f"0x{ts:x}",
            "delta": {"type": "send", "user": user, "destination": dest,
                      "sourceDex": "spot", "destinationDex": "spot", "token": token,
                      "amount": str(amount), "usdcValue": str(amount * 80), "fee": "0.0"}}


def _buy(ts, sz, px, *, side="B", coin="@107", tid=None, fee="0.0", fee_token="HYPE"):
    return {"time": ts, "coin": coin, "side": side, "sz": str(sz), "px": str(px),
            "tid": tid or ts, "fee": fee, "feeToken": fee_token,
            "dir": "Buy" if side == "B" else "Sell"}


SPOT = {"@107": "HYPE"}


def test_bridge_sends_filters_hype_to_bridge_from_wallet_only(dbs):
    rp = dbs["rp"]
    ups = [
        _send(10, 12.53),
        _send(11, 5.0, dest=OTHER),                       # not the bridge
        _send(12, 5.0, token="USDC"),                     # not HYPE
        _send(13, 5.0, user=OTHER),                       # inbound / other wallet
        {"time": 14, "hash": "0xw", "delta": {"type": "withdraw", "usdc": "100"}},
        _send(15, 0, h="0xzero"),                         # zero amount
    ]
    out = rp.bridge_sends(ups, W)
    assert [s["ts_ms"] for s in out] == [10]
    assert out[0]["amount"] == pytest.approx(12.53)


def test_match_buys_pairs_buys_inside_window_before_the_send(dbs):
    rp = dbs["rp"]
    t_send = 1_000_000_000_000
    fills = [
        _buy(t_send - 60_000, 12.53, 79.767, tid=1, fee="0.0084"),
        _buy(t_send - rp.MATCH_WINDOW_MS - 1, 12.0, 70.0, tid=2),   # outside window
        _buy(t_send + 1, 12.0, 70.0, tid=3),                         # after the send
        _buy(t_send - 30_000, 1.0, 80.0, side="A", tid=4),           # a sell
        _buy(t_send - 30_000, 1.0, 80.0, coin="BTC", tid=5),         # not HYPE
    ]
    used: set[str] = set()
    m = rp.match_buys({"ts_ms": t_send, "amount": 12.53}, fills, SPOT, used=used)
    assert m["kind"] == rp.KIND_BORROW_BUY_WITHDRAW
    assert m["hype_bought"] == pytest.approx(12.53)
    assert m["usdc"] == pytest.approx(12.53 * 79.767)
    assert m["fee_usdc"] == pytest.approx(0.0084 * 79.767)
    assert m["keys"] == ["tid:1"] and used == {"tid:1"}


def test_match_buys_rejects_quantity_outside_tolerance(dbs):
    rp = dbs["rp"]
    t = 1_000_000_000_000
    fills = [_buy(t - 1000, 5.0, 80.0, tid=1)]
    m = rp.match_buys({"ts_ms": t, "amount": 12.53}, fills, SPOT, used=set())
    assert m["kind"] == rp.KIND_WITHDRAW_ONLY and m["usdc"] == 0.0 and m["keys"] == []


def test_match_buys_never_reuses_a_fill_for_two_sends(dbs):
    rp = dbs["rp"]
    t = 1_000_000_000_000
    fills = [_buy(t - 1000, 12.0, 80.0, tid=1)]
    used: set[str] = set()
    m1 = rp.match_buys({"ts_ms": t, "amount": 12.0}, fills, SPOT, used=used)
    m2 = rp.match_buys({"ts_ms": t + 5, "amount": 12.0}, fills, SPOT, used=used)
    assert m1["kind"] == rp.KIND_BORROW_BUY_WITHDRAW
    assert m2["kind"] == rp.KIND_WITHDRAW_ONLY


# ─── 1b. retiros: scan + ledger ─────────────────────────────────────────────

def _fake_chain(monkeypatch, rp, updates, fills):
    calls = {"ledger": 0, "fills": 0}

    def fake_updates(wallet, start_ms):
        calls["ledger"] += 1
        return [u for u in updates if u["time"] >= start_ms]

    def fake_fills(wallet, a, b):
        calls["fills"] += 1
        return [f for f in fills if a <= f["time"] <= b]

    monkeypatch.setattr(rp, "_fetch_ledger_updates", fake_updates)
    monkeypatch.setattr(rp, "_fetch_fills", fake_fills)
    monkeypatch.setattr(rp, "_spot_map", lambda: SPOT)
    return calls


def test_scan_stores_the_proof_case_once_with_all_fields(dbs, monkeypatch):
    """The 1,000 USDC withdrawal of October (on-chain: 2026-10-14 14:15/14:16
    UTC — buy 12.53 HYPE @ 79.767 on @107, send 12.53 HYPE to the bridge)."""
    rp = dbs["rp"]
    t_buy, t_send = 1_791_987_300_000, 1_791_987_360_000   # 2026-10-14 14:15 / 14:16 UTC
    now = t_send + 86_400_000
    calls = _fake_chain(monkeypatch, rp,
                        [_send(t_send, 12.53, h="0x031c57cf")],
                        [_buy(t_buy, 12.53, 79.767, tid=42, fee="0.00842015")])
    res = rp.scan(W, now_ms=now)
    assert res == {"ok": True, "new": 1, "seen": 1, "wallet": W,
                   "from_ms": now - rp.BACKFILL_DAYS * 86_400_000}
    rows = rp.all_rows(W)
    assert len(rows) == 1
    r = rows[0]
    assert r["send_hash"] == "0x031c57cf" and r["ts_ms"] == t_send
    assert r["kind"] == rp.KIND_BORROW_BUY_WITHDRAW
    assert r["hype_bought"] == pytest.approx(12.53)
    assert r["hype_withdrawn"] == pytest.approx(12.53)
    assert r["usdc_borrowed"] == pytest.approx(12.53 * 79.767 + 0.00842015 * 79.767, rel=1e-6)
    assert r["buy_px"] == pytest.approx(79.767)
    assert json.loads(r["fill_keys"]) == ["tid:42"]
    assert r["destino"] == BRIDGE
    # Second scan: nothing new, no duplicate, cursor moved past the send.
    res2 = rp.scan(W, now_ms=now)
    assert res2["new"] == 0 and len(rp.all_rows(W)) == 1
    assert int(rp._meta_get(f"{rp.META_SCAN_CURSOR}:{W}")) == t_send
    assert calls["fills"] == 1          # fills fetched only when a send is new
    assert "2026-10-14 14:16 UTC" in rp.render_command()


def test_scan_records_a_bridge_send_without_buy_as_withdraw_only(dbs, monkeypatch):
    rp = dbs["rp"]
    t = 1_792_030_560_000
    _fake_chain(monkeypatch, rp, [_send(t, 20.0)], [])
    rp.scan(W, now_ms=t + 1)
    r = rp.all_rows(W)[0]
    assert r["kind"] == rp.KIND_WITHDRAW_ONLY and r["usdc_borrowed"] == 0.0
    assert r["hype_withdrawn"] == pytest.approx(20.0)
    assert "sin compra previa" in rp.format_row(r)


def test_scan_raises_and_degrades_when_the_chain_is_unreadable(dbs, monkeypatch):
    rp, hr = dbs["rp"], dbs["hr"]

    def boom(wallet, start_ms):
        raise RuntimeError("HL 500")

    monkeypatch.setattr(rp, "_fetch_ledger_updates", boom)
    with pytest.raises(RuntimeError):
        rp.scan(W, now_ms=1_792_030_560_000)
    assert hr.is_degraded("retiros")
    assert rp.all_rows(W) == []


def test_totals_windows_and_report_cursor(dbs, monkeypatch):
    rp = dbs["rp"]
    t1, t2 = 1_790_000_000_000, 1_792_000_000_000
    _fake_chain(monkeypatch, rp,
                [_send(t1, 11.64, h="0xa"), _send(t2, 12.53, h="0xb")],
                [_buy(t1 - 1000, 11.64, 94.434, tid=1), _buy(t2 - 1000, 12.53, 79.767, tid=2)])
    rp.scan(W, now_ms=t2 + 1)
    t = rp.totals(rp.all_rows(W))
    assert t["n"] == 2 and t["hype_withdrawn"] == pytest.approx(24.17)
    assert rp.usdc_borrowed_between(t1, t2 + 1, W) == pytest.approx(12.53 * 79.767)
    assert rp.usdc_borrowed_between(0, t1 - 1, W) == 0.0
    assert rp.excluded_fill_keys(W) == {"tid:1", "tid:2"}
    assert rp.get_report_cursor() == 0
    rp.set_report_cursor(t1)
    assert rp.get_report_cursor() == t1
    sec = rp.render_section(t1, t2 + 1, W)
    assert "RETIRO PERSONAL" in sec and "12.53 HYPE" in sec and "11.64 HYPE" not in sec.split("Acumulado")[0]
    assert "Acumulado: 2 retiros" in sec
    assert "no tocan PPC, track record ni tesis" in sec


# ─── 1b. retiros never touch PPC ───────────────────────────────────────────

def test_ppc_excludes_retiro_buys_and_counts_them(dbs, monkeypatch):
    from modules import hype_acquisition as ha
    rp = dbs["rp"]
    t = 1_792_030_560_000
    _fake_chain(monkeypatch, rp, [_send(t, 12.53, h="0xb")], [_buy(t - 1000, 12.53, 79.767, tid=77)])
    rp.scan(W, now_ms=t + 1)
    fills = [
        _buy(t - 10 * 86_400_000, 100.0, 50.0, tid=1),
        _buy(t - 1000, 12.53, 79.767, tid=77),            # the retiro buy
    ]
    monkeypatch.setattr(ha, "_fetch_fills", lambda w: fills)
    monkeypatch.setattr(ha, "_live_hype_balance", lambda w: 100.0)   # 12.53 already left
    monkeypatch.setattr(ha, "_resolve_spot_map", lambda: SPOT)
    monkeypatch.setattr(ha, "get_ppc_override", lambda: None)
    acq = ha.compute_hype_acquisition(W, spot_map=SPOT)
    assert acq.known is True
    assert acq.ppc_usd == pytest.approx(50.0)
    assert acq.buy_qty == pytest.approx(100.0)
    assert acq.retiros_fills_excluidos == 1
    assert acq.retiros_hype_excluido == pytest.approx(12.53)


def test_ppc_without_exclusion_would_have_moved(dbs, monkeypatch):
    """Control: the same fills WITHOUT a retiro row move the PPC — proving the
    exclusion is what keeps it at $50."""
    from modules import hype_acquisition as ha
    t = 1_792_030_560_000
    fills = [_buy(t - 10 * 86_400_000, 100.0, 50.0, tid=1), _buy(t - 1000, 12.53, 79.767, tid=77)]
    monkeypatch.setattr(ha, "_fetch_fills", lambda w: fills)
    monkeypatch.setattr(ha, "_live_hype_balance", lambda w: 112.53)
    monkeypatch.setattr(ha, "_resolve_spot_map", lambda: SPOT)
    acq = ha.compute_hype_acquisition(W, spot_map=SPOT)
    assert acq.known is True and acq.ppc_usd > 50.0 and acq.retiros_fills_excluidos == 0


def test_track_record_and_thesis_do_not_import_retiros():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for rel in ("modules/track_record.py", "modules/analysis.py", "fund_state.py",
                "modules/trade_ledger.py"):
        assert "retiros_personales" not in (root / rel).read_text(encoding="utf-8"), rel


# ─── panel + delta block ───────────────────────────────────────────────────

def test_pm_panel_prints_withdrawn_stack_and_carry_line(dbs, monkeypatch):
    from modules.portfolio_margin import compute_pm_state, format_pm_state_telegram
    pm = compute_pm_state(
        [{"coin": "HYPE", "total": 2971.4}, {"coin": "USDC", "total": -94_200.0, "borrowed": 94_200.0}],
        [], {"HYPE": 78.19}, ltv_map={"HYPE": 0.65},
    )
    carry = {"apr": 0.05, "apr_label": "HL live", "per_day": 12.9,
             "since_report": 280.1, "since_cursor": 300.0,
             "cursor_ts_ms": 1_790_000_000_000, "gap_flag": False}
    out = format_pm_state_telegram(pm, carry=carry, retiros_hype=24.17, retiros_n=2)
    assert "retiros personales acumulados −24.17 HYPE (2 retiros)" in out
    assert "Carry deuda: APR 5.00% (HL live)" in out
    assert "+$280.10" in out and "+$300.00" in out
    # unchanged when the caller has nothing to pass
    plain = format_pm_state_telegram(pm)
    assert "Carry deuda" not in plain and "retiros personales" not in plain


def test_delta_block_attributes_debt_move_to_retiro_and_interest():
    from modules.report_delta import format_report_delta_block, debt_attribution_line
    prev = {"ts": "2026-09-22T14:00:00+00:00", "pm_debt": 92_900.0}
    curr = {"ts": "2026-10-15T13:00:00+00:00", "pm_debt": 94_192.0}
    block = format_report_delta_block(curr, prev, attribution={"retiro": 1000.15, "interes": 280.0})
    assert "PM deuda" in block
    assert "deuda: +1,000 por retiro, +280 por interes, +12 sin atribuir" in block
    # residual under $1 is not printed
    assert "sin atribuir" not in debt_attribution_line(100.0, 1100.5, {"retiro": 1000.0, "interes": 0.5})
    # without attribution the block is the classic one
    assert "por retiro" not in format_report_delta_block(curr, prev)
    assert debt_attribution_line(None, 1.0, {"retiro": 1.0}) is None
    assert debt_attribution_line(1.0, 2.0, None) is None
    assert debt_attribution_line(1.0, 2.0, {"retiro": None, "interes": None}) is None


def test_registry_declares_both_subsystems_as_money():
    from modules.health_registry import SUBSYSTEMS
    assert SUBSYSTEMS["debt_carry"]["money"] is True
    assert SUBSYSTEMS["retiros"]["money"] is True


def test_reporte_wiring_present():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8")
    assert "_rp.render_section(_rp_prev, _rp_now)" in src
    assert "_rp.set_report_cursor(_rp_now)" in src
    assert "format_report_delta_block(_delta_kpis, _prev_kpis, attribution=_attrib)" in src
    assert "_carry_mark(MARK_LAST_REPORT" in src
    assert "carry=_carry_rep, retiros=_ret_rep" in src
    # periodic scan rides ledger_sync; PM alert job ticks the accrual
    assert "RETIROS periodic scan" in src
    assert "_carry_observe(getattr(pm, \"debt_usd\", None))" in src
    from commands_registry import COMMANDS
    names = {c.command for c in COMMANDS}
    assert {"retiros", "carrycursor", "atribucion"} <= names
