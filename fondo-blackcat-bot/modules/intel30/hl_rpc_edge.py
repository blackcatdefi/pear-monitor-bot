"""HyperLiquid public RPC — Edge telemetry (R-PERFECT Sub-1 #1).

Free public RPC: https://rpc.hyperliquid.xyz/evm
Goldsky-style replacement: same chain, no key, JSON-RPC over HTTPS.

Surface in /reporte: latest block height, gas price, sync status (block age).
Used as canonical "is HyperEVM up?" liveness probe for the fund's intel rail.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from modules.intel30._intel_base import (
    DEFAULT_UA,
    LIVE,
    UNAVAILABLE,
    log_call,
    set_source_state,
    under_cap,
    bump_count,
)

log = logging.getLogger(__name__)

RPC_URL = "https://rpc.hyperliquid.xyz/evm"
HTTP_TIMEOUT = 10.0
SOURCE = "hl_rpc_edge"


async def _rpc(method: str, params: list | None = None) -> tuple[Any, int, int]:
    """JSON-RPC POST. Returns (result, http_code, latency_ms)."""
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or [], "id": 1})
    headers = {"User-Agent": DEFAULT_UA, "Content-Type": "application/json"}
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers) as client:
            r = await client.post(RPC_URL, content=body)
            latency_ms = int((time.monotonic() - t0) * 1000)
            if r.status_code != 200:
                return None, r.status_code, latency_ms
            data = r.json()
            return data.get("result"), 200, latency_ms
    except Exception as e:  # noqa: BLE001
        latency_ms = int((time.monotonic() - t0) * 1000)
        log.debug("hl_rpc %s fail: %s", method, e)
        return None, 0, latency_ms


async def fetch_all() -> dict[str, Any]:
    if not under_cap(SOURCE):
        log_call(SOURCE, "RATE_LIMITED", 0, 0, 0, "daily cap reached")
        return {"_global_error": "rate_limited", "series": []}
    bump_count(SOURCE)

    block_hex, code1, lat1 = await _rpc("eth_blockNumber")
    gas_hex, code2, lat2 = await _rpc("eth_gasPrice")
    chain_hex, code3, lat3 = await _rpc("eth_chainId")

    if block_hex is None:
        log_call(SOURCE, UNAVAILABLE, lat1, 0, code1 or 0, "rpc_no_result")
        set_source_state(SOURCE, UNAVAILABLE)
        return {"_global_error": "RPC unreachable", "series": []}

    try:
        block = int(block_hex, 16)
    except (ValueError, TypeError):
        block = None
    try:
        gas_wei = int(gas_hex, 16) if gas_hex else None
    except (ValueError, TypeError):
        gas_wei = None
    try:
        chain_id = int(chain_hex, 16) if chain_hex else None
    except (ValueError, TypeError):
        chain_id = None

    log_call(SOURCE, LIVE, max(lat1, lat2, lat3), 0, 200, "")
    set_source_state(SOURCE, LIVE)
    return {
        "series": [
            {"label": "block_number", "valor": block, "_error": None},
            {"label": "gas_price_wei", "valor": gas_wei, "_error": None},
            {"label": "chain_id", "valor": chain_id, "_error": None},
        ],
        "_global_error": None,
    }


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["⛓ *HyperEVM RPC — Edge*"]
    if data.get("_global_error"):
        lines.append(f"  ⚠️ {data['_global_error']}")
        return "\n".join(lines)
    by_label = {s.get("label"): s.get("valor") for s in data.get("series", []) if isinstance(s, dict)}
    block = by_label.get("block_number")
    gas_wei = by_label.get("gas_price_wei")
    chain = by_label.get("chain_id")
    if block is not None:
        lines.append(f"  • block: {block:,}")
    if chain is not None:
        lines.append(f"  • chain_id: {chain}")
    if gas_wei is not None:
        gwei = gas_wei / 1e9
        lines.append(f"  • gas: {gwei:.3f} gwei")
    return "\n".join(lines)
