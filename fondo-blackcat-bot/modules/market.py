"""Market data aggregator: CoinGecko, Fear&Greed, CoinGlass, DefiLlama.

Round 7 additions:
    - Integrates `modules.coinglass.get_basket_oi_funding()` for the fund's
      basket (BTC/ETH/HYPE + SHORT basket WLD/STRK/AVAX/ZRO/ENA).
    - Exposes `coinglass_basket` key in fetch_market_data() so the raw-data
      payload to the LLM carries OI + funding across the basket.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from config import COINGLASS_API_KEY
from utils.http import get_json
from modules.coinglass import get_basket_oi_funding
from modules import health_registry
from utils.shape import UnexpectedShape, dig, require_mapping

log = logging.getLogger(__name__)

# ─── In-memory cache (TTL seconds) ───────────────────────────────────────────
_CACHE: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str, ttl: float) -> Any | None:
    item = _CACHE.get(key)
    if not item:
        return None
    ts, value = item
    if time.time() - ts > ttl:
        return None
    return value


def _cache_set(key: str, value: Any) -> None:
    _CACHE[key] = (time.time(), value)


# ─── CoinGecko ──────────────────────────────────────────────────────────────
# R-LIVE-FIX (2026-10-15) — POR QUE ESTA SECCION CAMBIO
# ---------------------------------------------------------------------------
# Produccion: ``simple/price`` y ``/global`` cada ~60s, 3 reintentos cada una,
# 429 en todas, "market" degradado de corrido durante horas. Tres causas, las
# tres de este archivo o de quien lo llama:
#
# 1. El refresco proactivo del dashboard (bot.py, ``dashboard_snapshot_refresh``)
#    corria cada 30s con ``force_refresh=True`` y arrastraba
#    ``fetch_market_data()`` → CoinGecko en cada vuelta. Precios para un
#    reporte on-demand no necesitan 30s; el job pasa a 5 min.
# 2. El fallo NO se cacheaba: ``coingecko_prices`` solo guardaba exitos, asi
#    que cada 429 garantizaba otro intento en la vuelta siguiente. Un 429 que
#    produce mas pedidos es un lazo de realimentacion, no un reintento.
# 3. CoinGecko era la fuente PRIMARIA de BTC/ETH/SOL/HYPE cuando el bot ya
#    trae el oraculo de HyperLiquid para todo lo que HL lista. Los precios del
#    fondo SON los de HL (es lo que HL usa para valuar el colateral PM);
#    CoinGecko queda SOLO para lo que HL no tiene (SILVER) y para el
#    enriquecimiento (market cap, dominancia, 24h change de lo no listado).
#
# Lo que NO cambia: una falla real sigue declarandose. Si despues de HL +
# CoinGecko falta el precio de un activo core, ``health_registry`` lo anota.
# Lo que SI cambia: un CoinGecko en castigo ya no degrada "market" cuando HL
# cubrio todos los precios core — porque el dato que importa SI se leyo.
COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
COINGECKO_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"

_GECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "HYPE": "hyperliquid",
    "GOLD": "pax-gold",
    "SILVER": "kinesis-silver",
    "PAXG": "pax-gold",
}

# Simbolo del reporte → simbolo del oraculo HL (perp). GOLD se valua con PAXG
# (mismo id en CoinGecko: pax-gold). SILVER no esta en HL → CoinGecko.
_HL_ALIAS = {"GOLD": "PAXG"}
# Activos cuyo precio es money path: si falta en HL Y en CoinGecko, degrada.
_CORE_PRICE_SYMBOLS = ("BTC", "ETH", "SOL", "HYPE")

# TTLs. Los precios HL se refrescan solos (hl_prices cachea 45s); lo que se
# cachea aca es el ENSAMBLE. CoinGecko keyless: 10 min para precios, 15 min
# para /global. Un reporte on-demand no distingue un market cap de hace 8 min.
PRICES_TTL_SEC = 300
GECKO_PRICES_TTL_SEC = 600
GECKO_GLOBAL_TTL_SEC = 900
# Cache NEGATIVO: tras un fallo de CoinGecko no se vuelve a pedir antes de esto
# (ademas del cooldown por host de utils.http, que puede ser mayor).
GECKO_FAIL_TTL_SEC = 120


def _hl_oracle_map() -> dict[str, float]:
    """Oraculo HL ``{COIN: px}`` via modules.hl_prices (sync, cacheado 45s).
    ``{}`` si no se pudo leer — el llamador decide si eso es degradacion."""
    try:
        from modules.hl_prices import get_oracle_prices
        return get_oracle_prices() or {}
    except Exception as exc:  # noqa: BLE001
        health_registry.swallowed("market", "hl_oracle_map")
        log.warning("hl oracle map failed: %s", exc)
        return {}


def _hl_prev_day_map() -> dict[str, float]:
    """``{COIN: prevDayPx}`` del mismo payload HL (para el 24h change)."""
    try:
        from modules.hl_prices import get_prev_day_prices
        return get_prev_day_prices() or {}
    except Exception:  # noqa: BLE001 — enriquecimiento, no money path
        return {}


def _gecko_cooldown_note() -> str | None:
    try:
        from utils.http import cooldown_remaining, cooldown_strikes
        rem = cooldown_remaining(COINGECKO_PRICE_URL)
        if rem > 0:
            return f"coingecko en cooldown {rem:.0f}s ({cooldown_strikes(COINGECKO_PRICE_URL)}x 429)"
    except Exception:  # noqa: BLE001
        return None
    return None


async def _coingecko_simple_price(gids: list[str]) -> dict[str, Any]:
    """Pide SOLO ``gids`` a CoinGecko. Cache 10 min por conjunto de ids,
    cache negativo 2 min. Levanta si no pudo leer (el llamador decide)."""
    key = "gecko_prices:" + ",".join(sorted(gids))
    cached = _cache_get(key, GECKO_PRICES_TTL_SEC)
    if cached is not None:
        return cached
    fail_key = key + ":fail"
    if _cache_get(fail_key, GECKO_FAIL_TTL_SEC) is not None:
        raise RuntimeError(
            f"coingecko/simple/price: fallo hace <{GECKO_FAIL_TTL_SEC}s, no se insiste")
    params = {
        "ids": ",".join(sorted(set(gids))),
        "vs_currencies": "usd",
        "include_24hr_change": "true",
        "include_market_cap": "true",
    }
    try:
        data = require_mapping(await get_json(COINGECKO_PRICE_URL, params=params,
                                              max_retries=1),
                               source="coingecko/simple/price")
        # C4: CoinGecko responde 200 con {"status":{"error_code":429}} cuando
        # rate-limitea. Sin esta guarda ningun gid aparecia, out quedaba {} y el
        # reporte imprimia "sin precios" en vez de "no pude leer precios".
        if not any(g in data for g in gids):
            raise UnexpectedShape(
                "coingecko/simple/price: 200 sin ninguno de los ids pedidos "
                f"(claves={sorted(data)[:6]})")
    except Exception:
        _cache_set(fail_key, True)
        raise
    _cache_set(key, data)
    return data


async def coingecko_prices() -> dict[str, Any]:
    """Precios del reporte: ``{SYM: {price_usd, change_24h, market_cap, source}}``.

    Orden de fuentes (R-LIVE-FIX):
      1. Oraculo HL para todo simbolo que HL lista (BTC/ETH/SOL/HYPE/PAXG/GOLD).
         ``source="hl_oracle"``; 24h change derivado de ``prevDayPx``.
      2. CoinGecko SOLO para lo que falto en HL (SILVER) y, best-effort, para
         ``market_cap`` de los que HL ya precio. Si CoinGecko esta en castigo
         y HL cubrio los core, NO es degradacion.
    El nombre se conserva por los 30+ llamadores; la forma de salida tambien.
    """
    cached = _cache_get("gecko_prices", PRICES_TTL_SEC)
    if cached is not None:
        return cached

    out: dict[str, Any] = {}
    oracle = _hl_oracle_map()
    prev = _hl_prev_day_map() if oracle else {}
    for sym in _GECKO_IDS:
        hl_sym = _HL_ALIAS.get(sym, sym)
        px = oracle.get(hl_sym)
        try:
            pxf = float(px) if px is not None else 0.0
        except (TypeError, ValueError):
            pxf = 0.0
        if pxf > 0:
            chg = None
            pd = prev.get(hl_sym)
            try:
                if pd and float(pd) > 0:
                    chg = (pxf / float(pd) - 1.0) * 100.0
            except (TypeError, ValueError):
                chg = None
            out[sym] = {"price_usd": pxf, "change_24h": chg,
                        "market_cap": None, "source": "hl_oracle"}

    missing_syms = [s for s in _GECKO_IDS if s not in out]
    # ids que faltan + ids de los ya precia­dos (solo para market_cap).
    want_gids = sorted({_GECKO_IDS[s] for s in _GECKO_IDS})
    gecko: dict[str, Any] = {}
    gecko_err: str | None = None
    try:
        gecko = await _coingecko_simple_price(want_gids)
    except Exception as exc:  # noqa: BLE001
        gecko_err = str(exc)
        log.warning("CoinGecko prices unavailable (%s): %s",
                    _gecko_cooldown_note() or "fallo", exc)

    for sym, gid in _GECKO_IDS.items():
        entry = gecko.get(gid) or {}
        if not isinstance(entry, dict) or not entry:
            continue
        if sym in out:
            # HL ya puso el precio; CoinGecko solo enriquece.
            out[sym]["market_cap"] = entry.get("usd_market_cap")
            if out[sym].get("change_24h") is None:
                out[sym]["change_24h"] = entry.get("usd_24h_change")
        else:
            out[sym] = {
                "price_usd": entry.get("usd"),
                "change_24h": entry.get("usd_24h_change"),
                "market_cap": entry.get("usd_market_cap"),
                "source": "coingecko",
            }

    core_missing = [s for s in _CORE_PRICE_SYMBOLS if not (out.get(s) or {}).get("price_usd")]
    if core_missing:
        # Esto SI es money path: sin precio core no hay valuacion honesta.
        health_registry.swallowed(
            "market",
            f"coingecko_prices: sin precio para {','.join(core_missing)} "
            f"(HL oracle={'ok' if oracle else 'vacio'}; coingecko={gecko_err or 'ok'})")
        if not out:
            return {}
    if gecko_err:
        # Constancia (no dentro del mapa de precios: los renders iteran
        # ``prices.items()`` esperando entradas con price_usd).
        _cache_set("gecko_note", {"error": gecko_err[:160],
                                  "missing_after_hl": missing_syms,
                                  "cooldown": _gecko_cooldown_note()})
    else:
        _CACHE.pop("gecko_note", None)
    _cache_set("gecko_prices", out)
    return out


def coingecko_note() -> dict[str, Any] | None:
    """Ultima constancia de CoinGecko indisponible (o None si leyo bien)."""
    return _cache_get("gecko_note", PRICES_TTL_SEC)


async def coingecko_global() -> dict[str, Any]:
    """Market cap total / dominancia BTC-ETH. Contexto, no money path.

    Cache 15 min + cache negativo 2 min. Si CoinGecko esta en castigo se
    devuelve ``{"unavailable": <motivo>}`` en vez de ``{}``: el render muestra
    n/d CON la razon, y "market" NO se degrada por un enriquecimiento ausente.
    """
    cached = _cache_get("gecko_global", GECKO_GLOBAL_TTL_SEC)
    if cached is not None:
        return cached
    if _cache_get("gecko_global:fail", GECKO_FAIL_TTL_SEC) is not None:
        return {"unavailable": _gecko_cooldown_note()
                or f"coingecko/global fallo hace <{GECKO_FAIL_TTL_SEC}s"}
    try:
        data = await get_json(COINGECKO_GLOBAL_URL, max_retries=1)
        # C4: sin la clave 'data' esto quedaba en {} y devolvia dominancia BTC
        # None, que el render dibuja como n/d indistinguible de un fallo real.
        d = dig(data, "data", source="coingecko/global", expect=dict)
        out = {
            "total_market_cap_usd": (d.get("total_market_cap") or {}).get("usd"),
            "total_volume_usd": (d.get("total_volume") or {}).get("usd"),
            "btc_dominance": (d.get("market_cap_percentage") or {}).get("btc"),
            "eth_dominance": (d.get("market_cap_percentage") or {}).get("eth"),
            "market_cap_change_24h": d.get("market_cap_change_percentage_24h_usd"),
        }
        _cache_set("gecko_global", out)
        return out
    except Exception as exc:  # noqa: BLE001 — contexto; se declara, no se traga
        _cache_set("gecko_global:fail", True)
        log.warning("CoinGecko global unavailable (%s): %s",
                    _gecko_cooldown_note() or "fallo", exc)
        return {"unavailable": f"{_gecko_cooldown_note() or 'coingecko/global'}: {str(exc)[:120]}"}


# ─── Fear & Greed ───────────────────────────────────────────────────────────
FNG_URL = "https://api.alternative.me/fng/?limit=30"


async def fear_greed() -> dict[str, Any]:
    cached = _cache_get("fng", 600)
    if cached is not None:
        return cached
    try:
        data = await get_json(FNG_URL)
        # C4: alternative.me responde 200 con {"metadata":{"error":...}}. Antes
        # eso daba items=[] y un `return {}` que el reporte leia como "sin
        # Fear&Greed hoy". Ahora la forma mala levanta; la lista vacia legitima
        # (que la API no produce nunca, pero podria) se sigue respetando.
        items = dig(data, "data", source="alternative.me/fng", expect=list)
        if not items:
            return {}
        latest = items[0]
        result = {
            "value": int(latest.get("value", 0)),
            "classification": latest.get("value_classification"),
            "timestamp": latest.get("timestamp"),
            "yesterday": int(items[1]["value"]) if len(items) > 1 else None,
            "week_ago": int(items[7]["value"]) if len(items) > 7 else None,
            "month_ago": int(items[29]["value"]) if len(items) > 29 else None,
        }
        _cache_set("fng", result)
        return result
    except Exception as exc:  # noqa: BLE001
        health_registry.swallowed("market", "fear_greed")
        log.warning("Fear&Greed failed: %s", exc)
        return {}


# ─── CoinGlass (v3 legacy endpoints, BTC-only) ──────────────────────────────
COINGLASS_BASE = "https://open-api-v3.coinglass.com/api"


async def _coinglass_get(path: str, params: dict[str, Any] | None = None) -> Any:
    if not COINGLASS_API_KEY:
        return None
    headers = {"CG-API-KEY": COINGLASS_API_KEY, "accept": "application/json"}
    try:
        return await get_json(f"{COINGLASS_BASE}{path}", headers=headers, params=params or {})
    except Exception as exc:  # noqa: BLE001
        health_registry.swallowed("market", "_coinglass_get")
        log.warning("CoinGlass %s failed: %s", path, exc)
        return None


async def coinglass_metrics() -> dict[str, Any]:
    """Best-effort CoinGlass aggregate (OI, liqs, funding, long/short)."""
    cached = _cache_get("coinglass", 300)
    if cached is not None:
        return cached
    if not COINGLASS_API_KEY:
        return {"available": False, "reason": "no_api_key"}
    out: dict[str, Any] = {"available": True}
    try:
        oi = await _coinglass_get("/futures/openInterest", {"symbol": "BTC"})
        liqs = await _coinglass_get("/futures/liquidation/v2/history", {"symbol": "BTC", "interval": "h1"})
        funding = await _coinglass_get("/futures/fundingRate", {"symbol": "BTC"})
        ls = await _coinglass_get("/futures/longShortRatio", {"symbol": "BTC", "interval": "h4"})
        out.update({
            "open_interest": oi,
            "liquidations_24h": liqs,
            "funding_btc": funding,
            "long_short_ratio_btc": ls,
        })
        _cache_set("coinglass", out)
    except Exception as exc:  # noqa: BLE001
        log.warning("CoinGlass aggregate failed: %s", exc)
        out["error"] = str(exc)
    return out


async def coinglass_basket() -> dict[str, Any]:
    """Round 7: OI + funding across the fund's basket via Coinglass v4."""
    cached = _cache_get("coinglass_basket", 300)
    if cached is not None:
        return cached
    try:
        result = await get_basket_oi_funding()
        _cache_set("coinglass_basket", result)
        return result
    except Exception as exc:  # noqa: BLE001
        log.warning("coinglass_basket failed: %s", exc)
        return {"available": False, "reason": str(exc), "basket": []}


# ─── DefiLlama ──────────────────────────────────────────────────────────────
LLAMA_PROTOCOLS = "https://api.llama.fi/protocols"
LLAMA_FEES = "https://api.llama.fi/overview/fees?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true"
LLAMA_STABLES = "https://stablecoins.llama.fi/stablecoins?includePrices=true"


async def defillama_top_fees(top_n: int = 10) -> list[dict[str, Any]]:
    cached = _cache_get(f"llama_fees_{top_n}", 1800)
    if cached is not None:
        return cached
    try:
        data = await get_json(LLAMA_FEES)
        protos = dig(data, "protocols", source="defillama/fees", expect=list)
        protos = sorted(protos, key=lambda p: p.get("total24h") or 0, reverse=True)[:top_n]
        out = [
            {
                "name": p.get("name"),
                "category": p.get("category"),
                "fees_24h": p.get("total24h"),
                "fees_7d": p.get("total7d"),
                "revenue_24h": p.get("revenue24h"),
            }
            for p in protos
        ]
        _cache_set(f"llama_fees_{top_n}", out)
        return out
    except Exception as exc:  # noqa: BLE001
        health_registry.swallowed("market", "defillama_top_fees")
        log.warning("DefiLlama fees failed: %s", exc)
        return []


async def defillama_stablecoin_supply() -> dict[str, Any]:
    cached = _cache_get("llama_stables", 1800)
    if cached is not None:
        return cached
    try:
        data = await get_json(LLAMA_STABLES)
        coins = dig(data, "peggedAssets", source="defillama/stablecoins",
                    expect=list)
        total = 0.0
        usdt = usdc = 0.0
        for c in coins:
            circ = (c.get("circulating") or {}).get("peggedUSD") or 0
            total += circ
            sym = (c.get("symbol") or "").upper()
            if sym == "USDT":
                usdt = circ
            elif sym == "USDC":
                usdc = circ
        out = {"total_supply_usd": total, "usdt": usdt, "usdc": usdc}
        _cache_set("llama_stables", out)
        return out
    except Exception as exc:  # noqa: BLE001
        health_registry.swallowed("market", "defillama_stablecoin_supply")
        log.warning("DefiLlama stables failed: %s", exc)
        return {}


# ─── Aggregate ──────────────────────────────────────────────────────────────
@health_registry.tracked("market", "fetch_market_data")
async def fetch_market_data() -> dict[str, Any]:
    prices, glob, fng, cg, cg_basket, fees, stables = await asyncio.gather(
        coingecko_prices(),
        coingecko_global(),
        fear_greed(),
        coinglass_metrics(),
        coinglass_basket(),
        defillama_top_fees(),
        defillama_stablecoin_supply(),
    )
    return {
        "status": "ok",
        "data": {
            "prices": prices,
            "global": glob,
            "fear_greed": fng,
            "coinglass": cg,
            "coinglass_basket": cg_basket,
            "top_fees": fees,
            "stablecoins": stables,
            # R-LIVE-FIX: de donde salieron los precios y si CoinGecko
            # (enriquecimiento) estaba disponible. Nunca un n/d mudo.
            "price_sources": {k: v.get("source") for k, v in prices.items()
                              if isinstance(v, dict)},
            "coingecko_note": coingecko_note(),
        },
    }
