"""X / Twitter intelligence — dynamic list-based timeline reader.

Architecture (Addendum 2 — Round 9):
    Primary & ONLY source: X API v2 List endpoint (Owned Reads — $0.001/req).
    The bot reads a PRIVATE X list owned by @BlackCatDeFi.
    List composition (~156 accounts) is managed manually in the X app.
    ZERO hardcoded usernames — the bot adapts automatically when accounts
    are added/removed from the list.

Env vars required:
    X_API_BEARER_TOKEN  — Bearer token from X Developer Console (Pay Per Use app)
    X_LIST_ID           — numeric ID of the private list

Cost: ~$1.80/month at 2h polling intervals (well under $20/cycle cap).
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

# ─── Config (env-driven, zero hardcoding) ──────────────────────────────────
X_API_BEARER_TOKEN = os.getenv("X_API_BEARER_TOKEN", "").strip()
X_LIST_ID = os.getenv("X_LIST_ID", "").strip()

# Cost tracking (in-memory, resets on restart)
_api_calls: list[dict[str, Any]] = []


def _track_call(endpoint: str, status: int) -> None:
    """Track an X API call for cost monitoring."""
    _api_calls.append({
        "endpoint": endpoint,
        "status": status,
        "ts": datetime.now(timezone.utc).isoformat(),
    })


def get_api_stats() -> dict[str, Any]:
    """Return X API usage stats for /providers."""
    total = len(_api_calls)
    ok = sum(1 for c in _api_calls if 200 <= c["status"] < 300)
    failed = total - ok
    cost_usd = total * 0.001  # $0.001 per request (Owned Reads)
    return {
        "total_calls": total,
        "ok": ok,
        "failed": failed,
        "cost_usd": cost_usd,
        "cost_str": f"${cost_usd:.3f}",
    }


# ─── Primary fetch: X API v2 List endpoint ─────────────────────────────────
_DIAG_NO_LIST_ID = "X_LIST_ID env var no configurada en Railway"
_DIAG_NO_BEARER = "X_API_BEARER_TOKEN env var no configurada en Railway"
_DIAG_401 = (
    "HTTP 401 — Bearer token inválido o revocado. "
    "Regenerar en developer.x.com y updatear X_API_BEARER_TOKEN en Railway."
)
_DIAG_402 = (
    "HTTP 402 — Créditos X API agotados. "
    "Top-up en console.x.com y cambiar auto-recharge a VISA 4463."
)
_DIAG_403 = (
    "HTTP 403 — Bearer sin permisos para la list. "
    "Verificá que la app pertenezca a @BlackCatDeFi y que la list sea accesible."
)
_DIAG_403_SPEND_CAP = (
    "HTTP 403 SpendCapReached — tu cuenta tiene créditos ($4.51 visibles) "
    "PERO alcanzó el SPEND CAP del ciclo de billing. "
    "Fix: developer.x.com → Products → Usage → AUMENTAR Spend Cap "
    "(NO es top-up, NO es payment method — es una cap separada). "
    "Reset automático: consultar 'reset_date' en response."
)
_DIAG_404 = (
    "HTTP 404 — List ID no encontrada. "
    "Verificá X_LIST_ID en Railway (actual ID: Fondo Black Cat Intel = 2046698139873378486)."
)
_DIAG_429 = "HTTP 429 — Rate limit hit. Retry en unos minutos (scheduler cachea cada 2h)."
_DIAG_TIMEOUT = "Timeout de red contra api.x.com — probablemente transitorio."
_DIAG_UNKNOWN = "Error no clasificado — revisar logs Railway para detalle."


def _diag_for_status(status: int) -> str:
    mapping = {
        401: _DIAG_401,
        402: _DIAG_402,
        403: _DIAG_403,
        404: _DIAG_404,
        429: _DIAG_429,
    }
    return mapping.get(status, f"HTTP {status} — {_DIAG_UNKNOWN}")


async def fetch_timeline_via_list(
    hours: int = 48,
    max_tweets: int = 500,
) -> tuple[list[dict] | None, str | None]:
    """Read the private X list — adaptive to user changes.

    Returns (tweets, error_diagnostic).
    On success: (list[tweet_dicts], None)
    On failure: (None, diagnostic_str) — diagnostic carries HTTP-status-specific hint.
    """
    if not X_LIST_ID:
        log.error("X_LIST_ID not configured")
        return None, _DIAG_NO_LIST_ID
    if not X_API_BEARER_TOKEN:
        log.error("X_API_BEARER_TOKEN not configured")
        return None, _DIAG_NO_BEARER

    url = f"https://api.x.com/2/lists/{X_LIST_ID}/tweets"
    params: dict[str, Any] = {
        "max_results": 100,
        "tweet.fields": "created_at,author_id,text,public_metrics",
        "expansions": "author_id",
        "user.fields": "username,name,verified",
    }
    headers = {"Authorization": f"Bearer {X_API_BEARER_TOKEN}"}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    all_tweets: list[dict] = []
    next_token: str | None = None
    last_error_diag: str | None = None

    async with httpx.AsyncClient(timeout=30) as c:
        for page in range(10):  # max 10 pages
            if next_token:
                params["pagination_token"] = next_token

            try:
                resp = await c.get(url, params=params, headers=headers)
            except httpx.TimeoutException as e:
                log.error("X API timeout: %s", e)
                _track_call("list_tweets", 0)
                last_error_diag = _DIAG_TIMEOUT
                break
            except Exception as e:
                log.error("X API error: %s", e)
                _track_call("list_tweets", 0)
                last_error_diag = f"{_DIAG_UNKNOWN} ({type(e).__name__}: {str(e)[:120]})"
                break

            _track_call("list_tweets", resp.status_code)

            if resp.status_code == 429:
                log.warning("X API rate limited, returning partial (%d tweets)", len(all_tweets))
                last_error_diag = _DIAG_429
                break

            if resp.status_code != 200:
                body_snip = resp.text[:300]
                log.error("X API %d: %s", resp.status_code, body_snip)
                # Parse body for X-specific error types (SpendCapReached etc.)
                try:
                    body_json = resp.json()
                except Exception:
                    body_json = {}
                title = (body_json.get("title") or "").lower()
                err_type = (body_json.get("type") or "").lower()
                reset_date = body_json.get("reset_date") or ""

                if resp.status_code == 403 and ("spendcap" in title or "credits" in err_type):
                    diag = _DIAG_403_SPEND_CAP
                    if reset_date:
                        diag += f" reset_date={reset_date}."
                    return None, diag

                diag = _diag_for_status(resp.status_code)
                # Include body snippet for non-standard errors
                if resp.status_code not in (401, 402, 403, 404, 429):
                    diag = f"{diag} Body: {body_snip}"
                return None, diag

            data = resp.json()
            batch = data.get("data", [])
            users_map = {
                u["id"]: u
                for u in data.get("includes", {}).get("users", [])
            }

            reached_cutoff = False
            for t in batch:
                created = datetime.fromisoformat(
                    t["created_at"].replace("Z", "+00:00")
                )
                if created < cutoff:
                    reached_cutoff = True
                    break
                user = users_map.get(t["author_id"], {})
                all_tweets.append({
                    "username": user.get("username", "unknown"),
                    "name": user.get("name", ""),
                    "verified": user.get("verified", False),
                    "text": t["text"],
                    "created_at": t["created_at"],
                    "metrics": t.get("public_metrics", {}),
                    "url": f"https://x.com/{user.get('username', 'i')}/status/{t['id']}",
                })

            if reached_cutoff or len(all_tweets) >= max_tweets:
                break

            next_token = data.get("meta", {}).get("next_token")
            if not next_token:
                break

    unique_accounts = len(set(t["username"] for t in all_tweets))
    log.info(
        "X timeline fetch: %d tweets de %d cuentas únicas (ventana %dh)",
        len(all_tweets),
        unique_accounts,
        hours,
    )
    # Partial success (rate limit mid-pagination) still returns tweets + diag
    if all_tweets and last_error_diag:
        log.info("X timeline partial result: %d tweets + diag: %s", len(all_tweets), last_error_diag)
        return all_tweets, None  # prefer partial success over error
    return all_tweets, last_error_diag


# ─── Public API (consumed by bot.py, analysis.py, etc.) ────────────────────
async def fetch_x_intel(hours: int = 48) -> dict[str, Any]:
    """Fetch X intel from the private list. Returns standard status dict."""
    tweets, diag = await fetch_timeline_via_list(hours=hours)

    if tweets is None:
        return {
            "status": "error",
            "error": diag or "X API list fetch failed — check X_LIST_ID + X_API_BEARER_TOKEN",
            "source": "x_api_list",
            "tweets": [],
        }

    if not tweets:
        return {
            "status": "ok",
            "source": "x_api_list",
            "tweets": [],
            "accounts": 0,
            "total": 0,
            "hours": hours,
            "note": "No tweets in time window",
            # Aliases for templates/timeline.py legacy formatter:
            "data": {},
            "accounts_scanned": 0,
            "total_tweets": 0,
        }

    # Sort by engagement (likes + retweets + replies)
    for t in tweets:
        m = t.get("metrics", {})
        t["_engagement"] = (
            (m.get("like_count") or 0)
            + (m.get("retweet_count") or 0) * 2
            + (m.get("reply_count") or 0)
        )
    tweets.sort(key=lambda t: t["_engagement"], reverse=True)

    unique_accounts = len(set(t["username"] for t in tweets))

    # Group by username for legacy formatter (templates/timeline.py expects
    # data: {username: [tweets...]}, accounts_scanned, total_tweets)
    by_user: dict[str, list[dict[str, Any]]] = {}
    for t in tweets:
        by_user.setdefault(t["username"], []).append(t)

    return {
        "status": "ok",
        "source": "x_api_list",
        "tweets": tweets,
        "accounts": unique_accounts,
        "total": len(tweets),
        "hours": hours,
        # Aliases for templates/timeline.py legacy formatter:
        "data": by_user,
        "accounts_scanned": unique_accounts,
        "total_tweets": len(tweets),
    }


async def debug_x_status() -> str:
    """Diagnostics for /debug_x command."""
    lines: list[str] = []
    lines.append("🔧 X/Twitter Diagnostics")
    lines.append("")

    # Config check
    lines.append("📋 Configuración:")
    lines.append(f"  X_LIST_ID: {'✅ set' if X_LIST_ID else '❌ NOT SET'}")
    lines.append(
        f"  X_API_BEARER_TOKEN: {'✅ set (' + X_API_BEARER_TOKEN[:8] + '...)' if X_API_BEARER_TOKEN else '❌ NOT SET'}"
    )
    lines.append("")

    # API stats
    stats = get_api_stats()
    lines.append("📊 API Stats (esta sesión):")
    lines.append(f"  Calls: {stats['total_calls']} (ok: {stats['ok']}, fail: {stats['failed']})")
    lines.append(f"  Cost: {stats['cost_str']}")
    lines.append("")

    # Cache state (scheduler)
    cs = get_cache_state()
    lines.append("💾 Cache scheduler (every 2h):")
    lines.append(f"  Last success: {cs.get('last_success_at') or '— nunca'}")
    lines.append(f"  Last attempt: {cs.get('last_attempt_at') or '— nunca'}")
    tc = cs.get("last_tweet_count", 0)
    ac = cs.get("last_account_count", 0)
    lines.append(f"  Last cache content: {tc} tweets de {ac} cuentas")
    succ_fail = cs.get("successive_failures", 0)
    if succ_fail > 0:
        lines.append(f"  ⚠️ {succ_fail} failures consecutivos")
    if cs.get("last_error"):
        lines.append(f"  Last error: {cs['last_error'][:300]}")
    lines.append("")

    # Live test
    if X_LIST_ID and X_API_BEARER_TOKEN:
        lines.append("🧪 Test en vivo...")
        tweets, diag = await fetch_timeline_via_list(hours=1, max_tweets=5)
        if tweets is not None:
            lines.append(f"  ✅ Conectado — {len(tweets)} tweets última hora")
            if tweets:
                usernames = set(t["username"] for t in tweets)
                lines.append(f"  Cuentas activas: {', '.join(sorted(usernames)[:5])}...")
        else:
            lines.append(f"  ❌ Fetch failed: {diag}")
    else:
        lines.append("⚠️ No se puede testear — faltan env vars")

    lines.append("")
    lines.append("💡 Para agregar/sacar cuentas, editá la list desde la app de X.")

    return "\n".join(lines)


async def format_intel_sources(hours: int = 24, max_tweets: int = 500) -> str:
    """Format active sources for /intel_sources command."""
    tweets, diag = await fetch_timeline_via_list(hours=hours, max_tweets=max_tweets)

    if not tweets:
        return f"❌ No se pudo leer la list.\nDiag: {diag or 'sin diagnóstico'}"

    by_user = Counter(t["username"] for t in tweets)
    top = by_user.most_common(20)
    total_accounts = len(by_user)

    msg = f"📡 Fuentes activas últimas {hours}h ({total_accounts} cuentas tweetearon)\n\n"
    msg += "Top 20 por volumen:\n"
    for username, count in top:
        msg += f"  @{username}: {count}\n"
    msg += f"\nTotal tweets capturados: {len(tweets)}"
    msg += "\n\n💡 Para agregar/sacar cuentas, editá la list 'Fondo Black Cat Intel' desde la app de X."

    return msg


# ─── Cached timeline for scheduler ─────────────────────────────────────────
_cached_timeline: dict[str, Any] | None = None
_cache_state: dict[str, Any] = {
    "last_success_at": None,     # ISO datetime of last successful cache refresh
    "last_attempt_at": None,     # ISO datetime of last attempt (success or fail)
    "last_error": None,          # diagnostic string of last error
    "last_tweet_count": 0,
    "last_account_count": 0,
    "successive_failures": 0,
}


async def poll_and_cache_timeline() -> None:
    """Scheduler job: fetch timeline and cache it for quick access.

    Never re-raises — the scheduler must keep running. All failures
    populate _cache_state["last_error"] with a diagnostic string.
    """
    global _cached_timeline
    log.info("[X_CACHE] Starting scheduled refresh")
    _cache_state["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
    try:
        result = await fetch_x_intel(hours=48)
        if result.get("status") == "ok":
            _cached_timeline = result
            _cache_state["last_success_at"] = datetime.now(timezone.utc).isoformat()
            _cache_state["last_error"] = None
            _cache_state["last_tweet_count"] = result.get("total", 0)
            _cache_state["last_account_count"] = result.get("accounts", 0)
            _cache_state["successive_failures"] = 0
            log.info(
                "[X_CACHE] Success: %d tweets from %d accounts",
                result.get("total", 0),
                result.get("accounts", 0),
            )
        else:
            err = result.get("error", "unknown")
            _cache_state["last_error"] = err
            _cache_state["successive_failures"] += 1
            log.warning(
                "[X_CACHE] FAILED (%d consecutive): %s",
                _cache_state["successive_failures"], err,
            )
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:200]}"
        _cache_state["last_error"] = err
        _cache_state["successive_failures"] += 1
        log.exception("[X_CACHE] Exception (%d consecutive)", _cache_state["successive_failures"])


def get_cached_timeline() -> dict[str, Any] | None:
    """Return the last cached timeline, or None."""
    return _cached_timeline


def get_cache_state() -> dict[str, Any]:
    """Return cache metadata for /debug_x."""
    return dict(_cache_state)
