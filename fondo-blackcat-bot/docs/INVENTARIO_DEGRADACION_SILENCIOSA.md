# R-BOT-DEFINITIVE — Inventario completo de degradacion silenciosa

Generado por `tools/silent_degradation_scan.py`. Este archivo es la Fase 1.1 del
mandato: TODO handler o fallback que convierte una falla en un valor plausible,
con archivo, linea, que se traga y con que lo reemplaza.

El guardian `tests/test_silent_degradation_guard.py` lo mantiene vivo: si aparece
un swallow nuevo en el money path sin instrumentar ni aceptar por escrito, la
suite se pone en rojo. Testear el comportamiento no alcanzaba para las siete
fallas que motivaron la ronda, porque ninguna levantaba; hay que testear la
FORMA del codigo, porque la forma era el bug.

- swallows totales en el repo: **666**
- en el camino del dinero: **112**
  - instrumentados (declaran la degradacion): **89**
  - aceptados por escrito: **23**
  - **sin cubrir: 0**
- cosmeticos, fuera del money path: **554**

## A. Camino del dinero — clase (a): pueden corromper un numero o una decision


### `auto/capital_calc.py`

| linea | funcion | atrapa | devuelve/hace | severidad | estado |
|---|---|---|---|---|---|
| 258 | `_get()` | `(TypeError, ValueError)` | `return 0.0` | low | aceptado |

### `auto/fund_state_v2.py`

| linea | funcion | atrapa | devuelve/hace | severidad | estado |
|---|---|---|---|---|---|
| 124 | `_registered_wallets()` | `Exception` | `return {}` | critical | instrumentado |
| 482 | `build_authoritative_state_block()` | `Exception` | `return ''` | critical | instrumentado |

### `auto/price_cache.py`

| linea | funcion | atrapa | devuelve/hace | severidad | estado |
|---|---|---|---|---|---|
| 57 | `read()` | `Exception` | `return {}` | critical | instrumentado |
| 88 | `record()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |

### `modules/cost_tracker.py`

| linea | funcion | atrapa | devuelve/hace | severidad | estado |
|---|---|---|---|---|---|
| 119 | `log_llm_call()` | `sqlite3.Error` | `pass (sigue con datos parciales)` | high | instrumentado |
| 144 | `_aggregate()` | `sqlite3.Error` | `pass (sigue con datos parciales)` | high | instrumentado |

### `modules/fund_state_reconciler.py`

| linea | funcion | atrapa | devuelve/hace | severidad | estado |
|---|---|---|---|---|---|
| 67 | `_load_state()` | `Exception` | `return {}` | critical | instrumentado |
| 76 | `_save_state()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |
| 88 | `_wallet_matches_basket_label()` | `Exception` | `return False` | critical | instrumentado |
| 107 | `reconcile_fund_state()` | `Exception` | `return []` | critical | instrumentado |
| 429 | `scheduled_reconcile()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |

### `modules/funding_tracker.py`

| linea | funcion | atrapa | devuelve/hace | severidad | estado |
|---|---|---|---|---|---|
| 254 | `fetch_funding_rates()` | `Exception` | `return {}` | critical | instrumentado |
| 266 | `funding_8h_bps()` | `(TypeError, ValueError)` | `return None` | low | aceptado |
| 415 | `build_funding_block()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |
| 501 | `build_funding_llm_block()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |

### `modules/hl_borrow_lend.py`

| linea | funcion | atrapa | devuelve/hace | severidad | estado |
|---|---|---|---|---|---|
| 59 | `_post()` | `ImportError` | `pass (sigue con datos parciales)` | low | instrumentado |
| 79 | `_safe_float()` | `(TypeError, ValueError)` | `return None` | low | aceptado |

### `modules/hl_prices.py`

| linea | funcion | atrapa | devuelve/hace | severidad | estado |
|---|---|---|---|---|---|
| 65 | `_post()` | `ImportError` | `pass (sigue con datos parciales)` | low | instrumentado |
| 85 | `_safe_float()` | `(TypeError, ValueError)` | `return None` | low | aceptado |

### `modules/hype_acquisition.py`

| linea | funcion | atrapa | devuelve/hace | severidad | estado |
|---|---|---|---|---|---|
| 112 | `_ms_to_date()` | `(TypeError, ValueError, OSError, OverflowError)` | `return None` | critical | aceptado |
| 137 | `_live_hype_balance()` | `Exception` | `return None` | critical | instrumentado |
| 147 | `_live_hype_balance()` | `(TypeError, ValueError)` | `return None` | low | aceptado |
| 164 | `_resolve_spot_map()` | `Exception` | `return {}` | critical | instrumentado |
| 228 | `_fetch_fills()` | `(TypeError, ValueError)` | `pass (sigue con datos parciales)` | low | aceptado |
| 237 | `_fetch_fills()` | `Exception` | `return None` | critical | instrumentado |
| 465 | `set_ppc_override()` | `Exception` | `return False` | critical | instrumentado |
| 480 | `clear_ppc_override()` | `Exception` | `return False` | critical | instrumentado |
| 501 | `get_ppc_override()` | `Exception` | `return None` | critical | instrumentado |

### `modules/integrity_reconcile.py`

| linea | funcion | atrapa | devuelve/hace | severidad | estado |
|---|---|---|---|---|---|
| 120 | `normalize_excerpt()` | `Exception` | `return ''` | critical | instrumentado |
| 299 | `_conn()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |
| 319 | `_dismiss()` | `Exception` | `return False` | critical | instrumentado |
| 334 | `_touch()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |
| 419 | `reconcile_persisted_flags()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |

### `modules/intel_memory.py`

| linea | funcion | atrapa | devuelve/hace | severidad | estado |
|---|---|---|---|---|---|
| 178 | `record_x_api_call()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |
| 203 | `last_successful_x_call_ts()` | `Exception` | `return None` | critical | instrumentado |
| 220 | `count_x_calls_since()` | `Exception` | `return 0` | critical | instrumentado |
| 242 | `official_x_cost_since()` | `Exception` | `return 0.0` | critical | instrumentado |
| 272 | `x_api_cost_projection()` | `Exception` | `return {'cost_7d': 0.0, 'calls_7d': 0, 'tweets_7d': 0, 'daily_avg_u (todo ceros/vacios)` | critical | instrumentado |
| 294 | `count_x_calls_today_calendar()` | `Exception` | `return 0` | critical | instrumentado |
| 316 | `count_x_calls_today_live_only()` | `Exception` | `return 0` | critical | instrumentado |
| 354 | `should_send_75pct_alert()` | `Exception` | `return False` | critical | instrumentado |
| 387 | `save_x_timeline_payload()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |
| 407 | `load_x_timeline_payload()` | `Exception` | `return (None, None) (todo ceros/vacios)` | critical | instrumentado |
| 440 | `x_cost_breakdown_by_caller()` | `Exception` | `return []` | critical | instrumentado |
| 468 | `x_cache_hit_rate()` | `Exception` | `return {'total_calls': 0, 'successful': 0, 'calls_per_day': 0.0} (todo ceros/vacios)` | critical | instrumentado |
| 650 | `track_llm_usage()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |
| 693 | `get_usage_stats()` | `Exception` | `return []` | critical | instrumentado |
| 739 | `save_unlock_events()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |
| 779 | `get_cached_unlocks()` | `Exception` | `return []` | critical | instrumentado |

### `modules/market.py`

| linea | funcion | atrapa | devuelve/hace | severidad | estado |
|---|---|---|---|---|---|
| 90 | `coingecko_prices()` | `Exception` | `return {}` | critical | instrumentado |
| 114 | `coingecko_global()` | `Exception` | `return {}` | critical | instrumentado |
| 148 | `fear_greed()` | `Exception` | `return {}` | critical | instrumentado |
| 164 | `_coinglass_get()` | `Exception` | `return None` | critical | instrumentado |
| 236 | `defillama_top_fees()` | `Exception` | `return []` | critical | instrumentado |
| 263 | `defillama_stablecoin_supply()` | `Exception` | `return {}` | critical | instrumentado |

### `modules/performance_attribution.py`

| linea | funcion | atrapa | devuelve/hace | severidad | estado |
|---|---|---|---|---|---|
| 61 | `_get_default_beta()` | `ValueError` | `pass (sigue con datos parciales)` | low | aceptado |
| 128 | `persist()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |
| 184 | `recent_attributions()` | `Exception` | `return []` | critical | instrumentado |

### `modules/pm_context.py`

| linea | funcion | atrapa | devuelve/hace | severidad | estado |
|---|---|---|---|---|---|
| 67 | `_f()` | `(TypeError, ValueError)` | `return 0.0` | low | aceptado |
| 85 | `select_primary_pm_state()` | `Exception` | `return None` | critical | instrumentado |
| 121 | `select_primary_pm_state()` | `Exception` | `return None` | critical | instrumentado |
| 339 | `build_pm_llm_block_from_wallets()` | `Exception` | `return ''` | critical | instrumentado |

### `modules/portfolio.py`

| linea | funcion | atrapa | devuelve/hace | severidad | estado |
|---|---|---|---|---|---|
| 50 | `_save_wallet_cache()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |
| 105 | `frontend_open_orders()` | `Exception` | `return []` | critical | instrumentado |
| 130 | `fetch_all_open_orders()` | `Exception` | `return []` | critical | instrumentado |
| 180 | `fetch_fills_since()` | `Exception` | `return []` | critical | instrumentado |
| 288 | `_f()` | `(TypeError, ValueError)` | `return 0.0` | low | aceptado |
| 385 | `_fetch_spot()` | `Exception` | `return []` | critical | instrumentado |
| 407 | `_to_float()` | `(TypeError, ValueError)` | `return None` | low | aceptado |
| 607 | `get_spot_price()` | `Exception` | `return None` | critical | instrumentado |
| 642 | `fetch_recent_fills()` | `Exception` | `return []` | critical | instrumentado |

### `modules/portfolio_margin.py`

| linea | funcion | atrapa | devuelve/hace | severidad | estado |
|---|---|---|---|---|---|
| 166 | `_f()` | `(TypeError, ValueError)` | `return 0.0` | low | aceptado |
| 186 | `_clean_ltv()` | `(TypeError, ValueError)` | `pass (sigue con datos parciales)` | low | aceptado |
| 236 | `_liq_threshold_for_ltv()` | `(TypeError, ValueError)` | `pass (sigue con datos parciales)` | low | aceptado |
| 243 | `_liq_threshold_for_ltv()` | `(TypeError, ValueError)` | `pass (sigue con datos parciales)` | low | aceptado |
| 510 | `compute_pm_state()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |
| 824 | `format_pm_state_telegram()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |

### `modules/spot_index.py`

| linea | funcion | atrapa | devuelve/hace | severidad | estado |
|---|---|---|---|---|---|
| 111 | `refresh_spot_index_map()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |

### `modules/trade_ledger.py`

| linea | funcion | atrapa | devuelve/hace | severidad | estado |
|---|---|---|---|---|---|
| 499 | `_alert()` | `Exception` | `pass (sigue con datos parciales)` | high | aceptado |
| 516 | `_alert()` | `Exception` | `return False` | critical | aceptado |
| 852 | `derive_leverage()` | `(TypeError, ValueError)` | `return None` | low | aceptado |
| 875 | `resolve_leverage()` | `(TypeError, ValueError)` | `pass (sigue con datos parciales)` | low | aceptado |
| 1100 | `sync_all()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |
| 1123 | `sync_all()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |
| 1143 | `sync_all()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |
| 1373 | `run_close_alerts()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |

### `modules/vault_deposits.py`

| linea | funcion | atrapa | devuelve/hace | severidad | estado |
|---|---|---|---|---|---|
| 155 | `_post_user_vault_equities()` | `ImportError` | `pass (sigue con datos parciales)` | low | instrumentado |
| 182 | `_safe_float()` | `(TypeError, ValueError)` | `return 0.0` | low | aceptado |
| 215 | `_resolve_vault_name()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |
| 228 | `_fund_depositor_wallets()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |
| 235 | `_fund_depositor_wallets()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |
| 244 | `_safe_int()` | `(TypeError, ValueError)` | `return 0` | low | aceptado |
| 386 | `get_vault_deposits_total()` | `Exception` | `return 0.0` | critical | instrumentado |
| 411 | `_fmt_lockup()` | `(ValueError, OverflowError, OSError)` | `return ''` | critical | instrumentado |

### `modules/vault_history.py`

| linea | funcion | atrapa | devuelve/hace | severidad | estado |
|---|---|---|---|---|---|
| 73 | `_safe_float()` | `(TypeError, ValueError)` | `return 0.0` | low | aceptado |
| 120 | `record_vault_snapshot()` | `Exception` | `return 0` | critical | instrumentado |
| 151 | `get_previous_snapshot()` | `Exception` | `return None` | critical | instrumentado |
| 174 | `get_all_snapshots()` | `Exception` | `return []` | critical | instrumentado |
| 220 | `compute_max_drawdown()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |
| 334 | `format_vault_evolution_line()` | `Exception` | `return ''` | critical | instrumentado |
| 373 | `format_vault_evolution_block()` | `Exception` | `return ''` | critical | instrumentado |

### `modules/x_store.py`

| linea | funcion | atrapa | devuelve/hace | severidad | estado |
|---|---|---|---|---|---|
| 126 | `get_since_id()` | `Exception` | `return None` | critical | instrumentado |
| 142 | `set_since_id()` | `(TypeError, ValueError)` | `pass (sigue con datos parciales)` | low | aceptado |
| 153 | `set_since_id()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |
| 170 | `last_fetch_ts()` | `Exception` | `return None` | critical | instrumentado |
| 207 | `log_fetch()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |
| 221 | `recent_fetch_log()` | `Exception` | `return []` | critical | instrumentado |
| 269 | `upsert_tweets()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |
| 288 | `prune_old()` | `Exception` | `return 0` | critical | instrumentado |
| 320 | `get_window()` | `Exception` | `pass (sigue con datos parciales)` | high | instrumentado |
| 397 | `posts_fetched_since()` | `Exception` | `return 0` | critical | instrumentado |

## B. Razon escrita de cada aceptacion

El guardian exige un minimo de 60 caracteres por razon: si no se puede explicar
en una frase completa por que ese default no miente, el swallow no se acepta.

- **`auto/capital_calc.py::_get::(TypeError, ValueError)::return 0.0`**  
  Guarda de coercion. _get() lee un campo opcional del estado del fondo y devuelve 0.0 cuando no es numerico. El llamador suma estos valores y ya contempla campos ausentes; un 0.0 aca significa 'ese campo no estaba', que es la verdad.
- **`modules/funding_tracker.py::funding_8h_bps::(TypeError, ValueError)::return None`**  
  Guarda de coercion sobre el rate horario que publica HL. Devuelve None (no 0.0) precisamente para que el llamador distinga 'no se pudo leer' de 'funding cero'; el render imprime n/d en vez de un numero inventado.
- **`modules/hl_borrow_lend.py::_safe_float::(TypeError, ValueError)::return None`**  
  Guarda de coercion de un unico valor. Devuelve None, que el llamador propaga como 'sin dato' en vez de convertirlo en 0.
- **`modules/hl_prices.py::_safe_float::(TypeError, ValueError)::return None`**  
  Guarda de coercion de un unico valor, ademas rechaza NaN explicitamente (f == f). Devuelve None y el llamador omite el activo del mapa de precios en vez de valuarlo en cero.
- **`modules/hype_acquisition.py::_fetch_fills::(TypeError, ValueError)::pass`**  
  Salta UN fill cuyo px/sz no parsea y sigue con el resto. Relevante para el PPC, pero el conteo de unidades cubiertas se reporta explicitamente contra el balance (ver Fase 3.3), asi que un fill perdido se ve como cobertura incompleta, no como un PPC falsamente exacto.
- **`modules/hype_acquisition.py::_live_hype_balance::(TypeError, ValueError)::return None`**  
  Guarda de coercion sobre el balance spot de HYPE. Devuelve None, y el bloque de PPC ya trata None como 'balance no disponible' y lo dice; no lo convierte en 0 unidades.
- **`modules/hype_acquisition.py::_ms_to_date::(TypeError, ValueError, OSError, OverflowError)::return None`**  
  Formatea un epoch en ms a 'YYYY-MM-DD' solo para mostrar la ventana de fechas de los fills. Devolver None es un 'no se', no un numero plausible: el render lo imprime como 'fecha desconocida' y ninguna cuenta de plata lo toca. Los fills cuya fecha no se pudo leer se cuentan aparte en fills_sin_fecha y se anuncian en la linea del PPC, asi que la ventana nunca se presenta como completa cuando no lo esta.
- **`modules/performance_attribution.py::_get_default_beta::ValueError::pass`**  
  El beta por defecto viene de una env var; si no parsea se usa la constante del modulo. Es configuracion mal escrita, no un dato de mercado, y el valor usado queda expuesto en /diagnostico.
- **`modules/pm_context.py::_f::(TypeError, ValueError)::return 0.0`**  
  Guarda de coercion de un unico campo del contexto de portfolio margin. Los campos que SI mueven decisiones (equity, deuda, LTV) se validan aguas arriba y su ausencia degrada pm_state por via instrumentada.
- **`modules/portfolio.py::_f::(TypeError, ValueError)::return 0.0`**  
  Guarda de coercion de un unico campo de la respuesta de HL. El fallo de transporte de esa misma respuesta esta instrumentado aparte, asi que un 0.0 aca solo puede venir de un campo vacio.
- **`modules/portfolio.py::_to_float::(TypeError, ValueError)::return None`**  
  Guarda de coercion que devuelve None, no 0.0: el llamador distingue ausencia de cero.
- **`modules/portfolio_margin.py::_clean_ltv::(TypeError, ValueError)::pass`**  
  Descarta un LTV no parseable en vez de propagarlo. Un LTV basura es peor que un LTV ausente: alimentaria el calculo de umbral de liquidacion. Descartarlo es la decision correcta y el faltante se ve en el bloque de PM.
- **`modules/portfolio_margin.py::_f::(TypeError, ValueError)::return 0.0`**  
  Guarda de coercion de un unico campo. Mismo criterio que portfolio._f.
- **`modules/portfolio_margin.py::_liq_threshold_for_ltv::(TypeError, ValueError)::pass`**  
  Ignora una entrada mal formada de la tabla de umbrales y sigue buscando en el resto. Si ninguna entrada sirve, la funcion devuelve None y el llamador no imprime umbral, en vez de imprimir uno equivocado. Aparece dos veces (dos bucles de parseo) y el criterio es el mismo.
- **`modules/trade_ledger.py::_alert::Exception::pass`**  
  _alert() es el que MANDA los avisos. Instrumentarlo con health_registry crearia recursion cuando el fallo sea del propio envio, y ademas el llamador ya cuenta los envios fallidos y reporta el conteo. Esta en SKIP_FUNCS de tools/instrument_swallows.py por la misma razon.
- **`modules/trade_ledger.py::_alert::Exception::return False`**  
  Misma funcion, rama que devuelve False. El False es el valor de retorno documentado para 'no se pudo enviar' y el llamador lo suma a su contador de fallos, asi que la degradacion ya viaja por el valor de retorno.
- **`modules/trade_ledger.py::derive_leverage::(TypeError, ValueError)::return None`**  
  Devuelve None = 'no derivable', que es justo lo que resolve_leverage necesita para pasar al siguiente origen (live, y despues assumed). El origen elegido se persiste en leverage_source y se imprime, asi que el ROE derivado de un leverage assumed sale marcado con ~.
- **`modules/trade_ledger.py::resolve_leverage::(TypeError, ValueError)::pass`**  
  Descarta un candidato de leverage no numerico y prueba el siguiente en la cadena de precedencia. El origen final queda registrado en leverage_source.
- **`modules/vault_deposits.py::_safe_float::(TypeError, ValueError)::return 0.0`**  
  Guarda de coercion de un campo de equity de vault. El fallo de transporte y el 200 con shape inesperado — que son los que SI podian borrar equity del NAV — ahora levantan en _as_equity_list(); lo que queda aca es solo un campo vacio.
- **`modules/vault_deposits.py::_safe_int::(TypeError, ValueError)::return 0`**  
  Guarda de coercion de un contador (numero de vaults). Mismo criterio.
- **`modules/vault_history.py::_safe_float::(TypeError, ValueError)::return 0.0`**  
  Guarda de coercion de un punto de la serie historica de vault. Los fallos de lectura de la serie completa estan instrumentados aparte.
- **`modules/x_store.py::set_since_id::(TypeError, ValueError)::pass`**  
  El since_id de X viene como string; si no es un entero se ignora y el cursor queda donde estaba. Eso produce refetch (costo), nunca perdida de tweets. El fallo de ESCRITURA del cursor, que si tiene consecuencias, esta instrumentado en el handler ancho de la misma funcion.

## C. Cosmeticos — clase (b): no tocan ningun numero publicable

Se listan para que la clasificacion quede auditable, no porque haya que actuar.


### `auto/boot_announcement_v2.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 38 | `announce_boot()` | `Exception` | `pass (sigue con datos parciales)` |
| 49 | `announce_boot()` | `Exception` | `return (implicito None)` |

### `auto/boot_dedup.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 80 | `last_announcement()` | `Exception` | `return None` |
| 136 | `mark_announced()` | `Exception` | `pass (sigue con datos parciales)` |
| 146 | `_reset_for_tests()` | `Exception` | `pass (sigue con datos parciales)` |
| 167 | `_backdate_for_tests()` | `Exception` | `pass (sigue con datos parciales)` |

### `auto/catalyst_alert_gate.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 138 | `_coerce_event_dt()` | `Exception` | `return None` |
| 183 | `was_post_sent()` | `Exception` | `return False` |
| 203 | `mark_post_sent()` | `Exception` | `pass (sigue con datos parciales)` |
| 212 | `_reset_for_tests()` | `Exception` | `pass (sigue con datos parciales)` |
| 224 | `status_summary()` | `Exception` | `pass (sigue con datos parciales)` |

### `auto/freshness.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 35 | `_parse_iso()` | `(TypeError, ValueError)` | `return None` |
| 52 | `age_seconds_of()` | `(TypeError, ValueError)` | `pass (sigue con datos parciales)` |

### `auto/hf_alert_gate.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 128 | `_read_state()` | `Exception` | `return None` |
| 274 | `record_emit()` | `Exception` | `pass (sigue con datos parciales)` |
| 286 | `clear_wallet()` | `Exception` | `pass (sigue con datos parciales)` |
| 295 | `_reset_for_tests()` | `Exception` | `pass (sigue con datos parciales)` |
| 315 | `status_summary()` | `Exception` | `pass (sigue con datos parciales)` |

### `auto/silent_mode.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 95 | `_write()` | `Exception` | `pass (sigue con datos parciales)` |
| 148 | `_reset_for_tests()` | `Exception` | `pass (sigue con datos parciales)` |

### `boot_announcement.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 34 | `_coerce_event_dt()` | `Exception` | `return None` |
| 100 | `announce_boot()` | `Exception` | `return (implicito None)` |
| 114 | `announce_boot()` | `Exception` | `pass (sigue con datos parciales)` |

### `bot.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 397 | `cmd_reporte()` | `Exception` | `pass (sigue con datos parciales)` |
| 414 | `cmd_reporte()` | `Exception` | `pass (sigue con datos parciales)` |
| 501 | `cmd_reporte()` | `Exception` | `pass (sigue con datos parciales)` |
| 503 | `cmd_reporte()` | `Exception` | `pass (sigue con datos parciales)` |
| 539 | `cmd_reporte()` | `Exception` | `pass (sigue con datos parciales)` |
| 557 | `cmd_reporte()` | `Exception` | `pass (sigue con datos parciales)` |
| 575 | `cmd_reporte()` | `Exception` | `pass (sigue con datos parciales)` |
| 606 | `cmd_reporte()` | `Exception` | `pass (sigue con datos parciales)` |
| 620 | `cmd_reporte()` | `Exception` | `pass (sigue con datos parciales)` |
| 631 | `cmd_reporte()` | `Exception` | `pass (sigue con datos parciales)` |
| 648 | `cmd_reporte()` | `Exception` | `pass (sigue con datos parciales)` |
| 681 | `cmd_reporte()` | `Exception` | `pass (sigue con datos parciales)` |
| 683 | `cmd_reporte()` | `Exception` | `pass (sigue con datos parciales)` |
| 708 | `cmd_reporte()` | `Exception` | `pass (sigue con datos parciales)` |
| 728 | `cmd_reporte()` | `Exception` | `pass (sigue con datos parciales)` |
| 878 | `cmd_cryexc()` | `Exception` | `pass (sigue con datos parciales)` |
| 984 | `cmd_pnl()` | `ValueError` | `pass (sigue con datos parciales)` |
| 1013 | `cmd_cierres()` | `Exception` | `pass (sigue con datos parciales)` |
| 1032 | `cmd_log()` | `ValueError` | `pass (sigue con datos parciales)` |
| 1220 | `cmd_add_event()` | `Exception` | `pass (sigue con datos parciales)` |
| 1293 | `cmd_export()` | `ValueError` | `return (implicito None)` |
| 1296 | `cmd_export()` | `ExportError` | `return (implicito None)` |
| 1308 | `cmd_export()` | `Exception` | `return (implicito None)` |
| 1317 | `cmd_export()` | `Exception` | `pass (sigue con datos parciales)` |
| 1358 | `cmd_brief()` | `Exception` | `pass (sigue con datos parciales)` |
| 1486 | `cmd_lmec_status()` | `Exception` | `pass (sigue con datos parciales)` |
| 1582 | `cmd_setlmec()` | `ValueError` | `pass (sigue con datos parciales)` |
| 1636 | `cmd_setppc()` | `(TypeError, ValueError)` | `return (implicito None)` |
| 2047 | `cmd_intel30_full()` | `Exception` | `pass (sigue con datos parciales)` |
| 2057 | `cmd_intel30_full()` | `Exception` | `pass (sigue con datos parciales)` |
| 2101 | `_alert_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 2111 | `_intel_processor_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 2117 | `_intel_processor_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 2130 | `_backup_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 2140 | `_weekly_cleanup_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 2160 | `cmd_variationalfunding()` | `_var.VariationalError` | `return (implicito None)` |
| 2165 | `cmd_variationalfunding()` | `Exception` | `return (implicito None)` |
| 2236 | `cmd_variationalalerts()` | `_var.VariationalError` | `return (implicito None)` |
| 2282 | `_variational_alerts_job()` | `Exception` | `return (implicito None)` |
| 2290 | `_variational_alerts_job()` | `Exception` | `return (implicito None)` |
| 2316 | `_variational_alerts_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 2397 | `_pm_monitor_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 2424 | `_farmdump_block()` | `Exception` | `return ''` |
| 2457 | `cmd_variationalcheck()` | `_var.VariationalError` | `return (implicito None)` |
| 2462 | `cmd_variationalcheck()` | `Exception` | `return (implicito None)` |
| 2562 | `cmd_pm()` | `Exception` | `pass (sigue con datos parciales)` |
| 2586 | `cmd_vaults()` | `Exception` | `pass (sigue con datos parciales)` |
| 2590 | `cmd_vaults()` | `Exception` | `pass (sigue con datos parciales)` |
| 2638 | `cmd_unlockcheck()` | `Exception` | `pass (sigue con datos parciales)` |
| 2675 | `cmd_check()` | `Exception` | `pass (sigue con datos parciales)` |
| 2717 | `cmd_telemetry()` | `Exception` | `pass (sigue con datos parciales)` |
| 2748 | `cmd_signals()` | `Exception` | `pass (sigue con datos parciales)` |
| 2773 | `cmd_halts()` | `Exception` | `pass (sigue con datos parciales)` |
| 2808 | `cmd_haltclear()` | `Exception` | `pass (sigue con datos parciales)` |
| 2879 | `_unlock_monitor_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 2898 | `_unlock_monitor_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 2911 | `_unlock_monitor_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 2913 | `_unlock_monitor_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 2934 | `_macro_calendar_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 2942 | `_macro_calendar_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 2952 | `_reconcile_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 2962 | `_kill_triggers_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 2973 | `_go_alerts_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 2989 | `_weekly_summary_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 2993 | `_weekly_summary_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 3011 | `_pat_expiry_job()` | `Exception` | `return (implicito None)` |
| 3027 | `_pat_expiry_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 3050 | `_selftest_cron_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 3088 | `_autodiagnostico_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 3117 | `_backup_volume_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 3128 | `_backup_volume_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 3130 | `_backup_volume_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 3156 | `_send()` | `Exception` | `pass (sigue con datos parciales)` |
| 3163 | `_ledger_sync_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 3181 | `_cost_alert_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 3183 | `_cost_alert_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 3205 | `_lmec_weekly_recheck_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 3214 | `_lmec_weekly_recheck_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 3222 | `_lmec_weekly_recheck_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 3236 | `_lmec_weekly_recheck_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 3255 | `_lmec_counter_refresh_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 3265 | `_lmec_counter_refresh_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 3274 | `_lmec_counter_refresh_job()` | `Exception` | `return (implicito None)` |
| 3277 | `_lmec_counter_refresh_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 3291 | `_portfolio_snapshot_refresh_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 3330 | `_risk_validator_should_alert()` | `Exception` | `pass (sigue con datos parciales)` |
| 3337 | `_risk_validator_should_alert()` | `Exception` | `pass (sigue con datos parciales)` |
| 3374 | `_risk_validator_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 3387 | `_cryexc_monitor_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 3407 | `_cryexc_monitor_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 3409 | `_cryexc_monitor_job()` | `Exception` | `pass (sigue con datos parciales)` |
| 3437 | `_runner()` | `Exception` | `pass (sigue con datos parciales)` |
| 3441 | `_runner()` | `Exception` | `pass (sigue con datos parciales)` |
| 3461 | `sync_commands_with_telegram()` | `Exception` | `return 0` |
| 3488 | `post_init()` | `Exception` | `pass (sigue con datos parciales)` |
| 3494 | `post_init()` | `Exception` | `pass (sigue con datos parciales)` |
| 3503 | `post_init()` | `Exception` | `pass (sigue con datos parciales)` |
| 3510 | `post_init()` | `Exception` | `pass (sigue con datos parciales)` |
| 3736 | `post_init()` | `Exception` | `pass (sigue con datos parciales)` |
| 4051 | `post_init()` | `Exception` | `pass (sigue con datos parciales)` |
| 4063 | `post_init()` | `Exception` | `pass (sigue con datos parciales)` |
| 4073 | `post_shutdown()` | `Exception` | `pass (sigue con datos parciales)` |
| 4247 | `main()` | `Exception` | `pass (sigue con datos parciales)` |

### `calendar_drift_guard.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 36 | `_resolve_db_path()` | `Exception` | `return None` |
| 40 | `_resolve_db_path()` | `Exception` | `return None` |
| 85 | `mark_past_events_at_boot()` | `sqlite3.Error` | `return 0` |

### `calendar_refresh.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 35 | `_load_calendar_from_source()` | `Exception` | `return []` |
| 54 | `_load_calendar_from_source()` | `Exception` | `return []` |

### `config.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 207 | `_load_vault_deposits()` | `Exception` | `return []` |

### `modules/aipear_auto_prompt.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 61 | `_capital_summary()` | `Exception` | `pass (sigue con datos parciales)` |
| 85 | `_recent_intel_themes()` | `Exception` | `return []` |
| 160 | `maybe_send_post_basket_prompt()` | `Exception` | `return False` |

### `modules/alert_dedup.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 144 | `clear()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/alerts.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 44 | `_load_state()` | `Exception` | `return {}` |
| 52 | `_save_state()` | `Exception` | `pass (sigue con datos parciales)` |
| 149 | `run_alert_cycle()` | `Exception` | `pass (sigue con datos parciales)` |
| 156 | `run_alert_cycle()` | `Exception` | `pass (sigue con datos parciales)` |
| 161 | `run_alert_cycle()` | `Exception` | `pass (sigue con datos parciales)` |
| 172 | `run_alert_cycle()` | `Exception` | `pass (sigue con datos parciales)` |
| 197 | `margin_stress_ratio()` | `(TypeError, ValueError)` | `return None` |

### `modules/alerts_margin.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 85 | `_get_state()` | `Exception` | `return (None, None, None) (todo ceros/vacios)` |
| 103 | `_set_state()` | `Exception` | `pass (sigue con datos parciales)` |
| 111 | `margin_used_band()` | `(TypeError, ValueError)` | `return 0` |
| 243 | `evaluate_iso_only_transition()` | `Exception` | `return False` |
| 313 | `evaluate_margin_used()` | `Exception` | `return (False, 0) (todo ceros/vacios)` |
| 325 | `_hf_band()` | `(TypeError, ValueError)` | `return 0` |
| 380 | `evaluate_pm_hf()` | `(TypeError, ValueError)` | `return (False, '') (todo ceros/vacios)` |
| 425 | `evaluate_pm_hf()` | `Exception` | `return (False, '') (todo ceros/vacios)` |
| 453 | `evaluate_position_liq_distance()` | `(TypeError, ValueError)` | `return (False, '') (todo ceros/vacios)` |
| 475 | `evaluate_position_liq_distance()` | `Exception` | `return (False, '') (todo ceros/vacios)` |
| 516 | `run_margin_alerts()` | `Exception` | `return 0` |
| 531 | `run_margin_alerts()` | `Exception` | `pass (sigue con datos parciales)` |
| 533 | `run_margin_alerts()` | `Exception` | `pass (sigue con datos parciales)` |
| 565 | `run_margin_alerts()` | `Exception` | `pass (sigue con datos parciales)` |
| 567 | `run_margin_alerts()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/analysis.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 41 | `_lmec_state_block()` | `Exception` | `return ''` |
| 101 | `_load_thesis()` | `Exception` | `return {}` |
| 156 | `_save_thesis()` | `Exception` | `pass (sigue con datos parciales)` |
| 261 | `_save_tesis_latest()` | `Exception` | `pass (sigue con datos parciales)` |
| 275 | `load_tesis_latest()` | `Exception` | `return (None, None) (todo ceros/vacios)` |
| 292 | `_save_last_analysis()` | `Exception` | `pass (sigue con datos parciales)` |
| 303 | `_load_last_analysis()` | `Exception` | `return None` |
| 443 | `_update_thesis_state()` | `json.JSONDecodeError` | `return None` |
| 446 | `_update_thesis_state()` | `LLMError` | `return None` |
| 449 | `_update_thesis_state()` | `Exception` | `return None` |
| 534 | `generate_report()` | `Exception` | `pass (sigue con datos parciales)` |
| 584 | `generate_report()` | `Exception` | `pass (sigue con datos parciales)` |
| 595 | `generate_report()` | `Exception` | `pass (sigue con datos parciales)` |
| 745 | `_build_degraded_report()` | `Exception` | `pass (sigue con datos parciales)` |
| 786 | `generate_thesis_check()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/backup_verify.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 283 | `verify_latest()` | `OSError` | `pass (sigue con datos parciales)` |
| 298 | `last_verification()` | `(OSError, json.JSONDecodeError)` | `return None` |

### `modules/backup_volume.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 61 | `_prune_old_backups()` | `OSError` | `pass (sigue con datos parciales)` |
| 106 | `_push_to_github()` | `OSError` | `pass (sigue con datos parciales)` |
| 226 | `run_backup()` | `OSError` | `pass (sigue con datos parciales)` |
| 237 | `get_last_backup_status()` | `(OSError, json.JSONDecodeError)` | `return None` |

### `modules/basket_killer.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 59 | `_load_state()` | `Exception` | `return {}` |
| 67 | `_save_state()` | `Exception` | `pass (sigue con datos parciales)` |
| 79 | `_btc_history_load()` | `Exception` | `return []` |
| 97 | `_btc_history_save()` | `Exception` | `pass (sigue con datos parciales)` |
| 193 | `_evaluate_pm_hf()` | `Exception` | `pass (sigue con datos parciales)` |
| 299 | `evaluate_all()` | `Exception` | `pass (sigue con datos parciales)` |
| 354 | `scheduled_check()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/bounce_tech.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 84 | `_load_bt_state()` | `Exception` | `return {}` |
| 94 | `_save_bt_state()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/btc_weekly_indicators.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 192 | `_f()` | `(TypeError, ValueError)` | `return None` |
| 228 | `_fetch_binance_weekly()` | `Exception` | `return None` |
| 241 | `_hl_post()` | `ImportError` | `pass (sigue con datos parciales)` |
| 282 | `_fetch_hl_weekly()` | `Exception` | `return None` |
| 331 | `refresh_and_persist()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/catalyst_scoring.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 100 | `_active_position_keys()` | `Exception` | `pass (sigue con datos parciales)` |
| 117 | `_active_position_keys()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/catalysts.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 157 | `add_catalyst()` | `Exception` | `return None` |
| 171 | `delete_catalyst()` | `Exception` | `return False` |
| 215 | `list_catalysts()` | `Exception` | `return []` |
| 262 | `_resolve_release_ids()` | `(KeyError, TypeError, ValueError)` | `pass (sigue con datos parciales)` |
| 264 | `_resolve_release_ids()` | `Exception` | `pass (sigue con datos parciales)` |
| 311 | `refresh_fred_catalysts()` | `Exception` | `return 0` |
| 334 | `seed_catalysts()` | `Exception` | `pass (sigue con datos parciales)` |
| 344 | `refresh_catalysts()` | `Exception` | `pass (sigue con datos parciales)` |
| 348 | `refresh_catalysts()` | `Exception` | `pass (sigue con datos parciales)` |
| 379 | `next_catalyst_candidates()` | `Exception` | `return []` |
| 412 | `build_llm_catalyst_block()` | `Exception` | `return ''` |

### `modules/coinglass.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 40 | `_get()` | `Exception` | `return {}` |

### `modules/compounding_detector.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 117 | `_extract_account_value()` | `(TypeError, ValueError)` | `return 0.0` |

### `modules/cryexc_intel.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 136 | `_persist_snapshot()` | `Exception` | `pass (sigue con datos parciales)` |
| 157 | `_load_latest_snapshot()` | `Exception` | `return None` |
| 205 | `mark_event_seen()` | `Exception` | `pass (sigue con datos parciales)` |
| 247 | `_fetch_binance_funding()` | `Exception` | `return []` |
| 283 | `_fetch_binance_movers()` | `Exception` | `return []` |
| 323 | `_fetch_hl_meta()` | `Exception` | `return {}` |
| 497 | `fetch_cryexc()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/dashboard.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 473 | `_render_html()` | `(TypeError, ValueError)` | `pass (sigue con datos parciales)` |
| 478 | `_render_html()` | `(TypeError, ValueError)` | `pass (sigue con datos parciales)` |
| 483 | `_render_html()` | `(TypeError, ValueError)` | `pass (sigue con datos parciales)` |
| 873 | `_build_state()` | `Exception` | `pass (sigue con datos parciales)` |
| 902 | `_build_state()` | `Exception` | `pass (sigue con datos parciales)` |
| 957 | `_build_state()` | `Exception` | `pass (sigue con datos parciales)` |
| 970 | `_build_state()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/dashboard_telegram.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 82 | `render_dashboard_telegram()` | `Exception` | `pass (sigue con datos parciales)` |
| 95 | `render_dashboard_telegram()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/diagnostics.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 77 | `_hours_since_iso()` | `Exception` | `return None` |
| 151 | `_b_gmail()` | `Exception` | `pass (sigue con datos parciales)` |
| 224 | `_b_volumen()` | `OSError` | `pass (sigue con datos parciales)` |
| 645 | `run_selftest()` | `Exception` | `pass (sigue con datos parciales)` |
| 662 | `run_selftest()` | `Exception` | `pass (sigue con datos parciales)` |
| 678 | `run_selftest()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/errors_log.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 80 | `fetch_recent()` | `Exception` | `return []` |
| 95 | `count_last_24h()` | `Exception` | `return 0` |
| 111 | `cleanup_old()` | `Exception` | `return 0` |
| 135 | `wrapper()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/farmdump_checks.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 403 | `_hl_asset_ctxs()` | `Exception` | `return {}` |
| 411 | `_f()` | `(TypeError, ValueError)` | `return None` |
| 478 | `fetch_hl_daily_closes()` | `Exception` | `return None` |
| 508 | `run_checks()` | `Exception` | `pass (sigue con datos parciales)` |
| 512 | `run_checks()` | `Exception` | `pass (sigue con datos parciales)` |
| 558 | `run_checks_safe()` | `Exception` | `return None` |

### `modules/feed_registry.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 186 | `_observed()` | `Exception` | `return {}` |
| 199 | `_horas_desde()` | `(ValueError, TypeError)` | `return None` |

### `modules/fund_state_auto_reconcile.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 155 | `handle_callback()` | `Exception` | `pass (sigue con datos parciales)` |
| 167 | `handle_callback()` | `Exception` | `return (implicito None)` |
| 174 | `handle_callback()` | `Exception` | `return (implicito None)` |
| 180 | `handle_callback()` | `Exception` | `pass (sigue con datos parciales)` |
| 194 | `handle_callback()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/gmail_intel.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 277 | `resolve_trash_folder()` | `Exception` | `pass (sigue con datos parciales)` |
| 393 | `_fetch_gm_msgids()` | `Exception` | `pass (sigue con datos parciales)` |
| 427 | `_fetch_uid_gm_msgids()` | `Exception` | `pass (sigue con datos parciales)` |
| 441 | `_fetch_uid_gm_msgids()` | `Exception` | `pass (sigue con datos parciales)` |
| 461 | `_fetch_gm_msgid()` | `Exception` | `pass (sigue con datos parciales)` |
| 493 | `_apply_post_action()` | `Exception` | `pass (sigue con datos parciales)` |
| 531 | `_apply_post_action()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/go_alerts.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 66 | `_load_state()` | `Exception` | `return {}` |
| 75 | `_save_state()` | `Exception` | `pass (sigue con datos parciales)` |
| 222 | `run_go_alert_cycle()` | `Exception` | `return 0` |

### `modules/health_registry.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 140 | `mark_ok()` | `Exception` | `pass (sigue con datos parciales)` |
| 168 | `mark_degraded()` | `Exception` | `pass (sigue con datos parciales)` |
| 189 | `swallowed()` | `Exception` | `pass (sigue con datos parciales)` |
| 203 | `clear()` | `Exception` | `pass (sigue con datos parciales)` |
| 217 | `reset_all()` | `Exception` | `pass (sigue con datos parciales)` |
| 308 | `_hours_since()` | `Exception` | `return None` |

### `modules/health_server.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 59 | `start_health_server()` | `Exception` | `pass (sigue con datos parciales)` |
| 69 | `start_health_server()` | `OSError` | `pass (sigue con datos parciales)` |
| 78 | `stop_health_server()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/heartbeat.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 48 | `build_heartbeat()` | `Exception` | `pass (sigue con datos parciales)` |
| 52 | `build_heartbeat()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/integrity_halt.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 93 | `is_price_action_context()` | `Exception` | `return False` |
| 170 | `_harvest_texts()` | `Exception` | `pass (sigue con datos parciales)` |
| 457 | `raise_flags()` | `Exception` | `pass (sigue con datos parciales)` |
| 471 | `get_active_flags()` | `Exception` | `return []` |
| 489 | `dismiss()` | `Exception` | `return False` |
| 621 | `reconcile_misattributed()` | `Exception` | `pass (sigue con datos parciales)` |
| 656 | `run_integrity_halt()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/intel30/_intel_base.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 88 | `log_call()` | `Exception` | `pass (sigue con datos parciales)` |
| 142 | `bump_count()` | `Exception` | `return 0` |
| 162 | `_state_db()` | `sqlite3.DatabaseError` | `pass (sigue con datos parciales)` |

### `modules/intel30/artemis_lite.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 66 | `fetch_all()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/intel30/asxn_data.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 77 | `_dias_desde()` | `ValueError` | `return None` |
| 80 | `_dias_desde()` | `ValueError` | `return None` |
| 104 | `_f()` | `(TypeError, ValueError)` | `return None` |

### `modules/intel30/cftc_cot.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 82 | `_f()` | `(TypeError, ValueError)` | `return 0.0` |

### `modules/intel30/dune_hl.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 72 | `fetch_all()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/intel30/eia_oil.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 78 | `fetch_wpsr()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/intel30/hl_info_api.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 32 | `_post()` | `ImportError` | `pass (sigue con datos parciales)` |
| 75 | `fetch_predicted_fundings()` | `(TypeError, ValueError)` | `pass (sigue con datos parciales)` |
| 87 | `fetch_predicted_fundings()` | `(TypeError, ValueError)` | `pass (sigue con datos parciales)` |

### `modules/intel30/hyperevmscan.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 56 | `fetch_all()` | `(ValueError, TypeError)` | `pass (sigue con datos parciales)` |
| 81 | `fetch_all()` | `(ValueError, TypeError)` | `pass (sigue con datos parciales)` |

### `modules/intel30/hypurrscan.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 52 | `_ms_to_iso()` | `(TypeError, ValueError)` | `return None` |
| 58 | `_ms_to_iso()` | `(OverflowError, OSError, ValueError)` | `return None` |

### `modules/intel30/kalshi_api.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 61 | `_sign()` | `ImportError` | `return (False, '') (todo ceros/vacios)` |
| 78 | `_sign()` | `Exception` | `return (False, '') (todo ceros/vacios)` |

### `modules/intel_processor.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 110 | `_parse_json_safely()` | `json.JSONDecodeError` | `return None` |

### `modules/intel_render.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 51 | `_clean()` | `Exception` | `return ''` |
| 62 | `_short_date()` | `Exception` | `return ''` |
| 76 | `_tier_handles()` | `Exception` | `pass (sigue con datos parciales)` |
| 92 | `_render_channel()` | `Exception` | `pass (sigue con datos parciales)` |
| 168 | `format_telegram_intel_block()` | `Exception` | `return ''` |
| 227 | `format_gmail_intel_block()` | `Exception` | `return ''` |

### `modules/intel_search.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 35 | `_has_fts5()` | `Exception` | `return False` |
| 98 | `_ensure_fts()` | `Exception` | `return False` |
| 142 | `search_intel()` | `Exception` | `pass (sigue con datos parciales)` |
| 164 | `search_intel()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/intel_selftest.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 189 | `run_selftest()` | `Exception` | `pass (sigue con datos parciales)` |
| 257 | `last_24h_call_summary()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/kill_scenarios.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 70 | `compute_kill_scenarios()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/ledger_invariants.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 185 | `check_invariants()` | `Exception` | `pass (sigue con datos parciales)` |
| 190 | `check_invariants()` | `Exception` | `pass (sigue con datos parciales)` |
| 198 | `_f()` | `(TypeError, ValueError)` | `return 0.0` |
| 344 | `recompute_from_fills()` | `Exception` | `pass (sigue con datos parciales)` |
| 349 | `recompute_from_fills()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/llm_router.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 172 | `_persist_usage()` | `Exception` | `pass (sigue con datos parciales)` |
| 193 | `_track_success()` | `Exception` | `pass (sigue con datos parciales)` |
| 246 | `_call_sonnet()` | `Exception` | `return None` |
| 280 | `_call_haiku()` | `Exception` | `return None` |
| 340 | `_call_gemini()` | `Exception` | `return None` |
| 424 | `get_cost_estimate()` | `Exception` | `return {'total': 0.0} (todo ceros/vacios)` |

### `modules/lmec_state.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 92 | `_path()` | `Exception` | `pass (sigue con datos parciales)` |
| 130 | `get_manual_inputs()` | `Exception` | `return {}` |
| 166 | `set_computed_inputs()` | `Exception` | `pass (sigue con datos parciales)` |
| 179 | `_is_computed_fresh()` | `Exception` | `return False` |
| 199 | `get_computed_inputs()` | `Exception` | `return {}` |
| 217 | `get_computed_meta()` | `Exception` | `return {'present': False, 'fresh': False} (todo ceros/vacios)` |
| 248 | `save()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/lmec_triggers.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 117 | `_manual_lmec_inputs()` | `Exception` | `return {}` |
| 129 | `_computed_lmec_inputs()` | `Exception` | `return {}` |
| 492 | `evaluate_lmec_triggers()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/macro_calendar.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 457 | `check_and_dispatch_alerts()` | `Exception` | `pass (sigue con datos parciales)` |
| 466 | `check_and_dispatch_alerts()` | `Exception` | `pass (sigue con datos parciales)` |
| 475 | `check_and_dispatch_alerts()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/macro_convergence.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 118 | `_fetch_channel_messages()` | `Exception` | `return []` |
| 122 | `_fetch_channel_messages()` | `Exception` | `return []` |
| 139 | `_fetch_channel_messages()` | `Exception` | `pass (sigue con datos parciales)` |
| 303 | `detect_convergence()` | `Exception` | `pass (sigue con datos parciales)` |
| 367 | `scheduled_check()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/margin_mode.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 50 | `_f()` | `(TypeError, ValueError)` | `return 0.0` |

### `modules/metrics.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 32 | `_safe_query()` | `Exception` | `return []` |
| 62 | `error_count_24h()` | `Exception` | `return 0` |
| 72 | `intel_memory_count()` | `Exception` | `return 0` |
| 80 | `sqlite_size_mb()` | `Exception` | `return 0.0` |
| 94 | `x_api_cost_summary()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/morning_brief.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 43 | `_fetch_overnight_market()` | `Exception` | `return {}` |
| 64 | `_fetch_active_alerts_count()` | `Exception` | `return 0` |
| 90 | `_fetch_today_events()` | `Exception` | `return []` |
| 99 | `_fetch_recent_macro_updates()` | `Exception` | `return ''` |
| 118 | `_fetch_fund_snapshot()` | `Exception` | `pass (sigue con datos parciales)` |
| 132 | `_fetch_fund_snapshot()` | `Exception` | `pass (sigue con datos parciales)` |
| 141 | `_fetch_fund_snapshot()` | `Exception` | `pass (sigue con datos parciales)` |
| 152 | `_fetch_fund_snapshot()` | `Exception` | `pass (sigue con datos parciales)` |
| 255 | `send_morning_brief()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/pat_status.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 87 | `_read_json()` | `Exception` | `return {}` |
| 95 | `_write_json()` | `Exception` | `pass (sigue con datos parciales)` |
| 119 | `parse_expiration()` | `ValueError` | `return None` |
| 138 | `fetch_expiration()` | `Exception` | `return None` |
| 146 | `fetch_expiration()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/pear_cross_validation.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 46 | `_fetch_pear_positions()` | `Exception` | `return None` |

### `modules/pear_staking.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 178 | `_fetch_pear_price()` | `Exception` | `pass (sigue con datos parciales)` |
| 193 | `_fetch_pear_price()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/pm_alert_monitor.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 211 | `_reset_for_tests()` | `Exception` | `pass (sigue con datos parciales)` |
| 242 | `staleness_note()` | `Exception` | `return ''` |

### `modules/portfolio_snapshot.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 271 | `_safe()` | `Exception` | `return None` |
| 529 | `_background_revalidate()` | `Exception` | `pass (sigue con datos parciales)` |
| 541 | `_kick_background_refresh()` | `RuntimeError` | `pass (sigue con datos parciales)` |
| 643 | `proactive_refresh()` | `Exception` | `return False` |
| 694 | `_build_portfolio_snapshot_inner()` | `Exception` | `pass (sigue con datos parciales)` |
| 703 | `_px()` | `(TypeError, ValueError)` | `return None` |
| 950 | `_build_portfolio_snapshot_inner()` | `Exception` | `pass (sigue con datos parciales)` |
| 1043 | `_build_portfolio_snapshot_inner()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/position_classifier.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 105 | `_to_float()` | `(TypeError, ValueError)` | `return None` |
| 344 | `classify_position()` | `Exception` | `pass (sigue con datos parciales)` |
| 452 | `classify_portfolio()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/pre_event_brief.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 92 | `_fund_snapshot_block()` | `(TypeError, ValueError)` | `pass (sigue con datos parciales)` |
| 96 | `_fund_snapshot_block()` | `Exception` | `pass (sigue con datos parciales)` |
| 107 | `_fund_snapshot_block()` | `Exception` | `pass (sigue con datos parciales)` |
| 115 | `_fund_snapshot_block()` | `Exception` | `pass (sigue con datos parciales)` |
| 216 | `check_and_dispatch()` | `Exception` | `return []` |
| 274 | `scheduled_check()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/predictive_alerts.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 122 | `_last_alert_ts()` | `Exception` | `return None` |
| 175 | `_sample_pm_hf()` | `Exception` | `pass (sigue con datos parciales)` |
| 184 | `_sample_hype_price()` | `Exception` | `return None` |
| 265 | `analyze_trends()` | `Exception` | `pass (sigue con datos parciales)` |
| 287 | `scheduled_check()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/pretrade_checklist.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 82 | `_intel_recent_for_token()` | `Exception` | `return []` |
| 146 | `_hl_funding_oi_volume()` | `Exception` | `pass (sigue con datos parciales)` |
| 152 | `_hl_funding_oi_volume()` | `Exception` | `pass (sigue con datos parciales)` |
| 158 | `_hl_funding_oi_volume()` | `Exception` | `pass (sigue con datos parciales)` |
| 161 | `_hl_funding_oi_volume()` | `Exception` | `pass (sigue con datos parciales)` |
| 175 | `_price_context()` | `Exception` | `pass (sigue con datos parciales)` |
| 205 | `_upcoming_unlocks_for()` | `Exception` | `pass (sigue con datos parciales)` |
| 208 | `_upcoming_unlocks_for()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/report_delta.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 31 | `_f()` | `(TypeError, ValueError)` | `return None` |
| 47 | `_parse_usd()` | `(TypeError, ValueError)` | `return None` |
| 67 | `collect_report_kpis()` | `Exception` | `pass (sigue con datos parciales)` |
| 76 | `collect_report_kpis()` | `Exception` | `pass (sigue con datos parciales)` |
| 82 | `collect_report_kpis()` | `Exception` | `pass (sigue con datos parciales)` |
| 87 | `collect_report_kpis()` | `Exception` | `pass (sigue con datos parciales)` |
| 132 | `save_report_kpis()` | `Exception` | `return False` |
| 151 | `load_last_kpis()` | `Exception` | `return None` |
| 195 | `_consume_baseline_note()` | `Exception` | `return ''` |
| 210 | `_age_text()` | `Exception` | `return ''` |
| 273 | `format_report_delta_block()` | `Exception` | `return ''` |

### `modules/scheduler_self_healing.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 129 | `_maybe_escalate()` | `Exception` | `pass (sigue con datos parciales)` |
| 143 | `_maybe_escalate()` | `Exception` | `pass (sigue con datos parciales)` |
| 177 | `runner()` | `Exception` | `return None` |

### `modules/screener_core.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 228 | `build_embedded_screener_block()` | `Exception` | `return None` |
| 244 | `build_embedded_screener_block()` | `Exception` | `return None` |

### `modules/signal_monitor.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 145 | `_reset_for_tests()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/sl_validator.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 54 | `sl_unreachable()` | `(TypeError, ValueError)` | `return False` |
| 133 | `should_alert()` | `Exception` | `return False` |
| 165 | `_digest_due()` | `Exception` | `return False` |
| 181 | `clear_condition()` | `Exception` | `pass (sigue con datos parciales)` |
| 213 | `find_unreachable()` | `Exception` | `pass (sigue con datos parciales)` |
| 228 | `run_sl_reachability_alerts()` | `Exception` | `return 0` |
| 247 | `run_sl_reachability_alerts()` | `Exception` | `pass (sigue con datos parciales)` |
| 266 | `run_sl_reachability_alerts()` | `Exception` | `pass (sigue con datos parciales)` |
| 268 | `run_sl_reachability_alerts()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/snapshots.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 131 | `build_snapshot()` | `(TypeError, ValueError)` | `pass (sigue con datos parciales)` |
| 159 | `save_snapshot()` | `Exception` | `return 0` |
| 178 | `previous_snapshot()` | `Exception` | `return None` |
| 193 | `latest_snapshot()` | `Exception` | `return None` |
| 225 | `_age_hours()` | `Exception` | `return 0.0` |

### `modules/source_alerts.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 78 | `_was_alerted()` | `sqlite3.Error` | `pass (sigue con datos parciales)` |
| 91 | `_record_alert()` | `sqlite3.Error` | `pass (sigue con datos parciales)` |
| 152 | `evaluate_matrix()` | `sqlite3.Error` | `pass (sigue con datos parciales)` |
| 177 | `get_persisted_state()` | `sqlite3.Error` | `pass (sigue con datos parciales)` |

### `modules/sqlite_backup.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 124 | `cleanup_old_backups()` | `Exception` | `pass (sigue con datos parciales)` |
| 151 | `cleanup_sqlite_weekly()` | `Exception` | `pass (sigue con datos parciales)` |
| 154 | `cleanup_sqlite_weekly()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/status_quick.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 123 | `build_status_block()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/telegram_intel.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 46 | `get_client()` | `Exception` | `return None` |
| 51 | `get_client()` | `Exception` | `pass (sigue con datos parciales)` |
| 63 | `stop_client()` | `Exception` | `pass (sigue con datos parciales)` |
| 72 | `_read_channel()` | `Exception` | `return []` |
| 172 | `scan_telegram_unread()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/telemetry.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 218 | `fetch_ctx_map()` | `Exception` | `return {}` |
| 237 | `fetch_perp_dexes()` | `Exception` | `return []` |
| 261 | `fetch_dex_ctx()` | `Exception` | `return {}` |
| 392 | `fetch_funding_avg_7d()` | `Exception` | `return (None, 0) (todo ceros/vacios)` |
| 421 | `fetch_low_7d()` | `Exception` | `return None` |

### `modules/throttle.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 56 | `is_throttled()` | `Exception` | `return (False, 0) (todo ceros/vacios)` |

### `modules/tradermap.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 64 | `_coerce_float()` | `(TypeError, ValueError)` | `return None` |

### `modules/tradermap_validator.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 185 | `get_indicator_overrides_safely()` | `Exception` | `return {}` |

### `modules/trailing_monitor.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 66 | `favorable_move_pct()` | `(TypeError, ValueError)` | `return None` |
| 108 | `_mark_fired()` | `Exception` | `pass (sigue con datos parciales)` |
| 198 | `evaluate_leg()` | `Exception` | `return (False, '') (todo ceros/vacios)` |
| 209 | `run_trailing_alerts()` | `Exception` | `return 0` |
| 220 | `run_trailing_alerts()` | `Exception` | `pass (sigue con datos parciales)` |
| 222 | `run_trailing_alerts()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/universal_screener.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 326 | `_reset_for_tests()` | `Exception` | `pass (sigue con datos parciales)` |
| 384 | `fetch_hl_ctx_full()` | `Exception` | `return {}` |
| 393 | `fetch_hl_ctx_full()` | `Exception` | `return {}` |
| 515 | `_evaluate_one()` | `Exception` | `pass (sigue con datos parciales)` |
| 652 | `advance_universe_state()` | `Exception` | `return 0` |

### `modules/unlock_monitor.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 284 | `_f()` | `(TypeError, ValueError)` | `return None` |
| 807 | `_conn()` | `Exception` | `pass (sigue con datos parciales)` |
| 943 | `_reset_for_tests()` | `Exception` | `pass (sigue con datos parciales)` |
| 955 | `_hl_post()` | `ImportError` | `pass (sigue con datos parciales)` |
| 987 | `fetch_4h_closes()` | `Exception` | `return None` |
| 1016 | `fetch_asset_ctx_map()` | `Exception` | `return {}` |
| 1038 | `fetch_btc_dominance()` | `Exception` | `return None` |
| 1171 | `compute_snapshot()` | `Exception` | `pass (sigue con datos parciales)` |
| 1205 | `compute_snapshot()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/unlocks.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 149 | `_parse_iso_or_epoch()` | `Exception` | `pass (sigue con datos parciales)` |
| 156 | `_parse_iso_or_epoch()` | `Exception` | `return None` |
| 168 | `_fetch_dropstab_token()` | `Exception` | `return None` |
| 210 | `_fetch_dropstab_token()` | `Exception` | `pass (sigue con datos parciales)` |
| 301 | `fetch_unlocks()` | `Exception` | `pass (sigue con datos parciales)` |
| 313 | `fetch_unlocks()` | `Exception` | `pass (sigue con datos parciales)` |
| 324 | `fetch_unlocks()` | `Exception` | `pass (sigue con datos parciales)` |
| 348 | `fetch_unlocks()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/variational.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 126 | `_to_float()` | `(TypeError, ValueError)` | `return None` |
| 152 | `parse_listing()` | `ValueError` | `return None` |

### `modules/version_info.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 110 | `_cost_24h_usd()` | `Exception` | `return 0.0` |
| 127 | `_backup_last_run()` | `Exception` | `return {'iso': '', 'ok': False, 'tarball': '', 'hours_ago': None} (todo ceros/vacios)` |

### `modules/weekly_summary.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 78 | `_capital_from()` | `Exception` | `return None` |
| 90 | `_capital_delta()` | `Exception` | `pass (sigue con datos parciales)` |
| 117 | `_highlight_fills()` | `Exception` | `return []` |
| 132 | `_highlight_intel()` | `Exception` | `return []` |
| 146 | `_error_count()` | `Exception` | `return 0` |
| 198 | `build_summary()` | `Exception` | `pass (sigue con datos parciales)` |
| 205 | `build_summary()` | `Exception` | `pass (sigue con datos parciales)` |
| 283 | `scheduled_summary()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/x_intel.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 172 | `_load_canonical_handles()` | `OSError` | `return []` |
| 632 | `fetch_timeline_via_list()` | `(TypeError, ValueError)` | `pass (sigue con datos parciales)` |
| 886 | `maybe_send_cost_alert()` | `Exception` | `pass (sigue con datos parciales)` |
| 1018 | `_notify_provider_fallback()` | `Exception` | `pass (sigue con datos parciales)` |
| 1090 | `fetch_x_intel()` | `Exception` | `pass (sigue con datos parciales)` |
| 1121 | `fetch_x_intel()` | `Exception` | `pass (sigue con datos parciales)` |
| 1138 | `fetch_x_intel()` | `Exception` | `pass (sigue con datos parciales)` |
| 1147 | `fetch_x_intel()` | `Exception` | `pass (sigue con datos parciales)` |
| 1177 | `fetch_x_intel()` | `Exception` | `pass (sigue con datos parciales)` |
| 1230 | `render_xrefresh_result()` | `Exception` | `pass (sigue con datos parciales)` |
| 1301 | `debug_x_status()` | `Exception` | `pass (sigue con datos parciales)` |
| 1366 | `get_cached_timeline()` | `Exception` | `pass (sigue con datos parciales)` |
| 1426 | `cache_banner_for_report()` | `Exception` | `pass (sigue con datos parciales)` |

### `modules/x_provider.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 114 | `official_credits_remaining()` | `ValueError` | `return 0.0` |
| 122 | `official_credits_remaining()` | `Exception` | `return 0.0` |
| 189 | `_record()` | `Exception` | `pass (sigue con datos parciales)` |
| 201 | `_parse_created_at()` | `ValueError` | `pass (sigue con datos parciales)` |
| 205 | `_parse_created_at()` | `ValueError` | `return None` |
| 262 | `_since_id_int()` | `(TypeError, ValueError)` | `return None` |
| 341 | `_fetch_list_pages()` | `(TypeError, ValueError)` | `pass (sigue con datos parciales)` |
| 390 | `_save_member_cache()` | `Exception` | `pass (sigue con datos parciales)` |
| 429 | `get_list_members()` | `Exception` | `pass (sigue con datos parciales)` |
| 498 | `_fetch_via_search()` | `(TypeError, ValueError)` | `pass (sigue con datos parciales)` |

### `morning_brief_scheduler.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 48 | `_coerce_event_dt()` | `Exception` | `return None` |
| 116 | `send_morning_brief_job()` | `Exception` | `return (implicito None)` |
| 133 | `send_morning_brief_job()` | `Exception` | `pass (sigue con datos parciales)` |

### `scheduler_calendar_v2.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 89 | `_mark_alert_sent()` | `Exception` | `pass (sigue con datos parciales)` |
| 165 | `_send_telegram()` | `Exception` | `pass (sigue con datos parciales)` |
| 254 | `run_calendar_alert_check()` | `Exception` | `pass (sigue con datos parciales)` |
| 282 | `run_calendar_alert_check()` | `Exception` | `pass (sigue con datos parciales)` |

### `scripts/reconcile_x_list.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 57 | `<module>()` | `Exception` | `pass (sigue con datos parciales)` |
| 84 | `load_canonical_handles()` | `OSError` | `pass (sigue con datos parciales)` |
| 194 | `_oauth_session()` | `ImportError` | `pass (sigue con datos parciales)` |

### `templates/formatters.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 159 | `_build_price_map()` | `Exception` | `pass (sigue con datos parciales)` |
| 169 | `_build_price_map()` | `Exception` | `pass (sigue con datos parciales)` |
| 272 | `_wallet_perp_contribution()` | `(TypeError, ValueError)` | `return 0.0` |
| 564 | `_basket_upnl_for_header()` | `(TypeError, ValueError)` | `pass (sigue con datos parciales)` |
| 597 | `_perp_upnl_split()` | `Exception` | `pass (sigue con datos parciales)` |
| 655 | `_tactical_book_label()` | `Exception` | `pass (sigue con datos parciales)` |
| 726 | `_price_for_symbol()` | `(TypeError, ValueError)` | `return None` |
| 736 | `_fmt_usd_compact()` | `(TypeError, ValueError)` | `return None` |
| 774 | `_normalize_unlock_epoch()` | `Exception` | `pass (sigue con datos parciales)` |
| 849 | `_next_catalyst_for_header()` | `Exception` | `pass (sigue con datos parciales)` |
| 869 | `_next_catalyst_for_header()` | `Exception` | `pass (sigue con datos parciales)` |
| 937 | `_next_catalyst_for_header()` | `Exception` | `pass (sigue con datos parciales)` |
| 1020 | `format_report_header()` | `(TypeError, ValueError)` | `pass (sigue con datos parciales)` |
| 1024 | `format_report_header()` | `(TypeError, ValueError)` | `pass (sigue con datos parciales)` |
| 1061 | `format_report_header()` | `Exception` | `pass (sigue con datos parciales)` |
| 1073 | `format_report_header()` | `Exception` | `pass (sigue con datos parciales)` |
| 1085 | `format_report_header()` | `Exception` | `pass (sigue con datos parciales)` |
| 1137 | `format_report_header()` | `Exception` | `pass (sigue con datos parciales)` |
| 1151 | `format_report_header()` | `Exception` | `pass (sigue con datos parciales)` |
| 1157 | `format_report_header()` | `Exception` | `pass (sigue con datos parciales)` |
| 1168 | `format_report_header()` | `Exception` | `pass (sigue con datos parciales)` |
| 1197 | `format_quick_positions()` | `(TypeError, ValueError)` | `pass (sigue con datos parciales)` |
| 1201 | `format_quick_positions()` | `(TypeError, ValueError)` | `pass (sigue con datos parciales)` |
| 1247 | `format_quick_positions()` | `Exception` | `pass (sigue con datos parciales)` |
| 1255 | `format_quick_positions()` | `Exception` | `pass (sigue con datos parciales)` |
| 1259 | `format_quick_positions()` | `(TypeError, ValueError)` | `pass (sigue con datos parciales)` |
| 1274 | `format_quick_positions()` | `Exception` | `pass (sigue con datos parciales)` |
| 1318 | `format_quick_positions()` | `Exception` | `pass (sigue con datos parciales)` |
| 1372 | `format_quick_positions()` | `Exception` | `pass (sigue con datos parciales)` |
| 1382 | `format_quick_positions()` | `Exception` | `pass (sigue con datos parciales)` |
| 1394 | `format_quick_positions()` | `Exception` | `pass (sigue con datos parciales)` |
| 1397 | `format_quick_positions()` | `Exception` | `pass (sigue con datos parciales)` |

### `timezone_validator.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 67 | `<module>()` | `Exception` | `pass (sigue con datos parciales)` |

### `tools/bug_class_scan.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 252 | `_sql_strings()` | `SyntaxError` | `return []` |

### `tools/silent_degradation_scan.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 170 | `_describe_return()` | `Exception` | `return None` |
| 242 | `scan_file()` | `SyntaxError` | `return []` |

### `utils/http.py`

| linea | funcion | atrapa | devuelve/hace |
|---|---|---|---|
| 56 | `post_json()` | `ImportError` | `pass (sigue con datos parciales)` |
