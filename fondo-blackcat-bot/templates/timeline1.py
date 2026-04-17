"""Compact formatter for the /timeline command — renders the last 48h of
X timeline activity from tracked accounts.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _short(txt: str, n: int = 220) -> str:
    t = (txt or "").replace("\n", " ").strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def _engagement(m: dict[str, Any]) -> int:
    return (
        int(m.get("like_count") or 0)
        + int(m.get("retweet_count") or 0) * 2
        + int(m.get("reply_count") or 0)
        + int(m.get("quote_count") or 0) * 2
    )


def format_timeline(x_intel: dict[str, Any] | None, top_n: int = 40) -> str:
    """Render a compact Spanish-language summary of last-48h tweets.

    Input is the dict returned by `fetch_x_intel(hours=48)`.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if not isinstance(x_intel, dict):
        return f"🐦 Timeline X — {now}\n\n— Sin datos."

    status = x_intel.get("status")
    if status != "ok":
        err = x_intel.get("error", "unknown_error")
        return (
            f"🐦 Timeline X — {now}\n\n"
            f"❌ No se pudo leer el timeline: {err}\n"
            "(verificá X_BEARER_TOKEN en Railway)"
        )

    data: dict[str, list[dict[str, Any]]] = x_intel.get("data") or {}
    scanned = x_intel.get("accounts_scanned", 0)
    total = x_intel.get("total_tweets", 0)

    # Flatten all tweets, tag with author, sort by engagement desc.
    flat: list[tuple[str, dict[str, Any]]] = []
    for uname, tweets in data.items():
        for t in tweets or []:
            flat.append((uname, t))
    flat.sort(key=lambda p: _engagement(p[1].get("metrics") or {}), reverse=True)

    header = (
        f"🐦 Timeline X (últimas 48h) — {now}\n"
        f"Cuentas con actividad: {scanned} | Tweets: {total} | "
        f"Mostrando top {min(top_n, len(flat))} por engagement\n"
        "─────────────────────────────"
    )
    if not flat:
        return header + "\n\n— Sin tweets nuevos en las últimas 48h."

    lines: list[str] = [header]
    for uname, t in flat[:top_n]:
        m = t.get("metrics") or {}
        eng = _engagement(m)
        likes = int(m.get("like_count") or 0)
        rts = int(m.get("retweet_count") or 0)
        ts = t.get("created_at", "")[:16].replace("T", " ")
        tid = t.get("id", "")
        url = f"https://x.com/{uname}/status/{tid}" if tid else ""
        lines.append("")
        lines.append(f"• @{uname} · {ts} · ♥{likes} 🔁{rts} (score {eng})")
        lines.append(f"  {_short(t.get('text', ''))}")
        if url:
            lines.append(f"  {url}")

    return "\n".join(lines)
