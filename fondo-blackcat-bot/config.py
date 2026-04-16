"""Environment variables, constants and static config for Fondo Black Cat bot."""
from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()


# ─── Telegram ───────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Telethon (userbot to read channels)
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0") or 0)
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE", "")
TELETHON_SESSION = os.getenv("TELETHON_SESSION", "")


# ─── APIs ───────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-6")
COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY", "")


# ─── Chains ─────────────────────────────────────────────────────────────────
HYPERLIQUID_API = os.getenv("HYPERLIQUID_API", "https://api.hyperliquid.xyz")
HYPEREVM_RPC = os.getenv("HYPEREVM_RPC", "https://rpc.hyperliquid.xyz/evm")
HYPEREVM_CHAIN_ID = 999
HYPERLEND_POOL_ADDRESS = os.getenv(
    "HYPERLEND_POOL_ADDRESS",
    "0x00A89d7a5A02160f20150EbEA7a2b5E4879A1A8b",
)


# ─── Fund wallets (HyperLiquid + HyperLend) — env-driven ────────────────────
def _load_fund_wallets() -> dict[str, str]:
    wallets: dict[str, str] = {}
    for i in range(1, 6):
        addr = os.getenv(f"FUND_WALLET_{i}", "").strip()
        label = os.getenv(f"FUND_WALLET_{i}_LABEL", f"Wallet {i}")
        if addr and addr.startswith("0x") and len(addr) == 42:
            wallets[addr.lower()] = label
    if not wallets:
        print("⚠️  WARNING: No FUND_WALLET_N env vars set. /posiciones will return empty.")
    else:
        print(f"✅ Loaded {len(wallets)} fund wallets from env:")
        for addr, label in wallets.items():
            print(f"   - {label}: {addr}")
    return wallets


FUND_WALLETS: dict[str, str] = _load_fund_wallets()

# Wallet usada para HyperLend flywheel (colateral kHYPE) — env-driven
HYPERLEND_WALLET = os.getenv("HYPERLEND_WALLET", "").strip().lower()


# ─── Thresholds & alerts ────────────────────────────────────────────────────
HF_WARN = 1.20       # HyperLend HF warning
HF_CRITICAL = 1.10   # HyperLend HF critical
HYPE_WARN = 34.0     # HYPE price (USD) warn
HYPE_CRITICAL = 30.0
BTC_WARN = 62_000.0
LIQ_PROXIMITY_PCT = 0.10  # Alertar si posición a <10% de liquidación
POLL_INTERVAL_MIN = int(os.getenv("POLL_INTERVAL_MIN", "5"))
ENABLE_ALERTS = os.getenv("ENABLE_ALERTS", "true").lower() == "true"


# ─── Tokens basket SHORT (ALT SHORT BLEED) ──────────────────────────────────
ALT_SHORT_BASKET = ["WLD", "STRK", "ZRO", "AVAX", "ENA"]
WAR_LONG = ["BRENT", "GOLD", "SILVER", "PAXG"]
WAR_SHORT = ["USA500", "NVDA", "TSLA", "HOOD"]

# HIP-3 dexes on Hyperliquid (perps on builder-deployed dexes)
HIP3_DEXES = ["cash", "para", "flx", "vntl", "hyna", "km", "abcd", "xyz"]


# ─── Telegram channels (tiered) ─────────────────────────────────────────────
CHANNELS = {
    "tier1": [
        {"name": "Medusa Capital", "handle": "medusa_capital_es", "focus": "Spanish macro/equity, geopolitical"},
        {"name": "AIXBT Daily Reports", "handle": "aixbtfeed", "focus": "Daily insights, institutional flows, catalysts"},
        {"name": "Agent Pear Signals", "handle": "agentpear", "focus": "Pair trade signals, Hyperliquid stats"},
        {"name": "Felix Protocol", "handle": "felixprotocol", "focus": "Hyperliquid ecosystem, protocol intel"},
        {"name": "ZordXBT", "handle": "zordxbt", "focus": "BTC technicals, key levels, trade setups"},
        {"name": "Monitoring The Situation", "handle": "monitoringbias", "focus": "Geopolitical breaking news, war, energy/oil"},
    ],
    "tier2": [
        {"name": "Prediction Desk News", "handle": "PredictionDeskNews", "focus": "Breaking news + Polymarket"},
        {"name": "Lookonchain", "handle": "lookonchainchannel", "focus": "Whale movements, smart money"},
        {"name": "Campbell Ramble", "handle": "campbellramble", "focus": "Macro analysis"},
        {"name": "Crypto Ballena", "handle": "CryptoBallenaOficial", "focus": "Spanish whale alerts"},
        {"name": "Kleomedes", "handle": "kleomedes_channel", "focus": "Trading analysis"},
        {"name": "Leandro Zicarelli", "handle": "leandro_zicarelli", "focus": "Spanish market analysis"},
    ],
    "tier3": [
        {"name": "PolyBot", "handle": "TradePolyBot", "focus": "Polymarket auto signals"},
        {"name": "Hyperdash Flows", "handle": "hyperdashflows", "focus": "Liquidations, large positions"},
        {"name": "ProLiquid Whales", "handle": "proliquid_whales", "focus": "Whale positions on HL"},
        {"name": "MLM OnChain", "handle": "mlmonchain", "focus": "On-chain analytics"},
        {"name": "Havoc Calls", "handle": "havoc_calls", "focus": "Trading calls"},
        {"name": "Lady Market", "handle": "lady_market", "focus": "Market signals"},
        {"name": "Chung Daily Note", "handle": "chungdailynote", "focus": "Daily notes"},
        {"name": "C4", "handle": "c4dotgg", "focus": "Community signals"},
        {"name": "MNC Crypto", "handle": "MNCcrypto", "focus": "Crypto drops/alerts"},
        {"name": "ZachXBT Investigations", "handle": "investigations", "focus": "Fraud/exploit alerts"},
        {"name": "HL Whale Alerts", "handle": "HyperliquidWhaleAlert", "focus": "Whale alerts"},
        {"name": "Oracle Signals", "handle": "oracle_signals", "focus": "Trading signals"},
    ],
}

CHANNEL_LIMITS = {"tier1": 200, "tier2": 50, "tier3": 20}


# ─── Paths ──────────────────────────────────────────────────────────────────
DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
