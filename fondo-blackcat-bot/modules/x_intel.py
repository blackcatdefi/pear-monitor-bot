"""X / Twitter intelligence â dynamic list-based timeline reader.

Architecture (Addendum 2 â Round 9):
    Primary & ONLY source: X API v2 List endpoint (Owned Reads â $0.001/req).
    The bot reads a PRIVATE X list owned by @BlackCatDeFi.
    List composition (~156 accounts) is managed manually in the X app.
    ZERO hardcoded usernames â the bot adapts automatically when accounts
    are added/removed from the list.

Env vars required:
    X_API_BEARER_TOKEN  â Bearer token from X Developer Console (Pay Per Use app)
    X_LIST_ID           â numeric ID of the private list

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

# âââ Config (env-driven, zero hardcoding) ââââââââââââââââââââââââââââââââââ
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


# âââ Primary fetch: X API v2 List endpoint âââââââââââââââââââââââââââââââââ
async def fetch_timeline_via_list(
    hours: int = 48,
    max_tweets: int = 500,
) -> list[dict] | None:
    """Read the private X list â adaptive to user changes.

    Returns a list of tweet dicts with keys:
        username, name, verified, text, created_at, metrics, url
    Or None on hard failure.
    """
    if not X_LIST_ID:
        log.error("X_LIST_ID not configured")
        return None
    if not X_API_BEARER_TOKEN:
        log.error("X_API_BEARER_TOKEN not configured")
        return None

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

    async with httpx.AsyncClient(timeout=30) as c:
        for page in range(10):  # max 10 pages
            if next_token:
                params["pagination_token"] = next_token

            try:
                resp = await c.get(url, params=params, headers=headers)
            except Exception as e:
                log.error("X API error: %s", e)
                _track_call("list_tweets", 0)
                break

            _track_call("list_tweets", resp.status_code)

            if resp.status_code == 429:
                log.warning("X API rate limited, returning partial (%d tweets)", len(all_tweets))
                break

            if resp.status_code != 200:
                log.error("X API %d: %s", resp.status_code, resp.text[:200])
                return None

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
        "X timeline fetch: %d tweets de %d cuentas Ãºnicas (ventana %dh)",
        len(all_tweets),
        unique_accounts,
        hours,
    )
    return all_tweets


# âââ Public API (consumed by bot.py, analysis.py, etc.) ââââââââââââââââââââ
async def fetch_x_intel(hours: int = 48) -> dict[str, Any]:
    """Fetch X intel from the private list. Returns standard status dict."""
    tweets = await fetch_timeline_via_list(hours=hours)

    if tweets is None:
        return {
            "status": "error",
            "error": "X API list fetch failed â check X_LIST_ID + X_API_BEARER_TOKEN",
            "source": "x_api_list",
            "tweets": [],
        }

    if not tweets:
        return {
            "status": "ok",
            "source": "x_api_list",
            "tweets": [],
            "accounts": 0,
            "note": "No tweets in time window",
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

    return {
        "status": "ok",
        "source": "x_api_list",
        "tweets": tweets,
        "accounts": unique_accounts,
        "total": len(tweets),
        "hours": hours,
    }


async def debug_x_status() -> str:
    """Diagnostics for /debug_x command."""
    lines: list[str] = []
    lines.append("ð§ X/Twitter Diagnostics")
    lines.append("")

    # Config check
    lines.append("ð ConfiguraciÃ³n:")
    lines.append(f"  X_LIST_ID: {'â set' if X_LIST_ID else 'â NOT SET'}")
    lines.append(
        f"  X_API_BEARER_TOKEN: {'â set (' + X_API_BEARER_TOKEN[:8] + '...)' if X_API_BEARER_TOKEN else 'â NOT SET'}"
    )
    lines.append("")

    # API stats
    stats = get_api_stats()
    lines.append("ð API Stats (esta sesiÃ³n):")
    lines.append(f"  Calls: {stats['total_calls']} (ok: {stats['ok']}, fail: {stats['failed']})")
    lines.append(f"  Cost: {stats['cost_str']}")
    lines.append("")

    # Live test
    if X_LIST_ID and X_API_BEARER_TOKEN:
        lines.append("ð§ª Test en vivo...")
        tweets = await fetch_timeline_via_list(hours=1, max_tweets=5)
        if tweets is not None:
            lines.append(f"  â Conectado â {len(tweets)} tweets Ãºltima hora")
            if tweets:
                usernames = set(t["username"] for t in tweets)
                lines.append(f"  Cuentas activas: {', '.join(sorted(usernames)[:5])}...")
        else:
            lines.append("  â Fetch failed â revisar token y list ID")
    else:
        lines.append("â ï¸ No se puede testear â faltan env vars")

    lines.append("")
    lines.append("ð¡ Para agregar/sacar cuentas, editÃ¡ la list desde la app de X.")

    return "\n".join(lines)


async def format_intel_sources(hours: int = 24, max_tweets: int = 500) -> str:
    """Format active sources for /intel_sources command."""
    tweets = await fetch_timeline_via_list(hours=hours, max_tweets=max_tweets)

    if not tweets:
        return "â No se pudo leer la list. VerificÃ¡ X_LIST_ID + token."

    by_user = Counter(t["username"] for t in tweets)
    top = by_user.most_common(20)
    total_accounts = len(by_user)

    msg = f"ð¡ Fuentes activas Ãºltimas {hours}h ({total_accounts} cuentas tweetearon)\n\n"
    msg += "Top 20 por volumen:\n"
    for username, count in top:
        msg += f"  @{username}: {count}\n"
    msg += f"\nTotal tweets capturados: {len(tweets)}"
    msg += "\n\nð¡ Para agregar/sacar cuentas, editÃ¡ la list 'Fondo Black Cat Intel' desde la app de X."

    return msg


# âââ Cached timeline for scheduler âââââââââââââââââââââââââââââââââââââââââ
_cached_timeline: dict[str, Any] | None = None


async def poll_and_cache_timeline() -> None:
    """Scheduler job: fetch timeline and cache it for quick access."""
    global _cached_timeline
    try:
        result = await fetch_x_intel(hours=48)
        if result.get("status") == "ok":
            _cached_timeline = result
            log.info(
                "X timeline cached: %d tweets from %d accounts",
                result.get("total", 0),
                result.get("accounts", 0),
            )
        else:
            log.warning("X timeline cache refresh failed: %s", result.get("error"))
    except Exception:
        log.exception("X timeline cache refresh error")


def get_cached_timeline() -> dict[str, Any] | None:
    """Return the last cached timeline, or None."""
    return _cached_timeline
