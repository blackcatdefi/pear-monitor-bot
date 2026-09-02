"""R-PEAR-ASSET-INTEGRATION — real HYPE acquisition metrics (kill junk basis).

The reports used to print a "cost basis" for the fund's HYPE core derived from
HyperCore ``entryNtl`` — a figure that is **0.0 for the migrated/bridged HYPE
balance** and otherwise fluctuates, so it never represented the real
acquisition price (it read as junk like "$8.46 / $11.67"). This module derives
the two metrics BCD actually wants, FROM REAL FILL HISTORY, and refuses to
print a number it can't stand behind:

  (a) **PPC contable** — weighted-average BUY price. Buys move it, sells do not.
  (b) **Precio neto de adquisición** — net of buys minus sells:
        (Σ buy_notional − Σ sell_notional) / (Σ buy_qty − Σ sell_qty)

Reliability gate (NEVER fabricate)
----------------------------------
HyperLiquid ``userFills`` returns only the most recent ~2000 fills, and a
bridged/migrated HYPE balance has NO buy fill on HL at all. Either case means
the fills do NOT explain the current on-chain balance, so a PPC computed from
them would be wrong. We therefore RECONCILE the net bought quantity against the
live HYPE spot balance and return ``known=False`` (→ "n/d") when:

  * no HYPE spot fills are found, or
  * the fill page is truncated (hit the 2000 cap) and unexplained, or
  * |net_filled_qty − on_chain_balance| / balance exceeds the tolerance.

Only when the fills fully reconcile with the balance do we surface real
numbers. Read-only, keyless, routed through the shared HL info client. NEVER
raises.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any
from modules import health_registry

log = logging.getLogger(__name__)

try:
    from config import PEAR_STAKING_WALLETS as _CFG_WALLETS  # reuse fund wallets
    from config import PM_PRIMARY_WALLET
except Exception:  # noqa: BLE001
    PM_PRIMARY_WALLET = "0xc7ae23316b47f7e75f455f53ad37873a18351505"
    _CFG_WALLETS = [PM_PRIMARY_WALLET]

# Reconciliation tolerance: fills must explain the balance within this fraction.
_RECONCILE_TOL = float(os.getenv("HYPE_ACQ_RECONCILE_TOL", "0.05") or 0.05)
# HL userFills hard cap — a full page strongly implies truncated history.
_FILLS_CAP = int(os.getenv("HYPE_ACQ_FILLS_CAP", "2000") or 2000)
# R-EQUITY-DEDUP-DREAMCASH: pagination depth for userFillsByTime (each page is
# up to _FILLS_CAP fills → 10 pages ≈ 20K fills, far beyond fund history).
_FILLS_MAX_PAGES = int(os.getenv("HYPE_ACQ_FILLS_MAX_PAGES", "10") or 10)
# Set by the REAL _fetch_fills on each call; consumed by the reconciliation
# gate. Monkeypatched test doubles never touch it (stays False).
_LAST_FETCH_TRUNCATED = False


@dataclass(frozen=True)
class HypeAcquisition:
    """Real HYPE acquisition metrics, or n/d when not derivable.

    ``known`` gates display: when False, BOTH ``ppc_usd`` and
    ``net_acq_usd`` are None and renderers MUST show "n/d" + ``reason``.
    """

    known: bool
    ppc_usd: float | None  # weighted-avg BUY price (buys move it, sells don't)
    net_acq_usd: float | None  # net of buys minus sells
    buy_qty: float
    sell_qty: float
    onchain_balance: float | None
    reason: str | None = None

    # ── R-BOT-DEFINITIVE Fase 3.3 — el porque, no solo el "no confiable" ──
    # Durante semanas el bloque de posiciones imprimio "fills no reconcilian
    # con el saldo, 32% gap — PPC no confiable" y nadie sabia que significaba.
    # Una linea alarmante cuyo significado se desconoce es peor que no tener
    # linea: no se puede actuar sobre ella y entrena a ignorar las alarmas.
    # Estos campos convierten esa frase en un hecho verificable: hasta que
    # fecha llegan los fills, cuantas unidades del saldo explican y cuantas
    # no. Con eso el "gap" deja de ser un misterio y pasa a ser una cuenta.
    fills_from_utc: str | None = None    # fill de HYPE spot mas antiguo visto
    fills_to_utc: str | None = None      # el mas reciente
    hype_fills: int = 0                  # cuantos fills de HYPE spot se vieron
    covered_qty: float = 0.0             # buys − sells (lo que los fills explican)
    uncovered_qty: float = 0.0           # saldo − covered (lo que no explican)
    truncated: bool = False              # se agoto la paginacion (historia mas larga)
    covered_ppc_usd: float | None = None  # PPC de la porcion cubierta, si existe
    # R-BOT-DEFINITIVE Fase 1.2 — fills que SI entraron al PPC pero cuyo
    # timestamp no se pudo leer. Sin este contador la ventana de fechas dice
    # "desde X" mientras el PPC incluye plata de antes de X, y la linea que
    # EXPLICA el numero queda mintiendo aunque el numero este bien.
    fills_sin_fecha: int = 0

    @property
    def coverage_pct(self) -> float | None:
        """Porcentaje del saldo on-chain que los fills alcanzan a explicar."""
        if not self.onchain_balance:
            return None
        return round(100.0 * self.covered_qty / self.onchain_balance, 1)


def _ms_to_date(ms: int | None) -> str | None:
    """Epoch en ms -> 'YYYY-MM-DD' UTC. None si no hay dato."""
    if not ms:
        return None
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _is_hype_spot(coin: Any, spot_map: dict[str, str] | None) -> bool:
    """True iff the fill's coin is the HYPE spot pair (e.g. 'HYPE' or '@107')."""
    if coin is None:
        return False
    s = str(coin)
    if s.upper() == "HYPE":
        return True
    # Resolve @N → ticker via the spot-index map when available.
    if spot_map and spot_map.get(s, "").upper() == "HYPE":
        return True
    return False


def _live_hype_balance(wallet: str) -> float | None:
    """Live HYPE spot balance for *wallet* via spotClearinghouseState."""
    try:
        from modules.hl_client import post_info_sync

        data = post_info_sync(
            {"type": "spotClearinghouseState", "user": wallet}
        )
    except Exception as exc:  # noqa: BLE001
        health_registry.swallowed("ppc", "_live_hype_balance")
        log.warning("hype_acquisition: balance read failed for %s: %s", wallet, exc)
        return None
    if not isinstance(data, dict):
        return None
    for b in data.get("balances") or []:
        if str(b.get("coin", "")).upper() == "HYPE":
            try:
                return float(b.get("total") or 0.0)
            except (TypeError, ValueError):
                return None
    return 0.0


def _resolve_spot_map() -> dict[str, str]:
    """Best-effort spotMeta map (@N → ticker) so '@107' resolves to HYPE.

    Returns {} on any failure — callers then match only the literal 'HYPE'
    coin, which simply yields a more conservative (n/d) reconciliation.
    """
    try:
        from modules.hl_client import post_info_sync
        from modules.spot_index import build_spot_index_map

        meta = post_info_sync({"type": "spotMeta"})
        return build_spot_index_map(meta) or {}
    except Exception as exc:  # noqa: BLE001
        health_registry.swallowed("ppc", "_resolve_spot_map")
        log.warning("hype_acquisition: spotMeta resolve failed: %s", exc)
        return {}


def _fetch_fills(wallet: str) -> list[dict] | None:
    """Full fill history for *wallet*. None on failure.

    R-EQUITY-DEDUP-DREAMCASH (2026-07-07): the old single ``userFills`` call
    was capped at ~2000 most-recent fills, so the PPC reconciliation saw only
    net 1987.38 HYPE vs 3006.28 on-chain (34% gap → permanent n/d). Now pages
    through ``userFillsByTime`` (startTime advanced past each page's max fill
    time, dedup by tid) up to ``_FILLS_MAX_PAGES``. Falls back to the legacy
    single call if the first page fails. Return type unchanged (list | None).
    """
    global _LAST_FETCH_TRUNCATED
    _LAST_FETCH_TRUNCATED = False
    try:
        import time as _time

        from modules.hl_client import post_info_sync

        end_ms = int(_time.time() * 1000)
        start_ms = 0
        out: list[dict] = []
        seen: set[Any] = set()
        pages_exhausted = True
        for page in range(_FILLS_MAX_PAGES):
            batch = post_info_sync({
                "type": "userFillsByTime", "user": wallet,
                "startTime": start_ms, "endTime": end_ms,
                "aggregateByTime": True,
            })
            if not isinstance(batch, list):
                if page == 0:
                    # First page failed → legacy single-call fallback.
                    data = post_info_sync(
                        {"type": "userFills", "user": wallet,
                         "aggregateByTime": True}
                    )
                    if isinstance(data, list):
                        _LAST_FETCH_TRUNCATED = len(data) >= _FILLS_CAP
                        return data
                    return None
                pages_exhausted = False
                break
            new = 0
            max_t = start_ms
            for f in batch:
                if not isinstance(f, dict):
                    continue
                key = f.get("tid") or (
                    f.get("oid"), f.get("time"), f.get("px"), f.get("sz"),
                )
                if key in seen:
                    continue
                seen.add(key)
                out.append(f)
                new += 1
                try:
                    t = int(f.get("time") or 0)
                    if t > max_t:
                        max_t = t
                except (TypeError, ValueError):
                    pass
            if len(batch) < _FILLS_CAP or new == 0:
                pages_exhausted = False
                break
            start_ms = max_t + 1
        # All pages consumed and every one was full → history likely longer.
        _LAST_FETCH_TRUNCATED = pages_exhausted
        return out
    except Exception as exc:  # noqa: BLE001
        health_registry.swallowed("ppc", "_fetch_fills")
        log.warning("hype_acquisition: fills read failed for %s: %s", wallet, exc)
        return None


def compute_hype_acquisition(
    wallet: str | None = None,
    *,
    spot_map: dict[str, str] | None = None,
) -> HypeAcquisition:
    """Derive HYPE PPC + net acquisition for the PM primary wallet.

    NEVER raises. Returns ``known=False`` (→ n/d) whenever the fill history
    cannot reliably reconstruct the live balance — see module docstring.
    """
    w = (wallet or PM_PRIMARY_WALLET or "").lower()
    if not w:
        return HypeAcquisition(
            known=False, ppc_usd=None, net_acq_usd=None,
            buy_qty=0.0, sell_qty=0.0, onchain_balance=None,
            reason="sin wallet primaria configurada",
        )

    if spot_map is None:
        spot_map = _resolve_spot_map()

    fills = _fetch_fills(w)
    if fills is None:
        return HypeAcquisition(
            known=False, ppc_usd=None, net_acq_usd=None,
            buy_qty=0.0, sell_qty=0.0, onchain_balance=None,
            reason="historial de fills no disponible (API)",
        )

    buy_qty = sell_qty = 0.0
    buy_notional = sell_notional = 0.0
    hype_fills = 0
    sin_fecha = 0
    t_min: int | None = None
    t_max: int | None = None
    for f in fills:
        if not _is_hype_spot(f.get("coin"), spot_map):
            continue
        try:
            sz = abs(float(f.get("sz") or 0.0))
            px = float(f.get("px") or 0.0)
        except (TypeError, ValueError):
            continue
        if sz <= 0 or px <= 0:
            continue
        side = str(f.get("side") or "").upper()
        is_buy = side == "B" or str(f.get("dir") or "").lower().startswith("buy")
        hype_fills += 1
        # Ojo con este bloque: el fill YA sumo a buy_qty/buy_notional arriba,
        # o sea que ya esta adentro del PPC. Si ademas no se le puede leer la
        # fecha, queda plata contada dentro de una ventana temporal que no la
        # incluye. Antes esto era un `pass` y la ventana mentia en silencio;
        # ahora se cuenta y se publica en el horizonte.
        try:
            t = int(f.get("time") or 0)
        except (TypeError, ValueError):
            t = 0
        if t > 0:
            t_min = t if t_min is None else min(t_min, t)
            t_max = t if t_max is None else max(t_max, t)
        else:
            sin_fecha += 1
        if is_buy:
            buy_qty += sz
            buy_notional += sz * px
        else:
            sell_qty += sz
            sell_notional += sz * px

    balance = _live_hype_balance(w)
    horizon = {"fills_from_utc": _ms_to_date(t_min),
               "fills_to_utc": _ms_to_date(t_max),
               "hype_fills": hype_fills,
               "fills_sin_fecha": sin_fecha,
               "truncated": bool(_LAST_FETCH_TRUNCATED)}

    if hype_fills == 0:
        return HypeAcquisition(
            known=False, ppc_usd=None, net_acq_usd=None,
            buy_qty=0.0, sell_qty=0.0, onchain_balance=balance,
            uncovered_qty=float(balance or 0.0),
            reason="sin fills de HYPE spot en el historial (saldo migrado/bridged)",
            **horizon,
        )

    # Reliability gate: the fills must explain the live balance.
    net_qty = buy_qty - sell_qty
    # Paginated fetch → truncation now means "max pages exhausted" (set by the
    # real _fetch_fills); the raw len-vs-cap check stays as a belt-and-braces
    # signal for the legacy fallback path.
    truncated = _LAST_FETCH_TRUNCATED or len(fills) >= _FILLS_CAP * _FILLS_MAX_PAGES
    horizon["truncated"] = bool(truncated)
    covered_ppc = buy_notional / buy_qty if buy_qty > 0 else None

    if balance is None:
        return HypeAcquisition(
            known=False, ppc_usd=None, net_acq_usd=None,
            buy_qty=buy_qty, sell_qty=sell_qty, onchain_balance=None,
            covered_qty=net_qty, covered_ppc_usd=covered_ppc,
            reason="no se pudo leer el balance HYPE on-chain para reconciliar",
            **horizon,
        )
    if balance > 0:
        mismatch = abs(net_qty - balance) / balance
    else:
        mismatch = 0.0 if abs(net_qty) < 1e-9 else 1.0
    if mismatch > _RECONCILE_TOL:
        # R-BOT-DEFINITIVE Fase 3.3 — decir QUE pasa, no solo que algo pasa.
        #
        # El texto viejo era "fills no reconcilian con el saldo, 32% gap —
        # PPC no confiable". Es cierto y es inutil: no dice por que faltan
        # unidades ni si eso es un problema. Hay dos causas posibles y son
        # muy distintas entre si:
        #
        #   1. La paginacion se agoto (truncated): la historia es mas larga
        #      que lo que se puede leer. Es una limitacion de lectura, y ahi
        #      SI corresponde decir que el dato es incompleto.
        #   2. La paginacion termino sola y aun asi el saldo es mayor: esas
        #      unidades entraron por fuera del libro de spot de HL (bridge,
        #      migracion, transferencia). No falta informacion por leer:
        #      esas unidades sencillamente no tienen precio de compra EN HL.
        #      Eso no es una anomalia ni algo para revisar; es la historia
        #      del saldo, y el PPC manual existe exactamente por eso.
        uncovered = balance - net_qty
        desde = horizon["fills_from_utc"] or "fecha desconocida"
        # Si hubo fills sin fecha legible, la ventana "desde X" NO cubre todo
        # lo que entro al PPC. Se dice, porque si no la explicacion del gap
        # apunta a una ventana que no es la real.
        if sin_fecha:
            desde += (f" (+{sin_fecha} fill(s) contados en el PPC con fecha "
                      f"ilegible, fuera de esa ventana)")
        cov = f"{net_qty:,.2f} de {balance:,.2f}"
        pct = 100.0 * net_qty / balance if balance else 0.0
        if truncated:
            reason = (
                f"historial de fills incompleto: se agoto la paginacion de la "
                f"API y solo se alcanza desde {desde}, que explica {cov} HYPE "
                f"({pct:.0f}%). El auto-PPC de esa porcion seria parcial, asi "
                f"que no se publica."
            )
        else:
            reason = (
                f"los fills spot de HL llegan hasta {desde} y explican {cov} "
                f"HYPE ({pct:.0f}%); las {uncovered:,.2f} HYPE restantes "
                f"entraron sin operacion de compra en HL (bridge/migracion), "
                f"asi que no tienen precio de adquisicion on-chain. No es un "
                f"error: por eso el PPC en uso es el manual."
            )
        return HypeAcquisition(
            known=False, ppc_usd=None, net_acq_usd=None,
            buy_qty=buy_qty, sell_qty=sell_qty, onchain_balance=balance,
            covered_qty=net_qty, uncovered_qty=uncovered,
            covered_ppc_usd=covered_ppc, reason=reason, **horizon,
        )

    ppc = covered_ppc
    net_acq = (
        (buy_notional - sell_notional) / net_qty if abs(net_qty) > 1e-9 else None
    )
    if ppc is None:
        return HypeAcquisition(
            known=False, ppc_usd=None, net_acq_usd=None,
            buy_qty=buy_qty, sell_qty=sell_qty, onchain_balance=balance,
            covered_qty=net_qty, reason="sin compras en el historial — PPC indefinido",
            **horizon,
        )
    return HypeAcquisition(
        known=True,
        ppc_usd=ppc,
        net_acq_usd=net_acq,
        buy_qty=buy_qty,
        sell_qty=sell_qty,
        onchain_balance=balance,
        reason=None,
        covered_qty=net_qty,
        uncovered_qty=balance - net_qty,
        covered_ppc_usd=covered_ppc,
        **horizon,
    )


# ── R-BOT-DEFINITIVE-2 T5: manual PPC override (/setppc) ─────────────────────
# The reconciliation gate correctly refuses to fabricate a PPC for the
# migrated/bridged HYPE balance, but BCD KNOWS his real numbers. /setppc stores
# a manual override in SQLite (timestamped) that the report renders as the
# primary line; the reconciliation-gap note stays as a secondary line.
def _ppc_conn():
    from modules.intel_memory import _get_conn
    conn = _get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ppc_override (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            ppc_usd REAL NOT NULL,
            net_acq_usd REAL NOT NULL,
            set_ts TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def set_ppc_override(ppc_usd: float, net_acq_usd: float) -> bool:
    """Persist the manual PPC + net-acquisition override. NEVER raises."""
    try:
        ppc = float(ppc_usd)
        net = float(net_acq_usd)
        if not (ppc > 0 and net > 0):
            return False
        from datetime import datetime, timezone
        conn = _ppc_conn()
        conn.execute(
            "INSERT INTO ppc_override (id, ppc_usd, net_acq_usd, set_ts) "
            "VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "ppc_usd=excluded.ppc_usd, net_acq_usd=excluded.net_acq_usd, "
            "set_ts=excluded.set_ts",
            (ppc, net, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as exc:  # noqa: BLE001
        health_registry.swallowed("ppc", "set_ppc_override")
        log.warning("set_ppc_override failed: %s", exc)
        return False


def clear_ppc_override() -> bool:
    """Remove the manual override (report reverts to fills/n-d). NEVER raises."""
    try:
        conn = _ppc_conn()
        cur = conn.execute("DELETE FROM ppc_override WHERE id=1")
        conn.commit()
        cleared = cur.rowcount > 0
        conn.close()
        return cleared
    except Exception as exc:  # noqa: BLE001
        health_registry.swallowed("ppc", "clear_ppc_override")
        log.warning("clear_ppc_override failed: %s", exc)
        return False


def get_ppc_override() -> dict[str, Any] | None:
    """{'ppc_usd', 'net_acq_usd', 'set_date'} or None. NEVER raises."""
    try:
        conn = _ppc_conn()
        row = conn.execute(
            "SELECT ppc_usd, net_acq_usd, set_ts FROM ppc_override WHERE id=1"
        ).fetchone()
        conn.close()
        if not row:
            return None
        return {
            "ppc_usd": float(row["ppc_usd"]),
            "net_acq_usd": float(row["net_acq_usd"]),
            "set_date": str(row["set_ts"] or "")[:10],
        }
    except Exception as exc:  # noqa: BLE001
        health_registry.swallowed("ppc", "get_ppc_override")
        log.warning("get_ppc_override failed: %s", exc)
        return None


def format_hype_acquisition_line(acq: HypeAcquisition) -> str:
    """One-line HYPE acquisition summary for Telegram. Honest n/d when unknown.

    R-BOT-DEFINITIVE-2 T5: a manual /setppc override (when set) is the PRIMARY
    line; the fills-reconciliation note (n/d reason) stays as a secondary line.
    """
    ov = get_ppc_override()
    if ov:
        line = (
            f"💠 HYPE adquisición — PPC contable: ${ov['ppc_usd']:,.2f} "
            f"(manual, set {ov['set_date']}) · "
            f"adq. neta: ${ov['net_acq_usd']:,.2f} (manual, set {ov['set_date']})"
        )
        if not acq.known:
            # Fase 3.3: la linea secundaria pasa de alarma sin significado a
            # explicacion cerrada. Se distingue tipograficamente el caso
            # "asi es la historia del saldo" (informativo) del caso "no
            # pudimos leer" (aviso), porque son cosas distintas y hasta ahora
            # se imprimian con el mismo tono.
            marca = "⚠️" if acq.truncated else "ℹ️"
            line += (
                f"\n   {marca} auto-PPC no calculable: "
                f"{acq.reason or 'no derivable de fills'}"
            )
        return line
    if not acq.known:
        return (
            "💠 HYPE adquisición — PPC contable: n/d · adq. neta: n/d  "
            f"({acq.reason or 'no derivable de fills'})"
        )
    net_txt = f"${acq.net_acq_usd:,.2f}" if acq.net_acq_usd is not None else "n/d"
    return (
        f"💠 HYPE adquisición — PPC contable (avg buy): ${acq.ppc_usd:,.2f} · "
        f"adq. neta (buys−sells): {net_txt}  "
        f"[buys {acq.buy_qty:,.2f} / sells {acq.sell_qty:,.2f} HYPE]"
    )
