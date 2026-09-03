"""Async HTTP helper with retry/backoff.

R-429-RETRY-AFTER (2026-09-03) — POR QUE ESTE ARCHIVO CAMBIO
===========================================================
Produccion reporto ``Precios y mercado — coingecko_global — 429 Too Many
Requests``. El 429 en si no es un bug nuestro: la IP de salida de Railway es
compartida y CoinGecko rate-limitea el endpoint keyless. Lo que SI era nuestro
son dos defectos de la politica de reintentos, y los dos empujaban en la
direccion equivocada:

1. **Se ignoraba ``Retry-After``.** Un 429 casi siempre viene con la cabecera
   que dice cuanto hay que esperar. Reintentabamos a los 2s y a los 4s contra
   una ventana que pedia mas: los tres intentos caian ADENTRO del castigo, o
   sea que el reintento no era una segunda chance, era mas trafico durante la
   penalizacion. Por eso los tres fallaban.

2. **Se dormia despues del ULTIMO intento.** El bucle esperaba el backoff
   completo y recien ahi levantaba. Con 3 intentos eso son 8 segundos de
   latencia del reporte tirados a la basura para no hacer absolutamente nada
   con ellos.

Ademas el backoff era exactamente igual para todos los llamadores, asi que
varias tareas que arrancan juntas reintentaban en lockstep y volvian a chocar.
El jitter rompe esa sincronizacion.

Lo que NO cambia: una falla real sigue levantando. Servir un payload cacheado
en silencio esta explicitamente prohibido por la doctrina de
``health_registry`` — "un default silencioso en el money path NUNCA es una
degradacion aceptable" — asi que el ❌ del subsistema se mantiene cuando de
verdad no se pudo leer. Esto reduce la probabilidad del 429, no la esconde.

R-429-TECHO (2026-09-03, misma jornada) — LA CORRECCION A LO DE ARRIBA
=====================================================================
El 429 volvio, y volvio porque quedaba un tercer defecto que la ronda anterior
no vio: ``MAX_BACKOFF_SEC`` es 30 y CoinGecko pide 60. O sea que arreglamos
"ignorabamos Retry-After" y despues lo ignorabamos igual, solo que con mas
estilo: leiamos los 60, los recortabamos a 30 y reintentabamos ADENTRO del
castigo. El intento extra no solo fallaba — era trafico durante la
penalizacion, que es lo que la alarga.

Ahora, si el servidor pide mas que el techo, no se reintenta: se corta y se
reporta. Esperar menos de lo que pidio el unico que esta contando no es una
segunda chance, es insistir.

Y una nota sobre el metodo, que importa mas que el parche: la ronda anterior
declaro el 429 "cerrado" con UN /diagnostico limpio. Un solo panel verde entre
dos castigos no es evidencia de que algo se arreglo — es evidencia de que en
ese momento no estaba fallando. No es lo mismo.
"""
from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# Techo del backoff. CoinGecko a veces manda Retry-After: 60. Un reporte no
# puede quedarse 60 segundos colgado de una fuente de contexto, asi que se
# espera hasta el techo y se reintenta igual; si el servidor sigue castigando,
# la falla se reporta, que es la respuesta honesta.
MAX_BACKOFF_SEC = 30.0

# Fraccion de jitter aplicada al backoff calculado (±25%). Sin esto, varias
# corrutinas que arrancan juntas reintentan en el mismo instante y se vuelven
# a pisar entre ellas.
JITTER = 0.25


def _retry_after_sec(exc: Exception) -> float | None:
    """Segundos que pide el servidor, o None si no lo dice.

    ``Retry-After`` admite dos formatos por RFC: segundos ("120") o una fecha
    HTTP. Los dos se ven en la practica y por eso se aceptan los dos; un valor
    que no parsea se ignora en vez de romper el reintento.
    """
    resp = getattr(exc, "response", None)
    raw = None
    try:
        raw = (resp.headers or {}).get("Retry-After") if resp is not None else None
    except Exception:  # noqa: BLE001 - una cabecera rara no puede tumbar el retry
        return None
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        seg = float(raw)
    except ValueError:
        pass
    else:
        # Un negativo en la forma numerica esta MAL formado. Tomarlo como 0
        # seria reintentar al instante — exactamente el comportamiento que
        # este modulo existe para no tener durante un castigo. Una FECHA
        # pasada, en cambio, si significa "ya podes": esa se recorta a 0 mas
        # abajo, y por eso los dos formatos no se tratan igual.
        return seg if seg >= 0 else None
    try:
        cuando = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if cuando is None:
        return None
    return max(0.0, cuando.timestamp() - time.time())


def _backoff(exc: Exception, attempt: int, base: float) -> float:
    """Cuanto esperar antes del proximo intento.

    ``Retry-After`` gana sobre el exponencial: es el unico dato que viene del
    lado que esta contando, y adivinar contra el es lo que hacia que los tres
    intentos cayeran adentro de la misma ventana de castigo.
    """
    pedido = _retry_after_sec(exc)
    espera = pedido if pedido is not None else base * (2 ** attempt)
    espera = min(espera, MAX_BACKOFF_SEC)
    espera *= 1.0 + random.uniform(-JITTER, JITTER)
    return max(0.0, espera)


async def request_json(
    method: str,
    url: str,
    *,
    max_retries: int = 3,
    base_backoff: float = 2.0,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
    **kwargs: Any,
) -> Any:
    """Perform an HTTP request and return parsed JSON, with backoff.

    Honours ``Retry-After`` on the responses that carry it, jitters the wait,
    and never sleeps after the final attempt.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(method, url, **kwargs)
                resp.raise_for_status()
                return resp.json()
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            last_exc = exc
            ultimo = attempt == max_retries - 1
            if ultimo:
                # Dormir aca no compra nada: no queda ningun intento que
                # aprovechar la espera. Solo agrega latencia al fallo.
                log.warning("HTTP %s %s failed (attempt %d/%d): %s — sin "
                            "reintentos restantes",
                            method, url, attempt + 1, max_retries, exc)
                break
            # R-429-TECHO: si el servidor pidio MAS de lo que estamos
            # dispuestos a esperar, reintentar es futil por definicion — el
            # proximo intento cae adentro del castigo que el propio servidor
            # acaba de declarar. Y no es neutro: ese intento es trafico extra
            # durante la penalizacion, o sea justo lo que la extiende. La
            # ronda anterior capo el backoff en 30s contra un Retry-After de
            # 60 y llamo al 429 "arreglado" con un solo /diagnostico limpio de
            # evidencia. No estaba arreglado: estaba entre dos castigos.
            pedido = _retry_after_sec(exc)
            if pedido is not None and pedido > MAX_BACKOFF_SEC:
                log.warning(
                    "HTTP %s %s: 429 con Retry-After %.0fs > techo %.0fs — se "
                    "corta el reintento (esperar menos garantiza otro rechazo "
                    "y alarga el castigo)",
                    method, url, pedido, MAX_BACKOFF_SEC)
                break
            wait = _backoff(exc, attempt, base_backoff)
            log.warning(
                "HTTP %s %s failed (attempt %d/%d): %s — retrying in %.1fs",
                method, url, attempt + 1, max_retries, exc, wait,
            )
            await asyncio.sleep(wait)
    assert last_exc is not None
    raise last_exc


async def get_json(url: str, **kwargs: Any) -> Any:
    return await request_json("GET", url, **kwargs)


async def post_json(url: str, json_body: Any, **kwargs: Any) -> Any:
    # R-BOT-DEFINITIVE WI-4: every HyperLiquid info-API POST is routed through
    # the SHARED rate-limited + TTL-cached client (modules.hl_client) so one
    # /reporte never re-issues the same request and 429s get jittered backoff.
    try:
        if url.rstrip("/").endswith("hyperliquid.xyz/info") and isinstance(json_body, dict):
            from modules.hl_client import post_info
            return await post_info(json_body)
    except ImportError:  # pragma: no cover — isolated tests without modules pkg
        pass
    return await request_json("POST", url, json=json_body, **kwargs)
