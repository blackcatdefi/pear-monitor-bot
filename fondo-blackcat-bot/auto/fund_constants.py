"""R-FUNDFIX (1 may 2026) — Stable, non-stale constants migrated from fund_state.py.

Purpose
-------
fund_state.py mixes two kinds of values:

1. Stale, manually-curated state — `BASKET_STATUS`, `BASKET_V5_PLAN`,
   `BASKET_V5_STATUS`, `BASKET_NOTE`. These were the root cause of the
   1 may 17:23 UTC LLM "BCD confirmar" bug: the prompt builder injected
   "v4 closed / v5 pending" while on-chain showed v6 ACTIVE.
2. Legitimate constants — HF thresholds, classify_fill helpers, BCD DCA
   plan, Trade del Ciclo (BCD-edited), Flywheel design notes. These are
   valid prompt material and don't conflict with on-chain truth.

This module re-exports group #2 only. New code should import from
`auto.fund_constants` instead of `fund_state` to avoid accidentally
pulling stale basket state into LLM prompts.

Backward compatibility
----------------------
fund_state.py keeps the legacy symbols for now (other call sites still
import them). The migration is opt-in per call site. The LLM prompt path
(templates/system_prompt.py) was migrated in R-FUNDFIX; non-prompt
consumers (heartbeat, status_quick, etc.) can migrate later.
"""
from __future__ import annotations

from fund_state import (
    BCD_DCA_PLAN,
    BASKET_PERP_TOKENS,
    BLOFIN_BALANCE_AVAILABLE,
    CORE_DCA_SPOT_TOKENS,
    FLYWHEEL_NOTE,
    HF_CRITICAL,
    HF_LIQUIDATION,
    HF_WARN,
    STABLE_TOKENS,
    TRADE_DEL_CICLO_BLOFIN_BALANCE_USD,
    TRADE_DEL_CICLO_LAST_CLOSE,
    TRADE_DEL_CICLO_LAST_ENTRY,
    TRADE_DEL_CICLO_LAST_UPDATE,
    TRADE_DEL_CICLO_LEVERAGE,
    TRADE_DEL_CICLO_NOTE,
    TRADE_DEL_CICLO_PLATFORM,
    TRADE_DEL_CICLO_PNL_REALIZED,
    TRADE_DEL_CICLO_STATUS,
    classify_fill,
    dca_tranches_for,
)

# ALT_SHORT_BLEED_WALLETS is just an immutable address-prefix list; safe to
# re-export. NOT a stale state.
from fund_state import ALT_SHORT_BLEED_WALLETS

__all__ = [
    "ALT_SHORT_BLEED_WALLETS",
    "BASKET_PERP_TOKENS",
    "BCD_DCA_PLAN",
    "BLOFIN_BALANCE_AVAILABLE",
    "CORE_DCA_SPOT_TOKENS",
    "FLYWHEEL_NOTE",
    "HF_CRITICAL",
    "HF_LIQUIDATION",
    "HF_WARN",
    "STABLE_TOKENS",
    "TRADE_DEL_CICLO_BLOFIN_BALANCE_USD",
    "TRADE_DEL_CICLO_LAST_CLOSE",
    "TRADE_DEL_CICLO_LAST_ENTRY",
    "TRADE_DEL_CICLO_LAST_UPDATE",
    "TRADE_DEL_CICLO_LEVERAGE",
    "TRADE_DEL_CICLO_NOTE",
    "TRADE_DEL_CICLO_PLATFORM",
    "TRADE_DEL_CICLO_PNL_REALIZED",
    "TRADE_DEL_CICLO_STATUS",
    "classify_fill",
    "dca_tranches_for",
]
