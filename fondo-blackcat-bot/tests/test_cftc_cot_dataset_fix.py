"""R-ONDEMAND (2026-05-09) — guard against /cot regression.

Bug: prior dataset code ``6dca-aqww`` is the legacy COT (Futures Only)
schema, which only carries ``noncomm_*`` and ``comm_*`` fields. The parser
was reading ``dealer_positions_*`` and ``lev_money_positions_*``, both
absent on legacy → silently coerced to 0 by ``_f()``. /cot rendered every
contract with ``lev_funds net=+0 · dealer net=+0``.

Fix: switch to TFF dataset ``gpe5-46if`` which exposes the right fields.

These tests are pure (no network): they assert the dataset URL and stub a
TFF response to confirm the parser now extracts non-zero net positions.
"""
from __future__ import annotations

import importlib

import pytest


def test_dataset_is_tff_not_legacy():
    import modules.intel30.cftc_cot as cot
    importlib.reload(cot)
    assert cot.DATASET == "gpe5-46if", (
        "TFF dataset code mismatch — /cot will render zeros if this regresses"
    )
    # Sanity: explicit assertion against the broken legacy id.
    assert cot.DATASET != "6dca-aqww"


@pytest.mark.asyncio
async def test_parser_extracts_nonzero_nets(monkeypatch):
    """Stub get_json with a real TFF row shape (BITCOIN 2026-05-05) and
    confirm the parser surfaces the right net positions."""
    import modules.intel30.cftc_cot as cot
    importlib.reload(cot)

    fake_row = {
        "report_date_as_yyyy_mm_dd": "2026-05-05T00:00:00.000",
        "market_and_exchange_names": "BITCOIN - CHICAGO MERCANTILE EXCHANGE",
        "dealer_positions_long_all": "6891",
        "dealer_positions_short_all": "2422",
        "asset_mgr_positions_long": "7096",
        "asset_mgr_positions_short": "909",
        "lev_money_positions_long": "4148",
        "lev_money_positions_short": "15886",
    }

    async def _stub(*args, **kwargs):  # noqa: ANN001
        return [fake_row], {"reason": "ok"}

    monkeypatch.setattr(cot, "get_json", _stub)
    out = await cot.fetch_latest_per_contract("BITCOIN")

    assert out["_error"] is None
    # Spot-check raw fields → derived nets in the formatter.
    assert out["long_dealer"] == 6891.0
    assert out["short_dealer"] == 2422.0
    assert out["long_levfunds"] == 4148.0
    assert out["short_levfunds"] == 15886.0

    text = cot.format_for_telegram({"series": [out], "_global_error": None})
    # dealer net = +4,469 ; lev_funds net = -11,738 ; asset_mgr net = +6,187
    assert "dealer net: +4,469" in text
    assert "lev_funds net: -11,738" in text
    assert "asset_mgr net: +6,187" in text
    # Negative regression guard — the broken output is gone.
    assert "lev_funds net: +0" not in text
    assert "dealer net: +0" not in text


@pytest.mark.asyncio
async def test_legacy_schema_returns_zeros_documented(monkeypatch):
    """Negative test that documents *why* the legacy dataset is wrong.

    If we ever accidentally repoint at ``6dca-aqww`` again, the parser
    would see ``noncomm_*``/``comm_*`` fields, miss every TFF field, and
    fall back to zeros. This test pins that behavior so a future regression
    is visible in the assertion (not just runtime data quality)."""
    import modules.intel30.cftc_cot as cot
    importlib.reload(cot)
    legacy_row = {
        "report_date_as_yyyy_mm_dd": "2026-05-05T00:00:00.000",
        "noncomm_positions_long_all": "19301",
        "noncomm_positions_short_all": "17860",
        "comm_positions_long_all": "349",
        "comm_positions_short_all": "1888",
    }

    async def _stub(*args, **kwargs):  # noqa: ANN001
        return [legacy_row], {"reason": "ok"}

    monkeypatch.setattr(cot, "get_json", _stub)
    out = await cot.fetch_latest_per_contract("BITCOIN")
    # All TFF fields are missing → coerced to 0. This is exactly the bug.
    assert out["long_dealer"] == 0.0
    assert out["short_dealer"] == 0.0
    assert out["long_levfunds"] == 0.0
    assert out["short_levfunds"] == 0.0
