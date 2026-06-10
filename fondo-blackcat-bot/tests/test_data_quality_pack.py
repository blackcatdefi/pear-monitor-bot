"""R-BOT-DEFINITIVE WI-9 — data quality pack tests (BCRA, Farside, dust, vaults)."""
from __future__ import annotations

import pytest


# ─── WI-9a: BCRA ─────────────────────────────────────────────────────────────

def test_bcra_mapping_fixed():
    from modules.intel30 import bcra_macro as b
    # id 5 is the A3500 wholesale FX rate, NEVER a policy rate.
    assert "A3500" in b.TRACKED[5]
    assert "Tasa" not in b.TRACKED[5].split("(")[0] or "TC" in b.TRACKED[5]
    # The active reference rate is TAMAR (id 44) — verified live 2026-06-10.
    assert 44 in b.TRACKED and "TAMAR" in b.TRACKED[44]
    assert 44 in b.RATE_IDS and 7 in b.RATE_IDS


def test_bcra_rate_out_of_bounds_renders_nd():
    from modules.intel30 import bcra_macro as b
    data = {"variables": [
        {"id": 44, "name": b.TRACKED[44], "fecha": "2026-06-09", "valor": 1446.17,
         "_error": None},
        {"id": 7, "name": b.TRACKED[7], "fecha": "2026-06-08", "valor": 19.88,
         "_error": None},
    ]}
    out = b.format_for_telegram(data)
    assert "n/d (valor fuera de rango)" in out
    assert "1,446" not in out and "1446" not in out
    assert "19.88" in out  # sane rate renders normally


def test_bcra_fx_rate_still_renders_as_fx():
    from modules.intel30 import bcra_macro as b
    data = {"variables": [
        {"id": 5, "name": b.TRACKED[5], "fecha": "2026-06-09", "valor": 1446.17,
         "_error": None},
    ]}
    out = b.format_for_telegram(data)
    assert "A3500" in out and "1,446" in out  # it IS the FX level, fine there


# ─── WI-9b: Farside pending vs explicit zero ─────────────────────────────────

def _farside_html(cells):
    row = "".join(f"<td>{c}</td>" for c in cells)
    return f"<table><tr><td>10 Jun 2026</td>{row}</tr></table>"


def test_farside_prepublication_zero_is_pending():
    from modules.intel30.farside_etfs import _parse_latest_row
    parsed = _parse_latest_row(_farside_html(["0.0", "0.0", "0.0", "0.0"]))
    assert parsed["total_flow_musd"] == 0.0
    assert parsed["pending"] is True


def test_farside_explicit_net_zero_is_real():
    from modules.intel30.farside_etfs import _parse_latest_row
    parsed = _parse_latest_row(_farside_html(["120.5", "(120.5)", "0.0"]))
    assert parsed["total_flow_musd"] == 0.0
    assert parsed["pending"] is False


def test_farside_format_renders_pending():
    from modules.intel30.farside_etfs import format_for_telegram
    out = format_for_telegram({"flows": [
        {"asset": "BTC", "date": "10 Jun 2026", "flow_musd": 0.0,
         "pending": True, "source": "farside", "_error": None},
        {"asset": "ETH", "date": "09 Jun 2026", "flow_musd": -55.3,
         "pending": False, "source": "farside", "_error": None},
    ]})
    assert "pending" in out and "+$0.0M" not in out
    assert "$-55.3M" in out


# ─── WI-9c: negative balances never in DUST ──────────────────────────────────

def test_negative_usdc_routed_out_of_dust():
    from templates.formatters import format_quick_positions
    wallets = [{
        "status": "ok",
        "data": {
            "wallet": "0xc7ae23316b47f7e75f455f53ad37873a18351505",
            "label": "Trading",
            "account_value": 20000.0,
            "total_ntl_pos": 0.0,
            "total_margin_used": 0.0,
            "withdrawable": 0.0,
            "unrealized_pnl_total": 0.0,
            "positions": [],
            "open_orders": [],
            "spot_balances": [
                {"coin": "USDC", "total": -7200.0, "hold": 0.0,
                 "entry_ntl": 0.0, "borrowed": 39000.0},
                {"coin": "PEAR", "total": 10.0, "hold": 0.0, "entry_ntl": 4.0},
            ],
        },
    }]
    out = format_quick_positions(wallets, [], market={"status": "error"})
    assert "Borrowed / saldos negativos" in out
    # The negative USDC must NOT appear inside the SPOT DUST list.
    if "SPOT DUST" in out:
        dust_section = out.split("SPOT DUST", 1)[1].split("Borrowed", 1)[0]
        assert "USDC -7" not in dust_section and "USDC -7,200" not in dust_section


# ─── WI-9d: vault registry dynamic ───────────────────────────────────────────

def test_vault_empty_state_message(monkeypatch):
    from modules import vault_deposits as vd
    r = vd.VaultDepositsResult(ok=True, total_usd=0.0, deposits=[])
    assert "sin depósitos activos" in vd.format_vault_deposits_telegram(r)


def test_vault_autodiscovered_renders(monkeypatch):
    from modules import vault_deposits as vd
    dep = vd.VaultDeposit(
        label="OVERDOSE", vault_address="0xoverdose",
        depositor_address="0xc7ae23316b47f7e75f455f53ad37873a18351505",
        cost_basis_usd=0.0, equity_usd=5074.0, pnl_usd=0.0,
        locked_until_ts=0, found=True, cost_basis_known=False,
        auto_discovered=True,
    )
    r = vd.VaultDepositsResult(ok=True, total_usd=5074.0, deposits=[dep])
    block = vd.format_vault_deposits_telegram(r)
    assert "OVERDOSE" in block and "$5,074" in block
    assert "Systemic" not in block


# ─── WI-9e: one-line intel degradation ───────────────────────────────────────

def test_asxn_failure_one_line():
    from modules.intel30 import asxn_data
    out = asxn_data.format_for_telegram({"_error": "Traceback (most recent call)…"})
    assert out == "🟪 ASXN: fuente no disponible este run"
    assert len(out.splitlines()) == 1


def test_hypurrscan_failure_one_line():
    from modules.intel30 import hypurrscan
    out = hypurrscan.format_for_telegram({"auctions": {"_error": "https://hypurrscan.io/ap"}})
    assert out == "🪶 HypurrScan: fuente no disponible este run"
    assert "http" not in out


def test_hl_info_failures_short_no_fragments():
    from modules.intel30 import hl_info_api
    out = hl_info_api.format_for_telegram({
        "perp_dexs": {"dexs": [], "_error": "HTTPStatusError: 429 Too Many…"},
        "predicted_fundings": {"fundings": {}, "_error": "429"},
    })
    assert "perpDexs: fuente no disponible este run" in out
    assert "predictedFundings: fuente no disponible este run" in out
    assert "429" not in out and "HTTPStatusError" not in out
