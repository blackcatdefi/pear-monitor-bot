"""R-PAT-RENEW (2026-05-20) — GitHub PAT expiry monitor.

Incident background
-------------------
The ``GITHUB_TOKEN`` fine-grained PAT used by ``backup_volume`` and
``fund_state_reconciler`` for autonomous git push almost expired with **no
preventive warning** — token ``pear-monitor-bot-round7`` had expiry
``2026-05-21 02:00 UTC`` and we only caught it ~24h before. The fund cannot
operate with the bot blind to its own deploy credentials, so this module
makes PAT expiry a first-class, monitored signal:

  * ``/pat_status``  → on-demand "days left" readout (admin command).
  * daily cron job   → Telegram alert when ``days_left <= PAT_ALERT_THRESHOLD_DAYS``
                       (default 14), deduped to once per UTC day.
  * ``/health``      → cached ``pat_status`` block (no network on the probe).

How we read the expiry
----------------------
GitHub returns the fine-grained PAT expiration in the response header
``github-authentication-token-expiration`` on *any* authenticated request
(format ``"2026-05-21 02:00:43 UTC"``). We read it from a cheap
``GET /user`` call, cache it under ``DATA_DIR`` and never raise into callers.

Env
---
    GITHUB_TOKEN                the active PAT (already used elsewhere).
    GITHUB_PAT_NAME             human label (default "pear-monitor-bot-round8").
    PAT_ALERT_THRESHOLD_DAYS    alert window in days (default 14).
    PAT_ALERT_ENABLED           SAFETY gate, default true (keep on).
    PAT_STATUS_CACHE_TTL_SEC    network refresh TTL seconds (default 21600 = 6h).
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger(__name__)

GITHUB_API_USER = "https://api.github.com/user"
EXPIRY_HEADER = "github-authentication-token-expiration"

try:  # config provides the canonical DATA_DIR (Railway Volume at /app/data)
    from config import DATA_DIR  # type: ignore
except Exception:  # noqa: BLE001
    DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
    os.makedirs(DATA_DIR, exist_ok=True)

_CACHE_FILE = os.path.join(DATA_DIR, "pat_status_cache.json")
_ALERT_STATE_FILE = os.path.join(DATA_DIR, "pat_alert_state.json")


# ───────────────────────── helpers ──────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _pat_name() -> str:
    return os.getenv("GITHUB_PAT_NAME", "pear-monitor-bot-round8").strip() or "pear-monitor-bot-round8"


def _threshold_days() -> int:
    try:
        return int(os.getenv("PAT_ALERT_THRESHOLD_DAYS", "14"))
    except (TypeError, ValueError):
        return 14


def _cache_ttl_sec() -> int:
    try:
        return int(os.getenv("PAT_STATUS_CACHE_TTL_SEC", "21600"))
    except (TypeError, ValueError):
        return 21600


def alert_enabled() -> bool:
    return os.getenv("PAT_ALERT_ENABLED", "true").strip().lower() != "false"


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:  # noqa: BLE001
        return {}


def _write_json(path: str, data: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except Exception:  # noqa: BLE001
        log.exception("pat_status: failed to persist %s", path)


def parse_expiration(raw: str | None) -> datetime | None:
    """Parse the GitHub expiry header value into an aware UTC datetime.

    Accepts ``"2026-05-21 02:00:43 UTC"`` (header format) and ISO 8601.
    Returns None on anything unparseable.
    """
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw:
        return None
    # Header format: "YYYY-MM-DD HH:MM:SS UTC"
    cleaned = raw[:-4].strip() if raw.upper().endswith(" UTC") else raw
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:  # last resort: ISO with offset / Z
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_expiration(token: str, *, timeout: int = 8) -> str | None:
    """Hit GET /user and return the raw expiry header value, or None.

    Never raises — returns None on any network / auth failure so callers can
    fall back to cache.
    """
    if not token:
        return None
    req = urllib.request.Request(GITHUB_API_USER)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "pear-monitor-bot-pat-status")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.headers.get(EXPIRY_HEADER)
    except Exception as exc:  # noqa: BLE001
        # A 401 (expired/revoked) raises HTTPError; surface header if present.
        hdrs = getattr(exc, "headers", None)
        if hdrs is not None:
            try:
                val = hdrs.get(EXPIRY_HEADER)
                if val:
                    return val
            except Exception:  # noqa: BLE001
                pass
        log.warning("pat_status: fetch_expiration failed: %s", exc)
        return None


def _days_and_hours_left(exp: datetime, now: datetime) -> tuple[int, int]:
    """Return (days_left, hours_left). Floors toward zero for the day count."""
    delta = exp - now
    total_hours = delta.total_seconds() / 3600.0
    days = int(total_hours // 24)
    hours = int(total_hours)
    return days, hours


# ───────────────────────── public API ───────────────────────────────────────

def get_pat_status(
    *,
    force_refresh: bool = False,
    allow_network: bool = True,
    now: datetime | None = None,
    token: str | None = None,
) -> dict:
    """Resolve the active PAT expiry status.

    Returns a dict that never raises:
        name, token_present, expiration_iso, expiration_raw, days_left,
        hours_left, expired, source ("live"|"cache"|"none"), error, checked_at.
    """
    now = now or _now()
    token = token if token is not None else os.getenv("GITHUB_TOKEN", "").strip()
    name = _pat_name()

    base = {
        "name": name,
        "token_present": bool(token),
        "expiration_iso": None,
        "expiration_raw": None,
        "days_left": None,
        "hours_left": None,
        "expired": None,
        "source": "none",
        "error": None,
        "checked_at": now.isoformat(),
    }

    if not token:
        base["error"] = "GITHUB_TOKEN not set"
        return base

    cache = _read_json(_CACHE_FILE)
    raw: str | None = None
    source = "none"

    cache_fresh = False
    if cache.get("expiration_raw") and cache.get("fetched_at"):
        try:
            fetched = datetime.fromisoformat(cache["fetched_at"])
            cache_fresh = (now - fetched).total_seconds() < _cache_ttl_sec()
        except Exception:  # noqa: BLE001
            cache_fresh = False

    if allow_network and (force_refresh or not cache_fresh):
        raw = fetch_expiration(token)
        if raw:
            source = "live"
            _write_json(_CACHE_FILE, {"expiration_raw": raw, "fetched_at": now.isoformat()})

    if raw is None and cache.get("expiration_raw"):
        raw = cache["expiration_raw"]
        source = "cache"

    if raw is None:
        base["error"] = "expiry unavailable (no live header, no cache)"
        return base

    exp = parse_expiration(raw)
    if exp is None:
        base["error"] = f"unparseable expiry: {raw!r}"
        base["expiration_raw"] = raw
        return base

    days, hours = _days_and_hours_left(exp, now)
    base.update(
        {
            "expiration_iso": exp.isoformat(),
            "expiration_raw": raw,
            "days_left": days,
            "hours_left": hours,
            "expired": exp <= now,
            "source": source,
        }
    )
    return base


def pat_alert_due(status: dict | None = None, *, threshold: int | None = None) -> bool:
    """True when the PAT is within the alert window (or already expired)."""
    if status is None:
        status = get_pat_status()
    threshold = _threshold_days() if threshold is None else threshold
    days = status.get("days_left")
    if days is None:
        return False
    return days <= threshold


def should_send_alert(status: dict | None = None, *, now: datetime | None = None,
                      threshold: int | None = None) -> bool:
    """Gate the daily cron alert: due + enabled + not already sent this UTC day."""
    if not alert_enabled():
        return False
    now = now or _now()
    if status is None:
        status = get_pat_status(now=now)
    if not pat_alert_due(status, threshold=threshold):
        return False
    state = _read_json(_ALERT_STATE_FILE)
    last = state.get("last_alert_date")
    return last != now.date().isoformat()


def record_alert_sent(*, now: datetime | None = None) -> None:
    now = now or _now()
    _write_json(_ALERT_STATE_FILE, {"last_alert_date": now.date().isoformat(),
                                    "last_alert_at": now.isoformat()})


def format_pat_status_block(status: dict | None = None) -> str:
    """Render the /pat_status message."""
    if status is None:
        status = get_pat_status()
    name = status.get("name", "?")
    if not status.get("token_present"):
        return ("\U0001f511 GitHub PAT status\n"
                f"Nombre esperado: {name}\n"
                "\u26a0\ufe0f GITHUB_TOKEN no está seteado en el entorno.")
    if status.get("error") and status.get("days_left") is None:
        return ("\U0001f511 GitHub PAT status\n"
                f"Nombre: {name}\n"
                f"\u26a0\ufe0f No pude leer la expiración: {status['error']}")

    days = status.get("days_left")
    hours = status.get("hours_left")
    exp_iso = status.get("expiration_iso", "?")
    threshold = _threshold_days()

    if status.get("expired"):
        emoji, verdict = "\U0001f6a8", "EXPIRADO — el push autónomo está caído"
    elif days is not None and days <= 3:
        emoji, verdict = "\U0001f6a8", f"CRÍTICO — renovar YA ({days}d)"
    elif days is not None and days <= threshold:
        emoji, verdict = "\u26a0\ufe0f", f"ALERTA — renovar pronto (<{threshold}d)"
    else:
        emoji, verdict = "\u2705", "OK"

    src = status.get("source", "?")
    when = ""
    if days is not None and days < 2 and hours is not None:
        when = f" (~{hours}h)"

    return (
        "\U0001f511 GitHub PAT status\n"
        f"Nombre:     {name}\n"
        f"Expira:     {exp_iso}\n"
        f"Días restantes: {days}{when}\n"
        f"Umbral alerta:  {threshold}d\n"
        f"Fuente:     {src}\n"
        f"{emoji} {verdict}"
    )


def health_pat_block() -> dict:
    """Cached-only snapshot for /health — never makes a network call."""
    try:
        s = get_pat_status(allow_network=False)
        return {
            "name": s.get("name"),
            "token_present": s.get("token_present"),
            "days_left": s.get("days_left"),
            "expiration_iso": s.get("expiration_iso"),
            "expired": s.get("expired"),
            "source": s.get("source"),
        }
    except Exception:  # noqa: BLE001
        return {"error": "pat_status unavailable"}
