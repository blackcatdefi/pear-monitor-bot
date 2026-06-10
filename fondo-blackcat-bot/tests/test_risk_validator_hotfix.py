"""R-RISK-VALIDATOR-HOTFIX — scheduler anti-pattern kill + real-risk replay tests.

Bug (found in the R-MARGIN-STRESS-HOTFIX live audit): bot.py registered
``lambda: asyncio.create_task(_risk_validator_job(application))`` on the
AsyncIOScheduler. Sync callables are dispatched to a thread-pool executor
where NO event loop runs, so ``asyncio.create_task`` raised
RuntimeError("no running event loop") every cycle — the job NEVER executed
in production. Same dead pattern: heartbeat, basket_close_detector and the
``_gated_broadcast`` wrapper (6 R18/R21 broadcast registrations).

Mandated acceptance tests:
  (a) scheduler registration executes WITHOUT RuntimeError (native async);
  (b) replay current production state (aave-HF ~1.50, BTC liq dist ~18%,
      SOL ~15%) → ZERO alerts;
  (c) synthetic HF crossing 1.20 → exactly ONE "observación" alert;
  (d) synthetic SOL liq-dist crossing below 12% → exactly ONE alert;
  (e) band transition + cooldown per the WI-3 state machine, no re-fire on
      restart (SQLite-persisted state).
Plus: drift-alert dedup at the new 5-min cadence (fingerprint + 6h cooldown,
persisted across restarts) and a source-level regression guard ensuring the
``lambda: asyncio.create_task`` anti-pattern never returns to bot.py.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import re

import pytest


@pytest.fixture
def am(monkeypatch, tmp_path):
    """alerts_margin with an isolated SQLite DB (same pattern as the WI-3 tests)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import config
    importlib.reload(config)
    import modules.alerts_margin as alerts_margin
    importlib.reload(alerts_margin)
    return alerts_margin


# ─── (a) scheduler registration runs without RuntimeError ──────────────────


def test_no_create_task_lambda_registrations_left() -> None:
    """Regression guard: the dead anti-pattern must NEVER return to bot.py."""
    import bot
    code_lines = [
        ln for ln in inspect.getsource(bot).splitlines()
        if not ln.lstrip().startswith("#")
    ]
    assert not re.search(r"lambda:\s*asyncio\.create_task", "\n".join(code_lines)), (
        "bot.py re-introduced a sync lambda + asyncio.create_task scheduler "
        "registration — AsyncIOScheduler runs sync callables in a thread-pool "
        "executor with no event loop, so the job silently dies with "
        "RuntimeError every cycle."
    )


def test_gated_broadcast_is_native_coroutine_and_runs() -> None:
    """_gated_broadcast must return an async callable AsyncIOScheduler can await."""
    import bot

    ran: list[str] = []

    async def _payload() -> None:
        ran.append("yes")

    runner = bot._gated_broadcast(_payload, lambda: True, "test_label")
    assert asyncio.iscoroutinefunction(runner), (
        "_gated_broadcast must return an async def — a sync wrapper is "
        "dispatched to the executor thread where create_task raises."
    )
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(runner())
    assert ran == ["yes"]

    # Gate closed → coroutine returns without dispatching.
    ran.clear()
    gated = bot._gated_broadcast(_payload, lambda: False, "test_label")
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(gated())
    assert ran == []


def test_risk_validator_job_executes_on_asyncio_scheduler_without_runtimeerror(
    monkeypatch, tmp_path
) -> None:
    """End-to-end: the job fires on a real AsyncIOScheduler with no RuntimeError."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from datetime import datetime, timezone

    import bot

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    executed: list[float] = []
    errors: list[BaseException] = []

    # Stub the validator internals: report enabled, zero failures.
    monkeypatch.setattr(bot, "r18_risk_check_report", lambda: "ok", raising=False)
    monkeypatch.setattr(bot, "r18_risk_check_enabled", lambda: True, raising=False)
    monkeypatch.setattr(bot, "_risk_validator_state_path",
                        lambda: str(tmp_path / "rv_state.json"))

    import modules.risk_config_validator as rcv
    monkeypatch.setattr(rcv, "run_checks", lambda: [])

    real_job = bot._risk_validator_job

    async def _tracked(app) -> None:
        try:
            await real_job(app)
            executed.append(1.0)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            raise

    async def _drive() -> None:
        sched = AsyncIOScheduler()
        sched.add_job(
            _tracked,
            "date",
            run_date=datetime.now(timezone.utc),
            args=[object()],
            id="risk_config_validator_test",
        )
        sched.start()
        for _ in range(40):  # up to 2s
            if executed or errors:
                break
            await asyncio.sleep(0.05)
        sched.shutdown(wait=False)

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_drive())
    assert not errors, f"risk_validator job raised: {errors!r}"
    assert executed, "risk_validator job never executed on the AsyncIOScheduler"


def test_old_sync_lambda_pattern_reproduces_the_bug() -> None:
    """Sanity: the OLD pattern really does die with RuntimeError off-loop.

    Documents the root cause: create_task from a thread with no running
    event loop (exactly what AsyncIOScheduler's executor does to sync jobs).
    """
    import concurrent.futures

    async def _noop() -> None:  # pragma: no cover - never reached
        pass

    def _old_style() -> None:
        asyncio.create_task(_noop())

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(_old_style)
        with pytest.raises(RuntimeError):
            fut.result(timeout=5)


# ─── (b) production replay → ZERO alerts ───────────────────────────────────


def test_replay_production_state_zero_alerts(am) -> None:
    """aave-HF ~1.50 + BTC liq dist ~18% + SOL ~15% → nothing fires."""
    should, msg = am.evaluate_pm_hf(1.50, now=1_000.0)
    assert should is False and msg == ""
    should, msg = am.evaluate_position_liq_distance("BTC", 18.0, now=1_000.0)
    assert should is False and msg == ""
    should, msg = am.evaluate_position_liq_distance("SOL", 15.0, now=1_000.0)
    assert should is False and msg == ""
    # Repeated polls (the 5-min cadence) stay silent.
    for t in (1_300.0, 1_600.0, 1_900.0):
        assert am.evaluate_pm_hf(1.50, now=t)[0] is False
        assert am.evaluate_position_liq_distance("BTC", 18.0, now=t)[0] is False
        assert am.evaluate_position_liq_distance("SOL", 15.0, now=t)[0] is False


# ─── (c) synthetic HF crossing 1.20 → exactly ONE observación alert ────────


def test_hf_crossing_120_fires_exactly_one_observacion(am) -> None:
    assert am.evaluate_pm_hf(1.50, now=1_000.0)[0] is False  # arm at safe band
    should, msg = am.evaluate_pm_hf(1.18, now=1_300.0)
    assert should is True
    assert "OBSERVACIÓN" in msg and "1.18" in msg
    # Same band, repeated polls → silence (no second alert).
    assert am.evaluate_pm_hf(1.18, now=1_600.0)[0] is False
    assert am.evaluate_pm_hf(1.15, now=1_900.0)[0] is False  # still band 2


# ─── (d) synthetic SOL liq-dist below 12% → exactly ONE alert ──────────────


def test_sol_liq_dist_crossing_12_fires_exactly_once(am) -> None:
    assert am.evaluate_position_liq_distance("SOL", 15.0, now=1_000.0)[0] is False
    should, msg = am.evaluate_position_liq_distance("SOL", 11.4, now=1_300.0)
    assert should is True
    assert "SOL" in msg and "<12%" in msg
    # Same band → silence.
    assert am.evaluate_position_liq_distance("SOL", 11.0, now=1_600.0)[0] is False
    # Escalation below 8% → ONE more (band transition).
    should, msg = am.evaluate_position_liq_distance("SOL", 7.9, now=1_900.0)
    assert should is True and "<8%" in msg
    assert am.evaluate_position_liq_distance("SOL", 7.5, now=2_200.0)[0] is False


# ─── (e) band transitions + cooldown + restart persistence ─────────────────


def test_hf_band_transition_and_no_refire_on_restart(am, tmp_path) -> None:
    am.evaluate_pm_hf(1.50, now=1_000.0)
    assert am.evaluate_pm_hf(1.25, now=1_100.0)[0] is True   # band 1 (info)
    assert am.evaluate_pm_hf(1.25, now=1_200.0)[0] is False  # cooldown/same band
    assert am.evaluate_pm_hf(1.15, now=1_300.0)[0] is True   # band 2 escalation
    assert am.evaluate_pm_hf(1.05, now=1_400.0)[0] is True   # band 3 escalation
    assert am.evaluate_pm_hf(1.05, now=1_500.0)[0] is False

    # "Restart": fresh module import over the SAME DATA_DIR/SQLite file —
    # state must survive and the unchanged reading must NOT re-fire.
    import modules.alerts_margin as am2
    importlib.reload(am2)
    assert am2.DB_PATH == am.DB_PATH
    assert am2.evaluate_pm_hf(1.05, now=1_600.0)[0] is False

    # Recovery re-arms silently; a NEW crossing fires again after cooldown.
    assert am2.evaluate_pm_hf(1.50, now=2_000.0)[0] is False
    cooldown = am2.COOLDOWN_SEC
    should, msg = am2.evaluate_pm_hf(1.18, now=2_000.0 + cooldown + 1.0)
    assert should is True and "OBSERVACIÓN" in msg


def test_liq_dist_no_refire_on_restart(am) -> None:
    am.evaluate_position_liq_distance("SOL", 15.0, now=1_000.0)
    assert am.evaluate_position_liq_distance("SOL", 11.0, now=1_100.0)[0] is True
    import modules.alerts_margin as am2
    importlib.reload(am2)
    assert am2.evaluate_position_liq_distance("SOL", 11.0, now=1_200.0)[0] is False


# ─── 5-min cadence drift-alert dedup (fingerprint + cooldown, persisted) ───


def test_drift_alert_dedup_fingerprint_and_cooldown(monkeypatch, tmp_path) -> None:
    import bot
    state = str(tmp_path / "risk_validator_state.json")
    monkeypatch.setattr(bot, "_risk_validator_state_path", lambda: state)

    # First sighting fires.
    assert bot._risk_validator_should_alert("fp-A", now=1_000.0) is True
    # Same fingerprint at the 5-min cadence → suppressed for 6h.
    assert bot._risk_validator_should_alert("fp-A", now=1_300.0) is False
    assert bot._risk_validator_should_alert("fp-A", now=1_000.0 + 5 * 3600) is False
    # Changed fingerprint → fires immediately.
    assert bot._risk_validator_should_alert("fp-B", now=1_400.0) is True
    # Persisted across "restart" (state file survives) → still suppressed.
    assert os.path.exists(state)
    assert bot._risk_validator_should_alert("fp-B", now=1_700.0) is False
    # Cooldown elapsed → re-alert the standing drift.
    assert bot._risk_validator_should_alert("fp-B", now=1_400.0 + 7 * 3600) is True


def test_risk_validator_registration_cadence_is_5min() -> None:
    """The registration block must use the 5-minute interval, not hours=2."""
    import bot
    src = inspect.getsource(bot)
    block = src.split("risk_config_validator proactive scheduler", 1)[1][:1600]
    assert "RISK_VALIDATOR_INTERVAL_MIN" in block
    assert '"5"' in block
    assert "RISK_VALIDATOR_INTERVAL_HOURS" not in block
