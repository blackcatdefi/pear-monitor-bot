"""Farside Investors ETF Flows (R-INTEL30 Phase 1 #5).

De facto industry-standard daily BTC/ETH/SOL spot ETF flows. No formal API —
HTML scrape of the public table. Cited by Bloomberg/CoinDesk/Reuters.

URLs:
    https://farside.co.uk/btc/   — BTC spot ETFs
    https://farside.co.uk/eth/   — ETH spot ETFs
    https://farside.co.uk/sol/   — SOL spot ETFs (newer)

100% free, no login. Daily refresh ~9-11pm ET.

Implementation note: We avoid pandas dependency on Railway by using simple regex
parsing of the table summary row (last row = total flow for the latest day).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

URLS = {
    "BTC": "https://farside.co.uk/btc/",
    "ETH": "https://farside.co.uk/eth/",
    "SOL": "https://farside.co.uk/sol/",
}
HTTP_TIMEOUT = 12.0

# Browser UA + full header stack to bypass Cloudflare 403 on Farside
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
    "Accept-Encoding": "gzip, deflate",  # NO 'br' — httpx without brotli=python lib won't decode it
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def _parse_latest_row(html: str) -> dict[str, Any]:
    """Extract latest data row from Farside table.

    Farside HTML structure has <tr><td>DATE</td><td>...</td><td>TOTAL</td></tr>.
    We grab all rows, pick the last one with a date in DD MMM YYYY format,
    and return the date + total flow column (always last numeric column).
    """
    # rows
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.DOTALL | re.IGNORECASE)
    latest_date = None
    latest_total = None
    pending = False
    for row in reversed(rows):  # bottom-up = newest first in Farside layout
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.DOTALL | re.IGNORECASE)
        if len(cells) < 2:
            continue
        # strip tags from cells
        clean = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        # Date format: "07 May 2026"
        if not re.match(r"^\d{1,2}\s+\w+\s+\d{4}$", clean[0]):
            continue
        # Parse every numeric cell in the row; last numeric col = total.
        numeric: list[float] = []
        for c in clean[1:]:
            cleaned = c.replace(",", "").replace("(", "-").replace(")", "").strip()
            try:
                numeric.append(float(cleaned))
            except (TypeError, ValueError):
                continue
        if numeric:
            latest_date = clean[0]
            latest_total = numeric[-1]
            # R-BOT-DEFINITIVE WI-9b: BEFORE the daily publication Farside
            # pre-fills the row with zeros — a 0.0 total with NO non-zero
            # per-ETF cell is "pending", NOT a real net-zero day. A real zero
            # requires at least one explicitly non-zero per-ETF cell.
            if latest_total == 0.0 and not any(abs(x) > 0 for x in numeric[:-1]):
                pending = True
            break
    return {"date": latest_date, "total_flow_musd": latest_total, "pending": pending}


BITBO_URLS = {
    "BTC": "https://bitbo.io/treasuries/etf-flows/",
    # bitbo only tracks BTC ETFs publicly; ETH/SOL fallback is best-effort
}


def _parse_bitbo_row(html: str) -> dict[str, Any]:
    """Extract latest BTC ETF flow row from bitbo HTML table.

    Format: <tr><td>Date</td><td>...</td><td>...Total</td></tr>
    Latest row is at the top of the table on bitbo.
    """
    # Look for first row with a date in MMMM DD, YYYY (or similar) and a numeric total
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.DOTALL | re.IGNORECASE)
    for row in rows:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.DOTALL | re.IGNORECASE)
        if len(cells) < 3:
            continue
        clean = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        # Date heuristics
        date_cell = clean[0]
        date_m = re.search(r"(\w+\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+\w+\s+\d{4}|\d{4}-\d{2}-\d{2})", date_cell)
        if not date_m:
            continue
        # Find a numeric cell that looks like a total (large, +/- M magnitude)
        for c in reversed(clean[1:]):
            cleaned = c.replace(",", "").replace("$", "").replace("M", "").replace("(", "-").replace(")", "").strip()
            try:
                v = float(cleaned)
                if abs(v) < 100000:  # filter out reasonable M-scale numbers
                    return {"date": date_m.group(1), "total_flow_musd": v}
            except (TypeError, ValueError):
                continue
    return {"date": None, "total_flow_musd": None}


async def fetch_etf(asset: str) -> dict[str, Any]:
    url = URLS.get(asset.upper())
    if not url:
        return {"asset": asset, "_error": "unknown_asset"}
    last_err = None
    # Try canonical Farside first
    try:
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            headers=BROWSER_HEADERS,
            follow_redirects=True,
            http2=False,
        ) as client:
            r = await client.get(url)
            if r.status_code == 200:
                parsed = _parse_latest_row(r.text)
                if parsed.get("date") is not None:
                    return {
                        "asset": asset.upper(),
                        "date": parsed["date"],
                        "flow_musd": parsed["total_flow_musd"],
                        "pending": bool(parsed.get("pending")),
                        "source": "farside",
                        "_error": None,
                    }
                last_err = "farside_parse_failed"
            else:
                last_err = f"farside_http_{r.status_code}"
    except Exception as e:
        log.warning("farside %s fail: %s", asset, e)
        last_err = str(e)[:60]

    # Fallback: bitbo.io (BTC only)
    bitbo_url = BITBO_URLS.get(asset.upper())
    if bitbo_url:
        try:
            async with httpx.AsyncClient(
                timeout=HTTP_TIMEOUT,
                headers=BROWSER_HEADERS,
                follow_redirects=True,
            ) as client:
                r = await client.get(bitbo_url)
                if r.status_code == 200:
                    parsed = _parse_bitbo_row(r.text)
                    if parsed.get("date") is not None:
                        return {
                            "asset": asset.upper(),
                            "date": parsed["date"],
                            "flow_musd": parsed["total_flow_musd"],
                            "source": "bitbo",
                            "_error": None,
                        }
                    last_err = "bitbo_parse_failed"
                else:
                    last_err = f"bitbo_http_{r.status_code}"
        except Exception as e:
            log.warning("bitbo %s fail: %s", asset, e)
            last_err = str(e)[:60]

    return {"asset": asset, "_error": last_err or "all_sources_failed"}


async def fetch_all() -> dict[str, Any]:
    tasks = [fetch_etf(a) for a in URLS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: list[dict[str, Any]] = []
    for r in results:
        if isinstance(r, Exception):
            out.append({"_error": str(r)})
        else:
            out.append(r)
    return {"flows": out}


def format_for_telegram(data: dict[str, Any]) -> str:
    flows = data.get("flows") or []
    lines = ["💰 *Farside — Spot ETF Flows*"]
    if not flows:
        return "\n".join(lines + ["  ⚠️ sin datos"])
    rendered = 0
    failed_assets = []
    for f in flows:
        if not isinstance(f, dict):
            continue
        asset = f.get("asset", "?")
        if f.get("_error"):
            failed_assets.append(asset)
            continue
        d = f.get("date", "?")
        flow = f.get("flow_musd")
        src = f.get("source") or "farside"
        # R-BOT-DEFINITIVE WI-9b: a pre-publication 0.0 renders "pending" —
        # a real $0.0M is shown ONLY when the source explicitly shows zero.
        if f.get("pending"):
            lines.append(f"  ⏳ {asset}: pending (sin publicar aún — {d})")
            rendered += 1
            continue
        if isinstance(flow, (int, float)):
            arrow = "📈" if flow > 0 else ("📉" if flow < 0 else "➖")
            tag = f" [{src}]" if src != "farside" else ""
            lines.append(f"  {arrow} {asset}: ${flow:+,.1f}M ({d}){tag}")
            rendered += 1
    if failed_assets:
        lines.append(f"  ⚠️ {','.join(failed_assets)} bloqueados (CF1010 datacenter)")
        lines.append("  → ver: farside.co.uk / sosovalue.com")
    if rendered == 0 and not failed_assets:
        lines.append("  ⚠️ sin datos")
    return "\n".join(lines)
