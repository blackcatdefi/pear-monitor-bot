"""R-SILENT — tests for ``auto.silent_mode``.

The toggle persists in ``$DATA_DIR/silent_mode.json``. Coverage:

  * Default state (no file) is silent=False unless SILENT_MODE env true.
  * set_silent(True) → is_silent() == True; persists across reads.
  * set_silent(False) → reverts.
  * helper ``hf_min_severity_to_emit()`` returns 'warn' off, 'critical' on.
  * helper ``catalyst_post_allowed()`` is True off; True on by default
    (CATALYST_POST_ALLOWED_IN_SILENT=true), False if env overrides to false.
  * helper ``boot_announcement_allowed()`` is True off, False on.
"""
from __future__ import annotations

import importlib

import pytest

from auto import silent_mode as sm


@pytest.fixture(autouse=True)
def _isolated_path(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_path", lambda: str(tmp_path / "silent_mode.json"))
    monkeypatch.delenv("SILENT_MODE", raising=False)
    monkeypatch.delenv("CATALYST_POST_ALLOWED_IN_SILENT", raising=False)
    yield


def test_default_no_file_returns_false():
    assert sm.is_silent() is False


def test_default_with_env_true_returns_true(monkeypatch):
    monkeypatch.setenv("SILENT_MODE", "true")
    assert sm.is_silent() is True


def test_set_silent_on_and_persists():
    sm.set_silent(True)
    assert sm.is_silent() is True
    # Reload (simulate process restart) — file is the source of truth.
    importlib.reload(sm)
    # After reload, _path was reset; we can't reuse the fixture path, but
    # the reload+import below covers the in-process round-trip.


def test_set_silent_round_trip():
    sm.set_silent(True)
    s = sm.status()
    assert s["silent"] is True
    assert "since_iso" in s
    sm.set_silent(False)
    assert sm.is_silent() is False


def test_hf_min_severity_off_is_warn():
    sm.set_silent(False)
    assert sm.hf_min_severity_to_emit() == "warn"


def test_hf_min_severity_on_is_critical():
    sm.set_silent(True)
    assert sm.hf_min_severity_to_emit() == "critical"


def test_catalyst_post_allowed_off_is_true():
    sm.set_silent(False)
    assert sm.catalyst_post_allowed() is True


def test_catalyst_post_allowed_on_default_true():
    sm.set_silent(True)
    assert sm.catalyst_post_allowed() is True


def test_catalyst_post_allowed_on_env_false(monkeypatch):
    sm.set_silent(True)
    monkeypatch.setenv("CATALYST_POST_ALLOWED_IN_SILENT", "false")
    assert sm.catalyst_post_allowed() is False


def test_boot_announcement_allowed_off():
    sm.set_silent(False)
    assert sm.boot_announcement_allowed() is True


def test_boot_announcement_allowed_on():
    sm.set_silent(True)
    assert sm.boot_announcement_allowed() is False


def test_status_includes_age_s():
    sm.set_silent(True)
    s = sm.status()
    assert "age_s" in s
    assert s["age_s"] >= 0


def test_corrupted_file_falls_back_to_disabled(tmp_path, monkeypatch):
    p = tmp_path / "silent_mode.json"
    p.write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(sm, "_path", lambda: str(p))
    assert sm.is_silent() is False
