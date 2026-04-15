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
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY", "")


# ─── Chains ─────────────────────────────────────────────────────────────────
HYPERLIQUID_API = os.getenv("HYPERLIQUID_API", "https://api.hyperliquid.xyz")
HYPEREVM_RPC = os.getenv("HYPEREVM_RPC", "https://rpc.hyperliquid.xyz/evm")
HYPEREVM_CHAIN_ID = 999
HYPERLEND_POOL_ADDRESS = os.getenv(
    "HYPERLEND_POOL_ADDRESS",
    "0x00A89d7a5A02160f20150EbEA7a2b5E4879A1A8b",
)


# ─── Fund wallets (HyperLiquid + HyperLend) ─────────────────────────────────
FUND_WALLETS: dict[str, str] = {
    "0xa44eF5eD21e59346275d35a65abB0b632e7Cb632": "Alt Short Bleed",
    "0x00bbA92E0f817C08d68f7F439Ba7cDB64E3bBb64": "Alt Short Bleed",
    "0xCDdF18c16EA359C64CaBe72B25e07F4D3F22e27e": "Alt Short Bleed (principal)",
    "0xc7AE0D7c82f6528a70C5dC2E83E6e5e8dBe37505": "Alt Short Bleed",
    "0x171b7C9E7e5B9F4Dc2654A5E39bD3Bb55EE329a7": "DreamCash (WAR TRADE)",
}

# Wallet usada para HyperLend flywheel (colateral kHYPE)
HYPERLEND_WALLET = "0xCDdF18c16EA359C64CaBe72B25e07F4D3F22e27e"


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
ALT_SHORT_BASKET = ["WLD", "STRK", "EIGEN", "SCR", "ZETA"]
WAR_LONG = ["BRENT", "GOLD", "SILVER", "PAXG"]
WAR_SHORT = ["USA500", "NVDA", "TSLA", "HOOD"]


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
