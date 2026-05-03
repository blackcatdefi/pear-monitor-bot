"""R-FINAL — Bug #2 tests.

Cover ``auto.hyperlend_reader.read_all_with_cache`` and ``format_hf_line``.

Scenarios:
  - 3× total RPC failure with cache populated → returns UNKNOWN with
    last_known_hf, NOT HF=∞.
  - 2nd-attempt success → returns OK and persists cache.
  - Suspicious zero (live=0 collateral, cache shows real recently) → UNKNOWN.
  - Truly empty wallet (cache empty too) → ZERO, no false alarm.
  - Kill switch (HYPERLEND_AUTOREADER=false) → bypass cache entirely.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from auto import hyperlend_reader  # noqa: E402


WALLET_FLY = "0xa44e0000000000000000000000000000000000ae"


def _ok_entry(addr: str, hf: float, coll: float, debt: float) -> dict:
    return {
        "status": "ok",
        "data": {
            "wallet": addr,
            "label": "Flywheel",
            "total_collateral_usd": coll,
            "total_debt_usd": debt,
            "available_borrows_usd": 0.0,
            "current_liquidation_threshold": 0.85,
            "ltv": 0.6,
            "health_factor": hf,
            "collateral_assets": [],
            "debt_assets": [],
        },
    }


def _zero_entry(addr: str) -> dict:
    return {
        "status": "ok",
        "data": {
            "wallet": addr,
            "label": "Flywheel",
            "total_collateral_usd": 0.0,
            "total_debt_usd": 0.0,
            "health_factor": float("inf"),
            "collateral_assets": [],
            "debt_assets": [],
        },
    }


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Redirect cache to a per-test tmp dir so tests don't leak state."""
    tmp_cache = tmp_path / "hyperlend_hf_cache.json"
    monkeypatch.setattr(hyperlend_reader, "_cache_path", lambda: str(tmp_cache))
    monkeypatch.setattr(hyperlend_reader, "ENABLED", True)
    monkeypatch.setattr(hyperlend_reader, "RETRY_MAX", 3)
    monkeypatch.setattr(hyperlend_reader, "RETRY_BASE_SEC", 0.0)  # speed up tests
    yield


def test_three_failures_returns_unknown_from_cache(monkeypatch, tmp_path):
    """If the underlying fetch raises 3×, we fall back to cache."""
    # Pre-populate cache with last good HF.
    cache_path = hyperlend_reader._cache_path()
    with open(cache_path, "w") as f:
        json.dump(
            {
                WALLET_FLY: {
                    "hf": 1.214,
                    "collateral_usd": 4018.0,
                    "debt_usd": 881.0,
                    "ts_epoch": time.time() - 60,
                    "ts_utc": "2026-04-30T14:00:00+00:00",
                }
            },
            f,
        )

    calls = {"n": 0}

    async def _broken():
        calls["n"] += 1
        raise RuntimeError("503 rate-limited")

    out = asyncio.run(hyperlend_reader.read_all_with_cache(_broken))

    assert calls["n"] == 3, "should retry RETRY_MAX times"
    assert len(out) == 1
    entry = out[0]
    assert entry["hf_status"] == "UNKNOWN"
    data = entry["data"]
    assert data["last_known_hf"] == pytest.approx(1.214)
    assert data["recovered_from_cache"] is True
    assert data["age_seconds"] is not None
    # CRITICAL: HF must NOT be inf
    assert data["health_factor"] != float("inf")


def test_second_attempt_succeeds_returns_ok():
    """First call raises, second returns the real entry → OK."""
    real = _ok_entry(WALLET_FLY, 1.214, 4018.0, 881.0)

    state = {"calls": 0}

    async def _flaky():
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("temp 503")
        return [real]

    out = asyncio.run(hyperlend_reader.read_all_with_cache(_flaky))

    assert state["calls"] == 2
    assert len(out) == 1
    assert out[0]["hf_status"] == "OK"
    assert out[0]["data"]["health_factor"] == pytest.approx(1.214)

    # Cache must have been persisted.
    with open(hyperlend_reader._cache_path()) as f:
        cache = json.load(f)
    assert WALLET_FLY in cache
    assert cache[WALLET_FLY]["hf"] == pytest.approx(1.214)


def test_first_attempt_succeeds_persists_cache():
    real = _ok_entry(WALLET_FLY, 1.5, 5000.0, 1000.0)

    async def _ok():
        return [real]

    out = asyncio.run(hyperlend_reader.read_all_with_cache(_ok))
    assert out[0]["hf_status"] == "OK"

    with open(hyperlend_reader._cache_path()) as f:
        cache = json.load(f)
    assert cache[WALLET_FLY]["hf"] == pytest.approx(1.5)
    assert cache[WALLET_FLY]["collateral_usd"] == pytest.approx(5000.0)
    assert cache[WALLET_FLY]["debt_usd"] == pytest.approx(1000.0)


def test_suspicious_zero_recovers_from_cache():
    """If live says 0 but cache shows real collateral seconds ago → UNKNOWN."""
    # Seed cache with a real recent reading.
    with open(hyperlend_reader._cache_path(), "w") as f:
        json.dump(
            {
                WALLET_FLY: {
                    "hf": 1.214,
                    "collateral_usd": 4018.0,
                    "debt_usd": 881.0,
                    "ts_epoch": time.time() - 30,
                    "ts_utc": "2026-04-30T14:30:00+00:00",
                }
            },
            f,
        )

    async def _zero_fetch():
        return [_zero_entry(WALLET_FLY)]

    out = asyncio.run(hyperlend_reader.read_all_with_cache(_zero_fetch))
    assert len(out) == 1
    # Suspicious zero → UNKNOWN, not ZERO
    assert out[0]["hf_status"] == "UNKNOWN"
    assert out[0]["data"]["recovered_from_cache"] is True
    assert out[0]["data"]["last_known_hf"] == pytest.approx(1.214)


def test_truly_empty_wallet_classified_zero():
    """Live=0 AND cache empty (or also 0) → ZERO is honest answer."""
    async def _zero_fetch():
        return [_zero_entry(WALLET_FLY)]

    out = asyncio.run(hyperlend_reader.read_all_with_cache(_zero_fetch))
    assert len(out) == 1
    # Cache was empty → believe the zero.
    assert out[0]["hf_status"] == "ZERO"


def test_kill_switch_bypasses_cache(monkeypatch):
    monkeypatch.setattr(hyperlend_reader, "ENABLED", False)

    real = _ok_entry(WALLET_FLY, 1.0, 100.0, 50.0)

    async def _ok():
        return [real]

    out = asyncio.run(hyperlend_reader.read_all_with_cache(_ok))
    # Still classifies but no cache write
    assert out[0]["hf_status"] == "OK"
    # Cache file was never written (no _save_cache when disabled).
    assert not os.path.isfile(hyperlend_reader._cache_path())


def test_format_hf_line_ok():
    entry = _ok_entry(WALLET_FLY, 1.214, 4018.0, 881.0)
    entry["hf_status"] = "OK"
    line = hyperlend_reader.format_hf_line(entry)
    assert "HF 1.214" in line
    assert "$4,018" in line
    assert "$881" in line


def test_format_hf_line_unknown_with_cache():
    entry = {
        "status": "ok",
        "hf_status": "UNKNOWN",
        "data": {
            "label": "Flywheel",
            "wallet": WALLET_FLY,
            "last_known_hf": 1.214,
            "age_seconds": 23 * 60,  # 23 min
            "recovered_from_cache": True,
        },
    }
    line = hyperlend_reader.format_hf_line(entry)
    assert "rate-limited" in line
    assert "1.214" in line
    assert "23min" in line
    # Must NOT contain HF=∞
    assert "HF ∞" not in line


def test_format_hf_line_unknown_no_cache():
    entry = {
        "status": "ok",
        "hf_status": "UNKNOWN",
        "data": {"label": "Flywheel", "wallet": WALLET_FLY},
    }
    line = hyperlend_reader.format_hf_line(entry)
    assert "rate-limited" in line
    # English copy after R-EN-PY migration:
    assert "no prior read" in line


def test_format_hf_line_zero():
    entry = _zero_entry(WALLET_FLY)
    entry["hf_status"] = "ZERO"
    line = hyperlend_reader.format_hf_line(entry)
    # English copy after R-EN-PY migration:
    assert "no positions" in line


def test_total_failure_no_cache_returns_empty():
    """If RPC dies and cache is empty, return [] (not synthetic ∞)."""
    async def _broken():
        raise RuntimeError("503")

    out = asyncio.run(hyperlend_reader.read_all_with_cache(_broken))
    assert out == []


def test_age_label_helper():
    assert hyperlend_reader._age_label(45) == "45s"
    assert hyperlend_reader._age_label(60 * 23) == "23min"
    assert hyperlend_reader._age_label(3600 * 5) == "5h"
    assert hyperlend_reader._age_label(None) == "?"


def test_classify_entry_paths():
    # OK with debt
    e = _ok_entry(WALLET_FLY, 1.5, 1000.0, 500.0)
    assert hyperlend_reader._classify_entry(e) == "OK"
    # OK with infinite HF (collateral, no debt)
    e2 = _ok_entry(WALLET_FLY, float("inf"), 1000.0, 0.0)
    assert hyperlend_reader._classify_entry(e2) == "OK"
    # ZERO
    e3 = _zero_entry(WALLET_FLY)
    assert hyperlend_reader._classify_entry(e3) == "ZERO"
    # error status
    e4 = {"status": "error", "data": {}}
    assert hyperlend_reader._classify_entry(e4) == "UNKNOWN"
