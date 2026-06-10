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
# Hybrid LLM architecture:
# Sonnet 4.6 (critical) + Gemini 2.5 Flash (routine) + Haiku 4.5 (fallback)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY", "")

# ─── X/Twitter (dynamic list — Addendum 2) ─────────────────────────────────
# Bearer token from X Developer Console (Pay Per Use app)
X_API_BEARER_TOKEN = os.getenv("X_API_BEARER_TOKEN", "")
# Private list ID — bot reads list composition at fetch time (zero hardcoded usernames)
X_LIST_ID = os.getenv("X_LIST_ID", "")

# ─── Gmail (IMAP for /reporte email intel) ──────────────────────────────────
GMAIL_EMAIL = os.getenv("GMAIL_EMAIL", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

# ─── Chains ─────────────────────────────────────────────────────────────────
HYPERLIQUID_API = os.getenv("HYPERLIQUID_API", "https://api.hyperliquid.xyz")
HYPEREVM_RPC = os.getenv("HYPEREVM_RPC", "https://rpc.hyperliquid.xyz/evm")
HYPEREVM_CHAIN_ID = 999

HYPERLEND_POOL_ADDRESS = os.getenv(
    "HYPERLEND_POOL_ADDRESS",
    "0x00A89d7a5A02160f20150EbEA7a2b5E4879A1A8b",
)

# ─── Fund wallets (HyperLiquid + HyperLend) — env-driven ───────────────────
def _load_fund_wallets() -> dict[str, str]:
    wallets: dict[str, str] = {}
    for i in range(1, 6):
        addr = os.getenv(f"FUND_WALLET_{i}", "").strip()
        label = os.getenv(f"FUND_WALLET_{i}_LABEL", f"Wallet {i}")
        if addr and addr.startswith("0x") and len(addr) == 42:
            wallets[addr.lower()] = label
    if not wallets:
        print("\u26a0\ufe0f WARNING: No FUND_WALLET_N env vars set. /posiciones will return empty.")
    else:
        print(f"\u2705 Loaded {len(wallets)} fund wallets from env:")
        for addr, label in wallets.items():
            print(f"  - {label}: {addr}")
    return wallets

FUND_WALLETS: dict[str, str] = _load_fund_wallets()

# Wallet usada para HyperLend flywheel (colateral kHYPE) — env-driven
HYPERLEND_WALLET = os.getenv("HYPERLEND_WALLET", "").strip().lower()

# ─── R-PMCORE (2026-06-01) — POST-MIGRACIÓN a HyperLiquid Portfolio Margin ──
# El fondo migró el 100% del capital FUERA de HyperLend y DENTRO de
# HyperLiquid Portfolio Margin. El "flywheel" viejo (colateral WHYPE/kHYPE +
# deuda UETH en HyperLend) YA NO EXISTE. El core del fondo ahora es el balance
# spot de HYPE en la wallet PRIMARIA (0xc7ae) que bajo Portfolio Margin
# funciona como colateral cross. Esta sección parametriza la nueva realidad.
#
# Wallet PRIMARIA (label "BlackCatDeFi EVM"): tiene el HYPE spot que ES el
# colateral del fondo. Default = la dirección on-chain confirmada; override
# por env si BCD rota la wallet primaria.
PM_PRIMARY_WALLET = os.getenv(
    "PM_PRIMARY_WALLET",
    "0xc7ae23316b47f7e75f455f53ad37873a18351505",
).strip().lower()

# Portfolio Margin: HYPE LTV = 0.50 → capacidad de borrow = 0.5 × colateral.
# El activo borrowable en PM es SOLO USDC/USDH (NO existe borrow de UETH).
PM_HYPE_LTV = float(os.getenv("PM_HYPE_LTV", "0.50") or 0.50)
# Margin ratio = deuda / capacidad-de-borrow. Umbrales (utilización de la
# capacidad): WARN 0.40, STRESS 0.70, CRÍTICO/pre-liq 0.85, LIQUIDACIÓN 0.95.
PM_WARN_RATIO = float(os.getenv("PM_WARN_RATIO", "0.40") or 0.40)
PM_STRESS_RATIO = float(os.getenv("PM_STRESS_RATIO", "0.70") or 0.70)
# R-PMALERT: pre-liquidation tier — una escalada ANTES del 0.95 de liquidación.
PM_CRITICAL_RATIO = float(os.getenv("PM_CRITICAL_RATIO", "0.85") or 0.85)
PM_LIQ_RATIO = float(os.getenv("PM_LIQ_RATIO", "0.95") or 0.95)
# Activos PM-elegibles como colateral spot además de HYPE (valuados a precio).
# Stablecoins NUNCA se cuentan como colateral de exposición (son cash/deuda).
PM_COLLATERAL_ASSETS = [
    s.strip().upper()
    for s in os.getenv("PM_COLLATERAL_ASSETS", "HYPE").split(",")
    if s.strip()
]

# Flywheel HyperLend DEPRECADO: las wallets 0xa44e ("DDS/Main") y 0xcddf
# ("Secondary") están CERRADAS — todo el HYPE migró afuera. Su cache on-chain
# quedó STALE (HF 1.429 de hace 600+ h, colateral/deuda residual). Con el flag
# en true (default), su colateral/deuda NO se cuenta en TOTAL EQUITY (evita
# inflar con residuo stale y evita doble-conteo: el HYPE real vive en spot).
# Se muestran como CERRADO/legacy si se muestran. Rollback: setear "false".
FLYWHEEL_DEPRECATED = os.getenv("FLYWHEEL_DEPRECATED", "true").lower() == "true"
LEGACY_FLYWHEEL_WALLETS = frozenset(
    w.strip().lower()
    for w in os.getenv(
        "LEGACY_FLYWHEEL_WALLETS",
        "0xa44e,0xcddf",  # prefijos legibles; el matcheo es por startswith
    ).split(",")
    if w.strip()
)

# Vault deposits: auto-descubrir TODOS los vaults donde la wallet del fondo
# tiene equity (vía userVaultEquities) — no solo los configurados a mano.
# Cada vault se trackea por separado (su propia serie temporal SQLite).
VAULT_AUTODISCOVER = os.getenv("VAULT_AUTODISCOVER", "true").lower() == "true"
# Equity mínima (USD) para considerar un vault deposit como real (no dust).
VAULT_DUST_USD = float(os.getenv("VAULT_DUST_USD", "1.0") or 1.0)

# ─── BlackCat vault DEPOSITS (capital del fondo DENTRO de vaults HL) ────────
# R-VAULTDEP (2026-05-30): el fondo depositó $5,000 USDC en el vault HL
# "Systemic Strategies HyperGrowth". Ese capital vive bajo la dirección del
# vault (NO en las wallets del fondo), así que la paridad-Rabby por-wallet lo
# omitía. Este tracker lee la equity viva del depositante vía el endpoint
# público keyless ``userVaultEquities`` y la suma al TOTAL EQUITY como línea
# propia (nunca contra margen perp ni USDC de wallet).
#
# Formato env ``BLACKCAT_VAULT_DEPOSITS`` = JSON list de objetos:
#   [{"vault_address": "0x..", "depositor_address": "0x..",
#     "label": "Nombre legible", "cost_basis": 5000}]
# Agregar más depósitos = editar la env var, sin tocar código.
def _load_vault_deposits() -> list[dict]:
    raw = os.getenv("BLACKCAT_VAULT_DEPOSITS", "").strip()
    if not raw:
        # R-BOT-DEFINITIVE WI-9d (2026-06-10): el seed hardcodeado "Systemic
        # Strategies HyperGrowth" fue REMOVIDO — el fondo ya salió de ese vault
        # y la entrada fija renderizaba "n/a (depositante no encontrado)" para
        # siempre. Los depósitos activos se AUTO-DESCUBREN dinámicamente vía el
        # endpoint userVaultEquities (VAULT_AUTODISCOVER, default on) para
        # 0xc7ae + todas las fund wallets. Config manual sigue disponible vía
        # la env var (para fijar cost basis / labels).
        return []
    try:
        import json as _json

        parsed = _json.loads(raw)
        if not isinstance(parsed, list):
            print("⚠️ BLACKCAT_VAULT_DEPOSITS no es una lista JSON — ignorado.")
            return []
        out: list[dict] = []
        for e in parsed:
            if not isinstance(e, dict):
                continue
            va = str(e.get("vault_address", "")).strip().lower()
            da = str(e.get("depositor_address", "")).strip().lower()
            if not (va.startswith("0x") and da.startswith("0x")):
                continue
            try:
                cb = float(e.get("cost_basis", 0) or 0)
            except (TypeError, ValueError):
                cb = 0.0
            out.append({
                "vault_address": va,
                "depositor_address": da,
                "label": str(e.get("label") or "Vault deposit"),
                "cost_basis": cb,
            })
        return out
    except Exception as _e:  # noqa: BLE001
        print(f"⚠️ BLACKCAT_VAULT_DEPOSITS parse error: {_e} — ignorado.")
        return []

BLACKCAT_VAULT_DEPOSITS: list[dict] = _load_vault_deposits()

# ─── Thresholds & alerts ────────────────────────────────────────────────────
# HyperLend real liquidation happens at HF < 1.00.
# Fund operative rules: monitor at 1.15, act at 1.10. Zone 1.10-1.20 is normal
# operative — do NOT raise alerts there.
HF_LIQUIDATION = 1.00  # Real HyperLend liquidation threshold
HF_CRITICAL = 1.10     # Fund operative action threshold
HF_WARN = 1.15         # Fund operative monitoring threshold
HYPE_WARN = 34.0       # HYPE price (USD) warn
HYPE_CRITICAL = 30.0
BTC_WARN = 62_000.0
# LIQ_PROXIMITY_PCT removido R-NOPRELIQ 2026-05-15 — basket usa SL/TP nativos HL.

POLL_INTERVAL_MIN = int(os.getenv("POLL_INTERVAL_MIN", "5"))
ENABLE_ALERTS = os.getenv("ENABLE_ALERTS", "true").lower() == "true"

# ─── Variational ("Farm the DUMP") ─────────────────────────────────────────
# Read-only, keyless. Scanner flags perps whose ANNUALIZED funding ≤ threshold;
# watches fire when funding reverts to baseline × reversion-fraction.
VARIATIONAL_FUNDING_THRESHOLD = float(os.getenv("VARIATIONAL_FUNDING_THRESHOLD", "-500") or -500)
VARIATIONAL_REVERSION_FRACTION = float(os.getenv("VARIATIONAL_REVERSION_FRACTION", "0.5") or 0.5)
VARIATIONAL_API_BASE = os.getenv(
    "VARIATIONAL_API_BASE",
    "https://omni-client-api.prod.ap-northeast-1.variational.io",
)
# Master switch for the periodic reversion-alert scheduler job (default on).
VARIATIONAL_ALERTS_ENABLED = os.getenv("VARIATIONAL_ALERTS_ENABLED", "true").lower() == "true"

# ─── R-FARMDUMP — pre-trade "Farm the DUMP" 5-check filter ──────────────────
# When a Variational reversion watch fires, the bot auto-runs the fund's
# mandatory short filter and appends a GO / CAUTION / NO-GO verdict. All
# thresholds are env-tunable; the verdict is a RECOMMENDATION only — BCD
# always makes the final call and executes manually. The bot never trades.
#
# Check 1 (funding reverted / not crowded):
#   FARMDUMP_FUNDING_MEAN_CEIL  — at or above this annualized %, funding is
#       considered "back near mean" → PASS gate (default -100).
#   FARMDUMP_FUNDING_CROWDED_FLOOR — at or below this, funding is still deeply
#       negative = crowded short / squeeze risk → FAIL (default -300).
#   FARMDUMP_FUNDING_SKIP_HIGH  — at or above this positive %, funding fully
#       reversed (overshoot) → FAIL, skip the trade (default +200).
# Check 2 (24h price action):
#   FARMDUMP_UPTREND_24H_WARN — +%24h at/above this = ripping → WARN (default 10).
#   FARMDUMP_UPTREND_24H_FAIL — +%24h at/above this = vertical, squeeze bait
#       to short into → FAIL (default 20).
# Check 3 (OI vs volume / liquidity):
#   FARMDUMP_MIN_VOL_USD — 24h USD volume below this = illiquid shitcoin =
#       self-liquidation / slippage risk → FAIL (default 1_000_000). Between
#       this and 2× it → WARN (borderline thin).
# Check 4 (daily trend): uses FARMDUMP_TREND_SMA_DAYS daily closes (default 7)
#   from Hyperliquid candles; FARMDUMP_TREND_UPTREND_PCT — multi-day % gain
#       above which a price > SMA counts as a strong uptrend → WARN/FAIL
#       (default 10).
FARMDUMP_FUNDING_MEAN_CEIL = float(os.getenv("FARMDUMP_FUNDING_MEAN_CEIL", "-100") or -100)
FARMDUMP_FUNDING_CROWDED_FLOOR = float(os.getenv("FARMDUMP_FUNDING_CROWDED_FLOOR", "-300") or -300)
FARMDUMP_FUNDING_SKIP_HIGH = float(os.getenv("FARMDUMP_FUNDING_SKIP_HIGH", "200") or 200)
FARMDUMP_UPTREND_24H_WARN = float(os.getenv("FARMDUMP_UPTREND_24H_WARN", "10") or 10)
FARMDUMP_UPTREND_24H_FAIL = float(os.getenv("FARMDUMP_UPTREND_24H_FAIL", "20") or 20)
FARMDUMP_MIN_VOL_USD = float(os.getenv("FARMDUMP_MIN_VOL_USD", "1000000") or 1_000_000)
FARMDUMP_TREND_SMA_DAYS = int(float(os.getenv("FARMDUMP_TREND_SMA_DAYS", "7") or 7))
FARMDUMP_TREND_UPTREND_PCT = float(os.getenv("FARMDUMP_TREND_UPTREND_PCT", "10") or 10)

# ─── Wallet fetch retry configuration ──────────────────────────────────────
WALLET_FETCH_TIMEOUT = int(os.getenv("WALLET_FETCH_TIMEOUT", "10"))  # seconds

# ─── Tokens basket SHORT (ALT SHORT BLEED) ─────────────────────────────────
ALT_SHORT_BASKET = ["WLD", "STRK", "ZRO", "AVAX", "ENA"]
WAR_LONG = ["BRENT", "GOLD", "SILVER", "PAXG"]
WAR_SHORT = ["USA500", "NVDA", "TSLA", "HOOD"]

# HIP-3 dexes on Hyperliquid (perps on builder-deployed dexes)
HIP3_DEXES = ["cash", "para", "flx", "vntl", "hyna", "km", "abcd", "xyz"]

# ─── Telegram channels (tiered) ────────────────────────────────────────────
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

# ─── R-AUDIT2-P1.4 — cycle/DCA blocklist + current fund plan ────────────────
# ZEC was liquidated and is OUT FOR GOOD: it belongs to a systemic
# undetectable-exploit class (privacy/shielded accounting), not a one-off bug.
# Blocklisted tickers are NEVER tagged as cycle-accumulation / DCA candidates
# anywhere (classifier, screeners, DCA-zone logic). They can still appear in
# generic intel. Extend via CYCLE_DCA_BLOCKLIST (comma-separated, additive).
CYCLE_DCA_BLOCKLIST = frozenset(
    {"ZEC"} | {
        t.strip().upper()
        for t in os.getenv("CYCLE_DCA_BLOCKLIST", "").split(",")
        if t.strip()
    }
)

# The current fund plan (post-ZEC): HYPE spot (frozen PM core) + BTC and SOL
# isolated 5x + Pear staked. Used as context for integrity scanning and plan
# alignment. Override via FUND_PLAN_ASSETS (comma-separated).
FUND_PLAN_ASSETS = frozenset(
    t.strip().upper()
    for t in os.getenv("FUND_PLAN_ASSETS", "HYPE,BTC,SOL,PEAR").split(",")
    if t.strip()
)

# ─── R-INTEGRITY-FIX — integrity-rumor subject-resolution alias map ─────────
# Maps coin / protocol / project NAMES to their canonical HL ticker so the
# INTEGRITY-HALT scanner binds a rumor to the asset it actually NAMES (not to
# whichever held position happens to be down). Seeded with the fund's assets +
# the Zcash/Orchard names that caused the 2026-06-05 false BTC STOP. Held
# tickers are always detected directly; this map adds NAME→ticker resolution.
# Extend without code changes via env INTEGRITY_ASSET_ALIASES as a
# comma-separated list of `name:TICKER` pairs (additive, case-insensitive).
_DEFAULT_INTEGRITY_ALIASES = {
    "bitcoin": "BTC", "btc": "BTC", "xbt": "BTC",
    "ethereum": "ETH", "ether": "ETH", "eth": "ETH",
    "solana": "SOL", "sol": "SOL",
    "hyperliquid": "HYPE", "hype": "HYPE", "hyperevm": "HYPE",
    "zcash": "ZEC", "zec": "ZEC", "orchard": "ZEC", "sapling": "ZEC",
    "monero": "XMR", "xmr": "XMR",
    "dash": "DASH", "secret": "SCRT", "scrt": "SCRT",
    "pear": "PEAR", "pear protocol": "PEAR",
    "arbitrum": "ARB", "arb": "ARB",
}


def _build_integrity_aliases():
    aliases = dict(_DEFAULT_INTEGRITY_ALIASES)
    raw = os.getenv("INTEGRITY_ASSET_ALIASES", "") or ""
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        name, _, ticker = pair.partition(":")
        name = name.strip().lower()
        ticker = ticker.strip().upper()
        if name and ticker:
            aliases[name] = ticker
    return aliases


INTEGRITY_ASSET_ALIASES = _build_integrity_aliases()

# ─── Paths ──────────────────────────────────────────────────────────────────
DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
LAST_ANALYSIS_FILE = os.path.join(DATA_DIR, "last_successful_analysis.json")
