"""R-PAT-RENEW — tests for the GitHub PAT expiry monitor."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from modules import pat_status


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Point cache/state files at a tmp dir and reset relevant env vars."""
    monkeypatch.setattr(pat_status, "_CACHE_FILE", str(tmp_path / "pat_cache.json"))
    monkeypatch.setattr(pat_status, "_ALERT_STATE_FILE", str(tmp_path / "pat_alert.json"))
    monkeypatch.delenv("PAT_ALERT_ENABLED", raising=False)
    monkeypatch.delenv("PAT_ALERT_THRESHOLD_DAYS", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_dummy")
    yield


NOW = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)


# ── parse_expiration ─────────────────────────────────────────────────────────

def test_parse_header_format():
    exp = pat_status.parse_expiration("2026-05-21 02:00:43 UTC")
    assert exp == datetime(2026, 5, 21, 2, 0, 43, tzinfo=timezone.utc)


def test_parse_iso_format():
    exp = pat_status.parse_expiration("2026-05-21T02:00:43")
    assert exp.tzinfo is not None
    assert exp.year == 2026 and exp.day == 21


@pytest.mark.parametrize("bad", [None, "", "   ", "not-a-date", 12345])
def test_parse_garbage_returns_none(bad):
    assert pat_status.parse_expiration(bad) is None


# ── get_pat_status ───────────────────────────────────────────────────────────

def _patch_fetch(monkeypatch, raw):
    monkeypatch.setattr(pat_status, "fetch_expiration", lambda token, **kw: raw)


def test_status_live_days_left(monkeypatch):
    # expiry 10 days out
    _patch_fetch(monkeypatch, "2026-05-30 12:00:00 UTC")
    s = pat_status.get_pat_status(now=NOW, force_refresh=True)
    assert s["token_present"] is True
    assert s["source"] == "live"
    assert s["days_left"] == 10
    assert s["expired"] is False


def test_status_no_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    s = pat_status.get_pat_status(now=NOW, token="")
    assert s["token_present"] is False
    assert s["days_left"] is None
    assert "not set" in s["error"]


def test_status_expired(monkeypatch):
    _patch_fetch(monkeypatch, "2026-05-19 12:00:00 UTC")  # yesterday
    s = pat_status.get_pat_status(now=NOW, force_refresh=True)
    assert s["expired"] is True
    assert s["days_left"] < 0


def test_status_falls_back_to_cache(monkeypatch):
    # First call populates cache via live fetch.
    _patch_fetch(monkeypatch, "2026-06-20 12:00:00 UTC")
    pat_status.get_pat_status(now=NOW, force_refresh=True)
    # Now network returns None → must use cached value.
    _patch_fetch(monkeypatch, None)
    s = pat_status.get_pat_status(now=NOW, force_refresh=True)
    assert s["source"] == "cache"
    assert s["days_left"] == 31


def test_status_no_network_no_cache(monkeypatch):
    _patch_fetch(monkeypatch, None)
    s = pat_status.get_pat_status(now=NOW, force_refresh=True)
    assert s["days_left"] is None
    assert s["error"]


# ── alert gating ─────────────────────────────────────────────────────────────

def test_alert_due_within_threshold(monkeypatch):
    _patch_fetch(monkeypatch, "2026-05-25 12:00:00 UTC")  # 5 days
    s = pat_status.get_pat_status(now=NOW, force_refresh=True)
    assert pat_status.pat_alert_due(s) is True


def test_alert_not_due_outside_threshold(monkeypatch):
    _patch_fetch(monkeypatch, "2026-07-01 12:00:00 UTC")  # >14 days
    s = pat_status.get_pat_status(now=NOW, force_refresh=True)
    assert pat_status.pat_alert_due(s) is False


def test_custom_threshold(monkeypatch):
    monkeypatch.setenv("PAT_ALERT_THRESHOLD_DAYS", "30")
    _patch_fetch(monkeypatch, "2026-06-10 12:00:00 UTC")  # 21 days
    s = pat_status.get_pat_status(now=NOW, force_refresh=True)
    assert pat_status.pat_alert_due(s) is True


def test_should_send_alert_dedup_per_day(monkeypatch):
    _patch_fetch(monkeypatch, "2026-05-25 12:00:00 UTC")  # 5 days → due
    s = pat_status.get_pat_status(now=NOW, force_refresh=True)
    assert pat_status.should_send_alert(s, now=NOW) is True
    pat_status.record_alert_sent(now=NOW)
    # Same UTC day → suppressed
    assert pat_status.should_send_alert(s, now=NOW) is False
    # Next UTC day → fires again
    tomorrow = NOW + timedelta(days=1)
    assert pat_status.should_send_alert(s, now=tomorrow) is True


def test_should_send_alert_disabled(monkeypatch):
    monkeypatch.setenv("PAT_ALERT_ENABLED", "false")
    _patch_fetch(monkeypatch, "2026-05-21 12:00:00 UTC")  # 1 day
    s = pat_status.get_pat_status(now=NOW, force_refresh=True)
    assert pat_status.should_send_alert(s, now=NOW) is False


# ── formatting + health ──────────────────────────────────────────────────────

def test_format_block_ok(monkeypatch):
    _patch_fetch(monkeypatch, "2026-07-01 12:00:00 UTC")
    s = pat_status.get_pat_status(now=NOW, force_refresh=True)
    txt = pat_status.format_pat_status_block(s)
    assert "GitHub PAT status" in txt
    assert "OK" in txt


def test_format_block_critical(monkeypatch):
    _patch_fetch(monkeypatch, "2026-05-21 12:00:00 UTC")  # ~1 day
    s = pat_status.get_pat_status(now=NOW, force_refresh=True)
    txt = pat_status.format_pat_status_block(s)
    assert "CR" in txt  # CRÍTICO

def test_health_block_no_network(monkeypatch):
    # Seed cache, then ensure health block never calls the network.
    _patch_fetch(monkeypatch, "2026-06-20 12:00:00 UTC")
    pat_status.get_pat_status(now=NOW, force_refresh=True)

    def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("health block must not hit network")

    monkeypatch.setattr(pat_status, "fetch_expiration", _boom)
    block = pat_status.health_pat_block()
    assert block["token_present"] is True
    assert block["expiration_iso"]
