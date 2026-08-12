"""R-UNIFIED-LIQ Phase B — twitterapi.io transport for the X list timeline.

WHY THIS EXISTS
---------------
The official X Pay-Per-Use API bills $0.005/post (~$10 every 4 days at our
volume). twitterapi.io serves the same list timeline at $0.0015/call +
$0.15/1K tweets (~$2-3/month). This module is a DROP-IN TRANSPORT: it
produces tweets in the EXACT dict shape ``fetch_timeline_via_list`` emits, so
everything downstream (x_store schema, client-side since_id frontier,
RT/reply filtering, scoring, render, /xrefresh, /debug_x) is untouched.

Backend selection (read at call time so tests / Railway redeploys pick up
env changes):
  * ``X_FETCH_BACKEND``   — "twitterapi_io" (default) | "official".
  * ``X_PROVIDER_API_KEY``— twitterapi.io key. While EMPTY the dispatcher in
    ``fetch_x_intel`` keeps serving through the official client (dark
    deploy: nothing changes until the owner pastes the key).
  * ``X_LIST_ID``         — raw numeric id OR full list URL (both accepted).

Fallback: if the provider list endpoint fails, we batch
``advanced_search`` queries over the cached list member handles (fetched
once, refreshed weekly). Client-side merge/dedupe; ONE deduped alert on
fallback activation (via modules.alert_dedup).

Guardrails honored: NO scheduler, NO budget caps, since_id stays a
CLIENT-SIDE STRING boundary, official X code stays dormant behind
``X_FETCH_BACKEND=official`` (never deleted).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

_BASE = os.getenv("X_PROVIDER_BASE_URL", "https://api.twitterapi.io").rstrip("/")
_LIST_URL = f"{_BASE}/twitter/list/tweets"
_MEMBERS_URL = f"{_BASE}/twitter/list/members"
_SEARCH_URL = f"{_BASE}/twitter/tweet/advanced_search"

# Provider credit pricing (mandate): $0.0015/call + $0.15/1K tweets.
PROVIDER_COST_PER_CALL_USD = float(os.getenv("X_PROVIDER_COST_PER_CALL", "0.0015"))
PROVIDER_COST_PER_1K_TWEETS_USD = float(
    os.getenv("X_PROVIDER_COST_PER_1K_TWEETS", "0.15")
)
PROVIDER_ENDPOINT_PREFIX = "provider/"
LIST_ENDPOINT_KEY = "provider/list_tweets"
SEARCH_ENDPOINT_KEY = "provider/advanced_search"
MEMBERS_ENDPOINT_KEY = "provider/list_members"

_MEMBER_CACHE_TTL_SEC = 7 * 24 * 3600.0  # weekly refresh
_SEARCH_BATCH_SIZE = 10  # handles per OR-query
_MAX_PAGES = 25
_HTTP_TIMEOUT = 30.0

# Meta of the last fetch (pages billed + raw tweets returned pre-filter) —
# mirrored into x_intel._last_fetch_meta by the dispatcher.
_last_meta: dict[str, int] = {"pages": 0, "returned": 0}
# Set True by fetch_timeline when the member-handle fallback actually served
# the data; the dispatcher reads+clears it to emit ONE deduped alert.
_fallback_served: dict[str, Any] = {"active": False, "reason": ""}


# ─── selection ───────────────────────────────────────────────────────────────

def normalize_list_id(raw: str | None) -> str:
    """Accept a raw numeric id or a full list URL; return the numeric id.

    Examples: "2046698139873378486", "https://x.com/i/lists/2046698139873378486",
    "https://twitter.com/i/lists/2046698139873378486?foo=1" → same id.
    Returns "" when nothing numeric is found.
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    if s.isdigit():
        return s
    m = re.search(r"/lists/(\d+)", s)
    if m:
        return m.group(1)
    m = re.search(r"(\d{8,})", s)  # last resort: longest digit run
    return m.group(1) if m else ""


def api_key() -> str:
    return os.getenv("X_PROVIDER_API_KEY", "").strip()


def list_id() -> str:
    return normalize_list_id(os.getenv("X_LIST_ID", ""))


_CREDITS_FLOOR_USD = float(os.getenv("X_OFFICIAL_CREDITS_FLOOR", "0.50"))


def official_credits_remaining() -> float:
    """Prepaid official-API balance still unburned (0.0 = mode disabled).

    R-BURN-CREDITS (2026-08-12): owner disabled auto-recharge with ~$19
    prepaid left on the official Console; we burn those first, then flip to
    the provider automatically. Balance = X_OFFICIAL_CREDITS_USD − official
    spend recorded in intel_memory.x_api_calls since X_OFFICIAL_CREDITS_SINCE
    (ISO UTC). Best effort: any failure returns 0.0 → provider serves.
    """
    try:
        total = float(os.getenv("X_OFFICIAL_CREDITS_USD", "0") or 0)
    except ValueError:
        return 0.0
    if total <= 0:
        return 0.0
    since = os.getenv("X_OFFICIAL_CREDITS_SINCE", "").strip()
    try:
        from modules.intel_memory import official_x_cost_since
        spent = official_x_cost_since(since)
    except Exception:  # noqa: BLE001
        return 0.0
    return max(total - spent, 0.0)


def backend_selected() -> str:
    """The configured backend selector (default twitterapi_io).

    Explicit X_FETCH_BACKEND ALWAYS wins. Without it, burn-credits auto mode:
    official while prepaid balance > floor ($0.50 safety buffer so a fetch
    never dies mid-flight on an empty Console), then twitterapi_io forever.
    """
    v = os.getenv("X_FETCH_BACKEND", "").strip().lower()
    if v == "official":
        return "official"
    if v in ("twitterapi_io", "twitterapi", "provider"):
        return "twitterapi_io"
    if official_credits_remaining() > _CREDITS_FLOOR_USD:
        return "official"
    return "twitterapi_io"


def provider_active() -> bool:
    """True when the provider transport should serve fetches.

    Dark-deploy contract: selecting twitterapi_io WITHOUT a key keeps the
    official client serving (nothing breaks before owner activation).
    """
    return backend_selected() == "twitterapi_io" and bool(api_key())


def backend_name() -> str:
    """Effective source for /health ``x_source``."""
    return "twitterapi_io" if provider_active() else "official"


def last_fetch_meta() -> dict[str, int]:
    return dict(_last_meta)


def pop_fallback_event() -> dict[str, Any] | None:
    """Return-and-clear the fallback-activation event (dispatcher reads it)."""
    if _fallback_served.get("active"):
        ev = dict(_fallback_served)
        _fallback_served.update(active=False, reason="")
        return ev
    return None


def effective_cost_per_post_usd() -> float:
    """Display-only per-post rate for /costs (call fee excluded)."""
    return PROVIDER_COST_PER_1K_TWEETS_USD / 1000.0


# ─── shared helpers ──────────────────────────────────────────────────────────

def _exclude_rt_replies() -> bool:
    return os.getenv("X_EXCLUDE_RT_REPLIES", "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _record(endpoint: str, status: int, pages: int, tweets: int, caller: str) -> None:
    try:
        from modules.intel_memory import record_x_api_call
        record_x_api_call(endpoint, status, pages=pages,
                          tweets_returned=tweets, caller=caller)
    except Exception:  # noqa: BLE001
        log.exception("x_provider cost record failed (non-fatal)")


def _parse_created_at(raw: Any) -> datetime | None:
    """twitterapi.io uses 'Tue Dec 10 07:00:30 +0000 2024'; tolerate ISO too."""
    s = str(raw or "").strip()
    if not s:
        return None
    for fmt in ("%a %b %d %H:%M:%S %z %Y",):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_canonical(t: dict, *, drop_rt_replies: bool) -> dict | None:
    """Provider tweet → the EXACT shape fetch_timeline_via_list emits.

    Returns None when the tweet must be skipped (RT/reply under the filter,
    or unparseable). Quote-tweets are KEPT (original commentary) — same
    doctrine as the official client.
    """
    if not isinstance(t, dict):
        return None
    if drop_rt_replies:
        if t.get("retweeted_tweet"):
            return None
        if t.get("isReply") is True or t.get("inReplyToId"):
            return None
    created = _parse_created_at(t.get("createdAt") or t.get("created_at"))
    tid = str(t.get("id") or "")
    if not tid or created is None:
        return None
    author = t.get("author") or {}
    username = str(author.get("userName") or author.get("username") or "unknown")
    try:
        from modules.x_intel import _sanitize_untrusted as _san
    except Exception:  # noqa: BLE001 — keep importable standalone
        def _san(x, **_kw):  # type: ignore[misc]
            return str(x or "")
    return {
        "id": tid,
        "username": username,
        "name": _san(author.get("name", "")),
        "verified": bool(
            author.get("isBlueVerified") or author.get("isVerified")
            or author.get("verified")
        ),
        "text": _san(t.get("text") or t.get("fullText") or ""),
        "created_at": created.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "metrics": {
            "like_count": int(t.get("likeCount") or 0),
            "retweet_count": int(t.get("retweetCount") or 0),
            "reply_count": int(t.get("replyCount") or 0),
            "quote_count": int(t.get("quoteCount") or 0),
        },
        "url": t.get("url") or f"https://x.com/{username}/status/{tid}",
    }


def _since_id_int(since_id: str | None) -> int | None:
    """since_id is a STRING end-to-end (ids > 2^53); compare as exact int."""
    if not since_id:
        return None
    try:
        return int(str(since_id))
    except (TypeError, ValueError):
        log.warning("[X_PROVIDER] invalid since_id %r — full-window fetch", since_id)
        return None


# ─── primary transport: list tweets ─────────────────────────────────────────

async def _fetch_list_pages(
    hours: int,
    max_tweets: int,
    caller: str,
    since_id: str | None,
) -> tuple[list[dict] | None, str | None]:
    lid = list_id()
    if not lid:
        return None, "X_LIST_ID not configured (raw id or list URL)"
    key = api_key()
    boundary = _since_id_int(since_id)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    drop = _exclude_rt_replies()

    out: list[dict] = []
    cursor = ""
    pages = 0
    raw_count = 0
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as c:
        for _page in range(_MAX_PAGES):
            params: dict[str, Any] = {"listId": lid}
            if cursor:
                params["cursor"] = cursor
            try:
                resp = await c.get(
                    _LIST_URL, params=params, headers={"X-API-Key": key},
                )
            except Exception as e:  # noqa: BLE001
                _record(LIST_ENDPOINT_KEY, 0, pages + 1, raw_count, caller)
                _last_meta.update(pages=pages + 1, returned=raw_count)
                return (out or None), (
                    f"twitterapi.io list fetch error ({type(e).__name__}: "
                    f"{str(e)[:120]})"
                )
            pages += 1
            if resp.status_code != 200:
                _record(LIST_ENDPOINT_KEY, resp.status_code, pages, raw_count, caller)
                _last_meta.update(pages=pages, returned=raw_count)
                snip = resp.text[:200]
                if out:
                    return out, None  # partial success beats an error
                return None, (
                    f"twitterapi.io list HTTP {resp.status_code}: {snip}"
                )
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                _record(LIST_ENDPOINT_KEY, 200, pages, raw_count, caller)
                _last_meta.update(pages=pages, returned=raw_count)
                return (out or None), "twitterapi.io list: unparseable JSON body"
            batch = data.get("tweets") or data.get("data") or []
            raw_count += len(batch)

            reached_boundary = False
            for t in batch:
                created = _parse_created_at(
                    (t or {}).get("createdAt") or (t or {}).get("created_at")
                )
                if created is not None and created < cutoff:
                    reached_boundary = True
                    break
                # Client-side since_id frontier — pages are newest→oldest, so
                # the first id at/below the high-water mark ends the walk.
                if boundary is not None:
                    try:
                        if int(str((t or {}).get("id"))) <= boundary:
                            reached_boundary = True
                            log.info(
                                "[X_PROVIDER] since_id boundary hit at id=%s "
                                "(boundary=%s)", (t or {}).get("id"), since_id,
                            )
                            break
                    except (TypeError, ValueError):
                        pass
                row = _to_canonical(t, drop_rt_replies=drop)
                if row is not None:
                    out.append(row)

            if reached_boundary or len(out) >= max_tweets:
                break
            cursor = str(data.get("next_cursor") or "")
            if not data.get("has_next_page") or not cursor:
                break

    _record(LIST_ENDPOINT_KEY, 200, pages, raw_count, caller)
    _last_meta.update(pages=pages, returned=raw_count)
    est = pages * PROVIDER_COST_PER_CALL_USD + raw_count / 1000.0 * (
        PROVIDER_COST_PER_1K_TWEETS_USD
    )
    log.info(
        "[X_PROVIDER_COST] caller=%s pages=%d tweets=%d est_cost=$%.4f "
        "boundary=%s", caller or "?", pages, raw_count, est,
        since_id or "—(backfill)",
    )
    return out, None


# ─── fallback: advanced_search over cached member handles ────────────────────

def _member_cache_path() -> str:
    try:
        from config import DATA_DIR
    except Exception:  # noqa: BLE001
        DATA_DIR = os.getenv("DATA_DIR", "/tmp")
    return os.path.join(DATA_DIR, "x_provider_list_members.json")


def _load_member_cache() -> tuple[list[str], float]:
    try:
        with open(_member_cache_path(), "r", encoding="utf-8") as fh:
            d = json.load(fh)
        handles = [str(h) for h in d.get("handles", []) if str(h).strip()]
        return handles, float(d.get("ts", 0.0))
    except Exception:  # noqa: BLE001
        return [], 0.0


def _save_member_cache(handles: list[str]) -> None:
    try:
        with open(_member_cache_path(), "w", encoding="utf-8") as fh:
            json.dump({"ts": time.time(), "handles": handles}, fh)
    except Exception:  # noqa: BLE001
        log.exception("x_provider member-cache save failed (non-fatal)")


async def get_list_members(caller: str = "", force: bool = False) -> list[str]:
    """List member handles — cached to disk, refreshed weekly. NEVER raises."""
    handles, ts = _load_member_cache()
    if handles and not force and (time.time() - ts) < _MEMBER_CACHE_TTL_SEC:
        return handles
    lid = list_id()
    if not lid or not api_key():
        return handles
    fresh: list[str] = []
    cursor = ""
    pages = 0
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as c:
            for _ in range(_MAX_PAGES):
                params: dict[str, Any] = {"listId": lid}
                if cursor:
                    params["cursor"] = cursor
                resp = await c.get(
                    _MEMBERS_URL, params=params,
                    headers={"X-API-Key": api_key()},
                )
                pages += 1
                if resp.status_code != 200:
                    break
                data = resp.json()
                for m in data.get("members") or data.get("users") or []:
                    h = str(
                        (m or {}).get("userName")
                        or (m or {}).get("username") or ""
                    ).strip().lstrip("@")
                    if h:
                        fresh.append(h)
                cursor = str(data.get("next_cursor") or "")
                if not data.get("has_next_page") or not cursor:
                    break
    except Exception:  # noqa: BLE001
        log.exception("x_provider get_list_members failed")
    finally:
        if pages:
            _record(MEMBERS_ENDPOINT_KEY, 200 if fresh else 0, pages, 0, caller)
    if fresh:
        dedup = sorted(set(fresh), key=str.lower)
        _save_member_cache(dedup)
        return dedup
    return handles  # stale cache beats nothing


async def _fetch_via_search(
    hours: int,
    max_tweets: int,
    caller: str,
    since_id: str | None,
) -> tuple[list[dict] | None, str | None]:
    handles = await get_list_members(caller=caller)
    if not handles:
        return None, "fallback unavailable: no cached list member handles"
    boundary = _since_id_int(since_id)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    drop = _exclude_rt_replies()
    since_clause = f" since:{cutoff.strftime('%Y-%m-%d_%H:%M:%S_UTC')}"

    merged: dict[str, dict] = {}
    pages = 0
    raw_count = 0
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as c:
        for i in range(0, len(handles), _SEARCH_BATCH_SIZE):
            batch_handles = handles[i:i + _SEARCH_BATCH_SIZE]
            query = "(" + " OR ".join(f"from:{h}" for h in batch_handles) + ")"
            query += since_clause
            cursor = ""
            for _ in range(3):  # a few pages per batch is plenty for 48h
                params: dict[str, Any] = {"query": query, "queryType": "Latest"}
                if cursor:
                    params["cursor"] = cursor
                try:
                    resp = await c.get(
                        _SEARCH_URL, params=params,
                        headers={"X-API-Key": api_key()},
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("x_provider search batch failed: %s", e)
                    break
                pages += 1
                if resp.status_code != 200:
                    break
                try:
                    data = resp.json()
                except Exception:  # noqa: BLE001
                    break
                batch = data.get("tweets") or data.get("data") or []
                raw_count += len(batch)
                stop = False
                for t in batch:
                    created = _parse_created_at(
                        (t or {}).get("createdAt") or (t or {}).get("created_at")
                    )
                    if created is not None and created < cutoff:
                        stop = True
                        break
                    if boundary is not None:
                        try:
                            if int(str((t or {}).get("id"))) <= boundary:
                                stop = True
                                break
                        except (TypeError, ValueError):
                            pass
                    row = _to_canonical(t, drop_rt_replies=drop)
                    if row is not None:
                        merged[row["id"]] = row  # client-side merge/dedupe
                if stop or len(merged) >= max_tweets:
                    break
                cursor = str(data.get("next_cursor") or "")
                if not data.get("has_next_page") or not cursor:
                    break
            if len(merged) >= max_tweets:
                break

    _record(SEARCH_ENDPOINT_KEY, 200 if pages else 0, max(pages, 1),
            raw_count, caller)
    _last_meta.update(pages=pages, returned=raw_count)
    out = sorted(merged.values(), key=lambda r: int(r["id"]), reverse=True)
    return out, None


# ─── public entry: dispatcher target ────────────────────────────────────────

async def fetch_timeline(
    hours: int = 48,
    max_tweets: int = 1200,
    caller: str = "",
    since_id: str | None = None,
) -> tuple[list[dict] | None, str | None]:
    """Provider-backed list timeline with member-search fallback.

    Contract mirrors ``fetch_timeline_via_list``: (tweets, None) on success
    (possibly []), (None, diagnostic) on failure. NEVER raises.
    """
    _last_meta.update(pages=0, returned=0)
    try:
        tweets, diag = await _fetch_list_pages(hours, max_tweets, caller, since_id)
    except Exception as e:  # noqa: BLE001 — robustness contract
        tweets, diag = None, f"x_provider internal error: {type(e).__name__}: {e}"
        log.exception("x_provider list path crashed")
    if tweets is not None:
        return tweets, diag

    # Fallback: advanced_search over the cached list member handles.
    log.warning("[X_PROVIDER] list endpoint failed (%s) — trying member "
                "search fallback", diag)
    try:
        fb_tweets, fb_diag = await _fetch_via_search(
            hours, max_tweets, caller, since_id,
        )
    except Exception as e:  # noqa: BLE001
        fb_tweets, fb_diag = None, f"fallback crashed: {type(e).__name__}"
        log.exception("x_provider search fallback crashed")
    if fb_tweets is not None:
        _fallback_served.update(active=True, reason=str(diag or "")[:200])
        return fb_tweets, None
    return None, f"{diag} | fallback: {fb_diag}"
