"""Epoch AI — model size + compute trends (R-PERFECT Sub-4 #3).

Source: https://epoch.ai/data/...  (CSV downloads, no key)
Free, no auth. Polled weekly is enough.

Tracks: notable_models.csv (model count + median compute trend).
"""
from __future__ import annotations

import csv
import io
import logging
from typing import Any

from modules.intel30._intel_base import LIVE, get_text, log_call

log = logging.getLogger(__name__)

SOURCE = "epoch_ai"
URL = "https://epoch.ai/data/notable_ai_models.csv"


async def fetch_all() -> dict[str, Any]:
    text, meta = await get_text(SOURCE, URL, timeout=15.0)
    if not text:
        return {"_global_error": meta.get("reason", "fetch_failed"), "series": []}
    try:
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
    except Exception as e:  # noqa: BLE001
        return {"_global_error": f"csv_parse: {e}", "series": []}
    # Pick top 5 most-recent rows
    rows.sort(key=lambda r: r.get("Publication date", ""), reverse=True)
    series = []
    for r in rows[:5]:
        try:
            series.append({
                "label": (r.get("System") or r.get("Model") or "?")[:40],
                "fecha": (r.get("Publication date") or "")[:10],
                "training_compute": r.get("Training compute (FLOP)", ""),
                "_error": None,
            })
        except Exception:  # noqa: BLE001
            continue
    log_call(SOURCE, LIVE, meta["latency_ms"], meta["bytes"], 200, f"{len(rows)} rows")
    return {"series": series, "_global_error": None, "_total_rows": len(rows)}


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["🧠 *Epoch AI — recent notable models*"]
    if data.get("_global_error"):
        lines.append(f"  ⚠️ {data['_global_error']}")
        return "\n".join(lines)
    total = data.get("_total_rows", 0)
    if total:
        lines.append(f"  · total tracked: {total}")
    for s in data.get("series", []):
        if not isinstance(s, dict) or s.get("_error"):
            continue
        lab = s.get("label", "?")
        fecha = s.get("fecha", "")
        lines.append(f"  • {lab} ({fecha})")
    return "\n".join(lines)
