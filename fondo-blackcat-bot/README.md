# Fondo Black Cat — Telegram Bot

Bot de Telegram en Python que actúa como analista personal del Fondo Black Cat.
Combina posiciones de HyperLiquid, estado on-chain de HyperLend, market data,
token unlocks y inteligencia de 24 canales de Telegram, todo procesado por
Claude (Anthropic API) para generar reportes accionables.

## Arquitectura

- `bot.py` — entry point: comandos + scheduler + Telethon
- `config.py` — env vars, FUND_WALLETS, CHANNELS, umbrales
- `modules/portfolio.py` — HyperLiquid info API (clearinghouseState)
- `modules/hyperlend.py` — web3.py → `getUserAccountData()` on HyperEVM
- `modules/market.py` — CoinGecko + Fear&Greed + CoinGlass + DefiLlama
- `modules/unlocks.py` — DefiLlama emissions API
- `modules/telegram_intel.py` — Telethon userbot, lectura tiered de canales
- `modules/analysis.py` — Anthropic Claude (Sonnet 4.5) genera reporte
- `modules/alerts.py` — APScheduler con HF/HYPE/BTC/liq alerts (edge-triggered)
- `templates/system_prompt.py` — prompt del Co-Gestor con tesis macro
- `templates/formatters.py` — formatters para `/posiciones` y `/hf`
- `utils/security.py` — decorator `@authorized` (solo TELEGRAM_CHAT_ID)
- `utils/telegram.py` — split de mensajes (>4096 chars)
- `utils/http.py` — async httpx con retry exponencial

## Comandos

- `/reporte` — reporte completo (portfolio + market + unlocks + intel + Claude analysis)
- `/posiciones` — snapshot rápido (todas las wallets + HyperLend HF)
- `/hf` — solo el Health Factor de HyperLend
- `/tesis` — análisis del estado de la tesis macro
- `/alertas` — toggle alertas automáticas on/off

Solo responde al `TELEGRAM_CHAT_ID` configurado. Todo otro chat es ignorado silenciosamente.

## Setup local

```bash
cd fondo-blackcat-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # editar con tus credenciales
```

### Generar Telethon StringSession (una vez, en tu MacBook)

```bash
export TELEGRAM_API_ID=...
export TELEGRAM_API_HASH=...
python scripts/generate_session.py
```

Te va a pedir tu número de teléfono y un código de Telegram. Al final imprime
el `StringSession` — copialo a Railway como `TELETHON_SESSION`.

### Correr local

```bash
python bot.py
```

## Deploy en Railway

1. Crear nuevo proyecto en Railway, conectar este repo, elegir el directorio
   `fondo-blackcat-bot/` como root del servicio.
2. Configurar variables de entorno (ver `.env.example`):
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELETHON_SESSION`
   - `ANTHROPIC_API_KEY`
   - `COINGLASS_API_KEY` (opcional)
3. Deploy. Railway corre `python bot.py` (ver `Procfile` y `railway.toml`).
4. Restart policy: `always`.

## Variables de entorno

| Variable | Descripción | Default |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Token de @BotFather | (requerido) |
| `TELEGRAM_CHAT_ID` | Tu chat ID personal — único autorizado | (requerido) |
| `TELEGRAM_API_ID` | my.telegram.org | (requerido para intel) |
| `TELEGRAM_API_HASH` | my.telegram.org | (requerido para intel) |
| `TELETHON_SESSION` | StringSession generada localmente | (requerido para intel) |
| `ANTHROPIC_API_KEY` | console.anthropic.com | (requerido para /reporte y /tesis) |
| `ANTHROPIC_MODEL` | Modelo Claude | `claude-sonnet-4-5` |
| `COINGLASS_API_KEY` | open-api-v3.coinglass.com | (opcional) |
| `HYPERLIQUID_API` | endpoint info | `https://api.hyperliquid.xyz` |
| `HYPEREVM_RPC` | RPC HyperEVM | `https://rpc.hyperliquid.xyz/evm` |
| `HYPERLEND_POOL_ADDRESS` | Pool contract | `0x00A89d7a5A02160f20150EbEA7a2b5E4879A1A8b` |
| `POLL_INTERVAL_MIN` | minutos entre alert cycles | `5` |
| `ENABLE_ALERTS` | habilitar scheduler | `true` |

## Umbrales de alerta (config.py)

| Métrica | Warn | Critical |
|---|---|---|
| HyperLend HF | < 1.20 | < 1.10 |
| HYPE price | < $34 | < $30 |
| BTC price | < $62,000 | — |
| Distancia a liquidación | < 10% | — |

## Notas

- **Margin usage hasta -200% en HyperLiquid es normal**, no se alertea.
- **PnL evaluado a nivel basket cross**, nunca por posición individual.
- **DreamCash (0x171b)** puede no mostrar perp positions si está en HIP-3.
- Cada módulo falla "graceful": si CoinGlass cae, el reporte se genera igual.
- Prompt caching activo en Anthropic para reducir costos del system prompt.
