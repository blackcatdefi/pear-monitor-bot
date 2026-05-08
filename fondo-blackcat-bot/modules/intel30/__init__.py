"""Round R-INTEL30 + R-PERFECT — free intel sources for daily fund intel.

Phase 1 (R-INTEL30, 11 modules):
    hl_info_api, asxn_data, hypurrscan, fred_api, farside_etfs, arkham_intel,
    eia_oil, isw_ctp, criptoya_ar, bcra_macro, apollo_spark.

Phase 2 (R-PERFECT, 16 modules):
    Sub-1 HL infra:    hl_rpc_edge, hyperevmscan, dune_hl, hypetrad
    Sub-2 Macro inst:  treasury_fiscal, nyfed_markets, cftc_cot
    Sub-3 On-chain:    l2beat, artemis_lite, visa_onchain, treasuries_bundle
    Sub-4 Flow+sent:   openinsider, capitol_trades, epoch_ai,
                       semianalysis_rss, finance_rss

Phase 3 (R-PERFECT, 3 modules):
    crypto_vol, kalshi_api, argy_extra

All modules implement the uniform contract:
    async def fetch_all() -> dict        # never raises; {"_error": ...} on failure
    def format_for_telegram(data) -> str # markdown-safe Telegram text

Shared helpers in `_intel_base.py` provide HTTP retry/observability/rate-limit.
"""

PHASE1 = [
    "hl_info_api", "asxn_data", "hypurrscan", "fred_api", "farside_etfs",
    "arkham_intel", "eia_oil", "isw_ctp", "criptoya_ar", "bcra_macro",
    "apollo_spark",
]

PHASE2 = [
    # Sub-1 HL infra
    "hl_rpc_edge", "hyperevmscan", "dune_hl", "hypetrad",
    # Sub-2 Macro inst
    "treasury_fiscal", "nyfed_markets", "cftc_cot",
    # Sub-3 On-chain
    "l2beat", "artemis_lite", "visa_onchain", "treasuries_bundle",
    # Sub-4 Flow + sentiment
    "openinsider", "capitol_trades", "epoch_ai",
    "semianalysis_rss", "finance_rss",
]

PHASE3 = [
    "crypto_vol", "kalshi_api", "argy_extra",
]

ALL_MODULES = PHASE1 + PHASE2 + PHASE3

__all__ = list(ALL_MODULES)
