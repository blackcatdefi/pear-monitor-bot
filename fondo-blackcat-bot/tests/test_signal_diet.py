"""R-SIGNAL-DIET (2026-07-20) — regression lock for the noise purge + the new
5/5 GO entry alert engine.

Locks, permanently:
  1. Noise sources DELETED: DCA price-zone pushes, heartbeat push, source-flap
     Telegram sends, btc_dca_63_65 + btc_near_dreamcash_liq kill triggers.
  2. /health exists (on-demand replacement for the heartbeat push).
  3. go_alerts diff engine: seeding is silent; a 4/5→5/5 crossing fires exactly
     ONE compact push; cooldown (<6h out) suppresses re-alert; ≥6h out re-alerts;
     >5 new GOs in one run collapse into ONE grouped message; screener failures
     stay silent until the 3rd consecutive, which pushes exactly once.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from modules import go_alerts  # noqa: E402
from modules.go_alerts import diff_go_set, extract_go_keys  # noqa: E402


# ─── 1. Noise sources are dead ───────────────────────────────────────────────

def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), "r", encoding="utf-8") as fh:
        return fh.read()


def test_dca_push_path_deleted():
    from modules import alerts
    assert not hasattr(alerts, "_run_dca_zone_alerts")
    assert not hasattr(alerts, "_dca_alerted_within_window")
    src = _read("modules/alerts.py")
    # Live f-string/literal (docstrings documenting the removal are fine).
    assert re.search(r"""["']\[DCA ALERT\]""", src) is None
    assert "from fund_state import BCD_DCA_PLAN" not in src


def test_heartbeat_push_deleted_health_on_demand():
    from modules import heartbeat
    assert not hasattr(heartbeat, "send_heartbeat")
    assert hasattr(heartbeat, "build_heartbeat")
    bot_src = _read("bot.py")
    # No scheduled heartbeat job remains…
    assert not re.search(r"add_job\(\s*_heartbeat_job", bot_src)
    assert "_heartbeat_job" not in bot_src.replace(
        "# R-SIGNAL-DIET (2026-07-20): _heartbeat_job ELIMINADO", "")
    # …and the on-demand /health command is wired.
    assert "async def cmd_health" in bot_src
    assert '"health": cmd_health,' in bot_src


def test_health_registered_in_commands_registry():
    import commands_registry as cr
    entries = [c for c in cr.COMMANDS if c.command == "health"]
    assert len(entries) == 1
    assert entries[0].handler_name == "cmd_health"


def test_flap_reports_log_only():
    bot_src = _read("bot.py")
    m = re.search(r"async def _selftest_cron_job.*?(?=\nasync def |\ndef )",
                  bot_src, re.DOTALL)
    assert m, "_selftest_cron_job not found"
    body = m.group(0)
    assert "send_bot_message" not in body
    assert "log-only" in body or "log.warning" in body


def test_go_alerts_job_registered():
    bot_src = _read("bot.py")
    assert "async def _go_alerts_job" in bot_src
    assert re.search(r"add_job\(\s*_go_alerts_job", bot_src)


# ─── 2. diff engine (pure) ───────────────────────────────────────────────────

_H = 3600.0


def test_seeding_first_run_is_silent():
    to_alert, st = diff_go_set({"SHORT:WLD", "LONG:ETH"}, {}, now=1000.0)
    assert to_alert == []
    assert st["seeded"] is True
    assert st["tokens"]["SHORT:WLD"]["in_go"] is True


def test_crossing_4of5_to_5of5_fires_once():
    _, st = diff_go_set({"SHORT:WLD"}, {}, now=0.0)          # seed
    to_alert, st = diff_go_set({"SHORT:WLD", "SHORT:STRK"}, st, now=60.0)
    assert to_alert == ["SHORT:STRK"]                         # exactly one
    # Same set next run → nothing.
    to_alert, st = diff_go_set({"SHORT:WLD", "SHORT:STRK"}, st, now=120.0)
    assert to_alert == []


def test_cooldown_suppresses_fast_reentry():
    _, st = diff_go_set(set(), {}, now=0.0)                   # seed empty
    to_alert, st = diff_go_set({"SHORT:ZRO"}, st, now=10.0)   # enters → alert
    assert to_alert == ["SHORT:ZRO"]
    _, st = diff_go_set(set(), st, now=20.0)                  # leaves
    # Re-enters after only 2h out → suppressed.
    to_alert, st = diff_go_set({"SHORT:ZRO"}, st, now=20.0 + 2 * _H)
    assert to_alert == []
    _, st = diff_go_set(set(), st, now=30.0 + 2 * _H)         # leaves again
    # Re-enters after 7h out → re-alerts.
    to_alert, st = diff_go_set({"SHORT:ZRO"}, st, now=30.0 + 9 * _H)
    assert to_alert == ["SHORT:ZRO"]


def test_sides_are_independent_keys():
    _, st = diff_go_set({"SHORT:AVA"}, {}, now=0.0)
    to_alert, _ = diff_go_set({"SHORT:AVA", "LONG:AVA"}, st, now=60.0)
    assert to_alert == ["LONG:AVA"]


# ─── 3. extract_go_keys ──────────────────────────────────────────────────────

class _Row:
    def __init__(self, ticker, go=False):
        self.ticker = ticker
        self.is_go_candidate = go
        self.gate = None


class _Res:
    def __init__(self, ranked, long_context):
        self.ranked = ranked
        self.long_context = long_context


def test_extract_go_keys_both_sides():
    res = _Res(
        ranked=[_Row("WLD", go=True), _Row("OP", go=False)],
        long_context=[_Row("ETH")],
    )
    keys, rows = extract_go_keys(res)
    assert keys == {"SHORT:WLD", "LONG:ETH"}
    assert rows["SHORT:WLD"].ticker == "WLD"


# ─── 4. full cycle: one compact push / grouping / failure gate ───────────────

@pytest.fixture()
def _iso_state(tmp_path, monkeypatch):
    monkeypatch.setattr(go_alerts, "STATE_FILE", str(tmp_path / "go_state.json"))
    monkeypatch.setattr(go_alerts, "TELEGRAM_CHAT_ID", "12345")
    sent: list[str] = []

    async def _fake_send(bot, chat_id, msg, **kw):
        sent.append(msg)

    monkeypatch.setattr(go_alerts, "send_bot_message", _fake_send)
    return sent


def _patch_screen(monkeypatch, res):
    import modules.universal_screener as scr

    async def _fake(*a, **k):
        return res

    monkeypatch.setattr(scr, "compute_screen_cached", _fake)


def _patch_telemetry(monkeypatch):
    import modules.telemetry as tel

    async def _ctx():
        return {}

    async def _build(row, ctx_map, cache=None):
        return row  # passthrough sentinel

    monkeypatch.setattr(tel, "fetch_ctx_map", _ctx)
    monkeypatch.setattr(tel, "_safe_build_from_row", _build)
    monkeypatch.setattr(tel, "format_token_compact",
                        lambda t: f"[compact {t.ticker}]")


def test_cycle_one_compact_push_on_crossing(_iso_state, monkeypatch):
    sent = _iso_state
    _patch_telemetry(monkeypatch)
    _patch_screen(monkeypatch, _Res([_Row("WLD", go=True)], []))
    assert asyncio.run(go_alerts.run_go_alert_cycle(None)) == 0  # seed silent
    assert sent == []
    # STRK crosses 4/5 → 5/5.
    _patch_screen(monkeypatch,
                  _Res([_Row("WLD", go=True), _Row("STRK", go=True)], []))
    assert asyncio.run(go_alerts.run_go_alert_cycle(None)) == 1
    assert len(sent) == 1                                     # ONE message
    assert "SHORT STRK" in sent[0]
    assert "[compact STRK]" in sent[0]
    assert "WLD" not in sent[0].replace("[compact STRK]", "")  # only entrant


def test_cycle_groups_more_than_five(_iso_state, monkeypatch):
    sent = _iso_state
    _patch_screen(monkeypatch, _Res([], []))
    asyncio.run(go_alerts.run_go_alert_cycle(None))           # seed
    rows = [_Row(f"T{i}", go=True) for i in range(7)]
    _patch_screen(monkeypatch, _Res(rows, []))
    assert asyncio.run(go_alerts.run_go_alert_cycle(None)) == 1
    assert len(sent) == 1
    assert "REGIME FLIP" in sent[0]
    assert "7" in sent[0]


def test_cycle_failure_gate_pushes_on_third_only(_iso_state, monkeypatch):
    sent = _iso_state
    import modules.universal_screener as scr

    async def _boom(*a, **k):
        raise RuntimeError("HL down")

    monkeypatch.setattr(scr, "compute_screen_cached", _boom)
    assert asyncio.run(go_alerts.run_go_alert_cycle(None)) == 0
    assert sent == []                                         # 1st: silent
    assert asyncio.run(go_alerts.run_go_alert_cycle(None)) == 0
    assert sent == []                                         # 2nd: silent
    asyncio.run(go_alerts.run_go_alert_cycle(None))
    assert len(sent) == 1 and "3" in sent[0]                  # 3rd: ONE push
    asyncio.run(go_alerts.run_go_alert_cycle(None))
    assert len(sent) == 1                                     # 4th: silent
    # Success resets the counter.
    _patch_screen(monkeypatch, _Res([], []))
    asyncio.run(go_alerts.run_go_alert_cycle(None))
    monkeypatch.setattr(scr, "compute_screen_cached", _boom)
    asyncio.run(go_alerts.run_go_alert_cycle(None))
    assert len(sent) == 1                                     # counter reset
