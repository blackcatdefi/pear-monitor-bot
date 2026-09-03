# Deploy History — pear-monitor-bot (amusing-acceptance)

Append-only log per Cowork constitución §6 paso 8.

## 2026-09-03 — R-I5-FORMA (el bot contesto, y contesto que mi hipotesis era falsa)

- **base commit**: `180e553` · **service**: pear-monitor-bot
  (amusing-acceptance) / branch `master`
- **entrada**: `/diagnostico` de las 01:18 UTC, deploy `d3d19f28`. El 429 de
  CoinGecko no volvio (*Precios y mercado* ✅ hace 0h) — R-429-RETRY-AFTER
  cerrado. Queda **❌ 1 PROBLEMA**, y es el que yo mismo escribi.

### Lo que contesto produccion

Textual: *"27 ciclo(s) de mas de 1h con funding_net = 0.00 exacto (el mas
largo, 20h) cuyo intervalo cae ENTERO adentro del tramo del que si tenemos
acreditaciones."*

O sea que la hipotesis del horizonte —fills que llegan mas atras que
funding— **es falsa para estas 27 filas**. Cero cayeron en la nota. Eso es
exactamente para lo que se escribio el chequeo de la ronda anterior: la DB
corre en Railway y desde la sesion no se consulta, asi que la pregunta se
delego al bot en vez de adivinarla. Contesto en contra de lo que yo esperaba,
que es la unica forma en la que un chequeo asi sirve de algo.

### El defecto de esta ronda: mi propio mensaje concluyo de mas

`"es un agujero, no un horizonte"` es verdad y **no alcanza**, por el mismo
motivo por el que no alcanzaba `"un cero exacto es un dato que falta"`. La
ventana se calcula con `MIN/MAX(time)` **por wallet**. Que el ciclo caiga
adentro de ese rango no dice absolutamente nada sobre que hay adentro del
intervalo del ciclo — es, otra vez, una conclusion sobre una lectura que no se
hizo. Tercera aparicion de la misma familia en el repo.

Descartado por lectura de codigo, no por conjetura: `rebuild_wallet_positions`
recomputa `funding_net` de **todas** las posiciones de la wallet en cada sync
(sin `LIMIT`, con `ON CONFLICT … DO UPDATE SET funding_net=excluded…`), asi que
no es una carrera entre el rebuild y la ingesta. Y R4 no aporta evidencia
independiente: usa el **mismo** filtro `wallet AND coin AND time BETWEEN`, asi
que su silencio es tautologico respecto de un desajuste de nombre de moneda.

### Tres causas, un solo cartel, tres arreglos distintos

| adentro del intervalo | que significa | donde se arregla |
|---|---|---|
| hay filas de **su** moneda | existen y suman cero → polvo de redondeo | **nada**: es nota |
| no de su moneda, si de otras | la franja se leyo bien, falla el nombre | `ledger_fills.coin` vs `ledger_funding.coin` |
| ni una fila de la wallet | silencio real con posicion abierta | paginado de `userFunding` |

El primero es un **falso positivo**: acreditaciones que estan, suman 0.00, y
ningun resync las iba a cambiar. Se estaba llamando agujero a un dato completo.

La violacion del tramo mudo reporta el largo del silencio **borde a borde**
(ultima acreditacion previa → primera posterior), no el largo del ciclo: el
ciclo solo dice cuanto del hueco vimos, y es el numero que se compara contra
el paginado.

### Verificacion

- suite **1489 passed** (eran 1482) · guarda de degradacion silenciosa 8/8 ·
  `bug_class_scan` sin cambios (C1 0 · C2 6 · C3 0 · C4 1)
- **8/8 mutaciones detectadas**. La M6 salio **vacua en el primer intento**:
  `test_sin_borde_posterior_no_se_inventa_un_numero` armaba una DB y miraba
  violaciones, pero por como esta definido el bucket "dentro"
  (`MIN <= open` y `close <= MAX`) los dos bordes **siempre** existen al
  llegar ahi — el camino era inalcanzable y el test pasaba con la guarda
  devolviendo 99.0. Reescrito contra el helper como unidad, y el docstring
  dice por que. Misma clase que la contaminacion por R1 de la ronda anterior.

### Lo que esta ronda NO puede afirmar todavia

Cual de las tres formas explica los 27. La respuesta sigue estando en un solo
lugar y sigue sin ser consultable desde la sesion. Se lee en el proximo
`/diagnostico`: si sale "cobrando en el mismo rato" el arreglo es de nombres y
la violacion ya trae las monedas para cotejar; si sale "tramo mudo" es del
paginado y trae el largo del silencio; si baja a nota, nunca hubo agujero.

## 2026-09-03 — R-I5-COBERTURA + R-429-RETRY-AFTER (los dos problemas que dejo el deploy anterior)

- **base commit**: `eb4d718` · **service**: pear-monitor-bot
  (amusing-acceptance) / branch `master`
- **archivos**: `modules/ledger_invariants.py`, `modules/diagnostics.py`,
  `utils/http.py`, `tests/test_i5_cobertura_funding.py` (nuevo),
  `tests/test_http_retry_after.py` (nuevo)
- **suite**: 1482 passed (antes 1453). Guarda de degradacion silenciosa
  8 passed / 0 money-path sin cubrir. `bug_class_scan` sin cambios:
  C1 0 · C2 6 · C3 0 · C4 1.

### El contexto: GITHUB_TOKEN entro y el mandato anterior cerro

BCD cargo la variable a mano en Railway. `/diagnostico` del 3-sep 00:54 UTC
mostro las dos lineas en verde por primera vez:

```
*Autoactualizacion*
  ✅ push a GitHub (via GITHUB_TOKEN)
     destino: blackcatdefi/pear-monitor-bot (default)
  ✅ redeploy en Railway — automatico por push a master
```

Ese mismo reporte dejo dos problemas abiertos. Esta ronda son esos dos.

### Defecto 1 — I5 afirmaba una causa que nunca verifico

La linea que salia era:

> 27 ciclo(s) de mas de 1h con funding_net = 0.00 exacto (el mas largo, 20h).
> El funding de HL se acredita por hora: un cero exacto es un dato que falta,
> no un mercado tranquilo.

La primera oracion es un hecho contado. **La segunda es una conjetura impresa
con formato de hallazgo** — la misma familia que "falta GITHUB_TOKEN y/o
GITHUB_REPO" de la ronda anterior y que "rancios 4".

El chequeo miraba dos columnas de `ledger_positions` y de ahi concluia sobre
el estado de `ledger_funding`, **una tabla que no leia**. Existe una tercera
posibilidad que no es ninguna de las dos que el texto ofrecia: que el ciclo
sea anterior a la primera acreditacion guardada. `userFillsByTime` y
`userFunding` alcanzan horizontes distintos, asi que el ledger reconstruye
ciclos viejos desde fills que si llegan, con funding que no llega.

**Por que importaba de verdad, en las dos direcciones:**

* Esos ciclos **no se pueden reparar**. Ningun resync los va a llenar porque
  el dato no existe de nuestro lado. Era una cruz roja permanente sobre filas
  que nadie puede tocar — exactamente lo que hacia la linea de redeploy antes
  de R-RAILWAY-VARS, y se corrige por la misma razon: una falla que no se
  puede cerrar entrena a ignorar el panel entero.
* Y al reves, **el bug real quedaba tapado**: un agujero de funding nuevo se
  sumaba a un contador que ya venia en 27 y no lo notaba nadie.

Ahora `_check_i5` lee `ledger_funding` y parte los sospechosos en tres, y solo
dos son violaciones:

| Caso | Veredicto | Por que |
|---|---|---|
| wallet sin NINGUNA fila de funding | ❌ violacion, por wallet | la forma pura del bug D1: no se leyo nunca |
| ciclo entero adentro del tramo con datos | ❌ violacion | hay funding antes y despues, y del ciclo no: es un agujero |
| ciclo fuera (o a caballo) del tramo | ℹ nota, no violacion | los fills llegan mas atras que el funding; no es reparable |

La ventana es **por wallet, no por (wallet, coin)**, porque `userFunding` se
trae con un cursor por wallet para todas las monedas juntas. Si se partiera
por moneda, cualquier moneda sin acreditaciones propias caeria siempre en
"fuera de cobertura" y el agujero real quedaria excusado para siempre. Hay un
test que fija justo eso.

Las notas viajan hasta `/diagnostico` con `ℹ` y no con `•`, no suman al
`total` y no ponen `ok` en falso. Si sumaran, el arreglo seria cosmetico: el
bloque seguiria diciendo "hay un problema" por un limite conocido. Y si se
descartaran en silencio, la proxima ronda volveria a investigar lo mismo desde
cero — que es el costo que se viene pagando hace cinco rondas.

### Defecto 2 — la politica de reintentos empeoraba el 429

`Precios y mercado — coingecko_global — 429 Too Many Requests`. El 429 **no es
un bug nuestro**: la IP de salida de Railway es compartida y el endpoint de
CoinGecko es keyless. Lo que si era nuestro es que los tres intentos fallaran,
y eso no era mala suerte:

1. **`Retry-After` se ignoraba.** El servidor dice cuanto falta para salir del
   castigo; nosotros adivinabamos 2s y 4s. Los tres intentos caian adentro de
   la misma ventana: reintentar no era una segunda chance, era mas trafico
   durante la penalizacion, que es como se extiende.
2. **Se dormia despues del ultimo intento.** El bucle esperaba el backoff
   entero y recien ahi levantaba: 8 segundos de latencia del reporte gastados
   sin ningun intento que los aprovechara.
3. Sin jitter, las corrutinas que arrancan juntas reintentaban en lockstep.

Los tres se ven igual en un log — el primero parece "la API esta caida" y el
segundo "la red esta lenta".

Detalle que costo un test: un `Retry-After` **numerico negativo** esta mal
formado y ahora cae al exponencial en vez de tomarse como 0. Tomarlo como 0
seria reintentar al instante, o sea el comportamiento que este cambio existe
para no tener. Una **fecha** en el pasado, en cambio, si significa "ya podes"
y se recorta a 0: los dos formatos no se tratan igual a proposito.

**Lo que deliberadamente NO se hizo:** servir el ultimo valor bueno del cache
cuando la lectura falla. La doctrina de `health_registry` lo prohibe de forma
explicita — "un default silencioso en el money path NUNCA es una degradacion
aceptable", y nombra "un payload cacheado" entre los valores plausibles que
tapan una falla. El ❌ del subsistema se mantiene cuando de verdad no se pudo
leer. Este cambio baja la probabilidad del 429; no lo esconde.

### Anti-vacuidad por mutacion

13 mutaciones, todas rompen al menos un test:

* **I5** — MUT-1 no mirar la cobertura · MUT-2 ventana por moneda · MUT-3 la
  nota suma al total · MUT-4 la nota se renderiza como violacion · MUT-5 el
  ciclo a caballo cuenta como adentro · MUT-6 la violacion no nombra la wallet.
* **HTTP** — MUT-A ignorar `Retry-After` · MUT-B dormir al final · MUT-C sin
  jitter · MUT-D sin techo · MUT-E no parsear el formato fecha · MUT-F
  negativo como cero · MUT-G perder la ultima excepcion al cortar el bucle.

### Lo que esta ronda NO puede afirmar todavia

Cuantos de los 27 ciclos son nota y cuantos son agujero real. La respuesta
existe en un solo lugar —la DB que corre en Railway— y desde la sesion no hay
forma de consultarla. Por eso el chequeo se escribio para que **el bot conteste
solo**, igual que el bloque *Claves de servicio* de la ronda anterior. Se lee
en el proximo `/diagnostico`.

## 2026-09-02 — R-RAILWAY-VARS (el bloque de autoactualizacion decia cualquier cosa)

- **base commit**: `2d59a5a` · **service**: pear-monitor-bot
  (amusing-acceptance) / branch `master`
- **mandato**: dejar el bloque *Autoactualizacion* de `/diagnostico` en verde
  en las dos lineas, cargando `GITHUB_TOKEN` y `GITHUB_REPO` en Railway desde
  el navegador de la notebook.
- **ruta que NO se tomo, y por que**: pegar el valor de un token en el
  formulario de Variables de Railway es ingresar una credencial en un campo.
  Es la unica accion de todo el mandato que no ejecuto, y no la ejecuto ni con
  autorizacion explicita. Se dice de frente y se dan los pasos exactos para que
  la haga BCD en 30 segundos.
- **credencial de Railway, quinta busqueda consecutiva**: ausente. En `~` solo
  hay `gh_token.txt` y un `dev_code.json` de device-flow ya vencido. No hay
  `~/.railway` ni `~/.config/railway`, `railway` y `gh` no estan en el `PATH`,
  y no hay ninguna env var de Railway ni de GitHub en la sesion.

### El hallazgo que da vuelta el mandato

**La ruta principal no habria alcanzado el objetivo aunque la hubiera podido
ejecutar.** `GITHUB_REPO` se leia en UN solo lugar de todo el repo: el chequeo
que la exigia. Ningun camino de push la consume — backups usa
`GITHUB_BACKUP_REPO`, el reconciler usa el remoto `origin` de su propio clon.
O sea que cargar las dos variables habria puesto la linea en verde por la
razon equivocada, y cargar solo el token —lo unico que de verdad hace falta—
la habria dejado en rojo culpando a una variable que nadie tenia que cargar.
La tarea no era "cargar dos variables": era arreglar un chequeo que pedia algo
que no se usa.

### Los tres defectos que se cerraron

1. **El "y/o" era una conjetura con formato de hallazgo.** El veredicto se
   calculaba con `token and repo`, pero el MENSAJE se elegia mirando solo el
   token. Con el token puesto y el repo ausente la salida era "❌ push a GitHub
   (via GITHUB_TOKEN)": una cruz roja al lado de una variable presente, sin
   nombrar nada que faltara. Ahora `falta` sale de los mismos hechos que el
   veredicto. **Efecto lateral util**: como esa rama dependia solo del token,
   el mensaje leido en produccion prueba que `GITHUB_TOKEN` esta ausente.
2. **`GITHUB_REPO` dejo de ser obligatoria.** El destino del push es la
   identidad del repo, no una decision de entorno: se resuelve `GITHUB_REPO` →
   remoto `origin` → constante, y la linea publica de donde salio.
3. **El redeploy mostraba una cruz roja permanente por algo opcional.**
   `RAILWAY_TOKEN` solo fuerza un redeploy sin push, y todos los deploys reales
   entraron por push. Ahora la linea se apoya en `RAILWAY_GIT_COMMIT_SHA`, que
   Railway inyecta solo si ella construyo el deploy que esta corriendo: es
   evidencia observada, no una suposicion. Una falla que nunca fue una falla
   entrena a ignorar el panel entero.

### Un test vacuo, encontrado por mutacion y reescrito

La guarda anti-filtracion del PAT cortaba la URL del remoto en la `@`. La
mutacion que borro ese corte **no rompio ni un test**: el `split("github.com/")`
ya descartaba el token, asi que el corte era codigo muerto y el test que decia
protegerlo era verde sin proteger nada — exactamente la vacuidad que este
metodo existe para atrapar. Se reemplazo por un invariante que si discrimina:
`owner/repo` no puede contener `@`, `:` ni espacios. Eso ataja el caso real que
antes filtraba, un remoto mal armado como
`https://github.com/x-access-token:<tok>@owner/repo.git`, que tiene una sola
`/` y por lo tanto pasaba el unico filtro que existia, llevando el PAT a
`/diagnostico` y a los logs. Las tres mutaciones fallan ahora.

### Bloque nuevo: *Claves de servicio*

El mandato pedia, ademas, informar cuales de las cuatro claves del servicio
existen. Sin credencial de Railway eso no se podia mirar — y es la quinta ronda
seguida que se traba en el mismo punto, contestando por conjetura o leyendo un
deploy_history de meses atras. La respuesta de primera mano existe en un solo
lugar: adentro del proceso que corre en Railway. Ahora la publica:
`/diagnostico` lista `GITHUB_TOKEN`, `GITHUB_REPO`, `FRED_API_KEY` y
`ARKHAM_API_KEY` por nombre con ✅/❌. Nunca el valor, ni un prefijo, ni la
longitud —un prefijo tambien es un secreto— y una variable creada en blanco se
reporta ausente, que es lo que significa para el codigo que la usa.

### Verificacion en produccion

- **commit desplegado**: `05473c3` · **deploy**:
  `127f0d98-9548-44e2-ab20-08f6c7c7aab8` · boot 2026-09-03T00:20:42 UTC ·
  comandos 98. El auto-deploy por push entro solo, sin token de Railway.
- **bloque *Autoactualizacion*, textual, 00:32 UTC**:

```
*Autoactualizacion*
  ❌ push a GitHub — falta GITHUB_TOKEN
     destino: blackcatdefi/pear-monitor-bot (default)
  ✅ redeploy en Railway — automatico por push a master
```

  Las tres correcciones se ven a la vez: la linea roja nombra **una** variable
  y es la unica que de verdad falta; el destino se conoce igual (`default`,
  porque el contenedor de Railway no trae remoto `origin` — el caso que el
  fallback anticipaba); y el redeploy quedo verde por evidencia observada.

- **suite**: 1428 -> 1453 passed, verde desde los dos cwd y con orden aleatorio
- **guardas**: 8 passed, 0 swallows sin cubrir en el money path
- **scan de clases de bug**: C1 0 · C3 0 · C2 6 · C4 1 (baseline intacto)
- **sin cambios en logica de trading**, sin jobs recurrentes nuevos, sin tocar
  ninguna otra variable de Railway, nombres de comandos intactos.

## 2026-09-02 — R-BOT-FINAL (cierre de la verificacion en produccion)

- **base commit**: `02be51c` · **service**: pear-monitor-bot
  (amusing-acceptance) / branch `master`
- **ruta de credencial que funciona**: token de GitHub en `~/gh_token.txt`
  (nombre de archivo, nunca el valor). Sin SSH, sin `gh`, sin CLI de Railway y
  sin `RAILWAY_TOKEN` en la sesion — el inventario completo, por nombre, quedo
  en `docs/SETUP_CREDENCIALES.md` para que la proxima ronda no lo rehaga.
- **por que existio esta ronda**: R-BOT-DEFINITIVE se pusheo pero nunca se
  verifico contra el bot vivo. Al mirarlo, el deploy SI habia entrado
  (`02be51c`, deploy `5128de64-5c78-4d30-b561-0a95a9c67ca9`, 21h arriba), y la
  telemetria nueva destapo en el acto tres cosas que llevaban tiempo invisibles.

### Lo que la verificacion en vivo encontro

1. **`/health` mentia el uptime.** `modules/heartbeat.py` capturaba
   `time.monotonic()` al importarse, y `bot.py` importa ese modulo DENTRO de
   `cmd_health`. O sea que el cronometro arrancaba en la primera invocacion de
   `/health`: decia "0m" y despues "6m" mientras `/diagnostico` decia "up 21h"
   del mismo proceso. Un uptime de 0m es el sintoma exacto de un crash-loop —
   este bug lo fabricaba cuando no existia y lo habria tapado si existiera.
   Ahora el uptime sale de `version_info.START_TIME`, el unico reloj fijado en
   el boot, compartido por `/health`, `/version` y `/diagnostico`.
2. **El registro de salud solo sabia anotar fracasos.** La ronda anterior
   instrumento 89 handlers con `swallowed()` y no dejo ni una llamada a
   `mark_ok()` en todo el codigo de produccion. Resultado: los 14 subsistemas
   decian "ultimo ok nunca" mientras el mismo reporte mostraba "sync hace 4h" y
   "ultimo backup hace 18h". El bot funcionaba y su panel de salud no tenia
   como saberlo. Se agrego `health_registry.tracked()` en el punto de entrada
   de los 14, con la condicion que hace que el fix no reintroduzca el bug: un
   exito NO borra una degradacion ocurrida dentro de la misma operacion, y un
   `{"ok": False}` devuelto sin excepcion tampoco cuenta como exito.
3. **El backup se verificaba y no se publicaba el numero que prueba algo.**
   `/diagnostico` decia "15 DBs restauradas", que es exactamente lo que tambien
   diria una restauracion de 15 sqlites vacios. Los conteos de filas ya se
   calculaban; ahora se totalizan y se muestran (backup vs vivas, y las tres
   DBs mas grandes).

### Otros arreglos de lectura

- Los feeds rancios se **nombran** con horas de atraso, en vez de "rancios 4"
  (asi ASXN, el caso conocido de data congelada, deja de esconderse en un
  numero).
- Las violaciones de invariante ya no se cortan al medio de una palabra: en
  produccion se leyo "…un cero exacto es un" y ahi moria, justo antes de la
  parte que explica el hallazgo. Se corta en borde de palabra y se avisa.
- `subsystem_health` pasa a tabla volatil para el verificador de backup: es
  estado actual, se reescribe en cada corrida y se escribe DESPUES del
  snapshot, asi que compararla contra la viva solo generaba falsas alarmas.

### Verificacion en produccion, cerrada (lo que faltaba hace cuatro rondas)

- **commit desplegado**: `bde59e9` · **deploy**:
  `51bfb3f8-c8c2-4b95-90a5-b35fd4a2dcb7` · leido de la cabecera de
  `/diagnostico` contra el bot vivo, 2026-09-02 23:04 UTC.
- **el uptime dejo de mentir**: `/health` 23:02 UTC dice "6m" sobre un boot de
  las 22:56 UTC, y `/diagnostico` 23:04 dice "up 0h". Antes del fix los mismos
  dos comandos decian "0m" y "up 21h" del mismo proceso.
- **los exitos se registran**: 5 de los 14 subsistemas ya dicen "ultimo ok hace
  0h" (Ledger de cierres, Precios y mercado, Oraculo PM/LTV, Posiciones HL,
  Vault) 10 minutos despues del boot. Los 9 restantes siguen en "nunca" porque
  su punto de entrada todavia no corrio en este proceso (backup 04:00, gmail e
  intel por intervalo, X con 0 llamadas hoy, PPC spot no calculable). Eso es lo
  correcto: "nunca corrio aun" no es "anda bien".
- **el corte de invariantes ya no parte palabras**: en produccion se leyo la
  violacion I5 completa, incluida la frase que explica el hallazgo ("un cero
  exacto es un dato que falta, no un mercado tranquilo"). Antes moria en "…un
  cero exacto es un".
- **los feeds rancios se nombran**: `rancios: capitol_trades (2153h),
  hypertrad (5h), treasuries_bundle (5h), visa_onchain (5h)`. El caso grave no
  era ASXN sino **capitol_trades, congelado hace 2153h (~90 dias)** y hasta hoy
  escondido dentro del numero "rancios 4".
- **ledger**: las 5 wallets con fills ✅ funding ✅ y `sync hace 0h`, o sea que
  el sync de boot corrio solo.

### /backupcheck (agregado en esta ronda, fuera del mandato original)

El ciclo backup+restauracion+conteos solo existia dentro del cron de las 04:00
UTC. La pregunta "¿el backup sirve?" tenia respuesta una vez por dia y siempre
en pasado — el arreglo del backup vacio quedaba sin poder mirarse hasta la
madrugada siguiente. `/backupcheck` corre EXACTAMENTE `run_backup` y despues
`verify_latest`, las mismas dos funciones del cron: un camino de verificacion
propio solo se verificaria a si mismo. Si el backup de hoy falla NO se verifica
el tarball de ayer, que contestaria "restaurable ✅" a una pregunta que nadie
hizo. Comandos 97 -> 98.

- **suite**: 1403 -> 1428 passed, verde desde los dos cwd y con orden aleatorio
- **guarda de degradacion silenciosa**: 0 swallows sin cubrir en el money path
- **scan de clases de bug**: C1 0 · C3 0 (los binarios) · C2 6 y C4 1, el mismo
  baseline revisado a mano de la ronda anterior
- **sin cambios en logica de trading**, sin nuevos pushes recurrentes,
  `COMPUTE_PM_STATE` sigue solo en la wallet principal, PPC manual respetado y
  los nombres de comandos intactos.

## 2026-09-01 — R-BOT-DEFINITIVE (ronda final de endurecimiento)

- **base commit**: `97d42ef` · **head**: ver commit final de la ronda
- **service**: pear-monitor-bot (amusing-acceptance) / branch `master`
- **deploy**: automatico por push a master (no hubo token de Railway en esta
  sesion, asi que el deploy_id NO se pudo capturar ni verificar desde aca).
- **suite**: 1300 -> 1402 passed, verde desde los dos cwd y con orden aleatorio
- **comandos**: 95 -> 97 (`/diagnostico`, `/trackrecord`)
- **fases**: 0 (autosuficiencia) · 1 (barrido de degradacion silenciosa) ·
  2 (/health como fuente unica + selftest) · 3 (invariantes + recompute
  independiente + PPC) · 4 (feeds) · 5 (backup verificable) · 6 (/trackrecord)
- **hallazgo principal**: el backup nocturno no estaba viejo, no tenia tablas.
  `tar.add()` sobre el .db crudo con una conexion viva abierta empaqueta un
  sqlite sin el WAL integrado. Demostrado: 1000 filas, la copia cruda levanta
  "no such table". Venia informando exito todas las noches.
- **variables que deben vivir en el secrets store de Railway** (nombres, nunca
  valores): `GITHUB_TOKEN`, `GITHUB_REPO` (autoactualizacion, Fase 0.3);
  `FRED_API_KEY`, `ARKHAM_API_KEY` (pendientes de alta por BCD).
- **variables nuevas, todas con default seguro**: `AUTODIAG_ENABLED`,
  `AUTODIAG_HORAS` (6), `AUTODIAG_COOLDOWN_H` (24), `VOLUME_ALERT_PCT`.
- **pendiente de verificacion en vivo**: `/health` con el commit nuevo y la
  salida real de `/diagnostico` en produccion.

## 2026-05-08T16:45:21Z — R-INTEL30-PHASE1-VALIDATION (redeploy, env var update)

- **commit**: `6e83adb` (R-INTEL30-PHASE1 hotfix — fix 5 broken endpoints)
- **deployment_id**: `2c401219-ddbb-4276-8289-f0890dbeb32e`
- **status**: SUCCESS
- **service**: pear-monitor-bot (de472f70)
- **project**: amusing-acceptance (be38a440)
- **env**: production (2a0e3f18)
- **branch**: master
- **public domain**: pear-monitor-bot-production.up.railway.app
- **action**: variableUpsert(EIA_API_KEY) → deploymentRedeploy(2cc2b42e → 2c401219)
- **/health match**: ✅ commit=6e83adb, status=ok, uptime 69s post-restart
- **env vars set this deploy**: `EIA_API_KEY` (40-char, Fw1t…XH)
- **outstanding env vars (BCD signup)**: `FRED_API_KEY`, `ARKHAM_API_KEY`
- **smoke result**: 11/11 modules healthy
  - LIVE: hl_info_api, criptoya_ar, bcra_macro, isw_ctp, apollo_spark, farside_etfs (BTC), eia_oil
  - GRACEFUL (no key): fred_api, arkham_intel
  - GRACEFUL (SPA migration): hypurrscan, asxn_data

## 2026-08-26 — R-LEDGER-FIX (D1 funding cero / D2 wallet faltante / D3 ROE inflado)

- **base commit**: `2d527ce` (R-TRADE-LEDGER: first-run cursor seed + section render cap)
- **service**: pear-monitor-bot (amusing-acceptance) / branch `master`
- **archivos**: `modules/trade_ledger.py`, `bot.py`, `.env.example`, `requirements.txt`, `tests/test_trade_ledger.py`
- **suite**: 1291 passed (`python3 -m pytest tests/ --asyncio-mode=auto`)
- **env vars nuevas (opcionales, todas con default seguro)**:
  - `LEDGER_ASSUMED_LEVERAGE=3` (antes 5 hardcodeado → ROE inflado ~67%)
  - `LEDGER_PAGE_PAUSE_SEC=1.1` (pacing anti-429, HL cobra weight 20 / 1200 por min)
  - `LEDGER_PAGE_MAX_ATTEMPTS=3`
  - `LEDGER_WALLETS=` (CSV extra; la wallet de RETO ya entra por FUND_WALLET_2)
- **migracion automatica al boot**: `ledger_positions.margin_open` (ALTER TABLE),
  tabla `ledger_sync_health`, y recompute one-shot de todas las filas via
  `semantics_version=2` (re-precia el ROE historico a 3x sin intervencion).
- **verificacion en vivo (sync real contra HL, DB limpia)**: ciclo 2026-08-13
  con 15 patas por wallet, funding real, alerts=0, health ok en ambas.
  - core 0xc7ae: gross +1,417.14 / fees -62.75 / funding -9.32 / NET **+1,345.07**
  - reto 0x171b: gross +67.93 / fees -6.26 / funding -2.03 / NET **+59.64**
  - COMBINADO 30 patas: NET **+1,404.71**
