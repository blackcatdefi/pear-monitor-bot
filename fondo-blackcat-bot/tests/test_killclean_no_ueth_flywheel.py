"""R-BOT-DEFINITIVE-KILLCLEAN (2026-06-15) — regression lock for the removal of
the dead UETH / HyperLend flywheel pair-trade kill trigger and alerts.

Asserts, permanently:
  1. The ``ueth_apy_above_10`` UETH-borrow-APY kill trigger no longer exists in
     the registry; the surviving HF trigger reads the LIVE Portfolio Margin
     aave-HF (``pm_hf_below_110``), never HyperLend.
  2. The three live [FLYWHEEL] / KILL-TRIGGER UETH alert strings are gone.
  3. ``/kill_status`` (format_kill_status + evaluate_all) only surfaces the
     four surviving triggers and never the word UETH / "flywheel unsustainable".
  4. No code path imports modules.hyperlend / auto.hyperlend_reader, and nothing
     calls a HyperLend / UETH-borrow-APY endpoint helper.
  5. The deleted dead-source modules do not exist.
"""
from __future__ import annotations

import os
import re
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from modules import basket_killer  # noqa: E402


# ─── 1. Kill-trigger registry: UETH gone, PM aave-HF survives ────────────────

SURVIVING_IDS = {
    "btc_above_82k_4h",
    "btc_dca_63_65",
    "pm_hf_below_110",
    "basket_drawdown_2k",
}


def _registry_ids() -> set[str]:
    import asyncio

    async def _run():
        out = set()
        for fn in basket_killer._TRIGGERS:
            # Each evaluator returns a TriggerResult with a stable trigger_id.
            # We don't need live data for the id — but the evaluators fetch, so
            # we read the id from the constructed result defensively.
            try:
                r = await fn()
                out.add(r.trigger_id)
            except Exception:
                pass
        return out

    return asyncio.run(_run())


def test_ueth_borrow_apy_trigger_removed_from_registry():
    assert len(basket_killer._TRIGGERS) == 4, (
        "Expected exactly 4 surviving kill triggers after UETH removal"
    )
    names = [fn.__name__ for fn in basket_killer._TRIGGERS]
    assert "_evaluate_ueth_borrow_apy" not in names
    assert not hasattr(basket_killer, "_evaluate_ueth_borrow_apy")
    # The dead HyperLend-sourced HF evaluator is gone; the live PM one survives.
    assert "_evaluate_hf_flywheel" not in names
    assert "_evaluate_pm_hf" in names


def test_pm_hf_trigger_uses_compute_pm_state_not_hyperlend():
    import inspect

    src = inspect.getsource(basket_killer._evaluate_pm_hf)
    # Reads the LIVE Portfolio Margin state…
    assert "select_primary_pm_state" in src or "compute_pm_state" in src
    # …and calls NO HyperLend / borrow-APY endpoint helper.
    assert "fetch_all_hyperlend(" not in src
    assert "fetch_reserve_rates(" not in src
    assert "get_borrow_apy(" not in src


def test_pm_hf_trigger_never_fires_without_live_debt(monkeypatch):
    """No fabricated/stale values: with no PM debt the trigger reports n/d and
    does NOT fire."""
    import asyncio

    async def _fake_wallets():
        return []

    class _PM:
        has_data = True
        debt_usd = 0.0
        aave_hf = 0.0
        liq_price = 0.0

    monkeypatch.setattr(
        "modules.portfolio.fetch_all_wallets", _fake_wallets, raising=False
    )
    monkeypatch.setattr(
        "modules.pm_context.select_primary_pm_state",
        lambda wallets, market=None: _PM(),
        raising=True,
    )
    res = asyncio.run(basket_killer._evaluate_pm_hf())
    assert res.trigger_id == "pm_hf_below_110"
    assert res.fired is False
    assert "n/d" in res.detail.lower()


# ─── 2/3. Alert + /kill_status honesty ───────────────────────────────────────

def test_kill_status_only_surviving_triggers_no_ueth():
    from modules.basket_killer import TriggerResult, format_kill_status

    results = [
        TriggerResult("btc_above_82k_4h", "BTC > $82K sustained 4h", False, "far", "BTC $70k", "alert_only"),
        TriggerResult("btc_dca_63_65", "BTC en zona DCA $63-65K", False, "far", "BTC $70k", "alert_only"),
        TriggerResult("pm_hf_below_110", "PM aave-HF < 1.10 (colateral HYPE)", False, "far", "PM aave-HF n/d", "suggest_close"),
        TriggerResult("basket_drawdown_2k", "Basket UPnL < -$2,000", False, "far", "Basket UPnL +$0", "alert_only"),
    ]
    text = format_kill_status(results)
    assert "UETH" not in text
    assert "flywheel unsustainable" not in text.lower()
    assert "borrow APY" not in text
    assert "PM aave-HF" in text


# ─── 4. No HyperLend / UETH-borrow-APY code path anywhere ────────────────────

# Forbidden patterns that would indicate a LIVE code path (not a comment that
# merely documents the removal). Imports and call-sites are anchored so prose
# like "...la fuente fetch_reserve_rates." (no paren) does not trip the scan.
_FORBIDDEN_PATTERNS = [
    re.compile(r"from\s+modules\.hyperlend\s+import"),
    re.compile(r"import\s+modules\.hyperlend\b"),
    re.compile(r"from\s+auto\.hyperlend_reader\s+import"),
    re.compile(r"from\s+auto\s+import\s+hyperlend_reader"),
    re.compile(r"\bfetch_all_hyperlend\s*\("),
    re.compile(r"\bfetch_reserve_rates\s*\("),
    re.compile(r"\bget_borrow_apy\s*\("),
    re.compile(r"\bcompute_flywheel\s*\("),
    re.compile(r"\bcompute_liq_matrix\s*\("),
]

# Exact dead alert strings that must never reappear in source.
_DEAD_ALERT_FRAGMENTS = [
    "evaluate rotation to stable or immediate partial repay",
    "Pair trade cost",
    "flywheel unsustainable",
    "rotating UETH debt to stable",
]


def _iter_source_files():
    for dirpath, _dirs, files in os.walk(_ROOT):
        # Skip the test suite itself and any virtualenv/caches.
        rel = os.path.relpath(dirpath, _ROOT)
        if rel.startswith("tests") or "__pycache__" in dirpath or rel.startswith("."):
            continue
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


def test_no_hyperlend_or_ueth_apy_code_path():
    offenders: list[str] = []
    for path in _iter_source_files():
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        for pat in _FORBIDDEN_PATTERNS:
            if pat.search(text):
                offenders.append(f"{os.path.relpath(path, _ROOT)} :: {pat.pattern}")
    assert not offenders, "Live HyperLend/UETH code path still present:\n" + "\n".join(offenders)


def test_dead_alert_strings_absent_from_source():
    offenders: list[str] = []
    for path in _iter_source_files():
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        for frag in _DEAD_ALERT_FRAGMENTS:
            if frag in text:
                offenders.append(f"{os.path.relpath(path, _ROOT)} :: {frag!r}")
    assert not offenders, "Dead flywheel/UETH alert string still present:\n" + "\n".join(offenders)


# ─── 5. Deleted dead-source modules are gone ─────────────────────────────────

@pytest.mark.parametrize(
    "relpath",
    [
        "modules/hyperlend.py",
        "modules/flywheel.py",
        "modules/liq_calc.py",
        "modules/rates_monitor.py",
        "auto/hyperlend_reader.py",
    ],
)
def test_dead_source_modules_deleted(relpath):
    assert not os.path.exists(os.path.join(_ROOT, relpath)), (
        f"{relpath} should have been deleted (dead HyperLend/flywheel source)"
    )


def test_hyperlend_modules_not_importable():
    import importlib

    for mod in ("modules.hyperlend", "modules.flywheel", "modules.liq_calc",
                "modules.rates_monitor", "auto.hyperlend_reader"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(mod)
