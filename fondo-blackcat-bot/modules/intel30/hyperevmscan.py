"""HyperEVMScan via Etherscan v2 unified key — chainid 999 (R-PERFECT Sub-1 #2).

Endpoint: https://api.etherscan.io/v2/api?chainid=999&module=...&action=...
Free key: signup at etherscan.io (works across 50+ chains).

Surface: ETH supply analogue (HYPE supply on HyperEVM), gas oracle, txcount probe.
Module degrades gracefully when ETHERSCAN_API_KEY is not set.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from modules.intel30._intel_base import (
    GRACEFUL_NO_KEY,
    LIVE,
    get_json,
    graceful_no_key_payload,
    log_call,
)

log = logging.getLogger(__name__)

API_KEY = os.getenv("ETHERSCAN_API_KEY", "").strip()
BASE = "https://api.etherscan.io/v2/api"
CHAIN_ID = 999  # HyperEVM
SOURCE = "hyperevmscan"


async def fetch_all() -> dict[str, Any]:
    if not API_KEY:
        return graceful_no_key_payload(
            SOURCE,
            "https://etherscan.io/myapikey",
            "ETHERSCAN_API_KEY",
        )

    series: list[dict[str, Any]] = []

    # 1) Latest block (analogue of stats/ethsupply doesn't apply to HyperEVM —
    # use proxy method that works across all v2 chains: eth_blockNumber)
    block_data, _meta = await get_json(
        SOURCE, BASE,
        params={
            "chainid": CHAIN_ID, "module": "proxy",
            "action": "eth_blockNumber", "apikey": API_KEY,
        },
        timeout=10.0,
    )
    if isinstance(block_data, dict):
        result = block_data.get("result", "")
        try:
            block = int(result, 16) if isinstance(result, str) else None
            series.append({"label": "block_number", "valor": block, "_error": None})
        except (ValueError, TypeError):
            series.append({"label": "block_number", "_error": "parse"})

    # 2) Gas oracle
    gas_data, _meta2 = await get_json(
        SOURCE, BASE,
        params={
            "chainid": CHAIN_ID, "module": "gastracker",
            "action": "gasoracle", "apikey": API_KEY,
        },
        timeout=10.0,
    )
    if isinstance(gas_data, dict) and gas_data.get("status") == "1":
        result = gas_data.get("result", {}) or {}
        try:
            series.append({
                "label": "gas_safe_gwei",
                "valor": float(result.get("SafeGasPrice", 0)),
                "_error": None,
            })
            series.append({
                "label": "gas_propose_gwei",
                "valor": float(result.get("ProposeGasPrice", 0)),
                "_error": None,
            })
        except (ValueError, TypeError):
            pass

    if not series:
        return {"_global_error": "all probes failed", "series": []}
    log_call(SOURCE, LIVE, 0, 0, 200, "summary")
    return {"series": series, "_global_error": None}


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["🔭 *HyperEVMScan (Etherscan v2)*"]
    if data.get("_status") == GRACEFUL_NO_KEY or data.get("_global_error", "").endswith("not set"):
        lines.append("  ⚠️ ETHERSCAN_API_KEY not set")
        lines.append("  → free signup: etherscan.io/myapikey")
        return "\n".join(lines)
    if data.get("_global_error"):
        lines.append(f"  ⚠️ {data['_global_error']}")
        return "\n".join(lines)
    series = data.get("series", []) or []
    for s in series:
        if not isinstance(s, dict) or s.get("_error"):
            continue
        label = s.get("label", "?")
        val = s.get("valor")
        if val is None:
            continue
        if "block" in label and isinstance(val, int):
            lines.append(f"  • {label}: {val:,}")
        elif "gwei" in label:
            lines.append(f"  • {label}: {val:.3f}")
        else:
            lines.append(f"  • {label}: {val}")
    return "\n".join(lines)
