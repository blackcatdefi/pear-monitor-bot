"""R-FUNDFIX (1 may 2026) — Stable, non-stale constants migrated from fund_state.py.

Purpose
-------
fund_state.py mixes two kinds of values:

1. Stale, manually-curated state — `BASKET_STATUS`, `BASKET_V5_PLAN`,
   `BASKET_V5_STATUS`, `BASKET_NOTE`. These were the root cause of the
   1 may 17:23 UTC LLM "BCD confirmar" bug: the prompt builder injected
   "v4 closed / v5 pending" while on-chain showed v6 ACTIVE.
2. Legitimate constants — HF thresholds, classify_fill helpers, BCD DCA
   plan, Flywheel design notes. These are valid prompt material and
   don't conflict with on-chain truth.

This module re-exports group #2 only. New code should import from
`auto.fund_constants` instead of `fund_state` to avoid accidentally
pulling stale basket state into LLM prompts.

R-NOPRELIQ + REMOVE BLOFIN (2026-05-15)
---------------------------------------
Blofin / Trade del Ciclo eliminados del fondo. Las constantes
``TRADE_DEL_CICLO_*`` y ``BLOFIN_BALANCE_AVAILABLE`` ya no se exportan
(ni existen en ``fund_state``). Importadores históricos deben quitar
esas referencias.
"""
from __future__ import annotations

from fund_state import (
    BCD_DCA_PLAN,
    BASKET_PERP_TOKENS,
    CORE_DCA_SPOT_TOKENS,
    FLYWHEEL_NOTE,
    FUND_DEFAULT_LEVERAGE,
    HF_CRITICAL,
    HF_LIQUIDATION,
    HF_WARN,
    STABLE_TOKENS,
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
    "CORE_DCA_SPOT_TOKENS",
    "FLYWHEEL_NOTE",
    "FUND_DEFAULT_LEVERAGE",
    "HF_CRITICAL",
    "HF_LIQUIDATION",
    "HF_WARN",
    "STABLE_TOKENS",
    "classify_fill",
    "dca_tranches_for",
]
