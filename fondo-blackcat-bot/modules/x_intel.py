"""X / Twitter intelligence — dynamic list-based timeline reader.

Architecture (Addendum 2 — Round 9 + Round 12 hardening):
    Primary & ONLY source: X API v2 List endpoint.
    The bot reads a PRIVATE X list owned by @BlackCatDeFi.
    ZERO hardcoded usernames — the bot adapts automatically when accounts
    are added/removed from the list.

Env vars required:
    X_API_BEARER_TOKEN  — Bearer token from X Developer Console (Pay Per Use app)
    X_LIST_ID           — numeric ID of the private list

Round 12 cost hardening (post Apr 22 $20.48 overrun):
    - Realistic cost model: $0.25 per 1,000 tweets returned (NOT $0.001/req).
      X bills per tweet data returned, not per HTTP request.
    - Internal gate: only 1 live list fetch per FETCH_COOLDOWN_HOURS (default 4h).
      Any caller inside the cooldown window falls back to the SQLite-persisted
      cache. This stops /reporte, /timeline, /debug_x from triggering fresh
      fetches on top of the scheduler.
    - Daily cap: max 15 X API calls in any 24h window (all handlers combined).
    - Pagination cap: 2 pages × 100 tweets = 200 tweets/fetch max.
    - Cost persistence: each call recorded to SQLite (survives redeploy).
    - Proactive alert: if 7d-trailing projection exceeds $5/mo, one Telegram
      message per 24h.
    - Target projected cost at 4h cadence, 211-member list: ≈$1.20/month.
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from modules.intel_memory import (
    count_x_calls_since,
    count_x_calls_today_calendar,
    count_x_calls_today_live_only,
    last_successful_x_call_ts,
    load_x_timeline_payload,
    record_x_api_call,
    save_x_timeline_payload,
    should_send_75pct_alert,
    should_send_cost_alert,
    x_api_cost_projection,
    x_cache_hit_rate,
    x_cost_breakdown_by_caller,
)

log = logging.getLogger(__name__)

# ─── Config (env-driven, zero hardcoding) ──────────────────────────────────
X_API_BEARER_TOKEN = os.getenv("X_API_BEARER_TOKEN", "").strip()
X_LIST_ID = os.getenv("X_LIST_ID", "").strip()

# Round 12 + Round 15: cost hardening knobs (env-overridable, no redeploy needed).
# Defaults moved in Round 15 after $20.48/7d → $70+/7d cost regression:
#   - cooldown 4h → 2h (BCD asked stricter; explicitly accepts slower /reporte)
#   - daily cap stays at 15
#   - X_LIVE_ENABLED kill switch added (default true; flip to false in Railway
#     to force cache-only mode without redeploy)
#   - X_RATE_LIMIT_HOURS / X_DAILY_CAP added as canonical names matching the
#     hotfix spec; legacy names kept as aliases for back-compat with existing
#     Railway vars.
FETCH_COOLDOWN_HOURS = float(
    os.getenv("X_RATE_LIMIT_HOURS", os.getenv("X_API_COOLDOWN_HOURS", "2"))
)
DAILY_CALL_CAP = int(
    os.getenv("X_DAILY_CAP", os.getenv("X_API_DAILY_CAP", "15"))
)
MAX_PAGES_PER_FETCH = int(os.getenv("X_API_MAX_PAGES", "2"))
COST_ALERT_THRESHOLD_USD = float(os.getenv("X_API_ALERT_THRESHOLD_USD", "5"))
# Round 15: master kill switch — when false, the bot NEVER calls X live,
# regardless of cooldown/cap state. Cache-only mode for emergencies.
X_LIVE_ENABLED = os.getenv("X_LIVE_ENABLED", "true").strip().lower() not in (
    "false", "0", "no", "off"
)
# Round 15: scheduler is opt-in. When ENABLE_ALERTS=true the bot still adds
# alert + intel-processor jobs, but the X timeline cache job is gated on this
# var. Default false → no automatic X API calls; only /reporte triggers fetch.
X_SCHEDULER_ENABLED = os.getenv("X_SCHEDULER_ENABLED", "false").strip().lower() in (
    "true", "1", "yes", "on"
)
LIST_ENDPOINT_KEY = "lists/tweets"


def _track_call(endpoint: str, status: int) -> None:
    """Back-compat shim. Real tracking now lives in intel_memory SQLite."""
    # Intentionally thin — rate limit + cost tracking happen in
    # fetch_timeline_via_list via record_x_api_call. Kept to avoid touching
    # legacy call sites.
    log.debug("x_intel._track_call: endpoint=%s status=%s", endpoint, status)


def get_api_stats() -> dict[str, Any]:
    """Return X API usage stats for /providers and /debug_x."""
    proj = x_api_cost_projection()
    last_ok = last_successful_x_call_ts(LIST_ENDPOINT_KEY)
    calls_24h = count_x_calls_since(24)
    return {
        "calls_7d": proj["calls_7d"],
        "tweets_7d": proj["tweets_7d"],
        "cost_7d_usd": proj["cost_7d"],
        "cost_7d_str": f"${proj['cost_7d']:.2f}",
        "daily_avg_usd": proj["daily_avg_usd"],
        "monthly_projection_usd": proj["monthly_projection_usd"],
        "monthly_projection_str": f"${proj['monthly_projection_usd']:.2f}",
        "calls_24h": calls_24h,
        "daily_cap": DAILY_CALL_CAP,
        "cooldown_hours": FETCH_COOLDOWN_HOURS,
        "last_success_ts": last_ok.isoformat() if last_ok else None,
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


_DIAG_INTERNAL_COOLDOWN = (
    "Internal cooldown active (1 live fetch per {cool}h). "
    "Usando cache — próxima fetch permitida: {next_allowed}."
)
_DIAG_INTERNAL_DAILY_CAP = (
    "Internal daily cap hit ({used}/{cap} calls hoy UTC). "
    "Fallback a cache hasta UTC midnight."
)
_DIAG_KILL_SWITCH = (
    "X_LIVE_ENABLED=false — modo cache-only forzado. "
    "Para reactivar: Railway Variables → X_LIVE_ENABLED=true."
)


def _within_cooldown() -> tuple[bool, datetime | None]:
    """Return (in_cooldown, next_allowed_ts)."""
    last_ok = last_successful_x_call_ts(LIST_ENDPOINT_KEY)
    if last_ok is None:
        return False, None
    now = datetime.now(timezone.utc)
    next_allowed = last_ok + timedelta(hours=FETCH_COOLDOWN_HOURS)
    return now < next_allowed, next_allowed


def _daily_cap_exceeded() -> tuple[bool, int]:
    """Round 15: count by UTC calendar day (resets at midnight UTC) so that
    a heavy trading session in the morning doesn't bleed quota into the next
    day. Was rolling-24h pre-Round 15; that mixed two trading days.
    """
    used = count_x_calls_today_calendar()
    return used >= DAILY_CALL_CAP, used


async def fetch_timeline_via_list(
    hours: int = 48,
    max_tweets: int = 200,
    caller: str = "",
    bypass_cooldown: bool = False,
) -> tuple[list[dict] | None, str | None]:
    """Read the private X list — adaptive to user changes.

    Round 12 hardening:
        - Enforces FETCH_COOLDOWN_HOURS at code level (not just scheduler).
          Any caller inside the window gets (None, cooldown_diag) — they
          should fall back to cache.
        - Enforces DAILY_CALL_CAP (rolling 24h window).
        - Caps pagination at MAX_PAGES_PER_FETCH.
        - Records every call (success/fail) to SQLite for cost projection.
        - Fires a Telegram-bound cost alert when 7d projection > $5/mo.
    `bypass_cooldown=True` is only for /debug_x test-in-vivo (max_tweets=5).

    Returns (tweets, error_diagnostic).
      On success: (list[tweet_dicts], None)
      On cooldown/cap: (None, diagnostic) — caller should use cache
      On failure: (None, diagnostic)
    """
    if not X_LIST_ID:
        log.error("X_LIST_ID not configured")
        return None, _DIAG_NO_LIST_ID
    if not X_API_BEARER_TOKEN:
        log.error("X_API_BEARER_TOKEN not configured")
        return None, _DIAG_NO_BEARER

    # ── Gate 0: kill switch (Round 15) ──────────────────────────────────
    # Bypassed only by /debug_x probe (bypass_cooldown=True) so BCD can still
    # diagnose connectivity even with X_LIVE_ENABLED=false.
    if not X_LIVE_ENABLED and not bypass_cooldown:
        log.info("[X_API_COST] kill switch active — caller=%s", caller)
        return None, _DIAG_KILL_SWITCH

    # ── Gate 1: cooldown ─────────────────────────────────────────────────
    if not bypass_cooldown:
        in_cd, next_allowed = _within_cooldown()
        if in_cd:
            na = next_allowed.isoformat() if next_allowed else "—"
            log.info("[X_API_COST] cooldown active — caller=%s next_allowed=%s", caller, na)
            return None, _DIAG_INTERNAL_COOLDOWN.format(cool=FETCH_COOLDOWN_HOURS, next_allowed=na)

    # ── Gate 2: daily cap (UTC calendar day) ─────────────────────────────
    cap_hit, used = _daily_cap_exceeded()
    if cap_hit:
        log.warning("[X_API_COST] daily cap hit — %d/%d today (caller=%s)", used, DAILY_CALL_CAP, caller)
        return None, _DIAG_INTERNAL_DAILY_CAP.format(used=used, cap=DAILY_CALL_CAP)

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
    pages_consumed = 0
    tweets_returned_by_api = 0  # raw count from X before time-filter (for cost)
    last_status = 0

    page_limit = max(1, min(MAX_PAGES_PER_FETCH, 10))

    async with httpx.AsyncClient(timeout=30) as c:
        for page in range(page_limit):
            if next_token:
                params["pagination_token"] = next_token

            try:
                resp = await c.get(url, params=params, headers=headers)
            except httpx.TimeoutException as e:
                log.error("X API timeout: %s", e)
                _track_call("list_tweets", 0)
                record_x_api_call(LIST_ENDPOINT_KEY, 0, pages=pages_consumed + 1,
                                  tweets_returned=0, caller=caller)
                last_error_diag = _DIAG_TIMEOUT
                break
            except Exception as e:
                log.error("X API error: %s", e)
                _track_call("list_tweets", 0)
                record_x_api_call(LIST_ENDPOINT_KEY, 0, pages=pages_consumed + 1,
                                  tweets_returned=0, caller=caller)
                last_error_diag = f"{_DIAG_UNKNOWN} ({type(e).__name__}: {str(e)[:120]})"
                break

            _track_call("list_tweets", resp.status_code)
            pages_consumed += 1
            last_status = resp.status_code

            if resp.status_code == 429:
                log.warning("X API rate limited, returning partial (%d tweets)", len(all_tweets))
                record_x_api_call(LIST_ENDPOINT_KEY, 429, pages=pages_consumed,
                                  tweets_returned=0, caller=caller)
                last_error_diag = _DIAG_429
                break

            if resp.status_code != 200:
                body_snip = resp.text[:300]
                log.error("X API %d: %s", resp.status_code, body_snip)
                # Record the failing call (cost is zero — no tweets returned —
                # but keeping it lets /debug_x show failure rate).
                record_x_api_call(LIST_ENDPOINT_KEY, resp.status_code,
                                  pages=pages_consumed, tweets_returned=0, caller=caller)
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
            tweets_returned_by_api += len(batch)
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

    # Round 12: record the aggregate call for cost tracking if we got a 200
    # and haven't already recorded a status >= 400 for this fetch.
    if last_status == 200 and tweets_returned_by_api > 0:
        record_x_api_call(
            LIST_ENDPOINT_KEY,
            200,
            pages=pages_consumed,
            tweets_returned=tweets_returned_by_api,
            caller=caller,
        )
        # Structured cost log line
        est_cost = (tweets_returned_by_api / 1000.0) * 0.25
        calls_24h = count_x_calls_since(24)
        calls_today = count_x_calls_today_calendar()
        proj = x_api_cost_projection()
        log.info(
            "[X_API_COST] caller=%s pages=%d tweets=%d est_cost=$%.4f "
            "calls_today=%d/%d (cap), calls_24h=%d, 7d=$%.2f mo_proj=$%.2f",
            caller or "?",
            pages_consumed,
            tweets_returned_by_api,
            est_cost,
            calls_today,
            DAILY_CALL_CAP,
            calls_24h,
            proj["cost_7d"],
            proj["monthly_projection_usd"],
        )

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


async def maybe_send_cost_alert(app=None) -> None:
    """Round 12: fire Telegram alert if 7d projection exceeds threshold.

    Called after every successful fetch (and from the scheduler if enabled).
    Throttled to one alert per 24h via intel_memory.x_api_alerts.
    `app` is the telegram Application; when None this is a dry-run.
    """
    fire, proj = should_send_cost_alert(COST_ALERT_THRESHOLD_USD)
    if not fire:
        return
    msg = (
        f"⚠️ X API cost alert\n\n"
        f"Proyección mensual: ${proj['monthly_projection_usd']:.2f} "
        f"(threshold ${COST_ALERT_THRESHOLD_USD:.2f})\n"
        f"Últimos 7d: ${proj['cost_7d']:.2f} en {proj['calls_7d']} calls, "
        f"{proj['tweets_7d']} tweets.\n\n"
        f"Considerá X_LIVE_ENABLED=false en Railway para forzar cache-only."
    )
    log.warning("[X_API_COST] ALERT fired: %s", msg.replace("\n", " | "))
    if app is not None:
        try:
            from config import TELEGRAM_CHAT_ID
            if TELEGRAM_CHAT_ID:
                from utils.telegram import send_bot_message  # R20: auto-stamp timestamp
                await send_bot_message(app.bot, int(TELEGRAM_CHAT_ID), msg)
        except Exception:
            log.exception("maybe_send_cost_alert send failed (non-fatal)")


async def maybe_send_75pct_alert(app=None) -> None:
    """Round 15: notify BCD when daily cap reaches 75%.

    Throttled once per UTC calendar day. Fires immediately after a successful
    live fetch (in fetch_x_intel) so BCD knows to slow down before hitting cap.
    """
    used = count_x_calls_today_calendar()
    if not should_send_75pct_alert(used, DAILY_CALL_CAP):
        return
    msg = (
        f"⚠️ X API daily cap 75%\n\n"
        f"Llevamos {used}/{DAILY_CALL_CAP} fetches hoy (UTC). "
        f"Una vez que llegue al cap, /reporte usará cache hasta UTC midnight.\n\n"
        f"Para forzar cache-only ya: Railway Variables → X_LIVE_ENABLED=false."
    )
    log.warning("[X_API_COST] 75pct ALERT: %s", msg.replace("\n", " | "))
    if app is not None:
        try:
            from config import TELEGRAM_CHAT_ID
            if TELEGRAM_CHAT_ID:
                from utils.telegram import send_bot_message  # R20: auto-stamp timestamp
                await send_bot_message(app.bot, int(TELEGRAM_CHAT_ID), msg)
        except Exception:
            log.exception("maybe_send_75pct_alert send failed (non-fatal)")


# ─── Public API (consumed by bot.py, analysis.py, etc.) ────────────────────
async def fetch_x_intel(
    hours: int = 48,
    caller: str = "fetch_x_intel",
    app=None,
) -> dict[str, Any]:
    """Fetch X intel from the private list. Returns standard status dict.

    `caller` is stamped on every recorded X API call (see intel_memory) so
    that /debug_x and the cost-audit queries can attribute spend by source.
    `app` (Telegram Application) lets us push the 75% daily-cap alert when
    relevant; pass None for headless calls.

    Round 15: on success the payload is mirrored to SQLite via
    save_x_timeline_payload so it survives Railway redeploys (the in-memory
    _cached_timeline used to be wiped on every restart).
    """
    tweets, diag = await fetch_timeline_via_list(hours=hours, caller=caller)

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

    payload = {
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

    # Round 15: persist to SQLite so the cache survives Railway redeploys.
    # Pre-Round 15 the only cache was the module-global _cached_timeline.
    try:
        save_x_timeline_payload(payload)
        global _cached_timeline
        _cached_timeline = payload
        _cache_state["last_success_at"] = datetime.now(timezone.utc).isoformat()
        _cache_state["last_attempt_at"] = _cache_state["last_success_at"]
        _cache_state["last_tweet_count"] = len(tweets)
        _cache_state["last_account_count"] = unique_accounts
        _cache_state["last_error"] = None
        _cache_state["successive_failures"] = 0
    except Exception:
        log.exception("[X_CACHE] persist after fetch_x_intel failed (non-fatal)")

    # Round 15: fire 75% daily-cap alert if applicable.
    try:
        await maybe_send_75pct_alert(app)
    except Exception:
        log.exception("75pct alert dispatch failed (non-fatal)")

    return payload


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

    # API stats (Round 12 — realistic cost model, SQLite-persisted)
    stats = get_api_stats()
    lines.append("📊 Costo X API (persistido en SQLite):")
    lines.append(f"  7d: {stats['cost_7d_str']} en {stats['calls_7d']} calls, {stats['tweets_7d']} tweets")
    lines.append(f"  Proyección mensual: {stats['monthly_projection_str']}")
    lines.append(f"  24h calls: {stats['calls_24h']}/{stats['daily_cap']} (cap interno)")
    lines.append(f"  Cooldown: {stats['cooldown_hours']}h entre fetches")
    last_ok = stats["last_success_ts"] or "— nunca"
    lines.append(f"  Último fetch OK: {last_ok}")
    lines.append("")

    # Cache state (scheduler)
    cs = get_cache_state()
    lines.append(f"💾 Cache scheduler (every {int(FETCH_COOLDOWN_HOURS)}h):")
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

    # Live test — bypass cooldown so /debug_x always probes real state
    # (max_tweets=5 keeps cost floor at ≈$0.00125 per probe)
    if X_LIST_ID and X_API_BEARER_TOKEN:
        lines.append("🧪 Test en vivo (bypass cooldown, 5 tweets max)...")
        tweets, diag = await fetch_timeline_via_list(
            hours=1, max_tweets=5, caller="debug_x", bypass_cooldown=True
        )
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
    tweets, diag = await fetch_timeline_via_list(
        hours=hours, max_tweets=max_tweets, caller="intel_sources"
    )

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


async def poll_and_cache_timeline(app=None) -> None:
    """Scheduler job: fetch timeline and cache it for quick access.

    Round 12: passes caller="scheduler" for cost attribution and invokes
    maybe_send_cost_alert(app) post-refresh so BCD receives a Telegram
    warning if the 7d-trailing cost extrapolates above threshold.

    Never re-raises — the scheduler must keep running. All failures
    populate _cache_state["last_error"] with a diagnostic string.
    """
    global _cached_timeline
    log.info("[X_CACHE] Starting scheduled refresh")
    _cache_state["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
    try:
        result = await fetch_x_intel(hours=48, caller="scheduler")
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

    # Fire cost alert if applicable (always attempted, even on failure)
    try:
        await maybe_send_cost_alert(app)
    except Exception:
        log.exception("[X_CACHE] cost alert invocation failed (non-fatal)")


def get_cached_timeline() -> dict[str, Any] | None:
    """Return the last cached timeline, or None.

    Round 15: prefer the in-memory cache (fast path), but fall back to SQLite
    when it's empty — this happens immediately after a Railway redeploy.
    """
    global _cached_timeline
    if _cached_timeline is not None:
        return _cached_timeline
    payload, saved_at = load_x_timeline_payload()
    if payload is None:
        return None
    _cached_timeline = payload
    if saved_at:
        _cache_state["last_success_at"] = saved_at.isoformat()
        _cache_state["last_tweet_count"] = int(payload.get("total") or 0)
        _cache_state["last_account_count"] = int(payload.get("accounts") or 0)
    return _cached_timeline


def get_cache_state() -> dict[str, Any]:
    """Return cache metadata for /debug_x and /x_status.

    Round 15: if the in-memory state is empty, hydrate from SQLite so the
    state survives redeploys.
    """
    if not _cache_state.get("last_success_at"):
        payload, saved_at = load_x_timeline_payload()
        if payload is not None and saved_at is not None:
            _cache_state["last_success_at"] = saved_at.isoformat()
            _cache_state["last_tweet_count"] = int(payload.get("total") or 0)
            _cache_state["last_account_count"] = int(payload.get("accounts") or 0)
    return dict(_cache_state)


# ─── Round 15: dashboard helpers for /x_status and /costos_x ──────────────

def cache_age_text() -> str:
    """Render '4h 22min' style age for the latest cached payload, or '—'."""
    cs = get_cache_state()
    iso = cs.get("last_success_at")
    if not iso:
        return "—"
    try:
        ts = datetime.fromisoformat(iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts
        hours = int(delta.total_seconds() // 3600)
        minutes = int((delta.total_seconds() % 3600) // 60)
        if hours == 0:
            return f"{minutes}min"
        return f"{hours}h {minutes}min"
    except Exception:
        return "—"


def cache_banner_for_report() -> str:
    """Return the one-liner banner shown at the top of /reporte's timeline
    section telling BCD whether the X timeline is live or cached.
    """
    cs = get_cache_state()
    iso = cs.get("last_success_at")
    age = cache_age_text()
    if not iso:
        return "📡 X Timeline: sin cache aún — primer fetch en este /reporte"
    return (
        f"📡 X Timeline: cache de hace {age} "
        f"— última actualización: {iso}"
    )


async def format_x_status() -> str:
    """Output for /x_status command."""
    cs = get_cache_state()
    proj = x_api_cost_projection()
    used_today = count_x_calls_today_calendar()
    successful_today = count_x_calls_today_live_only()
    last_ok = last_successful_x_call_ts(LIST_ENDPOINT_KEY)
    next_allowed = None
    if last_ok:
        next_allowed = last_ok + timedelta(hours=FETCH_COOLDOWN_HOURS)

    lines: list[str] = []
    lines.append("🛰  X API STATUS — Round 15 on-demand mode")
    lines.append("─" * 32)
    lines.append("")
    lines.append("⚙️ Configuración:")
    lines.append(f"  X_LIVE_ENABLED: {'✅ true' if X_LIVE_ENABLED else '🛑 false (cache-only)'}")
    lines.append(f"  X_RATE_LIMIT_HOURS: {FETCH_COOLDOWN_HOURS:.1f}h")
    lines.append(f"  X_DAILY_CAP: {DAILY_CALL_CAP}/día (UTC)")
    lines.append(f"  X_SCHEDULER_ENABLED: {'✅ on' if X_SCHEDULER_ENABLED else '🛑 off (Round 15 default)'}")
    lines.append("")
    lines.append("📊 Uso hoy (UTC calendar day):")
    lines.append(f"  Fetches usados: {used_today}/{DAILY_CALL_CAP}")
    lines.append(f"  Successful (200): {successful_today}")
    lines.append(f"  Last fetch OK: {last_ok.isoformat() if last_ok else '— nunca'}")
    if next_allowed:
        lines.append(f"  Próximo fetch permitido: {next_allowed.isoformat()}")
    else:
        lines.append("  Próximo fetch permitido: — sin cooldown activo")
    lines.append("")
    lines.append("💰 Costo (persistido SQLite):")
    lines.append(f"  Últimos 7d: ${proj['cost_7d']:.2f} ({proj['calls_7d']} calls, {proj['tweets_7d']} tweets)")
    lines.append(f"  Proyección mensual: ${proj['monthly_projection_usd']:.2f}")
    lines.append("")
    lines.append("💾 Cache state:")
    lines.append(f"  Last cache: {cs.get('last_success_at') or '— vacío'}")
    lines.append(f"  Last cache content: {cs.get('last_tweet_count', 0)} tweets de {cs.get('last_account_count', 0)} cuentas")
    lines.append(f"  Cache age: {cache_age_text()}")
    if cs.get("last_error"):
        lines.append(f"  Last error: {str(cs.get('last_error'))[:200]}")
    lines.append("")
    lines.append("ℹ️ Round 15: live fetches sólo on-demand vía /reporte/timeline. "
                 "Scheduler automático apagado por defecto.")
    return "\n".join(lines)


async def format_x_costos() -> str:
    """Output for /costos_x command — cost dashboard with caller breakdown."""
    proj = x_api_cost_projection()
    proj30 = x_cache_hit_rate(days=30)
    breakdown_7d = x_cost_breakdown_by_caller(days=7)
    breakdown_30d = x_cost_breakdown_by_caller(days=30)
    used_today = count_x_calls_today_calendar()

    lines: list[str] = []
    lines.append("💰 X API COSTOS — auditoría Round 15")
    lines.append("─" * 32)
    lines.append("")
    lines.append("📅 Resumen últimos 7d:")
    lines.append(f"  Total calls: {proj['calls_7d']}")
    lines.append(f"  Total tweets: {proj['tweets_7d']}")
    lines.append(f"  Costo estimado: ${proj['cost_7d']:.2f}")
    lines.append(f"  Daily avg: ${proj['daily_avg_usd']:.4f}")
    lines.append(f"  Proyección mensual: ${proj['monthly_projection_usd']:.2f}")
    lines.append("")
    lines.append("📅 Últimos 30d:")
    lines.append(f"  Total calls: {proj30.get('total_calls', 0)} ({proj30.get('successful', 0)} successful)")
    lines.append(f"  Calls/día promedio: {proj30.get('calls_per_day', 0):.2f}")
    lines.append("")
    lines.append(f"🌐 Fetches hoy (UTC): {used_today}/{DAILY_CALL_CAP}")
    lines.append("")
    if breakdown_7d:
        lines.append("👤 Breakdown por caller (7d):")
        for row in breakdown_7d[:10]:
            lines.append(
                f"  {row['caller']:<22} ${row['cost']:.4f}  "
                f"calls={row['calls']} (ok={row['ok']}/fail={row['fail']}) "
                f"tw={row['tweets']}"
            )
        lines.append("")
    if breakdown_30d:
        lines.append("👤 Breakdown por caller (30d):")
        for row in breakdown_30d[:10]:
            lines.append(
                f"  {row['caller']:<22} ${row['cost']:.4f}  "
                f"calls={row['calls']} tw={row['tweets']}"
            )
        lines.append("")
    lines.append("ℹ️ Cost model X API: $0.25 / 1,000 tweets returned")
    lines.append(f"ℹ️ Cooldown {FETCH_COOLDOWN_HOURS:.1f}h, cap {DAILY_CALL_CAP}/día")
    lines.append("ℹ️ Para forzar cache-only: Railway → X_LIVE_ENABLED=false")
    return "\n".join(lines)
