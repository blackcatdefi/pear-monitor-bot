# R-PUBLIC-START-NUCLEAR — Public Bot Silence Investigation

**Date:** 2026-05-04
**Bot:** `@PearProtocolAlertsBot` (id `8580501709`, project `gentle-luck`)
**Reported by:** BCD — pegó `/start` 21:59, 22:45, 23:36 UTC el 03-may, cero respuesta
**Status:** RESUELTO ✅

---

## Resumen ejecutivo (TL;DR)

El bot público estuvo MUDO desde el deploy R-BASKET (commit `fe52e4b`,
03-may 22:15 UTC) porque ese deploy entró en crash loop. Los tres rounds
posteriores que prometían arreglarlo (R-PUBLIC-START-FIX V1, V2, V3) **nunca
llegaron a Railway** porque el servicio tiene `ignoreWatchPatterns: true`,
así que `git push` a master *no* dispara redeploy automático.

Fix: trigger manual de deploy de `HEAD` (commit `f6a6b495`, ya con V3
diagnostics) vía Railway API. Boot logs limpios, `pending_update_count = 0`
sostenido durante 90s, /start handler atajado, polling vivo.

Hardening permanente agregado: `/health` ahora expone telemetría Telegram
específica (lifetime updates, last /start, registered handlers, polling
start time) para que cualquier regresión futura sea diagnosticable en
segundos, no en horas.

---

## Cronología

| Hora UTC          | Evento                                                                 |
|-------------------|------------------------------------------------------------------------|
| 03-may 22:15:40   | Deploy R-BASKET `c111e6d3` (fe52e4b) → status SUCCESS inicial          |
| 03-may 22:18:07   | Deploy entra en crash loop. Logs flood con error stacktraces de polling|
| 03-may 22:18:13   | Railway hits "rate limit 500 logs/sec" — 1278 mensajes dropeados       |
| 03-may 22:XX      | `restartPolicyMaxRetries: 10` agotado → status = CRASHED, container off|
| 03-may 23:20      | Push commit f6a6b49 (V3) a master — Railway lo IGNORA (ignoreWatchPatterns) |
| 03-may 23:59      | BCD pega `/start` por 1ª vez. Mensaje queda pending en Telegram queue. |
| 04-may 00:45      | BCD `/start` 2ª vez. Pending=2.                                        |
| 04-may 01:36      | BCD `/start` 3ª vez. Pending=3.                                        |
| 04-may 10:53:01   | Cowork dispara `serviceInstanceDeployV2` con HEAD f6a6b49              |
| 04-may 10:53:32   | Deploy `c8a0e579` → SUCCESS                                            |
| 04-may 10:53:21   | Logs limpios: getMe OK, deleteWebhook OK, polling started              |
| 04-may 10:54:50…  | 6/6 samples sustained `pending=0` cada 15s — polling vivo              |

---

## Evidencia forense (Estrategia 1 — Telegram API)

```
$ curl https://api.telegram.org/bot${TOKEN}/getMe
{ "ok": true, "result": { "id": 8580501709,
  "username": "PearProtocolAlertsBot", ... } }

$ curl https://api.telegram.org/bot${TOKEN}/getWebhookInfo
{ "ok": true, "result": { "url": "",
  "pending_update_count": 12 } }   ← 12 mensajes pending = polling muerto

$ curl 'https://api.telegram.org/bot${TOKEN}/getUpdates?offset=-5'
{ "result": [{ "update_id": 295050979, "message": { "from":
  { "id": 1901156709, "username": "BlackCatDeFi" },
  "text": "/start", "date": 1777850237 } }, ... ]}
   ← Confirma BCD ESTÁ pegando /start a este bot. No es shadow ban.
```

Token NO revocado (getMe 200), webhook NO conflictivo (url=""), updates SÍ
llegan a Telegram pero NADIE los está consumiendo.

## Evidencia forense (Estrategia 4 — Railway audit)

Listado deploys vía GraphQL `deployments(input: { projectId, serviceId, environmentId })`:

| createdAt              | status   | id          | commit  | mensaje                |
|------------------------|----------|-------------|---------|------------------------|
| 2026-05-03T22:15:40    | CRASHED  | c111e6d3    | fe52e4b | R-BASKET               |
| 2026-05-02T16:07:22    | REMOVED  | 6bea3a23    | 84c578d | R-NOSPAM               |
| ...                    | REMOVED  | (varios)    |         |                        |

**Solo UN deploy reciente, en estado CRASHED.** No hay overlap 409. La causa
era simple: el deploy crasheó y los siguientes pushes nunca corrieron.

Configuración descubierta:

```json
"ignoreWatchPatterns": true,
"restartPolicyType": "ON_FAILURE",
"restartPolicyMaxRetries": 10
```

`ignoreWatchPatterns: true` ⇒ cambios en GitHub NO disparan redeploy. Combinado con `restartPolicyMaxRetries: 10`, una vez que pasa el límite el bot queda
muerto silenciosamente hasta que alguien dispare manualmente.

---

## Estrategias intentadas

| # | Estrategia                          | Resultado        | Razón                              |
|---|-------------------------------------|------------------|------------------------------------|
| 1 | Diagnóstico forense (curl Telegram) | ✅ Diagnóstico   | Token OK, webhook OK, queue muerta |
| 2 | /health endpoint                    | ✅ Hardening     | Agregada telemetría Telegram-specific |
| 3 | Verbose error handlers              | Ya estaban       | V3 ya tenía polling_error/uncaughtException |
| 4 | Railway audit                       | ✅ Diagnóstico   | Encontró deploy CRASHED + ignoreWatchPatterns |
| 5 | Token rotation                      | ❌ No necesario  | getMe 200 OK, token vivo           |
| 6 | Stack swap a grammy                 | ❌ No necesario  | node-telegram-bot-api funciona     |
| 7 | Bot mínimo desde cero               | ❌ No necesario  | Existing stack OK                  |
| 8 | Migrar de Railway                   | ❌ No necesario  | Railway no era el problema         |
| 9 | Polling → Webhook                   | ❌ No necesario  | Polling funciona post-deploy       |

**Resolución:** Estrategia 4 (audit Railway) reveló la root cause real.
Trigger manual de deploy vía API resolvió.

---

## Mejoras permanentes agregadas (este round)

### `src/healthServer.js`

Telemetría Telegram-specific en `getStatus()`:

```json
{
  "telegram": {
    "polling_started_at": "2026-05-04T10:53:21.343Z",
    "updates_lifetime": 12,
    "last_update_at": "2026-05-04T10:55:30.123Z",
    "last_update_age_ms": 5400,
    "last_start_command_at": "2026-05-04T10:55:00.000Z",
    "last_start_command_from_user_id": "1901156709",
    "registered_handlers": ["start"],
    "handlers_count": 1
  },
  "deploy": {
    "deploy_id": "c8a0e579-...",
    "commit_sha": "f6a6b49..."
  }
}
```

Funciones nuevas exportadas: `recordTelegramUpdate`, `recordStartCommand`,
`recordPollingStarted`, `registerHandler`.

### `index.js`

Después de `bot.startPolling()`:

```javascript
const _hs = require('./src/healthServer');
_hs.recordPollingStarted();
bot.on('message', (msg) => { try { _hs.recordTelegramUpdate(msg); } catch (_) {} });
```

### `src/commandsStart.js`

Dentro de `attach(bot)`:

```javascript
let _hs = null;
try { _hs = require('./healthServer'); _hs.registerHandler('start'); } catch (_) {}
// ...inside /start handler:
if (_hs) { try { _hs.recordStartCommand(msg.from.id); } catch (_) {} }
console.log(`[commandsStart] /start received from user_id=${msg.from.id} ...`);
```

### `tests/healthServer_telegram.test.js`

6 regresión tests cubren todos los nuevos paths.

---

## Cómo diagnosticar este problema en el futuro (60 segundos)

```bash
# 1. Token + webhook estado
TOKEN=...  # de Railway env
curl -s "https://api.telegram.org/bot${TOKEN}/getMe" | jq .ok
curl -s "https://api.telegram.org/bot${TOKEN}/getWebhookInfo" | jq .result.pending_update_count

# 2. Si pending > 0 sostenido por >5min → polling muerto
# 3. Railway: chequear status del último deploy
curl -X POST https://backboard.railway.com/graphql/v2 \
  -H "Authorization: Bearer ${RAILWAY_TOKEN}" \
  --data '{"query":"query { deployments(input:{projectId:\"a8c939aa-338f-4a76-b2a6-f7551702c854\",serviceId:\"88dca3af-2e3b-48b9-950f-0fd50e74e887\",environmentId:\"9848b702-e186-40bf-85b7-811ef1342f12\"},first:1){ edges { node { id status meta } } } }"}'

# 4. Si status != SUCCESS → trigger redeploy manual:
curl -X POST https://backboard.railway.com/graphql/v2 \
  -H "Authorization: Bearer ${RAILWAY_TOKEN}" \
  --data '{"query":"mutation { serviceInstanceDeployV2(serviceId:\"88dca3af-2e3b-48b9-950f-0fd50e74e887\",environmentId:\"9848b702-e186-40bf-85b7-811ef1342f12\",commitSha:\"<HEAD_SHA>\") }"}'
```

`ignoreWatchPatterns: true` está en `railway.json` por diseño (evita
redeploys involuntarios mientras Cowork edita scripts auxiliares). El
trade-off: cuando un deploy crashea, hay que disparar el siguiente a mano.

---

## IDs Railway canónicos verificados (2026-05-04)

```
project    = a8c939aa-338f-4a76-b2a6-f7551702c854
service    = 88dca3af-2e3b-48b9-950f-0fd50e74e887
env (prod) = 9848b702-e186-40bf-85b7-811ef1342f12
```

(Memory previo tenía service/env IDs diferentes — Railway rota IDs cuando
recreás services o ramas. El project ID es el ancla estable.)
