"""R-VAULTDEP tests — HL vault-deposit tracker + TOTAL EQUITY folding.

Covers: live-shaped parsing, PnL vs cost basis, depositor-not-found,
total read failure ("n/a", contributes 0), null/missing field safety,
config seed default, and the capital_calc single-source folding /
no-double-count guarantee.
"""
from __future__ import annotations

import pytest

from auto.capital_calc import compute_net_capital
from modules import vault_deposits as vd


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset the in-memory cache before each test so monkeypatches apply."""
    vd._cache.update(ts=0.0, result=None)
    yield
    vd._cache.update(ts=0.0, result=None)


_ENTRY = {
    "vault_address": "0xd6e56265890b76413d1d527eb9b75e334c0c5b42",
    "depositor_address": "0xc7ae23316b47f7e75f455f53ad37873a18351505",
    "label": "Systemic Strategies HyperGrowth",
    "cost_basis": 5000.0,
}


def _patch_config(monkeypatch, entries, autodiscover=False):
    monkeypatch.setattr(vd, "BLACKCAT_VAULT_DEPOSITS", entries)
    # R-PMCORE: default these config-only tests to autodiscover OFF so they
    # exercise the configured-deposit path deterministically (no live scan).
    monkeypatch.setattr(vd, "VAULT_AUTODISCOVER", autodiscover)


# ─── happy path ────────────────────────────────────────────────────────────
def test_found_equity_and_pnl(monkeypatch):
    _patch_config(monkeypatch, [_ENTRY])
    monkeypatch.setattr(
        vd,
        "_post_user_vault_equities",
        lambda dep: [
            {"vaultAddress": _ENTRY["vault_address"], "equity": "5062.32",
             "lockedUntilTimestamp": 0},
            {"vaultAddress": "0xother", "equity": "1.0"},
        ],
    )
    r = vd.fetch_vault_deposits(force=True)
    assert r.ok is True
    assert round(r.total_usd, 2) == 5062.32
    assert len(r.deposits) == 1
    d = r.deposits[0]
    assert d.found is True
    assert round(d.equity_usd, 2) == 5062.32
    assert round(d.pnl_usd, 2) == 62.32  # equity - cost_basis
    assert vd.get_vault_deposits_total(force=True) == pytest.approx(5062.32)


def test_telegram_block_shows_label_and_pnl(monkeypatch):
    _patch_config(monkeypatch, [_ENTRY])
    monkeypatch.setattr(
        vd, "_post_user_vault_equities",
        lambda dep: [{"vaultAddress": _ENTRY["vault_address"],
                      "equity": "5062.0", "lockedUntilTimestamp": 0}],
    )
    block = vd.format_vault_deposits_telegram(vd.fetch_vault_deposits(force=True))
    assert "Systemic Strategies HyperGrowth" in block
    assert "$5,062" in block
    assert "PnL +$62" in block
    assert "$5,000" in block  # cost basis shown
    assert "DENTRO de protocolo HL" in block  # separate-from-wallet marker


# ─── value is NOT hardcoded — moves with PnL ────────────────────────────────
def test_uses_live_equity_not_cost_basis(monkeypatch):
    _patch_config(monkeypatch, [_ENTRY])
    monkeypatch.setattr(
        vd, "_post_user_vault_equities",
        lambda dep: [{"vaultAddress": _ENTRY["vault_address"], "equity": "4800.5"}],
    )
    r = vd.fetch_vault_deposits(force=True)
    assert round(r.total_usd, 2) == 4800.5  # below cost basis, negative PnL
    assert round(r.deposits[0].pnl_usd, 2) == -199.5


# ─── depositor not in vault ─────────────────────────────────────────────────
def test_depositor_not_found(monkeypatch):
    _patch_config(monkeypatch, [_ENTRY])
    monkeypatch.setattr(
        vd, "_post_user_vault_equities",
        lambda dep: [{"vaultAddress": "0xsomeothervault", "equity": "9.0"}],
    )
    r = vd.fetch_vault_deposits(force=True)
    assert r.ok is True  # query succeeded
    assert r.total_usd == 0.0  # but our vault wasn't found
    assert r.deposits[0].found is False
    block = vd.format_vault_deposits_telegram(r)
    assert "no encontrado" in block


# ─── read failure → n/a, contributes 0, never crashes ──────────────────────
def test_read_failure_returns_na(monkeypatch):
    _patch_config(monkeypatch, [_ENTRY])

    def _boom(dep):
        raise OSError("network down")

    monkeypatch.setattr(vd, "_post_user_vault_equities", _boom)
    r = vd.fetch_vault_deposits(force=True)
    assert r.ok is False
    assert r.total_usd == 0.0
    assert vd.get_vault_deposits_total(force=True) == 0.0
    assert "n/a (vault read failed)" in vd.format_vault_deposits_telegram(r)


# ─── null / missing fields are safe ─────────────────────────────────────────
def test_null_fields_safe(monkeypatch):
    _patch_config(monkeypatch, [_ENTRY])
    monkeypatch.setattr(
        vd, "_post_user_vault_equities",
        lambda dep: [{"vaultAddress": _ENTRY["vault_address"],
                      "equity": None, "lockedUntilTimestamp": None}],
    )
    r = vd.fetch_vault_deposits(force=True)
    assert r.ok is True
    assert r.deposits[0].equity_usd == 0.0
    assert r.deposits[0].locked_until_ts == 0


# ─── empty config → ok, total 0, empty render ───────────────────────────────
def test_no_config_entries(monkeypatch):
    _patch_config(monkeypatch, [])
    r = vd.fetch_vault_deposits(force=True)
    assert r.ok is True
    assert r.total_usd == 0.0
    assert r.deposits == []
    # R-BOT-DEFINITIVE WI-9d: the empty state is REAL information — say it.
    assert "sin depósitos activos" in vd.format_vault_deposits_telegram(r)


# ─── config seed default ─────────────────────────────────────────────────────
def test_config_seed_default_empty(monkeypatch):
    # R-BOT-DEFINITIVE WI-9d: the hardcoded "Systemic Strategies" seed was
    # REMOVED — active deposits are auto-discovered via userVaultEquities.
    monkeypatch.delenv("BLACKCAT_VAULT_DEPOSITS", raising=False)
    import config
    assert config._load_vault_deposits() == []


def test_config_env_override(monkeypatch):
    monkeypatch.setenv(
        "BLACKCAT_VAULT_DEPOSITS",
        '[{"vault_address":"0xAAA","depositor_address":"0xBBB",'
        '"label":"Test","cost_basis":123}]',
    )
    import config
    out = config._load_vault_deposits()
    assert len(out) == 1
    assert out[0]["vault_address"] == "0xaaa"  # lowercased
    assert out[0]["cost_basis"] == 123.0


def test_config_bad_json_ignored(monkeypatch):
    monkeypatch.setenv("BLACKCAT_VAULT_DEPOSITS", "{not json")
    import config
    assert config._load_vault_deposits() == []


# ─── capital_calc folds vault into TOTAL EQUITY, no double count ────────────
def test_capital_calc_folds_vault_no_double_count():
    base = {
        "hl_collateral_total": 73200, "hl_debt_total": 45300,
        "perp_equity_total": 2700, "spot_usd_total": 44,
        "spot_stables_total": 1700, "upnl_perp_total": 231,
        "pear_staked_total": 1224,
    }
    without = compute_net_capital(dict(base, vault_deposits_total=0.0))
    with_v = compute_net_capital(dict(base, vault_deposits_total=5062.32))
    # vault adds EXACTLY its own value to total equity, nothing else moves.
    assert with_v.vault_deposits_usd == pytest.approx(5062.32)
    assert with_v.total_equity_usd == pytest.approx(
        without.total_equity_usd + 5062.32
    )
    # NET (post-leverage exposure) and perp are NOT touched by the vault.
    assert with_v.net_total_usd == without.net_total_usd
    assert with_v.perp_equity_usd == without.perp_equity_usd


# ─── R-PMCORE: auto-discover ALL vaults the fund holds ──────────────────────
def test_autodiscover_enumerates_all_vaults(monkeypatch):
    """With autodiscover ON, every vault the depositor holds is tracked
    independently — even ones absent from BLACKCAT_VAULT_DEPOSITS."""
    monkeypatch.setattr(vd, "BLACKCAT_VAULT_DEPOSITS", [_ENTRY])
    monkeypatch.setattr(vd, "VAULT_AUTODISCOVER", True)
    monkeypatch.setattr(vd, "PM_PRIMARY_WALLET", _ENTRY["depositor_address"])
    monkeypatch.setattr(vd, "FUND_WALLETS", {})
    monkeypatch.setattr(vd, "_resolve_vault_name", lambda va: "Vault " + va[:6])
    monkeypatch.setattr(
        vd, "_post_user_vault_equities",
        lambda dep: [
            {"vaultAddress": _ENTRY["vault_address"], "equity": "5155.0"},
            {"vaultAddress": "0xedge", "equity": "4968.0"},
            {"vaultAddress": "0xbredo", "equity": "1581.0"},
            {"vaultAddress": "0xdust", "equity": "0.5"},  # below dust → skip
        ],
    )
    r = vd.fetch_vault_deposits(force=True)
    assert r.ok is True
    # 1 configured (found) + 2 auto (edge, bredo); dust excluded.
    found = [d for d in r.deposits if d.found]
    assert len(found) == 3
    addrs = {d.vault_address for d in found}
    assert "0xedge" in addrs and "0xbredo" in addrs
    # configured one keeps its label + cost basis; auto ones don't.
    cfg = next(d for d in found if d.vault_address == _ENTRY["vault_address"])
    assert cfg.cost_basis_known is True and cfg.auto_discovered is False
    auto = next(d for d in found if d.vault_address == "0xedge")
    assert auto.auto_discovered is True and auto.cost_basis_known is False
    # total = sum of all found equities (each counted once, no aggregation).
    assert round(r.total_usd, 2) == round(5155.0 + 4968.0 + 1581.0, 2)


def test_capital_calc_backward_compat_no_vault_key():
    net = compute_net_capital({
        "hl_collateral_total": 1000, "hl_debt_total": 0,
        "perp_equity_total": 0, "spot_usd_total": 0,
    })
    assert net.vault_deposits_usd == 0.0
