"""R-PM-LIQ — keyless HyperLiquid borrow-lend reserve LTV source (sync, cached).

WHY THIS EXISTS
---------------
The Portfolio Margin borrow capacity depends on each collateral asset's live
max-borrow LTV. R-LTV65-QUIET: the MAINTENANCE threshold is a SEPARATE
parameter (``PM_MAINT_LTV`` = 0.75) and is NO LONGER derived from the borrow
LTV — HL raised HYPE max-borrow 0.50 → 0.65 (2026-08-11) WITHOUT changing
maint. LTVs are NOT constants the bot should hardcode — HyperLiquid can
re-risk a reserve. The borrow-lend info endpoints are keyless and read-only
and report each reserve's live ``ltv``, so we pull it best-effort and the
caller falls back to the config default (``PM_MAX_BORROW_LTV`` = 0.65) only
when the API is unreachable — and SAYS SO (see ``ltv_map_provenance``).

R-LIVE-FIX (2026-10-15) — POR QUE ESTE ARCHIVO CAMBIO
=====================================================
Produccion devolvia ``HTTP Error 422: Unprocessable Entity`` en CADA llamada
desde hacia semanas. HyperLiquid cambio la forma del pedido:

* ``{"type": "borrowLendReserveState"}`` sin parametros YA NO EXISTE. El
  endpoint ahora exige ``"token": <indice spot>`` y devuelve UN solo reserve.
* El listado completo paso a ``{"type": "allBorrowLendReserveStates"}``, que
  devuelve ``[[token_index, state], ...]`` — pares, no dicts con ``coin``.

El parser viejo ("acepta lista o dict con reserves/tokens") no podia leer la
forma nueva aunque el pedido hubiera sido correcto: cada fila es una lista de
dos elementos, sin nombre de token adentro. El nombre sale de ``spotMeta``
(``tokens[].index`` → ``tokens[].name``), que ya estaba cacheado en el bot.

Y lo mas importante: la falla era SILENCIOSA en el panel. ``get_collateral_ltv_map``
devolvia ``{}`` y ``compute_pm_state`` seguia con ``PM_MAX_BORROW_LTV`` de
config. El numero coincidia (0.65 vivo = 0.65 config) asi que nadie lo vio,
pero la escalera de liquidacion — money path — se imprimia como medida cuando
era un default. Ahora:

* la degradacion se declara en ``health_registry`` (subsistema ``pm_state``),
  asi que /diagnostico y /health la muestran;
* ``ltv_map_provenance()`` dice de donde salio el mapa (``hl_live`` /
  ``hl_cache`` con edad / ``none`` con el error) y el panel PM lo imprime al
  lado del LTV. Un 0.65 de config se ve distinto de un 0.65 medido.

Contract
--------
* **Sync + keyless.** Mirrors ``modules.hl_prices`` (shared hl_client).
* **Never raises.** On any failure returns ``{}`` or the last-good cache
  (callers default per-token) and records provenance + degradation.
* **Cached 5 min** in-memory (no browser storage, no disk, no secrets).
* Returns ``{COIN_UPPER: ltv_float}`` for every reserve that reports a usable
  LTV in ``(0, 1)``. USDC/USDH report ``ltv: "0.0"`` (they are the BORROWED
  side, not collateral) and are intentionally excluded.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any

from modules import health_registry

try:
    from config import HYPERLIQUID_API
except Exception:  # noqa: BLE001 — keep importable in isolated tests
    HYPERLIQUID_API = "https://api.hyperliquid.xyz"

log = logging.getLogger(__name__)

_INFO_URL = f"{HYPERLIQUID_API}/info"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_HTTP_TIMEOUT_SEC = 8.0
_CACHE_TTL_SEC = 300.0

# Forma vigente del endpoint (verificada contra api.hyperliquid.xyz el
# 2026-10-15): listado completo = pares [token_index, state].
ALL_RESERVES_REQUEST: dict[str, Any] = {"type": "allBorrowLendReserveStates"}
# Forma vigente del endpoint individual: EXIGE ``token`` (indice spot).
SINGLE_RESERVE_TYPE = "borrowLendReserveState"

_cache: dict[str, Any] = {"ts": 0.0, "ltv": {}}
# Procedencia del ultimo mapa servido. Es lo que el panel imprime al lado del
# LTV para que un default de config nunca se lea como una medicion.
_provenance: dict[str, Any] = {
    "source": "none",       # hl_live | hl_cache | none
    "fetched_at": 0.0,      # epoch de la ultima lectura viva exitosa
    "error": None,          # ultimo error (str) cuando source != hl_live
}


def _post(payload: dict) -> Any:
    # R-BOT-DEFINITIVE WI-4: shared rate-limited + cached HL client first.
    # R-BOT-DEFINITIVE clase C2: el fallback urllib de abajo NO pasa por el
    # rate limiter compartido de hl_client. Activarlo en silencio lleva a 429,
    # y un 429 tragado es lo que produjo funding 0.00. Se conserva el fallback
    # pero se declara la degradacion en vez de pasar de largo.
    try:
        from modules.hl_client import post_info_sync
    except ImportError:  # pragma: no cover
        health_registry.swallowed(
            "pm_state", "hl_client no importable; urllib SIN rate limiter")
    else:
        return post_info_sync(payload)
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        _INFO_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": _UA},
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as r:
        return json.load(r)


def _safe_float(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f == f else None  # reject NaN
    except (TypeError, ValueError):
        return None


def build_token_index_map(spot_meta: Any) -> dict[int, str]:
    """``spotMeta`` → ``{token_index: TOKEN_NAME_UPPER}``. Pure. NEVER raises."""
    out: dict[int, str] = {}
    if not isinstance(spot_meta, dict):
        return out
    for tok in spot_meta.get("tokens") or []:
        if not isinstance(tok, dict):
            continue
        try:
            idx = int(tok.get("index"))
        except (TypeError, ValueError):
            continue
        name = str(tok.get("name") or "").upper().strip()
        if name:
            out[idx] = name
    return out


def _token_index_map() -> dict[int, str]:
    """Live ``spotMeta`` index→name map via the shared client (1h TTL)."""
    meta = _post({"type": "spotMeta"})
    return build_token_index_map(meta)


def _read_ltv_from_state(state: Any) -> float | None:
    if not isinstance(state, dict):
        return None
    for k in ("ltv", "maxLtv", "maxLTV", "LTV"):
        v = _safe_float(state.get(k))
        if v is not None:
            return v
    if isinstance(state.get("state"), dict):
        return _read_ltv_from_state(state["state"])
    return None


def _extract_ltv_map(data: Any, index_map: dict[int, str] | None = None) -> dict[str, float]:
    """Parse the reserve-state payload into ``{COIN: ltv}``. NEVER raises.

    Forma vigente (``allBorrowLendReserveStates``): ``[[token_index, state],
    ...]`` — el nombre del token NO viaja en el payload, se resuelve con
    ``index_map`` (de ``spotMeta``). Un indice sin nombre conocido se omite:
    adivinar el ticker de un colateral es exactamente el tipo de default que
    este modulo existe para no fabricar.

    Formas legacy (lista de dicts con ``coin``/``name``, o dict con
    ``reserves``/``tokens``) se siguen aceptando por si HL las devuelve.
    """
    out: dict[str, float] = {}
    index_map = index_map or {}
    rows: list[Any] = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in ("reserves", "tokens", "reserveStates", "data"):
            v = data.get(key)
            if isinstance(v, list):
                rows = v
                break
    for row in rows:
        name = ""
        state: Any = None
        if isinstance(row, (list, tuple)) and len(row) == 2:
            # Forma vigente: [token_index, state]
            try:
                idx = int(row[0])
            except (TypeError, ValueError):
                continue
            name = index_map.get(idx, "")
            state = row[1]
        elif isinstance(row, dict):
            name = str(
                row.get("coin") or row.get("name") or row.get("token")
                or row.get("symbol") or ""
            ).upper().strip()
            state = row
        if not name:
            continue
        ltv = _read_ltv_from_state(state)
        if ltv is not None and 0.0 < ltv < 1.0:
            out[name] = ltv
    return out


def _set_provenance(source: str, *, error: str | None = None,
                    fetched_at: float | None = None) -> None:
    _provenance["source"] = source
    _provenance["error"] = error
    if fetched_at is not None:
        _provenance["fetched_at"] = fetched_at


def ltv_map_provenance() -> dict[str, Any]:
    """Where the last served LTV map came from — for the PM panel.

    ``{"source": "hl_live"|"hl_cache"|"none", "age_s": float|None,
       "error": str|None}``. NEVER raises.
    """
    src = _provenance.get("source") or "none"
    fetched = float(_provenance.get("fetched_at") or 0.0)
    age = (time.time() - fetched) if fetched > 0 else None
    return {"source": src, "age_s": age, "error": _provenance.get("error")}


def ltv_source_label(coin: str = "HYPE") -> str:
    """Short human label for the panel: ``HL live`` / ``HL cache 7min`` /
    ``CONFIG — HL no leido (<error>)``. NEVER raises."""
    p = ltv_map_provenance()
    src = p["source"]
    if src == "hl_live":
        return "HL live"
    if src == "hl_cache":
        age = p["age_s"] or 0.0
        return f"HL cache {int(age // 60)}min"
    err = str(p.get("error") or "sin lectura")[:60]
    return f"CONFIG — HL no leido ({err})"


def get_collateral_ltv_map(force: bool = False) -> dict[str, float]:
    """Return ``{COIN: borrow_ltv}`` from HL ``allBorrowLendReserveStates``.

    Keyless, cached 5 min, NEVER raises. Returns ``{}`` (NOT a guess) on
    failure so callers apply their own conservative per-token default — and
    records the failure in ``health_registry`` (pm_state) + provenance so the
    default is never printed as a measurement.
    """
    now = time.time()
    if (
        not force
        and _cache["ltv"]
        and (now - _cache["ts"]) < _CACHE_TTL_SEC
    ):
        return dict(_cache["ltv"])

    try:
        index_map = _token_index_map()
        data = _post(dict(ALL_RESERVES_REQUEST))
        out = _extract_ltv_map(data, index_map)
        if out:
            _cache.update(ts=now, ltv=out)
            _set_provenance("hl_live", fetched_at=now)
            return dict(out)
        # 200 pero sin ningun reserve con LTV usable: no es un exito.
        raise ValueError(
            f"allBorrowLendReserveStates sin LTV usable "
            f"(filas={len(data) if isinstance(data, list) else '?'}, "
            f"tokens_resueltos={len(index_map)})")
    except Exception as e:  # noqa: BLE001 — robustness contract
        err = f"{type(e).__name__}: {e}"
        log.warning("hl_borrow_lend.get_collateral_ltv_map failed: %s", err)
        health_registry.swallowed("pm_state", f"get_collateral_ltv_map: {err[:120]}")
        if _cache["ltv"]:
            _set_provenance("hl_cache", error=err)
        else:
            _set_provenance("none", error=err)
        return dict(_cache["ltv"])


def get_reserve_state(token_index: int) -> dict[str, Any] | None:
    """Single reserve via ``borrowLendReserveState`` + ``token``. NEVER raises.

    Kept for callers that want one reserve's full state (rates, utilization,
    oraclePx). Returns ``None`` on failure.
    """
    try:
        data = _post({"type": SINGLE_RESERVE_TYPE, "token": int(token_index)})
        return data if isinstance(data, dict) else None
    except Exception as e:  # noqa: BLE001
        log.warning("hl_borrow_lend.get_reserve_state(%s) failed: %s", token_index, e)
        health_registry.swallowed("pm_state", f"get_reserve_state({token_index})")
        return None


# ------------------------------------------------------------------------
# R-LTV65-QUIET — maintenance-LTV live verification (best-effort).
# ------------------------------------------------------------------------
_maint_cache: dict[str, Any] = {"ts": 0.0, "maint": {}}

_MAINT_KEYS = ("maintenanceLtv", "maintLtv", "liquidationLtv", "liqThreshold",
               "maintenanceLTV", "liquidationThreshold")


def _extract_maint_map(data: Any, index_map: dict[int, str] | None = None) -> dict[str, float]:
    """Parse maintenance/liquidation LTV per reserve, if the payload carries it.

    HL does NOT publish a maintenance field today (verified 2026-10-15: the
    reserve state carries only ``ltv``), so this normally returns ``{}`` and
    the caller keeps the official formula ``LTV + (1-LTV)/2``. NEVER raises.
    """
    out: dict[str, float] = {}
    index_map = index_map or {}
    rows: list[Any] = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in ("reserves", "tokens", "reserveStates", "data"):
            v = data.get(key)
            if isinstance(v, list):
                rows = v
                break
    for row in rows:
        name = ""
        state: Any = None
        if isinstance(row, (list, tuple)) and len(row) == 2:
            try:
                name = index_map.get(int(row[0]), "")
            except (TypeError, ValueError):
                continue
            state = row[1]
        elif isinstance(row, dict):
            name = str(
                row.get("coin") or row.get("name") or row.get("token")
                or row.get("symbol") or ""
            ).upper().strip()
            state = row
        if not name or not isinstance(state, dict):
            continue
        m = None
        for k in _MAINT_KEYS:
            m = _safe_float(state.get(k))
            if m is not None:
                break
        if m is None and isinstance(state.get("state"), dict):
            for k in _MAINT_KEYS:
                m = _safe_float(state["state"].get(k))
                if m is not None:
                    break
        if m is not None and 0.0 < m < 1.0:
            out[name] = m
    return out


def get_maintenance_ltv_map(force: bool = False) -> dict[str, float]:
    """Return ``{COIN: maintenance_ltv}`` from HL, or ``{}`` if unverifiable.

    Keyless, cached 5 min, NEVER raises. When empty, callers keep the
    derived partial factor and /health surfaces that live maint verification
    is unavailable. An empty map here is EXPECTED (HL publishes no maint
    field) and is therefore not a degradation.
    """
    now = time.time()
    if (
        not force
        and _maint_cache["maint"]
        and (now - _maint_cache["ts"]) < _CACHE_TTL_SEC
    ):
        return dict(_maint_cache["maint"])
    try:
        index_map = _token_index_map()
        data = _post(dict(ALL_RESERVES_REQUEST))
        out = _extract_maint_map(data, index_map)
        if out:
            _maint_cache.update(ts=now, maint=out)
            return dict(out)
        return dict(_maint_cache["maint"])
    except Exception as e:  # noqa: BLE001 — robustness contract
        log.warning("hl_borrow_lend.get_maintenance_ltv_map failed: %s", e)
        return dict(_maint_cache["maint"])


def maint_ltv_verification_status() -> dict[str, Any]:
    """Status blob for /health: was live maint LTV verifiable for HYPE?

    ``{"verified": bool, "live_maint": float|None, "config_maint": float}``.
    NEVER raises.
    """
    # R-UNIFIED-LIQ: the config threshold is now DERIVED from the borrow LTV
    # (partial factor LTV+(1−LTV)/2) unless PM_MAINT_LTV manually overrides it.
    try:
        from config import PM_MAX_BORROW_LTV as _cfg_ltv
        from modules.portfolio_margin import _liq_threshold_for_ltv
        _cfg_maint = _liq_threshold_for_ltv(_cfg_ltv)
    except Exception:  # noqa: BLE001
        _cfg_maint = 0.825
    try:
        live = get_maintenance_ltv_map().get("HYPE")
    except Exception:  # noqa: BLE001
        live = None
    return {
        "verified": live is not None,
        "live_maint": live,
        "config_maint": float(_cfg_maint),
    }
