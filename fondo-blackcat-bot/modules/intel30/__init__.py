"""Round R-INTEL30 Phase 1 — 11 nuevos módulos de fuentes free para el daily intel.

Modules:
    hl_info_api    — Hyperliquid Info API expansion (perpDexs, predictedFundings, vault state)
    asxn_data      — ASXN HYPE buyback/burn/staking dashboards (data.asxn.xyz)
    hypurrscan     — HypurrScan REST (auctions, TWAPs, HIP-1 deploys)
    fred_api       — FRED St. Louis Fed (VIXCLS, DGS10, T10Y2Y, SOFR, WALCL, RRP, DXY)
    farside_etfs   — Farside Investors daily BTC/ETH/SOL ETF flows
    arkham_intel   — Arkham entity transfer monitor
    eia_oil        — EIA Open Data API (WPSR + STEO + nat gas)
    isw_ctp        — ISW + Critical Threats Project daily RSS
    criptoya_ar    — CriptoYa AR FX (blue/MEP/CCL/cripto-USD arb)
    bcra_macro     — BCRA official API (reservas, base monetaria, BADLAR)
    apollo_spark   — Apollo Academy Daily Spark (Torsten Slok)

All modules implement a uniform contract:
    async def fetch_<source>() -> dict        # never raises; returns {"_error": ...} on failure
    def format_for_telegram(data: dict) -> str  # returns Markdown-safe Telegram text
"""

__all__ = [
    "hl_info_api",
    "asxn_data",
    "hypurrscan",
    "fred_api",
    "farside_etfs",
    "arkham_intel",
    "eia_oil",
    "isw_ctp",
    "criptoya_ar",
    "bcra_macro",
    "apollo_spark",
]
