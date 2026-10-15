"""R-LIVE-FIX (2026-10-15) — los dos defectos que los logs de produccion
mostraban en vivo, pinneados para que no vuelvan.

1. TORMENTA 429 DE COINGECKO. ``simple/price`` y ``/global`` cada ~60s, tres
   reintentos cada una, 429 en todos, "market" degradado de corrido. Causas:
   el refresco del dashboard a 30s arrastraba CoinGecko; el fallo no se
   cacheaba (cada 429 garantizaba otro pedido); CoinGecko era la fuente
   PRIMARIA de precios que el bot ya tiene del oraculo HL.

2. HL_BORROW_LEND 422. ``borrowLendReserveState`` sin ``token`` ya no existe;
   el listado paso a ``allBorrowLendReserveStates`` con forma
   ``[[token_index, state], ...]``. El mapa LTV volvia ``{}`` en silencio y la
   escalera de liquidacion PM (money path) imprimia el LTV de config como
   si fuera medido.

Cada test de este archivo tiene una mutacion asociada en
``scripts/mutation_check_live_fix.py`` que lo hace fallar. Un test que no
muerde no es un test.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from utils import http as uh
from modules import market
from modules import hl_borrow_lend as hbl
from modules import health_registry


# ─── helpers ────────────────────────────────────────────────────────────────

def _resp(status: int, headers: dict[str, str] | None = None, body=None):
    req = httpx.Request("GET", "https://api.coingecko.com/api/v3/simple/price")
    return httpx.Response(status, headers=headers or {}, request=req,
                          json=body if body is not None else {"data": {}})


def _err(status: int, headers: dict[str, str] | None = None):
    r = _resp(status, headers)
    return httpx.HTTPStatusError(f"{status}", request=r.request, response=r)


class _Client:
    """AsyncClient falso: cuenta pedidos y sirve una secuencia."""
    calls: list[str] = []
    seq: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, **kw):
        _Client.calls.append(url)
        item = _Client.seq.pop(0) if _Client.seq else _err(429)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fake_http(monkeypatch):
    _Client.calls = []
    _Client.seq = []
    monkeypatch.setattr(uh.httpx, "AsyncClient", _Client)

    async def _nosleep(_s):
        return None
    monkeypatch.setattr(uh.asyncio, "sleep", _nosleep)
    monkeypatch.setattr(uh.random, "uniform", lambda a, b: 0.0)
    uh.reset_cooldowns()
    yield _Client
    uh.reset_cooldowns()


@pytest.fixture
def clean_market(monkeypatch):
    market._CACHE.clear()
    yield
    market._CACHE.clear()


ORACLE = {"BTC": 81000.0, "ETH": 2366.0, "SOL": 102.0, "HYPE": 78.9, "PAXG": 4152.0}
PREV = {"BTC": 80000.0, "ETH": 2400.0, "SOL": 100.0, "HYPE": 80.0, "PAXG": 4100.0}


@pytest.fixture
def hl_oracle(monkeypatch):
    monkeypatch.setattr(market, "_hl_oracle_map", lambda: dict(ORACLE))
    monkeypatch.setattr(market, "_hl_prev_day_map", lambda: dict(PREV))


# ═══════════════════════════════════════════════════════════════════════════
# 1. utils.http — cooldown por host que sobrevive a la llamada
# ═══════════════════════════════════════════════════════════════════════════

def test_un_429_pone_al_host_en_cooldown_y_la_siguiente_llamada_no_toca_la_red(fake_http):
    fake_http.seq = [_err(429)]
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(uh.get_json("https://api.coingecko.com/api/v3/simple/price"))
    assert len(fake_http.calls) == 1
    # Segunda llamada al MISMO host (otro path): cero trafico.
    with pytest.raises(uh.HostCoolingDown) as ei:
        asyncio.run(uh.get_json("https://api.coingecko.com/api/v3/global"))
    assert len(fake_http.calls) == 1, "en cooldown se volvio a pegar a la red"
    assert ei.value.remaining > 0
    assert ei.value.remaining <= uh.COOLDOWN_BASE_SEC + 1


def test_el_cooldown_crece_exponencial_con_429_consecutivos(fake_http, monkeypatch):
    url = "https://api.coingecko.com/api/v3/global"
    castigos = []
    for _ in range(3):
        uh._HOST_COOLDOWN.get("api.coingecko.com", {}).update(until=0.0)  # vence
        fake_http.seq = [_err(429)]
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(uh.get_json(url))
        castigos.append(uh.cooldown_remaining(url))
    assert castigos[0] <= uh.COOLDOWN_BASE_SEC + 1
    assert castigos[1] > castigos[0] * 1.8, castigos
    assert castigos[2] > castigos[1] * 1.8, castigos


def test_el_cooldown_no_pasa_el_techo(fake_http):
    url = "https://api.coingecko.com/api/v3/global"
    for _ in range(8):
        uh._HOST_COOLDOWN.get("api.coingecko.com", {}).update(until=0.0)
        fake_http.seq = [_err(429)]
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(uh.get_json(url))
    assert uh.cooldown_remaining(url) <= uh.COOLDOWN_MAX_SEC + 1


def test_retry_after_manda_sobre_el_castigo_calculado(fake_http):
    fake_http.seq = [_err(429, {"Retry-After": "600"})]
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(uh.get_json("https://api.coingecko.com/api/v3/global"))
    rem = uh.cooldown_remaining("https://api.coingecko.com/api/v3/global")
    assert 590 <= rem <= 600, rem


def test_un_exito_resetea_el_contador_de_strikes(fake_http):
    url = "https://api.coingecko.com/api/v3/global"
    fake_http.seq = [_err(429)]
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(uh.get_json(url))
    assert uh.cooldown_strikes(url) == 1
    uh._HOST_COOLDOWN["api.coingecko.com"]["until"] = 0.0
    fake_http.seq = [_resp(200)]
    asyncio.run(uh.get_json(url))
    assert uh.cooldown_strikes(url) == 0
    assert uh.cooldown_remaining(url) == 0.0


def test_el_cooldown_es_por_host_no_global(fake_http):
    fake_http.seq = [_err(429), _resp(200)]
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(uh.get_json("https://api.coingecko.com/api/v3/global"))
    # Otro host sigue libre.
    assert asyncio.run(uh.get_json("https://api.alternative.me/fng/")) == {"data": {}}


# ═══════════════════════════════════════════════════════════════════════════
# 2. modules.market — HL primero, CoinGecko solo para lo que HL no tiene
# ═══════════════════════════════════════════════════════════════════════════

def _gecko_ok_body():
    return {
        "bitcoin": {"usd": 1.0, "usd_24h_change": 9.9, "usd_market_cap": 1.6e12},
        "ethereum": {"usd": 1.0, "usd_market_cap": 3e11},
        "solana": {"usd": 1.0},
        "hyperliquid": {"usd": 1.0},
        "pax-gold": {"usd": 1.0},
        "kinesis-silver": {"usd": 56.5, "usd_24h_change": -4.0},
    }


def test_los_precios_core_salen_del_oraculo_hl_no_de_coingecko(fake_http, clean_market, hl_oracle):
    fake_http.seq = [_resp(200, body=_gecko_ok_body())]
    out = asyncio.run(market.coingecko_prices())
    for sym in ("BTC", "ETH", "SOL", "HYPE"):
        assert out[sym]["source"] == "hl_oracle", (sym, out[sym])
        assert out[sym]["price_usd"] == ORACLE[sym], (
            f"{sym}: CoinGecko ({out[sym]['price_usd']}) piso al oraculo HL")
    assert out["GOLD"]["price_usd"] == ORACLE["PAXG"]
    # Lo que HL no lista viene de CoinGecko.
    assert out["SILVER"]["source"] == "coingecko"
    assert out["SILVER"]["price_usd"] == 56.5


def test_el_24h_change_core_se_deriva_de_prevdaypx_de_hl(fake_http, clean_market, hl_oracle):
    fake_http.seq = [_resp(200, body=_gecko_ok_body())]
    out = asyncio.run(market.coingecko_prices())
    assert out["BTC"]["change_24h"] == pytest.approx(1.25)   # 81000/80000
    assert out["BTC"]["change_24h"] != 9.9                   # NO el de gecko
    # market cap si es enriquecimiento de CoinGecko.
    assert out["BTC"]["market_cap"] == 1.6e12


def test_coingecko_caido_con_hl_sano_no_degrada_market(fake_http, clean_market, hl_oracle, monkeypatch):
    marcas: list[str] = []
    monkeypatch.setattr(health_registry, "swallowed",
                        lambda sub, note="": marcas.append(f"{sub}:{note}"))
    fake_http.seq = [_err(429)]
    out = asyncio.run(market.coingecko_prices())
    assert out["HYPE"]["price_usd"] == ORACLE["HYPE"]
    assert "SILVER" not in out
    assert not [m for m in marcas if m.startswith("market:")], marcas
    note = market.coingecko_note()
    assert note and "SILVER" in note["missing_after_hl"]


def test_sin_hl_ni_coingecko_market_SI_degrada(fake_http, clean_market, monkeypatch):
    monkeypatch.setattr(market, "_hl_oracle_map", lambda: {})
    monkeypatch.setattr(market, "_hl_prev_day_map", lambda: {})
    marcas: list[str] = []
    monkeypatch.setattr(health_registry, "swallowed",
                        lambda sub, note="": marcas.append(f"{sub}:{note}"))
    fake_http.seq = [_err(429)]
    out = asyncio.run(market.coingecko_prices())
    assert out == {}
    assert any(m.startswith("market:") and "HYPE" in m for m in marcas), marcas


def test_un_fallo_de_coingecko_se_cachea_negativo_y_no_se_insiste(fake_http, clean_market, hl_oracle):
    fake_http.seq = [_err(500), _resp(200, body=_gecko_ok_body())]
    uh.reset_cooldowns()
    with pytest.raises(Exception):
        asyncio.run(market._coingecko_simple_price(["kinesis-silver"]))
    n = len(fake_http.calls)
    with pytest.raises(RuntimeError, match="no se insiste"):
        asyncio.run(market._coingecko_simple_price(["kinesis-silver"]))
    assert len(fake_http.calls) == n, "tras el fallo se volvio a pegar a CoinGecko"


def test_coingecko_se_pide_con_un_solo_intento(fake_http, clean_market, hl_oracle):
    """Tres intentos contra un 429 eran trafico durante la penalizacion."""
    fake_http.seq = [_err(500), _err(500), _err(500)]
    with pytest.raises(Exception):
        asyncio.run(market._coingecko_simple_price(["kinesis-silver"]))
    assert len(fake_http.calls) == 1, fake_http.calls


def test_el_ensamble_de_precios_se_cachea_5_min(fake_http, clean_market, hl_oracle):
    fake_http.seq = [_resp(200, body=_gecko_ok_body())]
    a = asyncio.run(market.coingecko_prices())
    b = asyncio.run(market.coingecko_prices())
    assert a is b
    assert market.PRICES_TTL_SEC >= 300
    assert market.GECKO_PRICES_TTL_SEC >= 600
    assert market.GECKO_GLOBAL_TTL_SEC >= 900


def test_global_caido_devuelve_el_motivo_no_un_dict_vacio(fake_http, clean_market):
    fake_http.seq = [_err(429)]
    g = asyncio.run(market.coingecko_global())
    assert "unavailable" in g and g["unavailable"]
    # y no se vuelve a pedir enseguida (cache negativo)
    n = len(fake_http.calls)
    g2 = asyncio.run(market.coingecko_global())
    assert "unavailable" in g2 and len(fake_http.calls) == n


def test_fetch_market_data_publica_las_fuentes_de_precio(fake_http, clean_market, hl_oracle, monkeypatch):
    async def _empty():
        return {}
    for fn in ("fear_greed", "coinglass_metrics", "coinglass_basket",
               "defillama_top_fees", "defillama_stablecoin_supply"):
        monkeypatch.setattr(market, fn, _empty)
    fake_http.seq = [_resp(200, body=_gecko_ok_body()), _err(429)]
    d = asyncio.run(market.fetch_market_data())["data"]
    assert d["price_sources"]["HYPE"] == "hl_oracle"
    assert d["price_sources"]["SILVER"] == "coingecko"


def test_el_refresco_del_dashboard_ya_no_corre_cada_30s():
    import bot
    assert bot.DASHBOARD_PROACTIVE_REFRESH_DEFAULT_SEC >= 300
    assert bot.DASHBOARD_PROACTIVE_REFRESH_MIN_SEC >= 120
    from pathlib import Path
    src = (Path(bot.__file__)).read_text(encoding="utf-8")
    assert 'os.getenv("DASHBOARD_PROACTIVE_REFRESH_INTERVAL", "30")' not in src


def test_unlock_btc_dominance_pasa_por_el_cache_compartido(monkeypatch):
    from modules import unlock_monitor as um
    llamadas = []

    async def _g():
        llamadas.append(1)
        return {"btc_dominance": 59.4}
    monkeypatch.setattr(market, "coingecko_global", _g)
    assert asyncio.run(um.fetch_btc_dominance()) == pytest.approx(59.4)
    assert llamadas == [1]


# ═══════════════════════════════════════════════════════════════════════════
# 3. modules.hl_borrow_lend — forma vigente del endpoint + procedencia
# ═══════════════════════════════════════════════════════════════════════════

SPOT_META = {"tokens": [
    {"index": 0, "name": "USDC"}, {"index": 150, "name": "HYPE"},
    {"index": 197, "name": "UBTC"}, {"index": 268, "name": "USDH"},
]}
ALL_RESERVES = [
    [0, {"ltv": "0.0", "oraclePx": "1.0"}],
    [150, {"ltv": "0.65", "oraclePx": "78.9"}],
    [197, {"ltv": "0.5", "oraclePx": "80848.0"}],
    [268, {"ltv": "0.0"}],
]


@pytest.fixture
def fresh_ltv():
    hbl._cache.update(ts=0.0, ltv={})
    hbl._maint_cache.update(ts=0.0, maint={})
    hbl._set_provenance("none", error=None, fetched_at=0.0)
    yield
    hbl._cache.update(ts=0.0, ltv={})
    hbl._set_provenance("none", error=None, fetched_at=0.0)


def _fake_hl(payloads: list[dict]):
    def _post(payload):
        payloads.append(dict(payload))
        if payload.get("type") == "spotMeta":
            return SPOT_META
        if payload == hbl.ALL_RESERVES_REQUEST:
            return ALL_RESERVES
        if payload.get("type") == "borrowLendReserveState":
            if "token" not in payload:
                raise RuntimeError("HTTP Error 422: Unprocessable Entity")
            for idx, st in ALL_RESERVES:
                if idx == payload["token"]:
                    return st
        raise RuntimeError(f"unexpected {payload}")
    return _post


def test_el_mapa_ltv_usa_allBorrowLendReserveStates_y_resuelve_nombres_por_spotMeta(monkeypatch, fresh_ltv):
    sent: list[dict] = []
    monkeypatch.setattr(hbl, "_post", _fake_hl(sent))
    m = hbl.get_collateral_ltv_map(force=True)
    assert m == {"HYPE": 0.65, "UBTC": 0.5}, m
    tipos = [p.get("type") for p in sent]
    assert "allBorrowLendReserveStates" in tipos, tipos
    # La forma vieja (sin token) no se manda nunca mas.
    assert not any(p == {"type": "borrowLendReserveState"} for p in sent), sent


def test_la_forma_vieja_del_endpoint_da_422_y_el_parser_nuevo_no_la_necesita(monkeypatch, fresh_ltv):
    sent: list[dict] = []
    monkeypatch.setattr(hbl, "_post", _fake_hl(sent))
    with pytest.raises(RuntimeError, match="422"):
        hbl._post({"type": "borrowLendReserveState"})
    assert hbl.get_reserve_state(150) == {"ltv": "0.65", "oraclePx": "78.9"}


def test_extract_ltv_map_lee_pares_indice_estado():
    idx = hbl.build_token_index_map(SPOT_META)
    assert idx[150] == "HYPE"
    out = hbl._extract_ltv_map(ALL_RESERVES, idx)
    assert out == {"HYPE": 0.65, "UBTC": 0.5}
    # Sin mapa de indices no se adivina ningun ticker.
    assert hbl._extract_ltv_map(ALL_RESERVES, {}) == {}
    # La forma legacy (dicts con coin) sigue aceptada.
    assert hbl._extract_ltv_map([{"coin": "HYPE", "ltv": 0.65}]) == {"HYPE": 0.65}


def test_exito_marca_procedencia_hl_live(monkeypatch, fresh_ltv):
    monkeypatch.setattr(hbl, "_post", _fake_hl([]))
    hbl.get_collateral_ltv_map(force=True)
    p = hbl.ltv_map_provenance()
    assert p["source"] == "hl_live" and p["error"] is None
    assert hbl.ltv_source_label() == "HL live"


def test_fallo_del_endpoint_se_declara_en_health_registry_y_en_la_procedencia(monkeypatch, fresh_ltv):
    marcas: list[str] = []
    monkeypatch.setattr(health_registry, "swallowed",
                        lambda sub, note="": marcas.append(f"{sub}:{note}"))

    def _boom(payload):
        raise RuntimeError("HTTP Error 422: Unprocessable Entity")
    monkeypatch.setattr(hbl, "_post", _boom)
    assert hbl.get_collateral_ltv_map(force=True) == {}
    assert any(m.startswith("pm_state:") and "422" in m for m in marcas), marcas
    p = hbl.ltv_map_provenance()
    assert p["source"] == "none" and "422" in (p["error"] or "")
    assert hbl.ltv_source_label().startswith("CONFIG")


def test_200_sin_ltv_usable_no_es_exito(monkeypatch, fresh_ltv):
    marcas: list[str] = []
    monkeypatch.setattr(health_registry, "swallowed",
                        lambda sub, note="": marcas.append(sub))

    def _post(payload):
        if payload.get("type") == "spotMeta":
            return SPOT_META
        return [[0, {"ltv": "0.0"}]]
    monkeypatch.setattr(hbl, "_post", _post)
    assert hbl.get_collateral_ltv_map(force=True) == {}
    assert "pm_state" in marcas
    assert hbl.ltv_map_provenance()["source"] == "none"


def test_cache_vieja_tras_fallo_se_etiqueta_hl_cache_con_edad(monkeypatch, fresh_ltv):
    monkeypatch.setattr(hbl, "_post", _fake_hl([]))
    hbl.get_collateral_ltv_map(force=True)
    hbl._provenance["fetched_at"] = time.time() - 7 * 60

    def _boom(payload):
        raise RuntimeError("timeout")
    monkeypatch.setattr(hbl, "_post", _boom)
    monkeypatch.setattr(health_registry, "swallowed", lambda *a, **k: None)
    m = hbl.get_collateral_ltv_map(force=True)
    assert m == {"HYPE": 0.65, "UBTC": 0.5}
    assert hbl.ltv_map_provenance()["source"] == "hl_cache"
    assert hbl.ltv_source_label() == "HL cache 7min"


# ═══════════════════════════════════════════════════════════════════════════
# 4. portfolio_margin — el panel dice de donde salio el LTV
# ═══════════════════════════════════════════════════════════════════════════

def _pm(ltv_map):
    from modules.portfolio_margin import compute_pm_state
    return compute_pm_state(
        [{"coin": "HYPE", "total": "3006.28"}, {"coin": "USDC", "total": "-91600"}],
        [], {"HYPE": 40.0}, ltv_map=ltv_map,
    )


def test_pm_state_con_mapa_vivo_dice_hl_live(monkeypatch, fresh_ltv):
    monkeypatch.setattr(hbl, "_post", _fake_hl([]))
    m = hbl.get_collateral_ltv_map(force=True)
    pm = _pm(m)
    assert pm.ltv_source == "HL live"
    assert pm.max_ltv == pytest.approx(0.65)
    assert pm.liq_threshold == pytest.approx(0.825)


def test_pm_state_sin_mapa_dice_config_y_por_que(monkeypatch, fresh_ltv):
    monkeypatch.setattr(health_registry, "swallowed", lambda *a, **k: None)

    def _boom(payload):
        raise RuntimeError("HTTP Error 422: Unprocessable Entity")
    monkeypatch.setattr(hbl, "_post", _boom)
    m = hbl.get_collateral_ltv_map(force=True)
    pm = _pm(m)
    assert pm.ltv_source.startswith("CONFIG")
    assert "422" in pm.ltv_source
    # El numero puede coincidir (0.65 = 0.65) — la ETIQUETA es la que no.
    assert pm.max_ltv == pytest.approx(0.65)


def test_el_render_pm_imprime_la_procedencia_del_ltv(monkeypatch, fresh_ltv):
    from modules.portfolio_margin import format_pm_state_telegram as render_pm_section
    monkeypatch.setattr(hbl, "_post", _fake_hl([]))
    pm = _pm(hbl.get_collateral_ltv_map(force=True))
    txt = render_pm_section(pm)
    assert "LTV 0.65 · HL live" in txt, txt


def test_la_escalera_con_ltv_vivo_reproduce_el_pin_de_hl(monkeypatch, fresh_ltv):
    """Pin del 22-sep: HL app 37.595 vs bot 37.61 con LTV 0.65 (factor 0.825).
    Misma formula, mismo LTV: partial = debt / (qty × 0.825)."""
    from modules.portfolio_margin import compute_borrow_liq_ladder
    monkeypatch.setattr(hbl, "_post", _fake_hl([]))
    ltv = hbl.get_collateral_ltv_map(force=True)["HYPE"]
    lad = compute_borrow_liq_ladder(91_600.0, 3006.28, ltv)
    assert lad["partial_factor"] == pytest.approx(0.825)
    assert lad["partial_price"] == pytest.approx(91_600.0 / (3006.28 * 0.825), rel=1e-9)
