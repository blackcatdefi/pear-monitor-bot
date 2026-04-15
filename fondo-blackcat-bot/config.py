"""Central configuration for Fondo Black Cat bot."""
from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.environ.get(key, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


# Telegram bot (python-telegram-bot)
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN", required=True)
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID", required=True)

# Telethon userbot (reads channels)
TELEGRAM_API_ID = int(_env("TELEGRAM_API_ID", "0") or 0)
TELEGRAM_API_HASH = _env("TELEGRAM_API_HASH", "")
TELEGRAM_PHONE = _env("TELEGRAM_PHONE", "")
TELETHON_SESSION = _env("TELETHON_SESSION", "")

# Anthropic (Claude)
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = _env("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

# Market data
COINGLASS_API_KEY = _env("COINGLASS_API_KEY", "")

# RPCs
HYPERLIQUID_RPC = _env("HYPERLIQUID_RPC", "https://api.hyperliquid.xyz") or "https://api.hyperliquid.xyz"
HYPEREVM_RPC = _env("HYPEREVM_RPC", "https://rpc.hyperliquid.xyz/evm") or "https://rpc.hyperliquid.xyz/evm"

# HyperLend (Aave v3 fork on HyperEVM)
HYPERLEND_POOL_ADDRESS = _env(
    "HYPERLEND_POOL_ADDRESS",
    "0x00A89d7a5A02160f20150EbEA7a2b5E4879A1A8b",
)

# Scheduler
ALERT_INTERVAL_MINUTES = int(_env("ALERT_INTERVAL_MINUTES", "5") or 5)
DAILY_REPORT_UTC_HOUR = int(_env("DAILY_REPORT_UTC_HOUR", "13") or 13)
ENABLE_AUTO_ALERTS = (_env("ENABLE_AUTO_ALERTS", "true") or "true").lower() == "true"


# Fondo Black Cat wallets (todas del fundador)
FUND_WALLETS = {
    "0xa44eF5eD21e59346275d35a65abB0b632e7Cb632": "Alt Short Bleed",
    "0x00bbA92E0f817C08d68f7F439Ba7cDB64E3bBb64": "Alt Short Bleed",
    "0xCDdF18c16EA359C64CaBe72B25e07F4D3F22e27e": "Alt Short Bleed (principal)",
    "0xc7AE0D7c82f6528a70C5dC2E83E6e5e8dBe37505": "Alt Short Bleed",
    "0x171b7C9E7e5B9F4Dc2654A5E39bD3Bb55EE329a7": "DreamCash (WAR TRADE)",
}

# Wallet que tiene la posición de HyperLend (mismo fundador)
HYPERLEND_WALLET = "0xCDdF18c16EA359C64CaBe72B25e07F4D3F22e27e"

# Basket SHORT tokens (para filtrar unlocks)
SHORT_BASKET = ["WLD", "STRK", "EIGEN", "SCR", "ZETA"]
CORE_TOKENS = ["HYPE", "PEAR"]

# Thresholds para alertas automáticas
HF_WARN = 1.20
HF_CRITICAL = 1.10
HYPE_PRICE_WARN = 34.0
HYPE_PRICE_CRITICAL = 30.0
BTC_PRICE_WARN = 62000.0
LIQUIDATION_PROXIMITY_WARN = 0.10  # 10%
