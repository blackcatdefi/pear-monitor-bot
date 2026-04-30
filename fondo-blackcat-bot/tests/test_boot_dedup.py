"""R-FINAL — Bug #3 tests.

Cover ``auto.boot_dedup`` SQLite-backed announcement gate.

Scenarios:
  - First boot ever → should_announce()=True.
  - 2nd boot within 30 min → False (suppressed).
  - 2nd boot after 31 min → True (window expired).
  - mark_announced persists; last_announcement returns proper shape.
  - Kill switch disables suppression entirely.
"""
from __future__ import annotations

import importlib
import os

import pytest

# Force a known DATA_DIR via ``data`` env handling — boot_dedup uses
# config.DATA_DIR if available, otherwise ``../data``. The tmp_path fixture
# below monkeypatches the ``_db_path`` directly to ensure isolation.

from auto import boot_dedup  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "boot_dedup.db"
    monkeypatch.setattr(boot_dedup, "_db_path", lambda: str(db))
    monkeypatch.setattr(boot_dedup, "ENABLED", True)
    monkeypatch.setattr(boot_dedup, "WINDOW_MIN", 30)
    yield


def test_first_call_returns_true():
    assert boot_dedup.last_announcement() is None
    assert boot_dedup.should_announce() is True


def test_second_call_within_window_suppresses():
    boot_dedup.mark_announced()
    assert boot_dedup.should_announce() is False


def test_second_call_after_window_allows():
    boot_dedup.mark_announced()
    boot_dedup._backdate_for_tests(31)
    assert boot_dedup.should_announce() is True


def test_at_exact_window_boundary_allows(monkeypatch):
    """Edge: exactly WINDOW_MIN minutes ago must allow (>= comparison)."""
    monkeypatch.setattr(boot_dedup, "WINDOW_MIN", 5)
    boot_dedup.mark_announced()
    boot_dedup._backdate_for_tests(5)
    assert boot_dedup.should_announce() is True


def test_just_before_window_suppresses(monkeypatch):
    monkeypatch.setattr(boot_dedup, "WINDOW_MIN", 5)
    boot_dedup.mark_announced()
    boot_dedup._backdate_for_tests(4)
    assert boot_dedup.should_announce() is False


def test_last_announcement_shape():
    boot_dedup.mark_announced()
    info = boot_dedup.last_announcement()
    assert info is not None
    assert "ts_epoch" in info
    assert "ts_utc" in info
    assert "age_s" in info
    assert info["age_s"] >= 0


def test_kill_switch_always_announces(monkeypatch):
    monkeypatch.setattr(boot_dedup, "ENABLED", False)
    boot_dedup.mark_announced()  # should be no-op when disabled
    assert boot_dedup.should_announce() is True
    # mark_announced does NOT write when disabled.
    assert boot_dedup.last_announcement() is None


def test_five_consecutive_boots_only_announces_once(monkeypatch):
    """Reproduce the apr-30 prod incident: 5 cold restarts in 5 minutes."""
    # Boot #1 — should announce.
    assert boot_dedup.should_announce() is True
    boot_dedup.mark_announced()
    # Boots #2..#5 within minutes — must all be suppressed.
    for _ in range(4):
        assert boot_dedup.should_announce() is False


def test_mark_announced_persists_across_imports(tmp_path, monkeypatch):
    """Re-importing boot_dedup must see the previously persisted timestamp."""
    db = tmp_path / "persist.db"
    monkeypatch.setattr(boot_dedup, "_db_path", lambda: str(db))
    boot_dedup.mark_announced()

    # Reload the module — simulate process restart.
    importlib.reload(boot_dedup)
    monkeypatch.setattr(boot_dedup, "_db_path", lambda: str(db))
    monkeypatch.setattr(boot_dedup, "ENABLED", True)
    monkeypatch.setattr(boot_dedup, "WINDOW_MIN", 30)
    # Right after restart, within the window → still suppressed.
    assert boot_dedup.should_announce() is False


def test_history_capped_at_50(monkeypatch):
    """Sanity: >50 inserts should not unbounded-grow the table."""
    for _ in range(60):
        boot_dedup.mark_announced()
    import sqlite3

    with sqlite3.connect(boot_dedup._db_path()) as c:
        cnt = c.execute("SELECT COUNT(*) FROM boot_log").fetchone()[0]
    assert cnt <= 50
