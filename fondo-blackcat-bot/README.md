# Fondo Black Cat — Telegram Bot

Analista personal automatizado para un fondo crypto/DeFi. Genera reportes de
inteligencia macro, monitorea posiciones de HyperLiquid, el HealthFactor de
HyperLend, y lee canales de Telegram para síntesis via Claude.

## Estructura

```
fondo-blackcat-bot/
├── bot.py                    # Entry point, command handlers, scheduler
├── config.py                 # Env vars, constants, wallets
├── requirements.txt
├── Procfile                  # Railway: worker: python bot.py
├── railway.toml
├── .env.example
├── modules/
│   ├── portfolio.py          # HyperLiquid API — perp positions
│   ├── hyperlend.py          # HyperLend on-chain (Aave v3 fork)
│   ├── market.py             # CoinGecko, DefiLlama, CoinGlass, F&G
│   ├── unlocks.py            # DefiLlama unlocks
│   ├── telegram_intel.py     # Telethon — read channels
│   ├── analysis.py           # Anthropic Claude — report synthesis
│   └── alerts.py             # Periodic alert checks
├── templates/
│   ├── daily_report.py       # Fallback template (no Claude)
│   └── telegram_report.py    # Intel summary formatter
└── scripts/
    └── generate_session.py   # Generate Telethon StringSession
```

## Setup

1. **Crear bot**: hablar con [@BotFather](https://t.me/BotFather), guardar token.
2. **Obtener chat_id**: hablarle al bot una vez y chequear
   `https://api.telegram.org/bot<TOKEN>/getUpdates`.
3. **Telegram API credentials** (para Telethon): crear app en
   [my.telegram.org](https://my.telegram.org) → `api_id`, `api_hash`.
4. **Generar StringSession** (solo una vez, localmente):
   ```bash
   pip install -r requirements.txt
   TELEGRAM_API_ID=... TELEGRAM_API_HASH=... python scripts/generate_session.py
   ```
   Pegar el output como `TELETHON_SESSION` en Railway.
5. **Anthropic API key**: [console.anthropic.com](https://console.anthropic.com).
6. **CoinGlass** (opcional): [coinglass.com](https://www.coinglass.com/pricing).

## Deployment en Railway

1. Nuevo proyecto → "Deploy from GitHub repo" → seleccionar este repo.
2. Set "Root Directory" a `fondo-blackcat-bot`.
3. Configurar todas las env vars de `.env.example`.
4. Deploy automático.

## Comandos del bot

| Comando | Descripción |
|---|---|
| `/start` | Lista de comandos |
| `/reporte` | Reporte completo (Claude analiza todo) |
| `/posiciones` | Portfolio snapshot + HF |
| `/hf` | HyperLend Health Factor |
| `/mercado` | Market data (BTC, ETH, F&G, etc.) |
| `/unlocks` | Token unlocks próximos 7d |
| `/tesis` | Estado de la tesis |
| `/alertas` | Toggle alertas automáticas |

## Alertas automáticas

Cada `ALERT_INTERVAL_MINUTES` (default 5):

- `HF < 1.20` → warning
- `HF < 1.10` → critical
- Posición a < 10% de liquidación
- `HYPE < $34` → warning (colateral flywheel)
- `HYPE < $30` → critical
- `BTC < $62K` → warning

Reporte diario completo a las `DAILY_REPORT_UTC_HOUR:00 UTC` (default 13:00 UTC).

## Seguridad

El bot **SOLO responde al `TELEGRAM_CHAT_ID` configurado**. Otros usuarios son
ignorados silenciosamente.

## Reglas del reporte (respetadas por Claude)

- PnL se evalúa a nivel basket cross, no por posición individual.
- Margin usage hasta -200% en HyperLiquid es normal.
- `DreamCash` (0x171b…) puede no mostrar perps si está en HIP-3.
- Directo, sin relleno, sin "buenos días". Siempre con números.
