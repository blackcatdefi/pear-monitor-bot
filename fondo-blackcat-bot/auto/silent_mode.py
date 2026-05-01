"""R-SILENT — global silent-mode toggle.

Persistent flag at ``$DATA_DIR/silent_mode.json`` that hardens the bot's
default denoise:

    silent off (default)
        Normal mode. HF gate (1.10/1.05/1.02), catalyst gate
        (critical/T-30min/T+15min), boot dedup 24h.

    silent on
        Emergency-only. Suppresses everything except:
          * HF below CRITICAL (1.05) and PRELIQ (1.02)
          * Post-event critical analysis (T+15min) IF
            CATALYST_POST_ALLOWED_IN_SILENT=true (default true)
          * Manual basket close detector (it lives outside this gate;
            it's already edge-triggered and rare)

API
---
``is_silent() -> bool``
``set_silent(value: bool) -> None``
``status() -> dict``       returns ``{silent, since_epoch, since_iso}``

Env override (read-only fallback):
    SILENT_MODE=true|false  → if the JSON file does not exist yet.

The toggle is **persistent** — survives cold restarts when the file lives
on the Railway Volume mounted at ``/app/data``.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


def _path() -> str:
    try:
        from config import DATA_DIR  # type: ignore

        base = DATA_DIR
    except Exception:  # noqa: BLE001
        base = os.path.join(os.path.dirname(__file__), "..", "data")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "silent_mode.json")


def _read() -> dict[str, Any]:
    p = _path()
    if not os.path.isfile(p):
        # honour env-level default
        env_default = os.getenv("SILENT_MODE", "false").strip().lower() in {"1", "true", "yes"}
        return {
            "silent": env_default,
            "since_epoch": time.time(),
            "since_iso": datetime.now(timezone.utc).isoformat(),
            "source": "env_default",
        }
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("silent_mode.json is not a dict")
            data.setdefault("silent", False)
            data.setdefault("since_epoch", time.time())
            data.setdefault("since_iso", datetime.now(timezone.utc).isoformat())
            data.setdefault("source", "file")
            return data
    except Exception:  # noqa: BLE001
        log.exception("silent_mode: read failed, falling back to disabled")
        return {
            "silent": False,
            "since_epoch": time.time(),
            "since_iso": datetime.now(timezone.utc).isoformat(),
            "source": "fallback",
        }


def _write(payload: dict[str, Any]) -> None:
    p = _path()
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, p)
    except Exception:  # noqa: BLE001
        log.exception("silent_mode: write failed")
        try:
            os.remove(tmp)
        except Exception:  # noqa: BLE001
            pass


def is_silent() -> bool:
    return bool(_read().get("silent", False))


def set_silent(value: bool) -> dict[str, Any]:
    """Toggle silent mode. Returns the new status payload."""
    payload = {
        "silent": bool(value),
        "since_epoch": time.time(),
        "since_iso": datetime.now(timezone.utc).isoformat(),
        "source": "manual",
    }
    _write(payload)
    return payload


def status() -> dict[str, Any]:
    s = _read()
    s["age_s"] = max(0, int(time.time() - float(s.get("since_epoch") or time.time())))
    return s


# ─── Helpers used by HF/catalyst/boot wiring ──────────────────────────────
def hf_min_severity_to_emit() -> str:
    """Returns the minimum severity that is allowed when silent.

    'warn' → emite todo (warn/critical/preliq)
    'critical' → emite solo critical/preliq (NO warn)
    """
    return "critical" if is_silent() else "warn"


def catalyst_post_allowed() -> bool:
    """When silent, only T+post critical alerts are allowed (configurable)."""
    if not is_silent():
        return True
    return os.getenv("CATALYST_POST_ALLOWED_IN_SILENT", "true").strip().lower() != "false"


def boot_announcement_allowed() -> bool:
    """Boot announcement is fully suppressed in silent mode."""
    return not is_silent()


def _reset_for_tests() -> None:
    p = _path()
    if os.path.isfile(p):
        try:
            os.remove(p)
        except Exception:  # noqa: BLE001
            pass
