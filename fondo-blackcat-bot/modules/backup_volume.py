"""R-PERFECT Phase 3 #5 — daily Volume backup.

Compresses /app/data/*.db + intel.log into /app/data/backup/<utc_date>.tar.gz.
Retains the last RETENTION_DAYS files (default 30). Records last successful run
timestamp to /app/data/backup_last.json so /health can surface it.

Optionally pushes the tarball to a backup branch via GITHUB_BACKUP_REPO +
GITHUB_TOKEN if both env vars are set; otherwise it stays local on the Volume.
Optional GitHub push uses subprocess git so we don't depend on extra libs.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# R-BOT-FINAL: registro de exito por subsistema (ver health_registry.tracked).
from modules import health_registry

log = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except (PermissionError, OSError):
    DATA_DIR = Path("/tmp/intel_data")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

BACKUP_DIR = DATA_DIR / "backup"
BACKUP_LAST_PATH = DATA_DIR / "backup_last.json"
RETENTION_DAYS = int(os.getenv("BACKUP_RETENTION_DAYS", "30"))
BACKUP_PATTERNS = ("*.db", "*.json", "intel.log", "*.log")


def _files_to_backup() -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for pattern in BACKUP_PATTERNS:
        for p in DATA_DIR.glob(pattern):
            if p.is_file() and p not in seen and BACKUP_DIR not in p.parents:
                seen.add(p)
                out.append(p)
    return out


def _prune_old_backups() -> int:
    """Delete tarballs older than RETENTION_DAYS. Return count pruned."""
    if not BACKUP_DIR.exists():
        return 0
    cutoff = time.time() - RETENTION_DAYS * 86400
    pruned = 0
    for p in BACKUP_DIR.glob("*.tar.gz"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                pruned += 1
        except OSError as e:
            log.debug("prune fail on %s: %s", p, e)
    return pruned


def _push_to_github(tarball: Path) -> tuple[bool, str]:
    """Optional: push tarball to backup branch. Returns (ok, reason)."""
    repo = os.getenv("GITHUB_BACKUP_REPO", "").strip()
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not repo or not token:
        return False, "no_backup_repo_env"
    work = DATA_DIR / "_backup_repo_work"
    try:
        if work.exists():
            shutil.rmtree(work)
        clone_url = f"https://x-access-token:{token}@github.com/{repo}.git"
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        # try fetch only the backup branch
        sp = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", "backup",
             clone_url, str(work)],
            capture_output=True, env=env, timeout=60,
        )
        if sp.returncode != 0:
            # branch may not exist; init one
            sp_init = subprocess.run(
                ["git", "clone", "--depth", "1", clone_url, str(work)],
                capture_output=True, env=env, timeout=60,
            )
            if sp_init.returncode != 0:
                return False, f"clone_fail: {sp_init.stderr.decode()[:80]}"
            subprocess.run(["git", "-C", str(work), "checkout", "--orphan", "backup"],
                           capture_output=True, env=env, timeout=20)
            subprocess.run(["git", "-C", str(work), "rm", "-rf", "."],
                           capture_output=True, env=env, timeout=20)
        dst_dir = work / "backups"
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / tarball.name
        shutil.copy2(tarball, dst)
        # rotate via git: keep last RETENTION_DAYS only
        kept = sorted(dst_dir.glob("*.tar.gz"))[-RETENTION_DAYS:]
        for old in dst_dir.glob("*.tar.gz"):
            if old not in kept:
                try:
                    old.unlink()
                except OSError:
                    pass
        subprocess.run(["git", "-C", str(work), "add", "-A"],
                       capture_output=True, env=env, timeout=20)
        sp_cmt = subprocess.run(
            ["git", "-C", str(work), "-c", "user.email=cowork@blackcatdefi",
             "-c", "user.name=cowork-backup", "commit", "-m",
             f"backup {tarball.name}"],
            capture_output=True, env=env, timeout=20,
        )
        if sp_cmt.returncode != 0 and b"nothing to commit" not in sp_cmt.stdout + sp_cmt.stderr:
            return False, f"commit_fail: {sp_cmt.stderr.decode()[:80]}"
        sp_push = subprocess.run(
            ["git", "-C", str(work), "push", "origin", "backup"],
            capture_output=True, env=env, timeout=60,
        )
        if sp_push.returncode != 0:
            return False, f"push_fail: {sp_push.stderr.decode()[:80]}"
        return True, "ok"
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"exception: {type(e).__name__}: {e!s:.80s}"
    finally:
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)


def _snapshot_db(src: Path, dst: Path) -> str | None:
    """Copia CONSISTENTE de una sqlite viva usando su propia API de backup.

    R-BOT-DEFINITIVE Fase 5.1 — por que esto no era opcional.
    ---------------------------------------------------------
    El backup viejo hacia tar.add() del archivo .db tal cual estaba en disco,
    mientras el bot escribia. Con WAL activo (que es el modo de estas DBs) el
    .db solo, sin su -wal, puede quedar en un estado que sqlite considera
    corrupto o simplemente viejo: las ultimas transacciones viven en el -wal,
    y el glob '*.db' nunca lo incluia.

    El resultado era un backup que se creaba sin errores, pesaba lo esperado,
    se guardaba con fecha y NO se podia restaurar. Nadie lo iba a descubrir
    hasta el dia que hiciera falta restaurarlo, que es el peor dia posible.

    sqlite3.Connection.backup() resuelve las dos cosas: toma el snapshot bajo
    lock, incluye lo que este en el WAL, y produce un archivo que ya es
    consistente por construccion.

    Devuelve None si salio bien, o el motivo del fallo.
    """
    import sqlite3
    try:
        src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=15.0)
    except sqlite3.Error as e:
        return f"open_ro: {e!s:.60s}"
    try:
        dst_con = sqlite3.connect(str(dst), timeout=15.0)
        try:
            src_con.backup(dst_con)
        finally:
            dst_con.close()
    except sqlite3.Error as e:
        return f"backup_api: {e!s:.60s}"
    finally:
        src_con.close()
    return None


@health_registry.tracked("backup", "run_backup")
def run_backup() -> dict[str, Any]:
    """Compress data files into a single tarball, prune old, push optional."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    name = f"backup-{now.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    tarball = BACKUP_DIR / name
    files = _files_to_backup()
    bytes_before = sum(p.stat().st_size for p in files if p.exists())
    stage = DATA_DIR / "_backup_stage"
    shutil.rmtree(stage, ignore_errors=True)
    db_fallos: list[str] = []
    try:
        stage.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tarball, "w:gz") as tar:
            for p in files:
                if p.suffix == ".db":
                    snap = stage / p.name
                    err = _snapshot_db(p, snap)
                    if err is None and snap.exists():
                        tar.add(snap, arcname=p.name)
                        continue
                    # Si el snapshot falla se guarda el archivo crudo igual —
                    # algo es mejor que nada — pero el fallo QUEDA REGISTRADO
                    # en el resultado. Un backup degradado que se reporta como
                    # perfecto es la misma clase de mentira que el resto de
                    # esta ronda viene a eliminar.
                    db_fallos.append(f"{p.name}: {err or 'snapshot vacio'}")
                tar.add(p, arcname=p.name)
    except (OSError, tarfile.TarError) as e:
        return {"ok": False, "reason": f"tar_fail: {e!s:.80s}",
                "ts_utc": int(time.time())}
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    pruned = _prune_old_backups()
    pushed_ok, push_reason = _push_to_github(tarball)
    out = {
        "ok": True,
        "ts_utc": int(time.time()),
        "iso": now.isoformat(timespec="seconds"),
        "tarball": tarball.name,
        "size_bytes": tarball.stat().st_size if tarball.exists() else 0,
        "files_n": len(files),
        "raw_bytes": bytes_before,
        "pruned_n": pruned,
        "pushed": pushed_ok,
        "push_reason": push_reason,
        # Fase 5.1: si alguna DB no pudo snapshotearse de forma consistente,
        # se dice aca y /health lo muestra. 'ok' sigue siendo True porque el
        # tarball existe, pero 'db_snapshot_fallos' impide que se lea como
        # un backup restaurable cuando no lo es.
        "db_snapshot_fallos": db_fallos,
    }
    try:
        with BACKUP_LAST_PATH.open("w", encoding="utf-8") as fh:
            json.dump(out, fh)
    except OSError as e:
        log.debug("backup_last write fail: %s", e)
    return out


def get_last_backup_status() -> dict[str, Any] | None:
    if not BACKUP_LAST_PATH.exists():
        return None
    try:
        with BACKUP_LAST_PATH.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        log.debug("backup_last read fail: %s", e)
        return None


def hours_since_last_backup() -> float | None:
    last = get_last_backup_status()
    if not last or not last.get("ok"):
        return None
    return (time.time() - int(last["ts_utc"])) / 3600.0


def format_for_telegram() -> str:
    last = get_last_backup_status()
    if not last:
        return "📦 *Backup* — sin snapshots aún"
    age_h = hours_since_last_backup()
    age_str = f"{age_h:.1f}h" if age_h is not None else "?"
    push_str = "✅ pushed" if last.get("pushed") else f"❌ {last.get('push_reason','')}"
    return (
        f"📦 *Backup*\n"
        f"  · last: `{last.get('iso','')}` ({age_str} ago)\n"
        f"  · file: `{last.get('tarball','?')}` "
        f"({last.get('size_bytes',0)//1024} KiB / {last.get('files_n',0)} files)\n"
        f"  · pruned old: {last.get('pruned_n',0)} · push: {push_str}"
    )
