# Handoff — el caso del funding faltante (cerrado 2026-09-03 03:06 UTC)

Para el proximo Claude que agarre este repo. Esto no es un changelog: es la
explicacion de un caso que tardo **seis rondas** en cerrarse y de por que las
primeras cinco fallaron. La parte util no son los commits, es el patron de
error, porque se va a repetir.

---

## 1. El sintoma

`/diagnostico` mostraba una violacion del invariante I5 del money path:

> 27 ciclos de posicion largos (el mas largo de 20h) sin ninguna fila de
> funding en tramos de mas de 24h de silencio.

En castellano: hubo posiciones abiertas durante horas y el ledger no tenia los
cobros/pagos de funding de ese rato. Eso mueve plata — el funding entra en el
PnL neto de cada ciclo. Una violacion ahi significa que un numero del fondo
puede estar mal.

## 2. La clase de bug que estaba abajo de todo

**Una conjetura impresa con el formato de un hallazgo.**

Es el patron que se repitio en las seis rondas. Un valor que es en realidad una
suposicion, un default o un eco, se renderiza con la tipografia de un dato
medido: un numero, un tilde verde, una frase afirmativa. Quien lo lee —yo
incluido— concluye algo que nadie verifico.

Las instancias concretas de este caso:

| # | Lo que se imprimia | Lo que en realidad era |
|---|---|---|
| 1 | `filas traidas 564` | El **eco** de HL: nos devolvia las filas que ya teniamos |
| 2 | `✅ ultimo intento hace 25min` | Un intento del **build anterior**, no de este |
| 3 | `hace 0h` | Podia ser "recien" o "hace 50 minutos" |
| 4 | `found > 0` en la tabla de pruebas | Una propiedad de **como se arma el pedido**, no una novedad |

Las cuatro me hicieron escribir conclusiones falsas en la memoria del proyecto.
La entrada de las 02:08 del 3 de septiembre esta **retractada** en
`deploy_history.md` por eso.

## 3. El defecto real, y por que costo tanto encontrarlo

El backfill pide a HL las filas de funding de un tramo mudo con
`funding_gaps`, que arma la ventana como `(prev, post)`, donde `prev` y `post`
son los timestamps de dos filas de funding **que ya estan guardadas**.

Consecuencia: **HL siempre devuelve al menos esas dos filas**. Siempre. Por
construccion del pedido.

El codigo usaba `found` (cuantas filas devolvio HL) como criterio de "este
tramo ya se probo y no hay nada". Como `found > 0` era practicamente
inevitable, pasaban tres cosas encadenadas:

1. Ningun tramo se marcaba nunca como probado-y-vacio.
2. Los mismos tramos se re-pedian en cada sync, para siempre.
3. El invariante nunca podia degradar los 27 ciclos a nota → **I5 rojo eterno**.

Y encima, un throttle viejo de `max_gaps=3` con un upsert sobre
`(wallet, a, b)` hacia que cada corrida re-probara **los mismos tres primeros
tramos** de cada wallet. Por eso dos `/diagnostico` separados por siete minutos
daban numeros byte-identicos: el sync corria y no hacia absolutamente nada,
una y otra vez.

**La medicion honesta ya existia y se estaba tirando a la basura.**
`_store_funding` devolvia cuantas filas **entraron a la base**, y el llamador
descartaba ese valor. Ahora se guarda como `nuevas` y es el criterio.

Detalle que importa: `nuevas` es exacto para este uso, no una aproximacion.
Como la ventana esta acotada por filas conocidas, toda fila nueva cae
necesariamente adentro del silencio.

## 4. Las seis correcciones

**R-FUNDING-HUECO** — construir el backfill: pedirle de nuevo a HL los tramos
mudos y anotar cada intento en una tabla `ledger_funding_probe`.

**R-FUNDING-LECTURA** — el arreglo anterior se mando **sin ninguna forma de
leer si se ejecuto**. Tres estados distintos ("no corrio", "corrio y fallo",
"corrio y no encontro nada") se veian identicos desde afuera y llevaban a
arreglos opuestos. Se agrego el bloque de reparacion al panel, con el error
persistido hasta ahi en vez de morir en el log de Railway — y limpiandose solo
cuando deja de pasar, porque una cruz que no se puede cerrar entrena a ignorar
el panel entero.

**R-429-TECHO** — no reintentar cuando el `Retry-After` supera el techo.

**R-FUNDING-NOVEDAD** — el nucleo. Separar el **eco** (`found`) de la
**novedad** (`nuevas`), en el criterio de exclusion, en el invariante y en el
panel. Decision clave: las 14 pruebas viejas quedaron en `nuevas = -1` =
**NO MEDIDO**, que no es `0` = "no trajo nada". Migrarlas a 0 habria puesto 27
ciclos en verde al instante sin haber tomado una sola medicion — el panel
volviendose verde por un default de columna.

**Re-dimensionado del throttle** — estaba puesto sobre la variable equivocada.
El rate limit de HL se paga por **pagina** (peso 20 contra 1200/min por IP
compartida), no por tramo: un hueco de 24h entra en una pagina, uno de 6 meses
necesita doce. Ademas `_fetch_funding_window` solo pausaba **entre paginas de
una misma ventana**, asi que N tramos de una pagina salian disparados sin
respiro. Subir el tope sin agregar la pausa entre tramos habria provocado
exactamente el 429 que el throttle existe para evitar. Y el "proximo sync" son
**seis horas**, no un momento: con tope 3 los 33 tramos tardaban tres dias.

**R-FUNDING-BUILD** — el panel presentaba la corrida de OTRO build como propia.
Un timestamp solo no dice **que codigo lo escribio**. Ahora `sync_all` graba el
commit corto junto a la marca y el panel compara los dos lados.

## 5. El veredicto (03:06 UTC, commit `9d38bd7`)

```
✅ ultimo intento hace 14min · 0 hueco(s) pendiente(s)
pruebas 33 (sin novedad 33) · filas NUEVAS 0 (eco HL 1061)
```

Como se lee, linea por linea:

- **`✅` sin la advertencia de build anterior** → el intento lo hizo *este*
  build. Eso es lo que R-FUNDING-BUILD vino a poder decir.
- **`0 huecos pendientes`** → los 33 tramos se probaron todos. No quedo ninguno
  cortado por presupuesto.
- **`sin novedad 33`, y ningun `sin medir`** → los 33 estan **medidos**, no
  supuestos. Las 14 viejas se re-probaron y salieron del estado `-1`.
- **`eco HL 1061`** → el transporte funciona. Un cero ahi habria significado
  pedido roto, que es un diagnostico completamente distinto.
- **`filas NUEVAS 0`** → de esas 1061 filas, **ninguna** era nueva.

**Conclusion: el dato no existe del lado de HL.** No hay resync que lo traiga.
La violacion I5 bajo a nota informativa y el money path quedo en **0
violaciones**, sin haber tocado ni un numero del fondo y sin haber apagado el
chequeo.

Esa distincion es todo el punto del caso: no se arreglo apagando la alarma, se
arreglo **midiendo** lo que la alarma no podia medir.

## 6. Lo que queda abierto

**Los 7 subsistemas en `ultimo ok nunca`.** Verificado, y **no es un defecto**:
los siete decoradores `@health_registry.tracked` llegaron en el commit `bde59e9`
del 2026-09-02 19:55 UTC, o sea hace ~7 horas. Los siete son jobs de baja
frecuencia (backup diario, gmail, reconciliacion, atribucion) que todavia no
volvieron a correr desde que se instrumentaron. El backup que el panel muestra
"hace 23h" corrio **antes** de que el decorador existiera. El propio panel lo
dice bien —"sin ningun exito registrado **aun**"— y no lo cuenta como problema.

**Prediccion falsable para la proxima ronda:** el backup diario dispara cerca
de las 04:00 UTC. Si en el `/diagnostico` posterior a esa hora "Backup sqlite"
**sigue** diciendo `nunca`, entonces si hay un problema de cableado y hay que
mirar por que `run_backup` no marca. Si pasa a verde, el registro esta bien y
los otros seis se van a ir llenando solos a medida que corran.

**Feeds:** 4 muertos (`artemis_lite`, `dune_hl`, `hyperevmscan`, `l2beat`) y
`capitol_trades` rancio hace 2157 horas — o sea ~90 dias, que es "muerto" con
otro nombre. Pendiente de otra ronda.

## 7. Como se verifica el trabajo en este repo

Esto no es opcional y es la razon por la que el caso cerro:

- **Mutacion, siempre.** Cada test tiene que **fallar** contra la conducta que
  prohibe. El arnes vive en `/tmp/mut3.py`: rompe la produccion a proposito, 20
  veces, y exige que la suite se caiga cada vez. Ultimo resultado **20/20**.
  Una vez dio 14/15 y la vacua era un test que parecia perfecto: cubria una
  rama inalcanzable, porque `_conn()` migra el esquema en cada conexion. Se
  arreglo con un unit test sobre un `sqlite3.connect` pelado.
- **Guarda de degradacion silenciosa** (`tests/test_silent_degradation_guard.py`).
  Cada `except` nuevo del money path tiene que estar instrumentado o **aceptado
  por escrito con su razon**. En esta ronda detecto sola mi `_commit_actual` y
  me obligo a justificarlo. Funciona. No agregar entradas al allowlist sin
  pensar: el punto es la obligacion de pensarlo.
- **`tools/bug_class_scan.py`** — TOTAL 7 (C1 0 · C2 6 · C3 0 · C4 1). Si sube,
  algo se agrego.
- **Suite completa:** 1532 tests, ~198s. El Bash necesita `timeout: 600000` o
  devuelve 143 y parece un fallo cuando es el timeout de la herramienta.
- **Comando:** `pytest --asyncio-mode=auto -p no:randomly --timeout=120
  --timeout-method=thread` (el `--asyncio-mode=auto` es obligatorio).
- **Python 3.10.** Nada de barras invertidas adentro de la parte de expresion
  de un f-string: es `SyntaxError`, y me tumbo 9 tests en esta misma ronda.

## 8. Si te llevas una sola cosa

Cuando un panel muestre un numero grande y tranquilizador, preguntate **quien
lo produjo y contra que**. `564` parecia reparacion y era el pedido
devolviendose a si mismo. `✅ hace 25min` parecia confirmacion y era otro build.

Y cuando mandes un arreglo, mandalo con **la forma de leer si se ejecuto**. Yo
mande uno sin eso y perdi una ronda entera sin poder distinguir entre tres
causas opuestas. El arreglo y su readout son el mismo entregable.
