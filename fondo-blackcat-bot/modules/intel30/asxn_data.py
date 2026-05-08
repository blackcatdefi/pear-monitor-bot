"""ASXN HYPE Buyback / Burn / Staking / Genesis (R-INTEL30 Phase 1 #2).

Only public tracker of the AF (Assistance Fund) buyback math, HYPE burn from
spot fees, validator stake, genesis-holder balance changes. ASXN runs its own
HL validator → first-party data.

Source: https://data.asxn.xyz
No formal API. Two integration paths:
    A) Probe undocumented JSON endpoints (subject to change)
    B) Read public CDN snapshot if exposed

We try documented patterns first then a fallback HTML probe. Module degrades
gracefully on parse failure.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

CANDIDATES = [
    # known dashboard URLs — we try to find a JSON snapshot via Vercel _next/data probe
    "https://data.asxn.xyz/api/hype-stats",
    "https://data.asxn.xyz/api/buybacks",
    "https://data.asxn.xyz/api/burns",
]
DASHBOARD = "https://data.asxn.xyz/dashboard/hype"
HTTP_TIMEOUT = 10.0
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"


async def fetch_hype_stats() -> dict[str, Any]:
    """Try API endpoints first, fall back to HTML scrape with regex extract."""
    last_err = None
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": UA}) as client:
        for url in CANDIDATES:
            try:
                r = await client.get(url)
                if r.status_code == 200:
                    try:
                        return {"source": url, "data": r.json(), "_error": None}
                    except Exception:
                        pass  # not JSON, try next
            except Exception as e:
                last_err = str(e)
                continue
        # HTML fallback
        try:
            r = await client.get(DASHBOARD)
            if r.status_code == 200:
                stats = _parse_dashboard_html(r.text)
                if stats:
                    return {"source": DASHBOARD, "data": stats, "_error": None}
            return {"source": DASHBOARD, "data": {}, "_error": f"html_no_data status={r.status_code}"}
        except Exception as e:
            return {"source": None, "data": {}, "_error": last_err or str(e)}


def _parse_dashboard_html(html: str) -> dict[str, Any]:
    """Heuristic extraction of HYPE buyback / burn / staking values.

    ASXN dashboards typically embed numbers in <span> or <div> with class names
    or in __NEXT_DATA__ JSON blob. We scan for canonical labels + nearby numbers.
    """
    out: dict[str, Any] = {}

    # Try Next.js __NEXT_DATA__ (most reliable)
    nxt = re.search(r'__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if nxt:
        try:
            import json
            blob = json.loads(nxt.group(1))
            # Walk pageProps for numeric leaves with relevant keys
            keys_of_interest = ("buyback", "burn", "burned", "stake", "genesis", "totalSupply", "circulating")
            stack = [blob]
            seen = 0
            while stack and seen < 200:
                node = stack.pop()
                seen += 1
                if isinstance(node, dict):
                    for k, v in node.items():
                        if isinstance(v, (int, float)) and any(s in k.lower() for s in keys_of_interest):
                            out[k] = v
                        elif isinstance(v, (dict, list)):
                            stack.append(v)
                elif isinstance(node, list):
                    stack.extend(node)
        except Exception as e:
            log.debug("asxn next_data parse fail: %s", e)

    # Regex fallback for canonical labels
    patterns = {
        "buyback_usd_total": r"buyback[s]?\s*(?:total)?[\s:$]*([\d,]+\.?\d*)\s*[MK]?",
        "burn_hype_total": r"burn(?:ed)?\s*(?:total)?[\s:]*([\d,]+\.?\d*)",
        "stake_hype_total": r"stake[d]?\s*(?:total)?[\s:]*([\d,]+\.?\d*)",
    }
    for key, pat in patterns.items():
        m = re.search(pat, html, re.IGNORECASE)
        if m and key not in out:
            try:
                out[key] = float(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return out


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["🟪 *ASXN — HYPE Flywheel*"]
    if data.get("_error"):
        lines.append(f"  ⚠️ {data['_error'][:80]}")
        lines.append(f"  → fallback: visitar {DASHBOARD}")
        return "\n".join(lines)
    payload = data.get("data") or {}
    if not payload:
        lines.append("  • (empty)")
        lines.append(f"  → ver {DASHBOARD}")
        return "\n".join(lines)

    # Render top buyback / burn / stake values
    keys_pretty = {
        "buyback_usd_total": "AF Buyback total (USD)",
        "burn_hype_total":   "HYPE Burned",
        "stake_hype_total":  "HYPE Staked",
    }
    rendered = 0
    for k, label in keys_pretty.items():
        v = payload.get(k)
        if isinstance(v, (int, float)):
            if v > 1_000_000:
                lines.append(f"  • {label}: {v/1_000_000:,.2f}M")
            else:
                lines.append(f"  • {label}: {v:,.2f}")
            rendered += 1
    if rendered == 0:
        # Just show first 5 numeric leaves
        nums = [(k, v) for k, v in payload.items() if isinstance(v, (int, float))][:5]
        for k, v in nums:
            lines.append(f"  • {k}: {v:,.2f}")
            rendered += 1
    if rendered == 0:
        lines.append(f"  ⚠️ no se pudo extraer; ver {DASHBOARD}")
    return "\n".join(lines)


async def fetch_all() -> dict[str, Any]:
    return await fetch_hype_stats()
