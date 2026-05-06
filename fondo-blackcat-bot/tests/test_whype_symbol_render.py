"""R-DASHBOARD-DOUBLECOUNT-FIX (2026-05-06) — Bug #2 regression lock.

Background
----------
On 2026-05-06 the main flywheel rendered::

    Collateral: 0.00 UETH ($75.7K)

The collateral USD figure was correct ($75.7K), but the symbol+amount were
nonsense — the actual collateral is **1,751.18 WHYPE**. Root cause: when
the per-reserve ``balanceOf()`` RPC call failed, ``collateral_symbol`` was
left as ``None`` on the live entry → the dashboard defaulted to the first
asset in the entry's collateral list, which happened to be UETH (the debt
asset). The pre-fix ``_persist_ok`` had a fallback chain for ``debt_symbol``
(via ``primary_debt.asset`` → known-reserve map) but NOT for collateral.

Fix surface (locked down by these tests)
----------------------------------------
* ``auto.hyperlend_reader._persist_ok`` — collateral fallback chain mirrors
  the debt one: live-data → ``primary_collateral.asset`` → known-reserve
  symbol map → previously cached value. Resolved value is written back to
  the live entry so the dashboard renders the correct symbol on the FIRST
  hit, not waiting for a future cache recovery cycle.
* ``modules.dashboard._RESERVE_SYMBOLS`` — render-time last-resort lookup
  in the HTML dashboard so even if the live entry slipped past
  _persist_ok with a missing symbol, the canonical reserve map still
  resolves WHYPE / kHYPE / UBTC / etc. by their on-chain address.
* ``modules.portfolio_snapshot.WalletSnapshot.collateral_asset`` — the
  raw asset address is now propagated through the snapshot dataclass so
  the dashboard's reserve map fallback has something to look up.
"""
from __future__ import annotations

import pytest

from auto.hyperlend_reader import (
    _KNOWN_RESERVE_SYMBOLS,
    _persist_ok,
    _sym_from_asset,
)
from modules.portfolio_snapshot import WalletSnapshot


# ─── Fallback chain: live → primary_collateral.asset → cache ───────────────
def test_persist_ok_resolves_collateral_symbol_from_primary_collateral():
    """Live ``collateral_symbol`` is None but ``primary_collateral.asset``
    is the canonical WHYPE address → fallback must resolve to "WHYPE"
    and write it back to the live entry."""
    entry = {
        "status": "ok",
        "data": {
            "wallet": "0xa44e8b9522a5f710e2b63ab790465af2f155b632",
            "health_factor": 1.214,
            "total_collateral_usd": 75_700.0,
            "total_debt_usd": 45_300.0,
            # Symptom: per-reserve balanceOf RPC failed → no symbol.
            "collateral_symbol": None,
            "collateral_balance": 0.0,
            "debt_symbol": "UETH",
            "debt_balance": 19.27,
            # …but the canonical primary_collateral.asset is set.
            "primary_collateral": {
                "asset": "0x5555555555555555555555555555555555555555",
            },
            "primary_debt": None,
        },
    }
    cache: dict = {}
    _persist_ok(entry, cache)
    # Resolved value written back to the live entry — the fix's whole
    # point is the dashboard reads the FIRST hit correctly.
    assert entry["data"]["collateral_symbol"] == "WHYPE"
    # Cache is also populated for future UNKNOWN recoveries.
    cached = cache["0xa44e8b9522a5f710e2b63ab790465af2f155b632"]
    assert cached["collateral_symbol"] == "WHYPE"


def test_persist_ok_falls_back_to_existing_cache_on_total_failure():
    """Live entry AND primary_collateral both missing — the cache still
    has a previously-seen value. _persist_ok must preserve it instead of
    null-overwriting."""
    entry = {
        "status": "ok",
        "data": {
            "wallet": "0xa44e8b9522a5f710e2b63ab790465af2f155b632",
            "health_factor": 1.214,
            "total_collateral_usd": 75_700.0,
            "total_debt_usd": 45_300.0,
            "collateral_symbol": None,
            "collateral_balance": 0.0,
            "primary_collateral": None,
        },
    }
    cache = {
        "0xa44e8b9522a5f710e2b63ab790465af2f155b632": {
            "hf": 1.214,
            "collateral_symbol": "WHYPE",
            "collateral_balance": 1751.18,
            "debt_symbol": "UETH",
            "debt_balance": 19.27,
        },
    }
    _persist_ok(entry, cache)
    # Live entry now carries the previously-cached good value.
    assert entry["data"]["collateral_symbol"] == "WHYPE"
    assert entry["data"]["collateral_balance"] == pytest.approx(1751.18)


def test_persist_ok_does_not_overwrite_live_collateral_symbol():
    """If the live entry already has a valid collateral_symbol, the
    fallback must NOT clobber it. The fallback fires only on missing
    data — defense-in-depth, not aggressive override."""
    entry = {
        "status": "ok",
        "data": {
            "wallet": "0xa44e8b9522a5f710e2b63ab790465af2f155b632",
            "health_factor": 1.5,
            "total_collateral_usd": 1000.0,
            "total_debt_usd": 0.0,
            "collateral_symbol": "kHYPE",
            "collateral_balance": 50.0,
            "primary_collateral": {
                "asset": "0x5555555555555555555555555555555555555555",  # WHYPE
            },
        },
    }
    _persist_ok(entry, {})
    # Live value preserved.
    assert entry["data"]["collateral_symbol"] == "kHYPE"


def test_known_reserve_symbol_map_includes_whype():
    """Defense lockdown: the known-reserve symbol map MUST contain the
    WHYPE address (0x5555…5555). This was the address the main flywheel
    needed to resolve and the absence of which left the live entry with
    ``collateral_symbol=None`` for months pre-fix."""
    assert _sym_from_asset("0x5555555555555555555555555555555555555555") == "WHYPE"
    assert (
        _KNOWN_RESERVE_SYMBOLS["0x5555555555555555555555555555555555555555"]
        == "WHYPE"
    )


def test_sym_from_asset_handles_case_insensitivity():
    """The known-reserve map keys are stored lowercase; lookup must
    normalise input."""
    assert _sym_from_asset("0x5555555555555555555555555555555555555555") == "WHYPE"
    assert _sym_from_asset("0x5555555555555555555555555555555555555555".upper()) == "WHYPE"
    # Mixed case
    mixed = "0x5555555555555555555555555555555555555555"
    assert _sym_from_asset(mixed) == "WHYPE"


def test_sym_from_asset_returns_none_on_unknown_address():
    """Unknown addresses must return None so the caller can fall through
    to its next fallback (e.g. existing cache, render-time map)."""
    assert _sym_from_asset(None) is None
    assert _sym_from_asset("") is None
    assert _sym_from_asset("0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef") is None


# ─── Render-time fallback: dashboard's _RESERVE_SYMBOLS map ────────────────
def test_dashboard_reserve_map_mirrors_hyperlend_reader():
    """The dashboard maintains its own copy of the reserve symbol map
    (avoiding a templates → auto import cycle). It MUST stay in lockstep
    with hyperlend_reader's _KNOWN_RESERVE_SYMBOLS."""
    # Re-derive the dashboard's local map by parsing dashboard source.
    # Cheap and avoids importing dashboard (which pulls aiohttp).
    import re
    from pathlib import Path

    src = Path(__file__).parent.parent / "modules" / "dashboard.py"
    text = src.read_text(encoding="utf-8")
    # Grab the _RESERVE_SYMBOLS dict literal lines.
    block_match = re.search(
        r"_RESERVE_SYMBOLS\s*=\s*\{(.+?)\}",
        text,
        flags=re.DOTALL,
    )
    assert block_match, "dashboard module no longer declares _RESERVE_SYMBOLS"
    block = block_match.group(1)
    pairs = re.findall(r'"(0x[0-9a-fA-F]{40})"\s*:\s*"([^"]+)"', block)
    dashboard_map = {addr.lower(): sym for addr, sym in pairs}
    # Every entry in the auth map must also be in the dashboard map.
    for addr, sym in _KNOWN_RESERVE_SYMBOLS.items():
        assert dashboard_map.get(addr) == sym, (
            f"dashboard _RESERVE_SYMBOLS drift: {addr} → "
            f"{dashboard_map.get(addr)!r} (expected {sym!r})"
        )


# ─── WalletSnapshot propagates collateral_asset for render-time fallback ───
def test_wallet_snapshot_carries_collateral_asset_field():
    """Bug #2 fix requires the dashboard renderer to have access to the
    raw asset address as a last-resort fallback when collateral_symbol
    is missing. WalletSnapshot must surface it."""
    ws = WalletSnapshot(
        address="0xa44e8b9522a5f710e2b63ab790465af2f155b632",
        label="Main Flywheel (DDS)",
        short="0xa44e…b632",
        hl_collateral_usd=75_700.0,
        hl_debt_usd=45_300.0,
        collateral_symbol=None,
        collateral_balance=0.0,
        collateral_asset="0x5555555555555555555555555555555555555555",
    )
    assert ws.collateral_asset == "0x5555555555555555555555555555555555555555"


def test_wallet_snapshot_collateral_asset_defaults_to_none():
    """Existing fixture / call-sites that don't yet pass collateral_asset
    must keep working — backwards-compat default is None."""
    ws = WalletSnapshot(
        address="0xc7ae23316b47f7e75f455f53ad37873a18351505",
        label="Trading",
        short="0xc7ae…1505",
    )
    assert ws.collateral_asset is None


# ─── End-to-end: missing live symbol resolves via reserve map ──────────────
def test_render_path_resolves_whype_via_reserve_map():
    """Simulate the full chain: live entry has no collateral_symbol but
    has the canonical asset address. After _persist_ok the entry should
    render WHYPE — no UETH cross-contamination."""
    entry = {
        "status": "ok",
        "data": {
            "wallet": "0xa44e8b9522a5f710e2b63ab790465af2f155b632",
            "health_factor": 1.214,
            "total_collateral_usd": 75_700.0,
            "total_debt_usd": 45_300.0,
            # Pre-fix bug: symbol came back as the debt asset's symbol.
            "collateral_symbol": None,
            "collateral_balance": 0.0,
            "primary_collateral": {
                "asset": "0x5555555555555555555555555555555555555555",
            },
            "debt_symbol": "UETH",
            "debt_balance": 19.27,
        },
    }
    _persist_ok(entry, {})
    # The collateral side must be WHYPE, NOT UETH.
    assert entry["data"]["collateral_symbol"] == "WHYPE"
    assert entry["data"]["debt_symbol"] == "UETH"
    assert entry["data"]["collateral_symbol"] != entry["data"]["debt_symbol"]
