# X API Audit — Post-Incidente 2026-04-22

Autor: Cowork Claude · Fecha: 2026-04-22 (sesión nocturna)
Contexto: post-mortem del overspend de $20.48 USD en 7 días que pegó el
Spend Cap de $20 y produjo HTTP 403 `SpendCapReached`. Reset automático:
2026-05-16.

Este documento responde los 5 puntos del audit pedidos por BCD tras los
commits de Round 11.2 (`7579f9b`) y el deploy `ad7cf24e` (Railway `amusing-
acceptance`, commit `a0450a3`).

---

## 1. Contador real de requests en los últimos 7 días

**Fuente:** SQLite en Railway (`data/intel_memory.db` — tabla
`x_api_calls`). Tracking implementado por `modules.intel_memory.record_x_api_call()`
en el commit `a0450a3` (Round 12).

**Estado al 2026-04-22 ~23:30 UTC (hora de este audit):**
- Endpoint `/2/lists/{id}/tweets` (scheduler + handlers): registrando desde
  el deploy `ad7cf24e` a las 2026-04-22 17:11 UTC.
- Endpoint `/2/users/:id/following`: **0 llamadas** desde el deploy de Round
  12. El uso de este endpoint quedó reservado a `scripts/reconcile_x_list.py`
  (offline), no se ejecuta desde el bot en runtime.
- Endpoint `/2/users/by` + `/2/users/:id/tweets`: **0 llamadas**. Estos son
  los endpoints del bug pre-R9 que quemó ~$20 en 7 días — fueron removidos
  del code path live en Round 9 (commit `66ab2d4`).
- Otros endpoints X: **ninguno** wired al bot.

**Números crudos del incidente (pre-fix, Apr 15–22):**
- 7 días de exposición del per-user endpoint pre-R9 (commits anteriores a
  `66ab2d4`): ~156 cuentas × 1 call/run × ~N runs/día.
- Total estimado según cost reconciliation con billing de X: ~82,000 tweets
  retornados / 7 días (implica ≈$20.48 en esos 7 días, a $0.25/1K tweets).
- **Spend Cap hit** el 2026-04-21 ~21:00 UTC. Desde ese momento todos los
  endpoints devuelven HTTP 403 `SpendCapReached` hasta el reset 2026-05-16.

**Post-fix (commit `a0450a3`, desde 2026-04-22 17:11 UTC):**
- 0 llamadas exitosas (cap sigue en 403). Las llamadas que intentan salir
  quedan gated por cooldown/daily cap antes de golpear X, y el scheduler
  corre cada 4h pero falla con 403 hasta reset. Todas las filas en
  `x_api_calls` con `status=403` hasta el 2026-05-16.

**Cómo verificar en vivo (post-reset):**
```bash
# Desde Railway shell o local con DATABASE sync:
sqlite3 data/intel_memory.db \
  "SELECT endpoint, status, COUNT(*), SUM(tweets_returned), ROUND(SUM(est_cost_usd),4) \
   FROM x_api_calls \
   WHERE ts >= datetime('now','-7 days') \
   GROUP BY endpoint, status;"
```

---

## 2. Costo proyectado con los nuevos rate limits

**Rate limits post-Round 12 (commit `a0450a3`):**
- Cooldown interno: `FETCH_COOLDOWN_HOURS = 4` (env `X_API_COOLDOWN_HOURS`).
- Daily cap rolling 24h: `DAILY_CALL_CAP = 15`.
- Paginación por call: `MAX_PAGES_PER_FETCH = 2` → ≤ 200 tweets/fetch.
- Alert threshold: `COST_ALERT_THRESHOLD_USD = 5`.
- Unit real de X Pay-Per-Use: **$0.25 / 1,000 tweets retornados** (confirmado
  vía reconciliación del burn 2026-04-15/22). NO $0.001/request.

**Peor caso teórico (cap duro 15 calls/día, todas exitosas con 200 tweets):**
- 15 calls/día × 200 tweets/call = 3,000 tweets/día
- 3,000 × $0.25 / 1,000 = **$0.75/día**
- × 30 días = **$22.50/mes** — POR ENCIMA del alert threshold $5.

**Caso esperado (scheduler 4h + handlers con cooldown activo):**
- 6 runs/día × ~100 tweets/run promedio (list 211 members, low-activity
  tweets filtrados) = 600 tweets/día × $0.25 / 1,000 = **$0.15/día**
- × 30 días = **≈$4.50/mes** — por debajo del alert threshold.
- Si la list queda en 211 post-reconcile, menor aún (~$3/mes).

**Conclusión:** el cap duro (15/día) está sobredimensionado vs. el uso real
(6 scheduler runs + probes esporádicos `/debug_x`). **No requiere bajar** el
cap en runtime — el cooldown de 4h + la alerta de $5 ya previenen burn. Si
el monthly projection real queda > $5 durante 2 semanas, **bajar
`DAILY_CALL_CAP` a 8** como medida adicional.

**TODO post-reset 2026-05-16:** validar cost_7d real con el query de la
sección 1 después de ~3 días de tráfico normal. Si `monthly_projection_usd
> 5`, ajustar.

---

## 3. Verificación de que la list X está en las cuentas correctas

**Situación actual (al 2026-04-22):**
- List: "Fondo Black Cat Intel" — ID `2046698139873378486` (público).
- Composición observada post-Round 9 bulk-add: ~600 miembros (browser-side
  adds corridos múltiples veces sin dedup).
- Target: **~211 miembros** = las cuentas que BCD sigue activamente.
- Diff teórico: ~389 cuentas sobran (adds duplicados / orgullos ajenos /
  followback loops).

**Blocker:** el Spend Cap en 403 impide ejecutar el diff ahora mismo (tanto
`/2/users/:id/following` como `/2/lists/:id/members` cuentan contra el cap).

**Script para reconciliar:** `fondo-blackcat-bot/scripts/reconcile_x_list.py`

**Inputs requeridos (env vars):**
- `X_API_BEARER_TOKEN` — bearer app-only para las lecturas.
- `X_LIST_ID` — ID de la list a reconciliar.
- `X_OWNER_USER_ID` — user ID de BCD (`1397263268691992576`, default
  hard-coded — override si cambia).
- `X_OAUTH_CONSUMER_KEY` / `X_OAUTH_CONSUMER_SECRET` /
  `X_OAUTH_ACCESS_TOKEN` / `X_OAUTH_ACCESS_TOKEN_SECRET` — OAuth 1.0a user
  context, **requerido sólo para `--apply`** (Bearer app-only no puede
  mutar list membership).

**Comando dry-run (ejecutar primero):**
```bash
cd fondo-blackcat-bot
python -m scripts.reconcile_x_list
# → imprime resumen, escribe /tmp/x_list_reconcile_add.json
#   y /tmp/x_list_reconcile_remove.json
```

**Outputs esperados (dry-run):**
- `/tmp/x_list_reconcile_add.json` — cuentas que BCD sigue y NO están en la
  list → normalmente vacío o ≤5.
- `/tmp/x_list_reconcile_remove.json` — cuentas en la list que BCD NO sigue
  → ≈389 entradas.
- STDOUT: `Follow set: N accounts | List: M members | ADD: x | REMOVE: y`.

**Comando apply (después de validar el dry-run):**
```bash
# Cargar las 4 credenciales OAuth 1.0a (se las genera BCD en developer.x.com)
export X_OAUTH_CONSUMER_KEY="..."
export X_OAUTH_CONSUMER_SECRET="..."
export X_OAUTH_ACCESS_TOKEN="..."
export X_OAUTH_ACCESS_TOKEN_SECRET="..."
python -m scripts.reconcile_x_list --apply
# → itera sobre remove list emitiendo DELETE /2/lists/:id/members/:uid
#   con sleep 0.5s entre calls (evitar rate limit)
```

**Cuándo ejecutar:** **cualquier momento después del 2026-05-16** (reset
automático del Spend Cap) O **después de que BCD suba el Spend Cap
manualmente** en developer.x.com → Products → Usage.

**Costo esperado de la reconciliación:**
- `/2/users/:id/following` paginado (1,000 items/página máx según X) →
  ≤1 call para 211 cuentas = ≈211 tweets-equivalente = $0.05.
- `/2/lists/:id/members` paginado (100/página) → 6 calls para 600 = $0.15.
- POST/DELETE mutations no cuentan contra tweet-billing (sólo contra rate
  limits per-user, no Spend Cap).
- **Total reconciliation: ≤$0.20**.

---

## 4. Logging de costo en cada request

**Implementación:** commit `a0450a3`, `modules/x_intel.py:329-333`.

**Formato de log (confirmado vivo en el código):**
```
[X_API_COST] caller=<scheduler|debug_x|intel_sources|fetch_x_intel|?>
  pages=<1|2>
  tweets=<int>
  est_cost=$<0.XXXX>
  calls_24h=<N>/<DAILY_CALL_CAP>
  7d=$<0.XX>
  mo_proj=$<X.XX>
```

**Ejemplo real esperado (próximo ciclo scheduler post-reset):**
```
2026-05-16 17:12:03 INFO x_intel — [X_API_COST] caller=scheduler pages=2
  tweets=180 est_cost=$0.0450 calls_24h=1/15 7d=$0.05 mo_proj=$4.50
```

**Cobertura por branch:**
- ✅ `status=200` (success): loggea pages + tweets + cost + proj.
- ✅ `status=429` (rate limit): loggea `tweets=0`, log warning separado.
- ✅ `status=403` (SpendCap): loggea el body parseado.
- ✅ timeout / non-HTTP errors: loggea `status=-1 tweets=0`.
- ✅ **Internal gates** (cooldown / daily cap hit antes de llegar a X):
  log separado `[X_API_COST] cooldown active` o `[X_API_COST] daily cap
  hit` con el caller. NO cuentan contra el budget.

**Dónde inspeccionar:**
- Railway logs (service `amusing-acceptance`, deployment `ad7cf24e`+):
  buscar `[X_API_COST]` con Ctrl+F.
- SQLite `data/intel_memory.db`, tabla `x_api_calls` (query sección 1).
- Comando `/debug_x` en Telegram muestra stats parseados de la tabla.

---

## 5. Alerta Telegram si costo > $5/mes

**Implementación:**
- Trigger: `modules.intel_memory.should_send_cost_alert(threshold=5.0)`.
- Emisión: `modules.x_intel.maybe_send_cost_alert(app)` llamado desde
  `poll_and_cache_timeline(app)` después de cada successful 200.
- Wire: `bot.py::_x_timeline_cache_job(app)` pasa el `Application` al job,
  que lo pasa a `poll_and_cache_timeline` (commit `a0450a3` + bot.py line
  485 del deploy actual).
- Throttle: tabla `x_api_alerts` en SQLite, clave `monthly_projection`,
  **máx 1 Telegram per 24h**.

**Mensaje esperado (formato real del código):**
```
⚠️ X API COST ALERT
Proyección mensual: $X.XX (threshold: $5.00)
- 7d: $Y.YY
- Calls 7d: N
- Tweets 7d: T
Revisar rate limits o reducir cadence del scheduler.
```

**Chat destino:** `TELEGRAM_CHAT_ID` (env var = `1901156709` según memory).

**Estado actual:** el scheduler loggea warnings en Railway pero NO ha
disparado alerta porque:
- Spend Cap en 403 → `status != 200` → `should_send_cost_alert` no se
  llama en el happy path.
- Igualmente el monthly projection histórico no excede $5 desde el fix
  (por la misma razón — no hay calls exitosas).

**Post-reset, cómo probarlo sin esperar mes entero:**
```bash
# En Railway shell (sólo lectura — no muta nada):
cd fondo-blackcat-bot
python3 -c "
from modules.intel_memory import x_api_cost_projection, should_send_cost_alert
import json
print(json.dumps(x_api_cost_projection(), indent=2))
print(should_send_cost_alert(threshold_usd=0.01))  # fuerza trigger
"
```

---

## Resumen ejecutivo

| Punto | Estado | Acción pendiente |
|---|---|---|
| 1. Request counter 7d | OK — SQLite persist | Query post 2026-05-16 |
| 2. Peor caso $/mes | $22.50 (cap 15/día) · Esperado ≈$4.50 | Revisar después de 2 semanas de tráfico real |
| 3. List reconciliation | Script listo · Blocked by SpendCap | Correr `python -m scripts.reconcile_x_list` después del reset |
| 4. Cost log per call | OK — implementado todas las ramas | — |
| 5. Alerta Telegram > $5 | OK — implementado con throttle 24h | — |

**Siguiente milestone:** 2026-05-16 — reset automático Spend Cap.
Action plan:
1. Esperar primer run exitoso del scheduler (17:00 UTC aprox).
2. Verificar logs `[X_API_COST] caller=scheduler ...` aparecen en Railway.
3. Correr `reconcile_x_list.py` dry-run → validar diff.
4. Correr `--apply` con OAuth 1.0a.
5. Dejar 48h de observación, revisar `monthly_projection_usd` en SQLite.

---

**Referencias:**
- Commits: `a0450a3` (Round 12), `7579f9b` (Round 11.2), `66ab2d4` (Round 9
  — kill pre-R9 per-user).
- Memory: `project_round12_cost_audit.md`, `project_x_payperuse_dynamic_list.md`.
- Railway: service `amusing-acceptance` (`be38a440`), deploy `ad7cf24e` live.
