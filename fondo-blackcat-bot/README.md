# Fondo Black Cat — Telegram Bot

Analista personal automatizado para un fondo crypto/DeFi. Corre 24/7 en Railway.

## Qué hace

- **Portfolio**: consolida las 5 wallets del fondo en HyperLiquid (perps + spot).
- **HyperLend**: lee on-chain el Health Factor, colateral, debt y available borrows.
- **Mercado**: agrega datos de CoinGecko, Fear & Greed, CoinGlass, DefiLlama.
- **Unlocks**: unlocks >$2M en los próximos 7 días (foco en basket SHORT + HYPE).
- **Intel**: lee 24 canales de Telegram via Telethon (3 tiers).
- **Análisis**: Claude Sonnet 4.5 genera el reporte final con la tesis del fondo.
- **Alertas**: cada 5 min chequea HF, distancia a liquidación, HYPE y BTC.

## Comandos

| Comando | Qué hace |
|---------|----------|
| `/reporte` | Reporte diario completo con Claude |
| `/posiciones` | Snapshot rápido wallets + HF |
| `/hf` | Health Factor HyperLend |
| `/tesis` | Validaciones / invalidaciones de la tesis |
| `/alertas` | Toggle on/off de alertas automáticas |

## Setup

```bash
cd fondo-blackcat-bot
pip install -r requirements.txt
cp .env.example .env
# llenar variables
python bot.py
```

### Telethon session (una sola vez)

Generá localmente la StringSession antes de deployar:

```python
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID = 12345
API_HASH = "xxxxx"
with TelegramClient(StringSession(), API_ID, API_HASH) as c:
    print(c.session.save())
```

Guardá el string en Railway como `TELETHON_SESSION`.

## Deploy en Railway

1. Crear proyecto nuevo en Railway, conectar el repo.
2. Root directory: `fondo-blackcat-bot/`
3. Configurar env vars (ver `.env.example`).
4. Deploy — el `Procfile` lanza `python bot.py` como worker.

## Arquitectura

```
bot.py                    Entry: handlers + scheduler
config.py                 Env vars, wallets, umbrales, canales
modules/
  portfolio.py            HyperLiquid perps + spot por wallet
  hyperlend.py            web3.py → Aave v3 Pool.getUserAccountData
  market.py               CoinGecko + F&G + CoinGlass + DefiLlama
  unlocks.py              DefiLlama /emissions, filtrado >$2M
  telegram_intel.py       Telethon userbot, 24 canales 3 tiers
  analysis.py             Anthropic API con SYSTEM_PROMPT Co-Gestor
  alerts.py               Checks periódicos edge-triggered
templates/
  daily_report.py         SYSTEM_PROMPT completo
  telegram_report.py      Header + keywords ceasefire
```
