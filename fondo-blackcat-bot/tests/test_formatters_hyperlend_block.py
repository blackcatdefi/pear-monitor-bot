"""R-HF-RENDER (3 may 2026) — regression tests for HYPERLEND block in /reporte.

REGRESSION (3 may 18:29 UTC): /reporte rendered

    HYPERLEND
      [0xa44e8b95] 0xa44e…b632
        HF: nan
        Collateral: $71.8K
        Borrowed: $45.0K
        Available borrow: $0.00
        LTV: 0.0% | LiqThr: 0.0%

while the HF<1.20 alert fired correctly (HF=1.2001) and the LLM analyzer
saw the right value. Inconsistency: the formatter wasn't reading
``hf_status`` from auto.hyperlend_reader.

Fix: branch on hf_status ∈ {OK, UNKNOWN, ZERO} so the block NEVER shows
literal "nan", and so UNKNOWN entries render as last-known + age (or as
"offline" when the cache is also empty).
"""
from __future__ import annotations

import os
import sys

# Make the project importable when running pytest from project root.
_HERE = os.path.dirname(__file__)
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from templates.formatters import format_hf, format_quick_positions  # noqa: E402


WALLET_FLY = "0xa44e0000000000000000000000000000000000ae"
WALLET_PRINCIPAL = "0xcddf0000000000000000000000000000000000ae"


def _ok_entry(addr: str, hf: float, coll: float, debt: float, label: str = "Reserva histórica") -> dict:
    return {
        "status": "ok",
        "hf_status": "OK",
        "label": label,
        "data": {
            "wallet": addr,
            "label": label,
            "total_collateral_usd": coll,
            "total_debt_usd": debt,
            "available_borrows_usd": 0.0,
            "current_liquidation_threshold": 0.752,
            "ltv": 0.617,
            "health_factor": hf,
            "collateral_assets": [],
            "debt_assets": [],
            "primary_collateral": None,
            "primary_debt": None,
            "collateral_symbol": None,
            "collateral_balance": 0.0,
            "debt_symbol": None,
            "debt_balance": 0.0,
        },
    }


def _unknown_entry(addr: str, last_hf, age_seconds, last_coll, last_debt, label: str = "Reserva histórica") -> dict:
    """Synthetic UNKNOWN entry as produced by hyperlend_reader on RPC failure."""
    return {
        "status": "ok",
        "hf_status": "UNKNOWN",
        "label": label,
        "data": {
            "wallet": addr,
            "label": label,
            # Reader sets these to NaN / 0 sentinels for UNKNOWN entries.
            "total_collateral_usd": last_coll or 0.0,
            "total_debt_usd": last_debt or 0.0,
            "available_borrows_usd": 0.0,
            "current_liquidation_threshold": 0.0,
            "ltv": 0.0,
            "health_factor": float("nan"),
            "last_known_hf": last_hf,
            "last_known_at_iso": "2026-05-03T17:36:00+00:00",
            "last_known_collateral_usd": last_coll,
            "last_known_debt_usd": last_debt,
            "age_seconds": age_seconds,
            "recovered_from_cache": True,
            "collateral_assets": [],
            "debt_assets": [],
            "primary_collateral": None,
            "primary_debt": None,
            "collateral_symbol": None,
            "collateral_balance": 0.0,
            "debt_symbol": None,
            "debt_balance": 0.0,
        },
    }


# ─── REGRESSION TEST 1 ────────────────────────────────────────────────
def test_hyperlend_block_ok_renders_real_hf_no_nan() -> None:
    """REGRESSION 3 may 18:29 UTC — /reporte must show real HF, not 'nan'.

    With an OK entry (real HF=1.2001, collateral $71.8K, borrowed $45K,
    LTV 61.7%, LiqThr 75.2%) the rendered HYPERLEND block must contain
    the real values and MUST NOT contain literal 'nan' or '0.0%'.
    """
    hl = [_ok_entry(WALLET_FLY, hf=1.2001, coll=71800.0, debt=45000.0)]
    block = format_quick_positions(wallets=[], hyperlend=hl)

    assert "HYPERLEND" in block
    # Bug-bait literals that previously appeared in /reporte
    lower = block.lower()
    assert "nan" not in lower, f"NaN literal in block:\n{block}"
    assert "0.0%" not in block, f"0.0% literal in block:\n{block}"
    # Real values must show
    assert "1.200" in block  # _fmt_hf prints 3 decimals
    assert "61.7%" in block
    assert "75.2%" in block
    assert "$71.8K" in block
    assert "$45.0K" in block


# ─── REGRESSION TEST 2 ────────────────────────────────────────────────
def test_hyperlend_block_unknown_status_renders_gracefully_with_cache() -> None:
    """UNKNOWN entry (RPC rate-limited) with cache must render last-known
    HF + age, not 'nan' / '0.0%'.
    """
    hl = [_unknown_entry(WALLET_FLY, last_hf=1.2150, age_seconds=23 * 60,
                        last_coll=71800.0, last_debt=45000.0)]
    block = format_quick_positions(wallets=[], hyperlend=hl)

    assert "HYPERLEND" in block
    lower = block.lower()
    assert "nan" not in lower
    # Either explicit "rate-limited" wording or last-HF visible
    assert ("rate-limited" in lower) or ("1.215" in block)
    # Last-known HF must show with 3 decimals
    assert "1.215" in block
    # Age label
    assert "23min" in block
    # Last cached collateral surfaced
    assert "$71.8K" in block


# ─── REGRESSION TEST 3 ────────────────────────────────────────────────
def test_hyperlend_block_unknown_status_no_cache_renders_offline() -> None:
    """UNKNOWN entry with NO cache (last_known_hf=None) must render
    'offline' / 'no cache available' instead of 'nan'.
    """
    hl = [_unknown_entry(WALLET_FLY, last_hf=None, age_seconds=None,
                        last_coll=None, last_debt=None)]
    block = format_quick_positions(wallets=[], hyperlend=hl)

    lower = block.lower()
    assert "nan" not in lower
    assert ("offline" in lower) or ("no cache available" in lower) or ("no prior cached read" in lower)


# ─── ADDITIONAL COVERAGE ──────────────────────────────────────────────
def test_format_hf_helper_handles_nan_returns_dash() -> None:
    """The standalone /hf command must also degrade gracefully on NaN."""
    hl = [_unknown_entry(WALLET_FLY, last_hf=1.2150, age_seconds=600,
                        last_coll=71800.0, last_debt=45000.0)]
    out = format_hf(hl)
    assert "nan" not in out.lower()
    assert "1.215" in out


def test_format_hf_helper_offline_no_cache() -> None:
    hl = [_unknown_entry(WALLET_FLY, last_hf=None, age_seconds=None,
                        last_coll=None, last_debt=None)]
    out = format_hf(hl)
    assert "nan" not in out.lower()
    assert ("offline" in out.lower()) or ("no cached" in out.lower())


def test_hyperlend_block_inf_hf_no_debt_renders_infinity() -> None:
    """An OK entry with HF=inf (collateral, no debt) must NOT show 'nan'.
    The `_fmt_hf` helper renders this as '∞ (no debt)'.
    """
    hl = [_ok_entry(WALLET_FLY, hf=float("inf"), coll=1000.0, debt=0.0)]
    block = format_quick_positions(wallets=[], hyperlend=hl)
    assert "nan" not in block.lower()
    assert "∞" in block


def test_legacy_entry_without_hf_status_still_renders_ok() -> None:
    """Backwards-compat: entries from older fetch paths (no hf_status
    field at all) must still render via the OK branch.
    """
    hl = [_ok_entry(WALLET_FLY, hf=1.45, coll=10000.0, debt=4000.0)]
    # Strip the hf_status to simulate a legacy entry
    del hl[0]["hf_status"]
    block = format_quick_positions(wallets=[], hyperlend=hl)
    assert "1.450" in block
    assert "nan" not in block.lower()


def test_two_unknown_wallets_render_both_with_cache_and_offline() -> None:
    """Mixed batch — flywheel cached, principal offline — must render
    both correctly without ever emitting 'nan'.
    """
    hl = [
        _unknown_entry(WALLET_FLY, last_hf=1.20, age_seconds=900,
                      last_coll=71800.0, last_debt=45000.0,
                      label="Flywheel"),
        _unknown_entry(WALLET_PRINCIPAL, last_hf=None, age_seconds=None,
                      last_coll=None, last_debt=None,
                      label="Principal"),
    ]
    block = format_quick_positions(wallets=[], hyperlend=hl)
    assert "nan" not in block.lower()
    assert "1.200" in block
    assert "rate-limited" in block.lower()
    # Principal renders offline
    assert ("offline" in block.lower()) or ("no prior cached read" in block.lower()) or ("no cache" in block.lower())
