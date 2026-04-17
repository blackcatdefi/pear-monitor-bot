"""X / Twitter intelligence — reads recent tweets from tracked accounts.

Uses the official X API v2 via a bearer token stored in the env var
`X_BEARER_TOKEN`.  Gracefully degrades (returns an error dict) if the token
is missing or the request fails, so /reporte never crashes on X problems.

Env vars:
    X_BEARER_TOKEN — OAuth 2.0 Bearer token (app-only auth).
    X_ACCOUNTS     — optional comma-separated list of X handles without @.
                     Defaults to DEFAULT_ACCOUNTS below.

Output shape (on success):
    {
        "status": "ok",
        "data": {
            "<username>": [
                {"id": "...", "created_at": "...", "text": "...", "metrics": {...}}
            ], ...
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
    "hyperliquidx", "hyperfndn", "DefiIgnas", "stablewatchHQ",
    "santimentfeed", "WhaleAlert", "CryptoHayes", "cz_binance",
    "VitalikButerin", "woonomic", "0xWhiteLotus", "DeFiDad",
]


def _accounts_from_env() -> list[str]:
    raw = os.getenv("X_ACCOUNTS", "").strip()
    if raw:
        return [a.strip().lstrip("@") for a in raw.split(",") if a.strip()]
    # Read from x_accounts.txt file (committed alongside this module)
    txt_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "x_accounts.txt")
    if os.path.isfile(txt_path):
        with open(txt_path) as f:
            file_content = f.read().strip()
            if file_content:
                return [a.strip().lstrip("@") for a in file_content.split(",") if a.strip()]
    return DEFAULT_ACCOUNTS


# Try both API domains — api.x.com may redirect to api.twitter.com or vice versa
API_BASES = ["https://api.x.com/2", "https://api.twitter.com/2"]

# Tiny in-memory cache: username(lower) -> user_id.  Cleared on redeploy.
_USER_ID_CACHE: dict[str, str] = {}


async def _resolve_batch(
    client: httpx.AsyncClient, batch: list[str], api_base: str
) -> tuple[int, str]:
    """Resolve a batch of usernames via /users/by. Returns (resolved_count, error_detail)."""
    try:
        resp = await client.get(
            f"{api_base}/users/by",
            params={"usernames": ",".join(batch)},
        )
        if resp.status_code == 200:
            data = resp.json()
            users = data.get("data") or []
            errors = data.get("errors") or []
            for u in users:
                _USER_ID_CACHE[u["username"].lower()] = u["id"]
            if errors:
                suspended = [e.get("value", "?") for e in errors if "suspend" in str(e.get("detail", "")).lower()]
                not_found = [e.get("value", "?") for e in errors if "not find" in str(e.get("detail", "")).lower()]
                if suspended:
                    log.info("X suspended accounts (skipped): %s", ", ".join(suspended[:10]))
                if not_found:
                    log.info("X not-found accounts (skipped): %s", ", ".join(not_found[:10]))
            return len(users), ""
        elif resp.status_code == 429:
            return 0, f"rate_limited (429)"
        elif resp.status_code == 401:
            return 0, f"unauthorized (401): token may be invalid — {resp.text[:200]}"
        elif resp.status_code == 403:
            return 0, f"forbidden (403): {resp.text[:200]}"
        else:
            return 0, f"http_{resp.status_code}: {resp.text[:200]}"
    except httpx.ConnectError as exc:
        return 0, f"connect_error: {exc}"
    except Exception as exc:  # noqa: BLE001
        return 0, f"exception: {exc}"


async def _resolve_user_ids(
    client: httpx.AsyncClient, usernames: list[str]
) -> dict[str, str]:
    """Return {username_lower: user_id}.  Uses /users/by?usernames=... (batch up to 100).
    Tries both api.x.com and api.twitter.com if the first fails.
    Falls back to individual resolution if batch completely fails.
    """
    missing = [u for u in usernames if u.lower() not in _USER_ID_CACHE]
    if not missing:
        return {u.lower(): _USER_ID_CACHE[u.lower()] for u in usernames if u.lower() in _USER_ID_CACHE}

    # --- Phase 1: Batch resolution (try each API base) ---
    batch_resolved = 0
    last_error = ""

    for api_base in API_BASES:
        batch_resolved = 0
        last_error = ""
        still_missing = [u for u in missing if u.lower() not in _USER_ID_CACHE]
        if not still_missing:
            break

        for i in range(0, len(still_missing), 100):
            batch = still_missing[i : i + 100]
            count, err = await _resolve_batch(client, batch, api_base)
            batch_resolved += count
            if err:
                last_error = err
                log.warning("X batch resolve via %s failed (batch %d): %s", api_base, i // 100, err)
                # If unauthorized/forbidden, don't try more batches on this base
                if "401" in err or "403" in err:
                    break

            # Small delay between batches to respect rate limits
            if i + 100 < len(still_missing):
                await asyncio.sleep(1)

        if batch_resolved > 0:
            log.info("X batch resolution via %s: %d/%d resolved", api_base, batch_resolved, len(missing))
            break  # Got some results, don't try next base
        else:
            log.warning("X batch resolution via %s: 0 resolved, trying next base...", api_base)

    # --- Phase 2: Individual fallback if batch got 0 ---
    if batch_resolved == 0:
        log.warning("X batch resolution failed on all bases (last error: %s). Trying individual resolution...", last_error)
        still_missing = [u for u in missing if u.lower() not in _USER_ID_CACHE]
        # Try first 20 accounts individually to diagnose the issue
        test_sample = still_missing[:20]
        individual_resolved = 0
        individual_errors: dict[str, int] = {}

        for api_base in API_BASES:
            if individual_resolved > 0:
                break
            for username in test_sample:
                if username.lower() in _USER_ID_CACHE:
                    continue
                try:
                    resp = await client.get(
                        f"{api_base}/users/by/username/{username}",
                    )
                    if resp.status_code == 200:
                        data = resp.json().get("data")
                        if data:
                            _USER_ID_CACHE[data["username"].lower()] = data["id"]
                            individual_resolved += 1
                    else:
                        err_key = f"{resp.status_code}"
                        individual_errors[err_key] = individual_errors.get(err_key, 0) + 1
                        if resp.status_code in (401, 403):
                            log.error(
                                "X individual resolution: %d on %s — TOKEN IS LIKELY INVALID. Response: %s",
                                resp.status_code, api_base, resp.text[:300]
                            )
                            break  # Token is bad, stop trying
                except Exception as exc:  # noqa: BLE001
                    log.warning("X individual resolve %s: %s", username, exc)
                await asyncio.sleep(0.5)  # Be gentle with rate limits

        if individual_resolved > 0:
            log.info("X individual fallback: %d/%d resolved", individual_resolved, len(test_sample))
            # Now resolve the rest individually
            remaining = [u for u in still_missing[20:] if u.lower() not in _USER_ID_CACHE]
            for username in remaining:
                try:
                    resp = await client.get(
                        f"{API_BASES[0]}/users/by/username/{username}",
                    )
                    if resp.status_code == 200:
                        data = resp.json().get("data")
                        if data:
                            _USER_ID_CACHE[data["username"].lower()] = data["id"]
                    elif resp.status_code == 429:
                        log.warning("X individual resolve rate-limited, stopping")
                        break
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(0.5)
        else:
            log.error(
                "X individual resolution also failed: errors=%s — BEARER TOKEN IS LIKELY INVALID OR EXPIRED",
                individual_errors
            )

    result = {u.lower(): _USER_ID_CACHE[u.lower()] for u in usernames if u.lower() in _USER_ID_CACHE}
    log.info("X total resolved: %d/%d usernames", len(result), len(usernames))
    return result


async def _fetch_user_tweets(
    client: httpx.AsyncClient,
    username: str,
    user_id: str,
    cutoff: datetime,
    max_results: int = 20,
) -> list[dict[str, Any]]:
    # Try each API base
    for api_base in API_BASES:
        try:
            resp = await client.get(
                f"{api_base}/users/{user_id}/tweets",
                params={
                    "max_results": max_results,
                    "tweet.fields": "created_at,public_metrics",
                    "exclude": "retweets,replies",
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("X tweets %s exception on %s: %s", username, api_base, exc)
            continue

        if resp.status_code == 429:
            log.warning("X tweets %s rate limited on %s, skipping", username, api_base)
            return []
        if resp.status_code != 200:
            log.warning(
                "X tweets %s failed %d on %s: %s",
                username, resp.status_code, api_base, resp.text[:200],
            )
            continue  # Try next base

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
            out.append(
                {
                    "id": t.get("id"),
                    "created_at": created,
                    "text": (t.get("text") or "").strip(),
                    "metrics": t.get("public_metrics") or {},
                }
            )
        return out

    return []


async def fetch_x_intel(
    hours: int = 24, accounts: list[str] | None = None
) -> dict[str, Any]:
    """Fetch last `hours` of tweets from `accounts` (or env default).

    Returns a dict with status=ok|error.  On error, always returns a dict
    with `error` key so the caller can branch without try/except.
    """
    if not X_BEARER_TOKEN:
        return {"status": "error", "error": "x_bearer_token_not_configured"}

    handles = accounts or _accounts_from_env()
    if not handles:
        return {"status": "error", "error": "no_accounts_configured"}

    log.info("X intel: resolving %d accounts...", len(handles))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    headers = {"Authorization": f"Bearer {X_BEARER_TOKEN}"}

    async with httpx.AsyncClient(
        headers=headers, timeout=15.0, follow_redirects=True
    ) as client:
        # Single resolution attempt (internal retries + individual fallback)
        uids = await _resolve_user_ids(client, handles)

        if not uids:
            return {
                "status": "error",
                "error": "could_not_resolve_any_user",
                "detail": "Both batch and individual resolution failed. Check X_BEARER_TOKEN validity.",
                "accounts_attempted": len(handles),
            }

        log.info("X intel: %d/%d accounts resolved, fetching tweets...", len(uids), len(handles))

        # Fan out — concurrency=10, with rate-limit awareness.
        sem = asyncio.Semaphore(10)

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
            "accounts_resolved": len(uids),
            "accounts_total": len(handles),
            "total_tweets": total,
        }
