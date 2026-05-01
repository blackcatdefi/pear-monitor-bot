"""R-SILENT — tests for ``auto.price_cache``.

The cache provides last-known BTC/ETH/HYPE prices for the dashboard's
fallback band when ``modules.market.fetch_market_data`` fails on cold
start.

Coverage:
  * Empty cache → read() returns {}.
  * record(btc, eth, hype) → read() returns full triplet + age_s.
  * record(None, eth, None) → preserves prior btc/hype, updates eth.
  * age_s grows with time.
"""
from __future__ import annotations

import time

import pytest

from auto import price_cache as pc


@pytest.fixture(autouse=True)
def _isolated_path(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "_path", lambda: str(tmp_path / "price_cache.json"))
    yield


def test_empty_cache_returns_empty_dict():
    assert pc.read() == {}


def test_record_then_read_full_triplet():
    pc.record(60000.0, 3000.0, 35.0)
    data = pc.read()
    assert data["btc"] == 60000.0
    assert data["eth"] == 3000.0
    assert data["hype"] == 35.0
    assert "ts_epoch" in data
    assert "age_s" in data
    assert data["age_s"] >= 0


def test_record_partial_preserves_prior_values():
    pc.record(60000.0, 3000.0, 35.0)
    # Now an "ETH-only update" — btc and hype should be preserved.
    pc.record(None, 3100.0, None)
    data = pc.read()
    assert data["btc"] == 60000.0
    assert data["eth"] == 3100.0
    assert data["hype"] == 35.0


def test_record_all_none_preserves_all():
    pc.record(60000.0, 3000.0, 35.0)
    pc.record(None, None, None)
    data = pc.read()
    assert data["btc"] == 60000.0
    assert data["eth"] == 3000.0
    assert data["hype"] == 35.0


def test_age_s_grows():
    pc.record(60000.0, 3000.0, 35.0)
    data1 = pc.read()
    time.sleep(1.1)
    data2 = pc.read()
    assert data2["age_s"] >= data1["age_s"]


def test_corrupted_file_returns_empty(tmp_path, monkeypatch):
    p = tmp_path / "price_cache.json"
    p.write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(pc, "_path", lambda: str(p))
    assert pc.read() == {}


def test_record_writes_atomically(tmp_path, monkeypatch):
    """Sanity: after a write, the file exists and the .tmp does not."""
    import os

    p = tmp_path / "price_cache.json"
    monkeypatch.setattr(pc, "_path", lambda: str(p))
    pc.record(60000.0, 3000.0, 35.0)
    assert os.path.isfile(str(p))
    assert not os.path.isfile(str(p) + ".tmp")
