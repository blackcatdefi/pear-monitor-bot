"""P1.4 — legacy-concept sweep regression guard.

The flywheel migrated to Portfolio Margin. The LLM-facing system prompt and
the report-facing state block must NOT resurrect dead flywheel vocabulary
(UETH, WHYPE, "health factor", "flip UETH", the kHYPE/UETH pair trade). The
bare token "HyperLend" may survive ONLY inside an explicit CERRADO / legacy /
STALE deprecation guard — never presented as live state. The header core KPI
must read PM-health, not the legacy "HF FLYWHEEL" line, when the flywheel is
deprecated (the default).

Extends the R-NOLEGACY pattern (test_btc_long_no_legacy_short) — this test
FAILS if any dead-flywheel string reappears in the default-path surfaces.

NOTE: ``kHYPE`` on its own is a REAL spot token (a HYPE price proxy) and is
deliberately NOT banned; only the ``kHYPE/UETH`` pair-trade phrasing is.
"""
from __future__ import annotations

import importlib
import os
import re

# Dead flywheel concepts that must NEVER appear as live vocabulary.
DEAD_TERMS = ("ueth", "whype", "health factor", "flip ueth", "khype/ueth")
# Allowed deprecation markers when "HyperLend" / "HF" is mentioned.
DEPRECATION_MARKERS = (
    "cerrado", "closed", "stale", "cache", "caché", "migrad", "legacy",
    "nunca", "no contar", "no existe", "deprecad",
)


def _default_prompt_surfaces() -> str:
    os.environ["FLYWHEEL_DEPRECATED"] = "true"
    import config
    importlib.reload(config)
    from templates import system_prompt
    importlib.reload(system_prompt)
    block = system_prompt.build_fund_state_block()
    return block + "\n" + system_prompt.SYSTEM_PROMPT


def test_default_prompt_has_no_dead_flywheel_terms():
    text = _default_prompt_surfaces().lower()
    for term in DEAD_TERMS:
        assert term not in text, f"dead flywheel term resurfaced: {term!r}"


def test_default_llm_context_block_has_no_dead_terms():
    os.environ["FLYWHEEL_DEPRECATED"] = "true"
    import config
    importlib.reload(config)
    from templates import formatters
    importlib.reload(formatters)
    # The LLM user content (compile_raw_data) with no live HyperLend data.
    block = formatters.compile_raw_data([], None, {}, None, None).lower()
    for term in DEAD_TERMS:
        assert term not in block, f"dead flywheel term in LLM context: {term!r}"


def test_hyperlend_only_in_deprecation_context():
    text = _default_prompt_surfaces()
    offenders = []
    for line in text.splitlines():
        low = line.lower()
        if "hyperlend" in low and not any(m in low for m in DEPRECATION_MARKERS):
            offenders.append(line.strip())
    assert not offenders, f"HyperLend presented as live state: {offenders}"


def test_no_live_hf_threshold_table_in_default_prompt():
    # The legacy "HF < 1.xx" liquidation threshold table belongs to the
    # rollback path only — it must not render when the flywheel is deprecated.
    text = _default_prompt_surfaces()
    assert not re.search(r"HF\s*[<>≥]", text), "legacy HF threshold table leaked into default prompt"


def test_header_core_kpi_is_pm_health_not_hf_flywheel():
    os.environ["FLYWHEEL_DEPRECATED"] = "true"
    import config
    importlib.reload(config)
    from templates import formatters
    importlib.reload(formatters)
    # A primary wallet with HYPE collateral, no debt → CALM PM core.
    wallets = [{
        "status": "ok",
        "data": {
            "wallet": config.PM_PRIMARY_WALLET,
            "label": "Trading",
            "spot_balances": [{"coin": "HYPE", "total": 1000.0}],
            "positions": [],
            "open_orders": [],
            "account_value": 0.0,
        },
    }]
    market = {"HYPE": {"price_usd": 40.0}}
    header = formatters.format_report_header(wallets, [], market, None)
    assert "PM SALUD" in header
    assert "HF FLYWHEEL" not in header
