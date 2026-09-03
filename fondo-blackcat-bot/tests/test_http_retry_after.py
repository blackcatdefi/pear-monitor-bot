"""R-429-RETRY-AFTER (2026-09-03) — la politica de reintentos empeoraba el 429.

EL SINTOMA
==========
/diagnostico del 3-sep reporto:

    MONEY subsistema degradado: Precios y mercado — coingecko_global —
    HTTPStatusError: Client error '429 Too Many Requests'

El 429 no es un bug nuestro: la IP de salida de Railway es compartida y el
endpoint de CoinGecko es keyless. Lo que si era nuestro es que los TRES
intentos fallaran, y eso no era mala suerte.

LOS DOS DEFECTOS
================
1. ``Retry-After`` se ignoraba. El servidor dice cuanto falta para salir del
   castigo; nosotros adivinabamos 2s y 4s. Los tres intentos caian adentro de
   la misma ventana, o sea que reintentar no era una segunda chance: era mas
   trafico durante la penalizacion, que es como se extiende.

2. Se dormia DESPUES del ultimo intento. El bucle esperaba el backoff entero y
   recien ahi levantaba. Ocho segundos de latencia del reporte gastados en no
   hacer nada.

Ninguno de los dos se ve en un log: el primero parece "la API esta caida" y el
segundo parece "la red esta lenta".

LO QUE ESTOS TESTS NO HACEN
===========================
No verifican que el 429 desaparezca — no depende de nosotros. Verifican que
cuando el servidor dice cuanto esperar, se le haga caso, y que no se gaste
tiempo en una espera que ya no sirve para nada.
"""
from __future__ import annotations

import asyncio
import email.utils
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from utils import http as uh  # noqa: E402


@pytest.fixture()
def dormidas(monkeypatch):
    """Registra cada sleep sin dormir de verdad. El test mide la POLITICA."""
    reg: list[float] = []

    async def _fake(sec):
        reg.append(float(sec))

    monkeypatch.setattr(uh.asyncio, "sleep", _fake)
    monkeypatch.setattr(uh.random, "uniform", lambda a, b: 0.0)  # sin jitter
    return reg


def _resp(status: int, headers: dict[str, str] | None = None):
    req = httpx.Request("GET", "https://api.coingecko.com/api/v3/global")
    return httpx.Response(status, headers=headers or {}, request=req,
                          json={"data": {}})


def _err(status: int, headers: dict[str, str] | None = None):
    r = _resp(status, headers)
    return httpx.HTTPStatusError(f"{status}", request=r.request, response=r)


def _cliente(secuencia):
    """AsyncClient falso que devuelve/levanta la secuencia dada, en orden."""
    restos = list(secuencia)

    class _C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, **kw):
            item = restos.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    return _C


# ─── 1. Retry-After gana sobre el exponencial ───────────────────────────────

def test_se_espera_lo_que_pide_el_servidor_y_no_lo_que_adivinamos(
        monkeypatch, dormidas):
    """El defecto exacto: 2s y 4s contra una ventana que pedia 12."""
    monkeypatch.setattr(uh.httpx, "AsyncClient", _cliente([
        _err(429, {"Retry-After": "12"}),
        _resp(200),
    ]))
    asyncio.run(uh.get_json("https://api.coingecko.com/api/v3/global"))
    assert dormidas == [12.0], (
        f"se ignoro Retry-After y se reintento adentro de la ventana de "
        f"castigo: {dormidas}")


def test_retry_after_en_formato_fecha_tambien_se_respeta(monkeypatch, dormidas):
    """El RFC admite los dos formatos y CoinGecko usa segundos, pero otras
    fuentes del bot mandan fecha. Parsear uno solo deja al otro en el
    exponencial sin que nada lo diga."""
    cuando = datetime.now(timezone.utc) + timedelta(seconds=9)
    monkeypatch.setattr(uh.httpx, "AsyncClient", _cliente([
        _err(503, {"Retry-After": email.utils.format_datetime(cuando)}),
        _resp(200),
    ]))
    asyncio.run(uh.get_json("https://x.test"))
    assert dormidas and 7.0 <= dormidas[0] <= 10.0, dormidas


@pytest.mark.parametrize("valor", ["", "ya-mismo", "-3", None])
def test_un_retry_after_ilegible_cae_al_exponencial_y_no_rompe(
        monkeypatch, dormidas, valor):
    heads = {} if valor is None else {"Retry-After": valor}
    monkeypatch.setattr(uh.httpx, "AsyncClient", _cliente([
        _err(429, heads),
        _resp(200),
    ]))
    asyncio.run(uh.get_json("https://x.test"))
    assert dormidas == [2.0], (
        f"con la cabecera rota hay que volver al exponencial, no quedarse "
        f"sin esperar ni explotar: {dormidas}")


def test_una_espera_desmedida_se_recorta_al_techo(monkeypatch, dormidas):
    """Retry-After: 600 no puede colgar un reporte diez minutos. Se espera
    hasta el techo y se reintenta igual; si el castigo sigue, se reporta."""
    monkeypatch.setattr(uh.httpx, "AsyncClient", _cliente([
        _err(429, {"Retry-After": "600"}),
        _resp(200),
    ]))
    asyncio.run(uh.get_json("https://x.test"))
    assert dormidas == [uh.MAX_BACKOFF_SEC], dormidas


def test_sin_cabecera_el_backoff_sigue_siendo_exponencial(monkeypatch, dormidas):
    """Guarda contra el arreglo de mas: quitar el exponencial dejaria a todas
    las fuentes que NO mandan la cabecera reintentando de inmediato."""
    monkeypatch.setattr(uh.httpx, "AsyncClient", _cliente([
        _err(500), _err(500), _err(500), _resp(200),
    ]))
    assert asyncio.run(
        uh.get_json("https://x.test", max_retries=4)) == {"data": {}}
    assert dormidas == [2.0, 4.0, 8.0], dormidas


# ─── 2. no dormir despues del ultimo intento ────────────────────────────────

def test_no_se_duerme_despues_del_ultimo_intento(monkeypatch, dormidas):
    """Ocho segundos de latencia para no aprovecharlos con ningun intento."""
    monkeypatch.setattr(uh.httpx, "AsyncClient", _cliente([
        _err(429, {"Retry-After": "5"})] * 3))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(uh.get_json("https://x.test"))
    assert len(dormidas) == 2, (
        f"con 3 intentos hay 2 esperas utiles; una tercera se gasta antes de "
        f"levantar: {dormidas}")


def test_con_un_solo_intento_no_se_duerme_nada(monkeypatch, dormidas):
    monkeypatch.setattr(uh.httpx, "AsyncClient", _cliente([_err(429)]))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(uh.get_json("https://x.test", max_retries=1))
    assert dormidas == []


def test_la_excepcion_que_se_propaga_es_la_ultima_de_verdad(
        monkeypatch, dormidas):
    """Cortar el bucle con `break` no puede perder el error: sin el, el
    subsistema quedaria degradado sin decir por que."""
    monkeypatch.setattr(uh.httpx, "AsyncClient", _cliente([
        _err(500), _err(429)]))
    with pytest.raises(httpx.HTTPStatusError) as ei:
        asyncio.run(uh.get_json("https://x.test", max_retries=2))
    assert ei.value.response.status_code == 429


# ─── 3. lo que no cambia ────────────────────────────────────────────────────

def test_el_camino_feliz_no_duerme_ni_reintenta(monkeypatch, dormidas):
    monkeypatch.setattr(uh.httpx, "AsyncClient", _cliente([_resp(200)]))
    assert asyncio.run(uh.get_json("https://x.test")) == {"data": {}}
    assert dormidas == []


def test_una_falla_real_sigue_levantando(monkeypatch, dormidas):
    """La tentacion al tocar esto es devolver {} y "que el reporte siga".

    La doctrina de health_registry lo prohibe: un default silencioso en el
    money path no es una degradacion aceptable. Quien llama decide.
    """
    monkeypatch.setattr(uh.httpx, "AsyncClient", _cliente([_err(429)] * 3))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(uh.get_json("https://x.test"))


def test_el_jitter_existe_y_desincroniza(monkeypatch):
    """Sin jitter, las corrutinas que arrancan juntas reintentan en el mismo
    instante y se vuelven a pisar. Se mide que dos esperas del MISMO caso no
    sean identicas."""
    monkeypatch.setattr(uh.httpx, "AsyncClient", _cliente([_err(429)] * 99))
    e = _err(429, {"Retry-After": "10"})
    esperas = {round(uh._backoff(e, 0, 2.0), 6) for _ in range(40)}
    assert len(esperas) > 1, "el backoff es determinista: no hay jitter"
    assert all(7.0 <= x <= 13.0 for x in esperas), sorted(esperas)[:5]


def test_el_techo_no_se_pasa_ni_con_jitter(monkeypatch):
    """El jitter se aplica DESPUES del recorte, asi que puede empujar por
    encima del techo. Se admite el margen del jitter y nada mas."""
    e = _err(429, {"Retry-After": "600"})
    esperas = [uh._backoff(e, 0, 2.0) for _ in range(200)]
    assert max(esperas) <= uh.MAX_BACKOFF_SEC * (1 + uh.JITTER) + 1e-9
    assert min(esperas) >= 0.0


def test_un_error_de_red_sin_response_no_rompe_el_calculo(dormidas):
    """httpx.ConnectError no trae .response. Leer la cabecera sin guarda
    convertiria un timeout en un AttributeError adentro del except."""
    exc = httpx.ConnectError("sin ruta al host")
    assert uh._retry_after_sec(exc) is None
    assert uh._backoff(exc, 1, 2.0) == pytest.approx(4.0)
