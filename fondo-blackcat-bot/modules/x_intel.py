"""X / Twitter intelligence — reads recent tweets from tracked accounts.

Primary source: official X API v2 via bearer token in X_BEARER_TOKEN env var.
Fallbacks (in order, when API fails):
    1. Nitter RSS (multiple public instances)
    2. None — return empty but with a clear error message

The previous version silently returned `could_not_resolve_any_user` when the
bearer token was rate-limited / expired, leaving /reporte without any X intel.
This version logs the actual HTTP status + body, and falls through to Nitter.

Env vars:
    X_BEARER_TOKEN  — OAuth 2.0 Bearer token (app-only auth).  Optional.
    X_ACCOUNTS      — optional comma-separated list of X handles without @.
                      Defaults to DEFAULT_ACCOUNTS below.
    NITTER_INSTANCES — optional comma-separated list of nitter hosts to try.

Output shape (on success):
    {
        "status": "ok",
        "source": "x_api" | "nitter",
        "data": {"<username>": [{"id","created_at","text","metrics"}, ...], ...},
        "accounts_scanned": N,
        "total_tweets": N,
    }
"""
from __future__ import annotations

import asyncio
import email.utils
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "").strip()

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

# Public Nitter instances. They go down often; we try each in sequence.
DEFAULT_NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
    "https://nitter.tiekoetter.com",
    "https://nitter.unixfox.eu",
]


def _nitter_instances() -> list[str]:
    raw = os.getenv("NITTER_INSTANCES", "").strip()
    if raw:
        return [h.strip().rstrip("/") for h in raw.split(",") if h.strip()]
    return DEFAULT_NITTER_INSTANCES


def _accounts_from_env() -> list[str]:
    raw = os.getenv("X_ACCOUNTS", "").strip()
    if raw:
        return [a.strip().lstrip("@") for a in raw.split(",") if a.strip()]
    txt_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "x_accounts.txt")
    if os.path.isfile(txt_path):
        with open(txt_path) as f:
            file_content = f.read().strip()
        if file_content:
            return [a.strip().lstrip("@") for a in file_content.split(",") if a.strip()]
    return DEFAULT_ACCOUNTS


API_BASE = "https://api.x.com/2"
_USER_ID_CACHE: dict[str, str] = {}


# ─── X API v2 primary path ──────────────────────────────────────────────
async def _resolve_user_ids(
    client: httpx.AsyncClient, usernames: list[str]
) -> tuple[dict[str, str], list[str]]:
    """Return ({username_lower: user_id}, [raw_errors]). Uses /users/by."""
    missing = [u for u in usernames if u.lower() not in _USER_ID_CACHE]
    errors: list[str] = []
    for i in range(0, len(missing), 100):
        batch = missing[i : i + 100]
        try:
            resp = await client.get(
                f"{API_BASE}/users/by",
                params={"usernames": ",".join(batch)},
            )
            log.info(
                "X users/by batch %d status=%d bytes=%d",
                i // 100, resp.status_code, len(resp.content),
            )
            if resp.status_code == 200:
                payload = resp.json()
                for u in payload.get("data") or []:
                    _USER_ID_CACHE[u["username"].lower()] = u["id"]
                if not payload.get("data"):
                    log.warning("X users/by 200 but empty data — body: %s", resp.text[:400])
            elif resp.status_code == 429:
                log.warning(
                    "X users/by rate limited at batch %d, pausing 60s; headers=%s",
                    i // 100,
                    {k: v for k, v in resp.headers.items() if "limit" in k.lower() or "reset" in k.lower()},
                )
                await asyncio.sleep(60)
                resp2 = await client.get(
                    f"{API_BASE}/users/by",
                    params={"usernames": ",".join(batch)},
                )
                log.info("X users/by retry status=%d", resp2.status_code)
                if resp2.status_code == 200:
                    for u in resp2.json().get("data") or []:
                        _USER_ID_CACHE[u["username"].lower()] = u["id"]
                else:
                    errors.append(f"batch {i//100} retry {resp2.status_code}: {resp2.text[:200]}")
                    log.warning("X users/by retry failed %d: %s", resp2.status_code, resp2.text[:400])
            else:
                errors.append(f"batch {i//100} status {resp.status_code}: {resp.text[:200]}")
                log.warning(
                    "X users/by failed status=%d body=%s headers=%s",
                    resp.status_code,
                    resp.text[:400],
                    dict(resp.headers),
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"batch {i//100} exception: {exc}")
            log.warning("X users/by exception batch %d: %s", i // 100, exc)
        if i + 100 < len(missing):
            await asyncio.sleep(1)

    resolved = {
        u.lower(): _USER_ID_CACHE[u.lower()]
        for u in usernames
        if u.lower() in _USER_ID_CACHE
    }
    return resolved, errors


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

    if resp.status_code == 429:
        log.warning("X tweets %s rate limited, skipping", username)
        return []
    if resp.status_code != 200:
        log.warning(
            "X tweets %s failed %d: %s", username, resp.status_code, resp.text[:200]
        )
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
        out.append(
            {
                "id": t.get("id"),
                "created_at": created,
                "text": (t.get("text") or "").strip(),
                "metrics": t.get("public_metrics") or {},
            }
        )
    return out


# ─── Nitter RSS fallback ────────────────────────────────────────────────
_NITTER_STATS_RE = re.compile(r"(\d[\d,]*)")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    return _HTML_TAG_RE.sub("", s or "").strip()


async def _fetch_nitter_rss_one(
    client: httpx.AsyncClient, instance: str, username: str
) -> tuple[str, list[dict[str, Any]]]:
    url = f"{instance}/{username}/rss"
    try:
        resp = await client.get(url, follow_redirects=True, timeout=12.0)
    except Exception as exc:  # noqa: BLE001
        return f"{instance}: exception {exc}", []

    if resp.status_code != 200 or not resp.content:
        return f"{instance}: status {resp.status_code}", []
    body = resp.content
    # Some instances return HTML error pages with 200 — quick sniff for <rss
    if b"<rss" not in body[:200] and b"<feed" not in body[:200]:
        return f"{instance}: non-rss body", []

    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        return f"{instance}: parse {exc}", []

    items: list[dict[str, Any]] = []
    # RSS 2.0 structure: root -> channel -> item*
    channel = root.find("channel")
    if channel is None:
        # Atom
        for entry in root.findall("{http://www.w3.org/2005/Atom}entry"):
            items.append(_parse_atom_entry(entry))
    else:
        for item in channel.findall("item"):
            items.append(_parse_rss_item(item))

    return "ok", items


def _parse_rss_item(item: ET.Element) -> dict[str, Any]:
    title_el = item.find("title")
    desc_el = item.find("description")
    pub_el = item.find("pubDate")
    guid_el = item.find("guid")
    link_el = item.find("link")

    text = _strip_html(desc_el.text if desc_el is not None else (title_el.text if title_el is not None else ""))
    created = None
    if pub_el is not None and pub_el.text:
        try:
            dt = email.utils.parsedate_to_datetime(pub_el.text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            created = dt.isoformat()
        except Exception:  # noqa: BLE001
            created = pub_el.text

    tid = None
    if guid_el is not None and guid_el.text:
        m = re.search(r"(\d+)", guid_el.text)
        if m:
            tid = m.group(1)
    if not tid and link_el is not None and link_el.text:
        m = re.search(r"status/(\d+)", link_el.text)
        if m:
            tid = m.group(1)

    return {
        "id": tid,
        "created_at": created,
        "text": text,
        "metrics": {},  # Nitter RSS doesn't give us engagement counts
    }


def _parse_atom_entry(entry: ET.Element) -> dict[str, Any]:
    ns = "{http://www.w3.org/2005/Atom}"
    title_el = entry.find(ns + "title")
    content_el = entry.find(ns + "content") or entry.find(ns + "summary")
    pub_el = entry.find(ns + "published") or entry.find(ns + "updated")
    id_el = entry.find(ns + "id")

    text = _strip_html(content_el.text if content_el is not None else (title_el.text if title_el is not None else ""))
    created = pub_el.text if pub_el is not None else None
    tid = None
    if id_el is not None and id_el.text:
        m = re.search(r"(\d+)", id_el.text)
        if m:
            tid = m.group(1)
    return {
        "id": tid,
        "created_at": created,
        "text": text,
        "metrics": {},
    }


async def _nitter_fetch_all(
    client: httpx.AsyncClient, usernames: list[str], cutoff: datetime
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    instances = _nitter_instances()
    # Find a working instance by probing the first username
    working_instance: str | None = None
    probe_errors: list[str] = []
    probe_user = usernames[0] if usernames else "jack"
    for inst in instances:
        status, items = await _fetch_nitter_rss_one(client, inst, probe_user)
        if status == "ok":
            working_instance = inst
            log.info("Nitter fallback: using instance %s", inst)
            break
        probe_errors.append(f"{inst}: {status}")

    if not working_instance:
        log.warning("All Nitter instances failed probes: %s", probe_errors)
        return {}, probe_errors

    sem = asyncio.Semaphore(4)

    async def _one(uname: str) -> tuple[str, list[dict[str, Any]]]:
        async with sem:
            status, items = await _fetch_nitter_rss_one(client, working_instance, uname)
        if status != "ok":
            return uname, []
        filtered: list[dict[str, Any]] = []
        for it in items:
            if it.get("created_at"):
                try:
                    dt = datetime.fromisoformat(
                        str(it["created_at"]).replace("Z", "+00:00")
                    )
                    if dt < cutoff:
                        continue
                except Exception:  # noqa: BLE001
                    pass
            filtered.append(it)
        return uname, filtered

    pairs = await asyncio.gather(*[_one(u) for u in usernames])

    data: dict[str, list[dict[str, Any]]] = {}
    for uname, msgs in pairs:
        if msgs:
            data[uname] = msgs

    return data, probe_errors


# ─── Public entry point ─────────────────────────────────────────────────
async def fetch_x_intel(
    hours: int = 24, accounts: list[str] | None = None
) -> dict[str, Any]:
    """Fetch last `hours` of tweets from `accounts`.

    Tries X API first, falls back to Nitter RSS.  Never raises.
    """
    handles = accounts or _accounts_from_env()
    if not handles:
        return {"status": "error", "error": "no_accounts_configured"}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    # ── Attempt 1: X API v2 ──
    api_errors: list[str] = []
    if X_BEARER_TOKEN:
        headers = {"Authorization": f"Bearer {X_BEARER_TOKEN}"}
        try:
            async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
                uids, resolve_errors = await _resolve_user_ids(client, handles)
                api_errors.extend(resolve_errors)
                if not uids:
                    log.warning(
                        "X user resolution empty on first attempt, retrying in 2.5s"
                    )
                    await asyncio.sleep(2.5)
                    uids, more_errors = await _resolve_user_ids(client, handles)
                    api_errors.extend(more_errors)

                if uids:
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

                    if data:
                        return {
                            "status": "ok",
                            "source": "x_api",
                            "data": data,
                            "accounts_scanned": len(data),
                            "total_tweets": total,
                        }
                    api_errors.append("x_api returned no tweets (token valid but empty)")
                else:
                    api_errors.append("x_api could not resolve any user")
        except Exception as exc:  # noqa: BLE001
            log.exception("X API path failed")
            api_errors.append(f"x_api exception: {exc}")
    else:
        api_errors.append("X_BEARER_TOKEN not configured")

    # ── Attempt 2: Nitter RSS ──
    log.warning("Falling back to Nitter. Prior X API errors: %s", api_errors[:5])
    nitter_errors: list[str] = []
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 FondoBlackCatBot/1.0"},
            timeout=15.0,
        ) as client:
            data, probe_errors = await _nitter_fetch_all(client, handles, cutoff)
            nitter_errors.extend(probe_errors)
            if data:
                total = sum(len(v) for v in data.values())
                return {
                    "status": "ok",
                    "source": "nitter",
                    "data": data,
                    "accounts_scanned": len(data),
                    "total_tweets": total,
                    "x_api_errors": api_errors[:5],
                }
    except Exception as exc:  # noqa: BLE001
        log.exception("Nitter fallback failed")
        nitter_errors.append(f"nitter exception: {exc}")

    # Nothing worked — return structured error with diagnostic detail
    return {
        "status": "error",
        "error": "all_sources_failed",
        "x_api_errors": api_errors[:10],
        "nitter_errors": nitter_errors[:10],
    }
