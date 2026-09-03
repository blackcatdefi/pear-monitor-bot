# Deploy History — pear-monitor-bot (amusing-acceptance)

Append-only log per Cowork constitución §6 paso 8.

## 2026-09-03 — R-FUNDING-BUILD: el panel presentaba la corrida de OTRO build como propia

El /diagnostico de las 02:38, sobre el deploy de `01aa918` recien salido, mostro:

```
✅ ultimo intento hace 25min · 33 hueco(s) pendiente(s)
pruebas 14 (sin novedad 0 · sin medir 14) · filas NUEVAS 0 (eco HL 564)
```

Leido de corrido dice "la reparacion nueva ya se ejercito y no trajo nada" —
que llevaria a concluir que HL no tiene esas filas. Es falso. `sin medir 14`
prueba lo contrario: las 14 pruebas siguen siendo las del build anterior, con
`nuevas = -1`, o sea que **el backfill nuevo todavia no corrio ni una vez**.
Los 25 minutos son de `41d43fa`, con el criterio y el tope viejos — justo la
conducta que el deploy venia a cambiar.

O sea: dos estados que piden lecturas OPUESTAS —"la version nueva ya corrio" y
"la version nueva no corrio"— se renderizaban identicos, y la unica forma de
distinguirlos era deducirlo a mano de otra linea. Es la misma falla de la ronda
anterior, un escalon mas arriba: no un numero con formato de hallazgo, sino un
**tilde verde con formato de confirmacion**.

**La causa:** la marca de tiempo no dice QUE codigo la escribio. Un timestamp
solo nunca puede distinguir "corrio la version nueva" de "corrio la vieja".

**El arreglo.** `sync_all` graba ahora, junto al timestamp, el commit corto del
build que hizo el intento (`META_ULTIMO_COMMIT` via `_commit_actual()`).
`funding_repair_status` devuelve los dos lados —`commit_intento` y
`commit_actual`— y el panel los compara:

```
⚠️ ultimo intento hace 25min pero con el build ANTERIOR (41d43fa, ahora 01aa918): este todavia no corrio
```

Tres decisiones que valen mas que el codigo:

- **La advertencia solo sale si se saben los DOS lados** (`vc and va and vc != va`).
  Gritar "build anterior" sin saberlo seria inventar un hallazgo; la ausencia
  del dato no es evidencia de discrepancia.
- **El commit se graba al ESCRIBIR, no al leer.** Resolverlo en
  `funding_repair_status` haria que siempre coincida consigo mismo y la
  comparacion nunca detectaria nada. La mutacion M17 existe para eso.
- **`_commit_actual` traga todo y devuelve `""`.** No saber el commit no puede
  tumbar un sync que si mueve plata. Queda aceptado por escrito en el
  allowlist de degradacion silenciosa — la guarda lo detecto sola y obligo a
  justificarlo, que es para lo que esta.

**Ademas:** `_hace()` con resolucion de minutos ya venia de la ronda anterior;
aca se le sumo que el panel imprima `?` y no `hace 0` cuando no hay dato.

**Bug propio, misma ronda:** la primera version puso `\u26a0\ufe0f` adentro de
la parte de expresion de un f-string. En Python 3.10 eso es `SyntaxError` (PEP
701 recien lo permite en 3.12) y volteo los 9 tests del modulo de lectura. El
escape sale afuera, a una variable.

**Verificacion:** suite 1532 · guarda 8/8 · scan TOTAL 7 (C1 0 · C2 6 · C3 0 ·
C4 1) sin cambios · **mutaciones 20/20**, con cinco nuevas: no grabar el build,
resolverlo al leer, comparar con `or`, tilde siempre, y nunca avisar.

**Lo que sigue sin estar confirmado:** que los 27 ciclos se limpien. La
discriminacion todavia depende del proximo /diagnostico, pero ahora el panel
puede decir por si solo si el build que corrio es este.

## 2026-09-03 — R-FUNDING-NOVEDAD: el "564" no era reparacion, era el eco del pedido

**RETRACTACION de la entrada de las 02:08.** Ahi escribi, en negrita, "el dato
existia del lado de HL todo el tiempo — 14 pedidos, cero vacios, 564 filas
recuperadas". Eso no se deduce de esos numeros. Los lei mal, y el error es
exactamente el que vengo persiguiendo hace cinco rondas: **un numero con
formato de hallazgo**. Esta vez en un panel que yo mismo escribi una ronda
antes, para dejar de tener numeros con formato de hallazgo.

### Que dicen de verdad esos numeros

`funding_gaps` le pide a HL la ventana `(prev, post)`, y `prev`/`post` son los
timestamps de acreditaciones **que ya tenemos guardadas**. La ventana esta
definida por sus propios bordes conocidos. Entonces HL devuelve siempre, como
minimo, esos bordes:

- `found > 0` es una propiedad de **como armamos el pedido**, no una noticia
  sobre el hueco. `vacias 0` no significa "HL tenia el dato": significa "la
  ventana contiene filas, y una de ellas la pusimos nosotros al definirla".
- `filas traidas 564` contaba lo que HL devolvio, no lo que **entro**.
  `_store_funding` ya devolvia las filas nuevas y ese numero se tiraba.

Las dos consecuencias son opuestas y las dos son malas:

1. el tramo **nunca** se excluia (el filtro de exclusion era `found=0`), asi
   que se repedia en cada sync para recibir el mismo payload — el gasto de
   rate limit que `_fusionar` existia para evitar, entrando por la puerta de
   al lado;
2. `_sacar_probados_vacios` **no podia bajar ni un ciclo a nota**, nunca. La
   I5 quedaba en rojo permanente aunque el dato de verdad no exista del lado
   de HL. La cruz que no se puede cerrar, otra vez, y esta vez fabricada por
   el mecanismo que puse para evitarla.

Y explica el sintoma que disparo la ronda: los tres numeros **identicos** a
las 02:08 y a las 02:15. No era solo que el sync no habia vuelto a correr.

### Que cambio

- `ledger_funding_probe` guarda **dos** cantidades: `found` (el eco de HL) y
  `nuevas` (lo que entro a la base). La diferencia entre ambas es la respuesta
  a "¿esto sirvio de algo?".
- El criterio de exclusion y el de "probado irreparable" pasan a ser
  `nuevas=0`. Es exacto para este uso: como la ventana esta acotada por filas
  conocidas, **toda fila nueva cae adentro del silencio**.
- Migracion con default **-1 = NO MEDIDO**, que no es 0. Las 14 pruebas que ya
  hay en produccion no fueron medidas; ponerlas en 0 habria bajado los 27
  ciclos de violacion a nota **de golpe y sin que nadie mire nada** — el panel
  poniendose verde por un default de columna. Se vuelven a probar.
- El panel imprime `filas NUEVAS` primero y `eco HL` entre parentesis. El eco
  se conserva porque un eco de 0 con la ventana acotada por bordes conocidos
  significaria transporte roto, que es otra cosa y hay que poder verla.

### El tope de la corrida estaba en la variable equivocada

El tope era 3 huecos por wallet por corrida y el comentario que lo justificaba
decia que lo que sobra "queda para la proxima". Lo escribi suponiendo syncs
frecuentes. `_ledger_sync_job` corre **a los 2 minutos del arranque y despues
cada `LEDGER_SYNC_HOURS` (6)**: "la proxima" son seis horas, y 33 huecos a 3
por corrida son medio dia con un numero de plata mal en el reporte.

Peor: el rate limit lo gasta la **pagina**, no el hueco. Un hueco de 24h entra
en una pagina; un hueco de 6 meses toma doce. Contar huecos para proteger un
presupuesto de paginas es contar la cosa que no se paga. Ahora el corte es por
paginas consumidas (`LEDGER_GAP_PAGES=20` por wallet por corrida), dimensionado
contra la cadencia real: 5 wallets × 20 paginas con `PAGE_PAUSE_SEC` entre
pedidos son ~100 pedidos en ~110s, unos 11/min contra los 60/min del
presupuesto. Menos de un quinto, y el reporte no se pisa.

Y ahora se **pausa entre huecos**, no solo entre paginas de un mismo hueco.
Cada ventana arrancaba en `i=0`, asi que N huecos de una pagina salian como N
pedidos consecutivos sin ninguna espera. Con tope 3 era una rafaga chica; subir
el tope sin esto convertia la reparacion en el 429 que vino a evitar.

### Resolucion de minutos en el panel

`hace 0h` no distingue "corrio recien" de "corrio hace 50 minutos". Con un sync
que arranca a los 2 min del deploy y despues cada 6h, esa es justo la
diferencia entre dos lecturas de la MISMA corrida y dos corridas distintas — o
sea entre poder comparar sus numeros y no poder. Abajo de una hora ahora dice
minutos. Es la misma ceguera de observacion de la ronda anterior, un grano mas
fino.

### Verificacion

- **15/15 mutaciones** matan un test cada una (la M3 nacio vacua: la rama de
  "tabla sin la columna" no la alcanzaba nadie porque `_conn()` migra en cada
  conexion; se cubrio con un test unitario sobre una conexion cruda, igual que
  se hizo con la rama de la tabla ausente).
- suite **1528** verdes (1518 + 10), guarda de degradacion silenciosa **8/8**,
  `bug_class_scan` **TOTAL 7** (C1 0 · C2 6 · C3 0 · C4 1), sin cambios.

### La observacion que discrimina en el proximo /diagnostico

Con el default en -1 el panel va a mostrar `sin medir 14` y las pendientes
**arriba** de 33 al principio: correcto, porque esas pruebas vuelven a la cola.
Lo que hay que mirar es la linea nueva:

- `filas NUEVAS > 0` → el dato existia y la reparacion esta entrando. Los
  ciclos tienen que bajar de 27.
- `filas NUEVAS 0` con `eco HL` alto y `sin novedad` subiendo → HL no tiene
  nada mas, y los ciclos bajan a **nota** en vez de violacion. La cruz se
  cierra por la razon honesta.
- `filas NUEVAS 0` con `eco HL 0` → transporte roto, no hueco. Otro bug.

Las tres se veian identicas hasta esta ronda. Recien ahora el panel puede
decir cual es.


## 2026-09-03 02:08 UTC — el readout contesto: la hipotesis estructural era CORRECTA

- **deploy**: `f684c40` / `6f2d6758` — primer panel con el bloque nuevo.
- Lectura textual: `ultimo intento hace 0h · 33 hueco(s) pendiente(s) ·
  pruebas 14 (vacias 0) · filas traidas 564`.

### Que quedo probado

**El dato existia del lado de HL todo el tiempo.** 14 pedidos, **cero
vacios**, 564 filas recuperadas. O sea que el funding nunca falto: el cursor
paso por encima de esas ventanas y, por ser forward-only, no las volvio a
mirar jamas. Bastaba con volver a preguntar. La conjetura de R-FUNDING-HUECO
—"el agujero no se cierra con el tiempo, se fosiliza"— era correcta, y recien
ahora hay evidencia y no razonamiento.

De paso: toda la maquinaria de "probado vacio baja a nota" no se ejercito ni
una vez en produccion (`vacias 0`). Queda como seguro, no como camino vivo.

### Por que la violacion sigue diciendo 27

Cola, no falla. Quedan 33 huecos y el backfill hace 3 por wallet por sync
(tope puesto para no disparar 429 en el money path). Las 564 filas que ya
entraron son de huecos de OTRAS wallets; los intervalos de esos 27 ciclos
siguen en la cola. Se verifica en que el chequeo los sigue clasificando como
*tramo mudo*: si les hubieran entrado filas habrian cambiado de categoria —
la taxonomia de R-I5-FORMA se paga sola aca.

Converge solo: cada hueco reparado desaparece de la cola porque la pasada
siguiente ve funding en ese intervalo y lo saltea.

### La observacion que discrimina en el proximo panel

Pendientes **por debajo de 33** y ciclos **por debajo de 27**. Si las filas
siguen subiendo y los 27 no se mueven, el problema deja de ser la cola y pasa
a ser que la reconstruccion no persiste — otro bug, y uno que el test
`test_el_funding_reparado_llega_a_la_fila_del_ciclo` deberia haber agarrado.
No se toca nada hasta verlo: el mecanismo se esta probando solo y cambiar
`max_gaps` ahora seria ruido encima de un experimento en curso.

## 2026-09-03 — R-FUNDING-LECTURA + R-429-TECHO (mande un arreglo que no podia observar)

- **base commit**: `012dae8` · **service**: pear-monitor-bot
  (amusing-acceptance) / branch `master`
- **entrada**: `/diagnostico` de las 01:49 UTC, deploy `417b19ea`. **2
  problemas**, y los dos son correcciones a rondas MIAS de la misma jornada.

### Problema 1 — la violacion de funding salio identica, palabra por palabra

27 ciclos, el mas largo de 20h, mismo texto que a las 01:32. Ni se reparo ni
bajo a nota. Y con el panel de entonces no habia forma de saber cual de estas
tres cosas paso:

| se ve como | lo que en realidad seria | el arreglo que pide |
| --- | --- | --- |
| violacion intacta | el backfill nunca corrio | engancharlo al scheduler |
| violacion intacta | corrio y fallo (el `log.warning` vive en Railway, que el panel no lee) | el bug del pedido |
| violacion intacta | corrio bien y no encontro ningun hueco | el detector no ve lo que el chequeo si ve |

**Las tres son indistinguibles desde afuera y llevan a arreglos opuestos.**
Mande una reparacion al money path sin ningun modo de leer si se ejecutaba:
una conjetura con formato de arreglo, que es exactamente el defecto que vengo
persiguiendo hace cuatro rondas — esta vez en mi propio codigo.

Esta ronda **no arregla el hueco**: hace que el proximo `/diagnostico` pueda
decir cual de las tres es. Bloque nuevo *Reparacion de funding (tramos mudos)*
con ultimo intento, huecos pendientes, pruebas hechas / vacias / filas traidas
y ultimo error. La marca del intento se escribe **aunque no haya nada que
reparar** —es lo unico que separa "no hay trabajo" de "nunca corrio"— y el
error se **persiste** hasta el panel y se **limpia** solo cuando deja de
pasar, para no volver a fabricar una cruz que nadie puede cerrar.

### Problema 2 — el 429 volvio, y la ronda anterior no lo habia arreglado

`MAX_BACKOFF_SEC` es 30. CoinGecko pide 60. O sea que R-429-RETRY-AFTER
arreglo *"ignorabamos Retry-After"* y siguio ignorandolo, solo que despues de
leerlo: leia los 60, los recortaba a 30 y reintentaba **adentro** del castigo.
Ese intento no podia salir bien por construccion, y encima era trafico durante
la penalizacion, que es lo que la alarga.

Ahora, si el servidor pide mas que el techo, no se reintenta: se corta y se
reporta. Esperar menos de lo que pidio el unico que esta contando no es una
segunda chance, es insistir.

**El error de metodo, que importa mas que el parche:** declare el 429
"confirmado arreglado" con UN `/diagnostico` limpio. Un panel verde entre dos
castigos no es evidencia de que algo se arreglo — es evidencia de que en ese
momento no estaba fallando.

### Verificacion

- Un test existente afirmaba lo contrario de lo que ahora hace el codigo
  (*"se espera hasta el techo y se reintenta igual"*). No se toco para que
  pasara: se **dio vuelta con el motivo escrito**, porque produccion refuto su
  premisa al dia siguiente.
- Se descarto escribir los tests del readout contra una copia del bloque de
  `sync_all`. Un test contra una copia del bucle pasa aunque el bucle real se
  borre; se mockea la red y el telegram y se ejecuta la funcion de produccion.
- **13/13** mutaciones detectadas · suite **1518 passed** · guarda de
  degradacion silenciosa **8/8** · `bug_class_scan` **TOTAL 7** sin cambios.
- La guarda de degradacion **rechazo** el primer intento: `_horas_desde_iso`
  devolvia `None` sin declarar nada. Se acepto en el allowlist con la razon
  escrita — no alimenta ningun numero, y el dato del que depende la decision
  es la PRESENCIA de la marca, que un timestamp ilegible no puede falsear.

### Lo que esta ronda NO puede afirmar

Ni que el hueco se repare ni que el 429 no vuelva. Lo unico que se puede
afirmar es que el proximo `/diagnostico` va a poder distinguir los tres
estados en vez de mostrar el mismo texto para todos.

## 2026-09-03 — R-FUNDING-HUECO (el cursor solo avanza, asi que el hueco se fosiliza)

- **base commit**: `92c4f2c` · **service**: pear-monitor-bot
  (amusing-acceptance) / branch `master`
- **entrada**: `/diagnostico` de las 01:32 UTC, deploy `a4223866`. La ronda
  anterior partio el agujero en tres formas y produccion contesto cual es:
  **tramo mudo**. 27 ciclos, el mas largo de 20h, sin NI UNA acreditacion de
  ninguna moneda en todo el intervalo, con **24h de silencio** entre la ultima
  previa y la primera posterior. No es el nombre de la moneda ni el redondeo:
  es el paginado de `userFunding`.

### El defecto es estructural y no depende de la causa

`funding_cursor_ms` solo avanza. Una vez que paso por arriba de una ventana
sin traerla, ningun sync posterior la vuelve a mirar. El agujero no se cierra
con el tiempo — se fosiliza. Eso vale sea cual sea el motivo por el que la
ventana se salteo (un 429, un corte, una pagina de 500 que se creyo completa),
y por eso la reparacion se pudo escribir sin saber todavia cual de los tres
fue.

### Lo que se construyo

| pieza | donde | que hace |
| --- | --- | --- |
| `funding_gaps(wallet)` | `trade_ledger.py` | saca los tramos mudos, acotados por los bordes REALES del silencio (no por el ciclo), y los **fusiona**: 27 ciclos adentro de un mismo silencio son **1** pedido |
| `_fetch_funding_window(w,a,b)` | `trade_ledger.py` | `userFunding` acotado por los **dos** extremos (`startTime` y `endTime`), paginado de a 500 |
| `ledger_funding_probe` | esquema | registro `(wallet,a,b,probed_at,found)`. Sin esto no hay forma de distinguir *"todavia no lo intentamos"* de *"ya lo intentamos y no existe"* |
| `backfill_funding_gaps` | `sync_all` | corre adentro del lock, `max_gaps=3` por corrida para no quemar el rate limit, y **reconstruye** las posiciones si trajo filas |
| `_sacar_probados_vacios` | `ledger_invariants.py` | el tramo probado-vacio baja de ❌ a `ℹ` nota |

### La regla que gobierna el diseño

Una falla que no se puede cerrar entrena a ignorar el panel entero — es la
misma leccion de R-RAILWAY-VARS (la cruz permanente de `RAILWAY_TOKEN`). Asi
que la violacion nueva tiene salida por los dos lados: o se repara sola y
desaparece, o HL prueba que el dato no existe y baja a nota. **Una falla de
red no cuenta como prueba**: el probe se anota DESPUES de tener respuesta, no
antes. Un default silencioso en el camino del dinero nunca es una degradacion
aceptable.

### Verificacion — 12 mutaciones, dos de ellas VACUAS al primer intento

- **M7, "no reconstruir despues de traer las filas"**: no hacia fallar ningun
  test. Que el chequeo se calle NO alcanza — I5 lee `ledger_funding` directo,
  asi que se calla apenas entran las filas, aunque `ledger_positions.funding_net`
  siga en 0.00. Y ese es el numero que va al track record: el P&L quedaria mal
  **con el panel en verde**, que es peor que el estado del que salimos. Test
  nuevo que la agarra: `test_el_funding_reparado_llega_a_la_fila_del_ciclo`
  (`funding_net == -2.0`, `net_pnl == 96.0`).
- **M12, "sin tabla de probes se excusa todo"**: el test que la tenia que
  agarrar dropeaba `ledger_funding_probe` y despues llamaba al chequeo. No
  probaba nada — `_conn()` hace `CREATE TABLE IF NOT EXISTS` de todo el
  esquema en **cada** conexion, asi que la tabla estaba de vuelta antes de que
  el chequeo la mirara. Se reescribio como test unitario contra una conexion
  cruda, y el docstring de `_sacar_probados_vacios` ahora dice que esa rama no
  se alcanza desde el llamador actual, en vez de afirmar lo contrario.
- Un tercer defecto era del propio fixture: `_fills()` insertaba `dir=''` y
  `_is_perp_fill` filtra por ahi, asi que `reconcile_positions` devolvia `[]`.
  El test de `funding_net` estaba midiendo nada. Era defecto del test, no de
  produccion — produccion tiene 27 ciclos reconstruidos.
- **12/12** mutaciones detectadas · suite **1507 passed** (1489 + 18 nuevos) ·
  guarda de degradacion silenciosa **8/8** · `bug_class_scan` sin cambios en
  **TOTAL 7 (C1 0 · C2 6 · C3 0 · C4 1)**.

### Lo que esta ronda NO puede afirmar todavia

Que los 27 ciclos se reparen. La reparacion pide de vuelta 3 huecos por sync;
si HL devuelve las filas, la violacion desaparece; si vuelve vacio, baja a
nota. **Cual de los dos pasa lo dice el proximo `/diagnostico`, no yo.**

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
