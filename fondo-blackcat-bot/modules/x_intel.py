"""X / Twitter intelligence — reads recent tweets from tracked accounts.

Uses the official X API v2 via a bearer token stored in the env var
`X_BEARER_TOKEN`. Gracefully degrades (returns an error dict) if the token is
missing or the request fails, so /reporte never crashes on X problems.

Env vars:
    X_BEARER_TOKEN  — OAuth 2.0 Bearer token (app-only auth).
    X_ACCOUNTS      — optional comma-separated list of X handles without @.
                      Defaults to DEFAULT_ACCOUNTS below.

Output shape (on success):
    {
        "status": "ok",
        "data": {
            "<username>": [
                {"id": "...", "created_at": "...", "text": "...", "metrics": {...}}
            ],
            ...
        },
        "accounts_scanned": N,
        "total_tweets": N,
    }
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "").strip()

# Curated list of crypto/DeFi/macro handles relevant to Fondo Black Cat.
DEFAULT_ACCOUNTS: list[str] = [
    "hyperliquidx",
    "hyperfndn",
    "DefiIgnas",
    "stablewatchHQ",
    "santimentfeed",
    "WhaleAlert",
    "CryptoHayes",
    "cz_binance",
    "VitalikButerin",
    "woonomic",
    "0xWhiteLotus",
    "DeFiDad",
]


def _accounts_from_env() -> list[str]:
    raw = os.getenv("X_ACCOUNTS", "").strip()
    if not raw:
        return DEFAULT_ACCOUNTS
    return [a.strip().lstrip("@") for a in raw.split(",") if a.strip()]


API_BASE = "https://api.x.com/2"

# Tiny in-memory cache: username(lower) -> user_id. Cleared on redeploy.
_USER_ID_CACHE: dict[str, str] = {}


async def _resolve_user_ids(client: httpx.AsyncClient, usernames: list[str]) -> dict[str, str]:
    """Return {username_lower: user_id}. Uses /users/by?usernames=... (batch up to 100)."""
    missing = [u for u in usernames if u.lower() not in _USER_ID_CACHE]
    if missing:
        try:
            resp = await client.get(
                f"{API_BASE}/users/by",
                params={"usernames": ",".join(missing[:100])},
            )
            if resp.status_code == 200:
                for u in (resp.json().get("data") or []):
                    _USER_ID_CACHE[u["username"].lower()] = u["id"]
            else:
                log.warning("X users/by failed %d: %s", resp.status_code, resp.text[:200])
        except Exception as exc:  # noqa: BLE001
            log.warning("X users/by exception: %s", exc)
    return {u.lower(): _USER_ID_CACHE[u.lower()] for u in usernames if u.lower() in _USER_ID_CACHE}


async def _fetch_user_tweets(
    client: httpx.AsyncClient,
    username: str,
    user_id: str,
    cutoff: datetime,
    max_results: int = 20,
) -> list[dict[str, Any]]:
    try:
        resp = await client.get(
            f"{API_BASE}/users/{user_id}/tweets",
            params={
                "max_results": max_results,
                "tweet.fields": "created_at,public_metrics",
                "exclude": "retweets,replies",
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("X tweets %s exception: %s", username, exc)
        return []
    if resp.status_code != 200:
        log.warning("X tweets %s failed %d: %s", username, resp.status_code, resp.text[:200])
        return []
    items = resp.json().get("data") or []
    out: list[dict[str, Any]] = []
    for t in items:
        created = t.get("created_at")
        if created:
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if dt < cutoff:
                    continue
            except Exception:  # noqa: BLE001
                pass
        out.append({
            "id": t.get("id"),
            "created_at": created,
            "text": (t.get("text") or "").strip(),
            "metrics": t.get("public_metrics") or {},
        })
    return out


async def fetch_x_intel(hours: int = 24, accounts: list[str] | None = None) -> dict[str, Any]:
    """Fetch last `hours` of tweets from `accounts` (or env default).

    Returns a dict with status=ok|error. On error, always returns a dict with
    `error` key so the caller can branch without try/except.
    """
    if not X_BEARER_TOKEN:
        return {"status": "error", "error": "x_bearer_token_not_configured"}

    handles = accounts or _accounts_from_env()
    if not handles:
        return {"status": "error", "error": "no_accounts_configured"}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    headers = {"Authorization": f"Bearer {X_BEARER_TOKEN}"}

    async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
        uids = await _resolve_user_ids(client, handles)
        if not uids:
            return {"status": "error", "error": "could_not_resolve_any_user"}

        # Fan out — 1 request per handle, concurrency=5 to be polite to X rate limits.
        sem = asyncio.Semaphore(5)

        async def _do(uname: str) -> tuple[str, list[dict[str, Any]]]:
            uid = uids.get(uname.lower(), "")
            if not uid:
                return uname, []
            async with sem:
                msgs = await _fetch_user_tweets(client, uname, uid, cutoff)
            return uname, msgs

        pairs = await asyncio.gather(*[_do(h) for h in handles])

    data: dict[str, list[dict[str, Any]]] = {}
    total = 0
    for uname, msgs in pairs:
        if msgs:
            data[uname] = msgs
            total += len(msgs)

    return {
        "status": "ok",
        "data": data,
        "accounts_scanned": len(data),
        "total_tweets": total,
    }

