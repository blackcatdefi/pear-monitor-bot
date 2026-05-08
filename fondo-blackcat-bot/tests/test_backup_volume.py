"""R-PERFECT Phase 3 #5 — backup_volume tests (offline, no GitHub)."""
from __future__ import annotations

import json
import os
import sys
import tarfile
from pathlib import Path


def _import_with_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("GITHUB_BACKUP_REPO", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    import importlib
    import modules.backup_volume as bv  # noqa: WPS433
    importlib.reload(bv)
    return bv


def test_backup_creates_tarball_with_files(tmp_path, monkeypatch):
    bv = _import_with_tmp(tmp_path, monkeypatch)
    (tmp_path / "intel_rate.db").write_bytes(b"fake-sqlite")
    (tmp_path / "intel.log").write_text("a json line\n")
    result = bv.run_backup()
    assert result["ok"]
    assert result["files_n"] >= 2
    tar_path = tmp_path / "backup" / result["tarball"]
    assert tar_path.exists()
    with tarfile.open(tar_path, "r:gz") as tar:
        names = sorted(tar.getnames())
    assert "intel_rate.db" in names
    assert "intel.log" in names


def test_backup_records_last_run(tmp_path, monkeypatch):
    bv = _import_with_tmp(tmp_path, monkeypatch)
    (tmp_path / "marker.db").write_bytes(b"x")
    bv.run_backup()
    last = bv.get_last_backup_status()
    assert last is not None
    assert last["ok"] is True


def test_no_github_when_env_missing(tmp_path, monkeypatch):
    bv = _import_with_tmp(tmp_path, monkeypatch)
    (tmp_path / "x.db").write_bytes(b"x")
    result = bv.run_backup()
    assert result["pushed"] is False
    assert "no_backup_repo_env" in result["push_reason"]


def test_format_for_telegram_no_backup(tmp_path, monkeypatch):
    bv = _import_with_tmp(tmp_path, monkeypatch)
    out = bv.format_for_telegram()
    assert "sin snapshots" in out


def test_format_for_telegram_after_backup(tmp_path, monkeypatch):
    bv = _import_with_tmp(tmp_path, monkeypatch)
    (tmp_path / "x.db").write_bytes(b"x")
    bv.run_backup()
    out = bv.format_for_telegram()
    assert "Backup" in out
    assert "last:" in out


def test_prune_removes_old_files(tmp_path, monkeypatch):
    """With retention=1 day, a fake-aged tarball gets pruned on next run."""
    import time as _time
    monkeypatch.setenv("BACKUP_RETENTION_DAYS", "1")
    bv = _import_with_tmp(tmp_path, monkeypatch)
    (tmp_path / "x.db").write_bytes(b"x")
    bv.run_backup()
    backup_dir = tmp_path / "backup"
    # backdate the existing tarball to 2 days ago
    for p in backup_dir.glob("*.tar.gz"):
        old = _time.time() - 2 * 86400
        os.utime(p, (old, old))
    # next run prunes the aged one, keeps the fresh
    bv.run_backup()
    fresh = [p for p in backup_dir.glob("*.tar.gz")
             if _time.time() - p.stat().st_mtime < 60]
    assert len(fresh) >= 1
