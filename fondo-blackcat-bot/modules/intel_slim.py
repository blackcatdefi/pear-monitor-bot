"""R-COST2 (2026-06-26): slim the R-INTEL30 payload before the FULL ANALYSIS.

The /reporte LLM context (``templates.formatters.compile_raw_data``) JSON-
serializes the ENTIRE ``merged_intel`` dict, including
``merged_intel["intel30"]`` — a nested dict of up-to-11 source payloads (FRED
macro, Farside ETF flows, ISW geopol, Arkham whales, ASXN, HypurrScan
auctions, BCRA / CriptoYa AR FX, EIA oil, Apollo Spark, HL info extras…).

Several of those *raw* ``fetch_all()`` payloads carry multi-row tables,
historical series or long transaction lists that the FULL ANALYSIS narrative
never quotes verbatim — it only needs the curated levels and headlines (VIX,
10Y, 2Y spread, DXY, SOFR, Fed funds, F&G; geopolitical headlines; ETF flow
nets; notable whale / smart-money moves; AR brecha summary).

Each intel30 source module already ships a ``format_for_telegram(payload)``
digest: the SAME compact, human-readable block surfaced to BCD in the
R-INTEL30 message. This helper replaces each raw source payload with that
digest text, dropping the raw series while preserving every signal the
analysis actually uses.

IMPORTANT — display is unaffected: the user-facing R-INTEL30 Telegram message
is built separately in ``bot.py`` from the UNTOUCHED ``intel30_payload`` (and
each module's ``format_for_telegram``), so the report the user reads stays
byte-identical. This helper only shrinks the copy that enters the LLM.

This mirrors ``modules.x_intel.slim_x_intel_for_llm`` (R-COST 2026-06-26),
which already slimmed the X timeline the same way.
"""
from __future__ import annotations

import importlib
import logging
from typing import Any

log = logging.getLogger(__name__)

# intel30 payload-key → source module that produced it (and owns the digest).
# Keys match bot.py's ``intel30_modules`` mapping fed into merged_intel.
_INTEL30_MODULES: dict[str, str] = {
    "hl_info": "modules.intel30.hl_info_api",
    "asxn": "modules.intel30.asxn_data",
    "hypurrscan": "modules.intel30.hypurrscan",
    "fred": "modules.intel30.fred_api",
    "farside_etfs": "modules.intel30.farside_etfs",
    "arkham": "modules.intel30.arkham_intel",
    "eia": "modules.intel30.eia_oil",
    "isw_ctp": "modules.intel30.isw_ctp",
    "criptoya_ar": "modules.intel30.criptoya_ar",
    "bcra": "modules.intel30.bcra_macro",
    "apollo_spark": "modules.intel30.apollo_spark",
}


def _digest_for(key: str, payload: Any) -> str | None:
    """Return the source module's compact telegram digest for ``payload``.

    ``None`` when there is no known module for ``key``, no
    ``format_for_telegram`` callable, the digest is empty (silent-skip sources
    like Arkham-without-key), or any error — callers keep the raw payload then.
    """
    mod_path = _INTEL30_MODULES.get(key)
    if not mod_path:
        return None
    mod = importlib.import_module(mod_path)
    fmt = getattr(mod, "format_for_telegram", None)
    if not callable(fmt):
        return None
    txt = fmt(payload)
    if isinstance(txt, str) and txt.strip():
        return txt.strip()
    return None


def slim_intel_for_llm(intel: Any) -> Any:
    """Return an LLM-only copy of ``merged_intel`` with intel30 slimmed.

    * Returns ``intel`` unchanged when it is not a dict.
    * Shallow-copies the top level so callers' originals are never mutated
      (``compile_raw_data`` pops ``bounce_tech`` off the dict it receives).
    * Rebuilds ``intel["intel30"]`` as a NEW dict where each source payload is
      replaced by ``{"_llm_digest": <telegram digest>}``; any source whose
      digest cannot be produced keeps its raw payload (graceful degrade).
    * All other intel keys (x_intel — already slimmed upstream —, gmail,
      cryexc, tradermap, bounce_tech, unread_scan, legacy intel…) pass through
      untouched.
    """
    if not isinstance(intel, dict):
        return intel

    slim: dict[str, Any] = dict(intel)  # shallow copy — never mutate caller's

    i30 = intel.get("intel30")
    if isinstance(i30, dict):
        slim_i30: dict[str, Any] = {}
        slimmed_keys: list[str] = []
        for key, payload in i30.items():
            digest: str | None = None
            try:
                digest = _digest_for(key, payload)
            except Exception:  # noqa: BLE001
                log.debug("intel_slim: digest for %s failed — keeping raw", key)
                digest = None
            if digest is not None:
                slim_i30[key] = {"_llm_digest": digest}
                slimmed_keys.append(key)
            else:
                slim_i30[key] = payload  # keep raw on any failure / empty
        if slimmed_keys:
            slim_i30["_llm_slim"] = True
            slim_i30["_llm_note"] = (
                "intel30 recortado para el LLM: cada fuente reemplazada por su "
                "digest compacto (mismos niveles/headlines que ve BCD en el "
                "mensaje R-INTEL30); series crudas/tablas históricas omitidas. "
                f"Fuentes recortadas: {', '.join(slimmed_keys)}."
            )
        slim["intel30"] = slim_i30

    return slim
