"""R-REPORTE-SCREENER-EMBED (2026-06-09) — compact 15-SHORT / 15-LONG screener
block embedded in /reporte.

WHAT THIS IS — A FORMATTING/SELECTION LAYER, NEVER A SECOND ENGINE
    /reporte now carries a COMPACT screener section: the TOP 15 most-shorteable
    and TOP 15 most-longable names over the full HL+VAR universe, and NOTHING
    of the long tail (no RESTO / NO-GO, no DATA INSUFICIENTE — those stay in
    the standalone /unlockcheck, which is unchanged).

    The math is the EXACT R-SCREEN 5-gate engine: this module calls
    ``universal_screener.compute_screen`` — THE SAME function /unlockcheck
    calls — and only selects + formats. It re-implements NO indicator, NO
    gate, NO threshold, so /reporte and /unlockcheck can never diverge
    (engine parity, test B6).

SHORT RANKING (verbatim from R-SCREEN)
    ``ScreenResult.ranked`` is already sorted by ``short_score`` (pass-count
    desc → squeeze forced to the bottom → higher z+ / lower Hurst). We take
    the head, EXCLUDING any name in squeeze — squeeze is an INVIOLABLE
    exclusion in gating AND ranking, so it can never appear among "most
    shorteable" no matter its z.

LONG RANKING (symmetric read — CONTEXT only, never a mandate)
    R-SCREEN already computes the mirror long read per name
    (``universal_screener.long_read``: z oversold, Hurst mean-revert,
    funding≤0 = shorts crowded, capitulation guard). It only FLAGGED names;
    here we add a deterministic symmetric SCORE over those SAME fields
    (mirror of ``short_score``: component count ×100, capitulation −1000,
    more-negative z and lower Hurst as tiebreaks) to produce an ordered TOP
    15. No new indicator is computed.

MANDATE-FAITHFUL LABELING (fund rule)
    The LONG block carries the explicit disclaimer: longs en alts son
    TÁCTICOS / decisión de BCD + AiPear — NO el mandato (mandato = HYPE-core
    long + libro short amplio). El bot NUNCA selecciona tokens.

NO PADDING WITH NOISE
    Only names with a meaningful signal are shown (SHORT: ≥3/5 gates y sin
    squeeze; LONG: ≥3 componentes espejo y sin capitulación). Fewer than 15 →
    "solo N con señal"; zero → explicit "0 candidatos".
"""
from __future__ import annotations

import logging
from typing import Optional

from modules.universal_screener import (
    ScreenResult,
    ScreenRow,
    compute_screen,
    short_pass_count,  # noqa: F401  (re-exported for parity assertions)
    _fmt_hurst,
    _fmt_z,
)

log = logging.getLogger(__name__)

EMBED_TOP_N = 15

# Minimum gate/component count for a name to be "with signal" — never pad the
# embedded block with 0-2/5 noise just to reach 15 lines.
MIN_SIGNAL_COUNT = 3

SHORT_HEADER = "🔴 TOP 15 SHORT"
LONG_HEADER = "🟢 TOP 15 LONG"
LONG_DISCLAIMER = (
    "LONG = táctico / decisión BCD + AiPear — NO mandato "
    "(mandato = HYPE-core long + libro short de alts). El bot NUNCA selecciona tokens."
)


# ─── LONG-side symmetric ranking (reads ONLY fields the engine computed) ─────
def long_component_count(r: ScreenRow) -> int:
    """Mirror of ``short_pass_count`` over the LONG read the engine already
    produced: data + z-oversold + mean-revert + capitulation-clear + funding≤0."""
    lr = r.long
    return (
        int(r.data_ok)
        + int(lr.z_oversold)
        + int(lr.mean_reverting)
        + int(not lr.capitulating)
        + int(lr.funding_crowded_short)
    )


def long_score(r: ScreenRow) -> float:
    """Deterministic longability score — exact MIRROR of ``short_score``.

    component-count ×100 dominant; CAPITULATION (the mirror of squeeze: a
    falling knife is un-longable) forces −1000 below every clean name;
    tiebreaks: MORE NEGATIVE z (more oversold) and lower Hurst (more
    mean-reverting) rank higher. Reads only engine-computed fields.
    """
    if not r.data_ok:
        return float("-inf")
    score = long_component_count(r) * 100.0
    if r.long.capitulating:
        score -= 1000.0
    g = r.gate
    z = g.z if (g and g.z is not None) else 0.0
    z = max(-3.0, min(3.0, z))
    h = g.hurst if (g and g.hurst is not None) else 0.5
    score += (-z) * 5.0          # more oversold = higher
    score += (0.5 - h) * 20.0    # more mean-reverting = higher
    return score


# ─── Top-N selection (same ScreenResult /unlockcheck consumes) ───────────────
def short_top(res: ScreenResult, n: int = EMBED_TOP_N) -> list[ScreenRow]:
    """Head of the EXISTING short ranking — squeeze names excluded outright
    (inviolable) and sub-signal (<3/5) names never padded in."""
    out: list[ScreenRow] = []
    for r in res.ranked:  # already sorted by short_score desc by the engine
        if r.gate is not None and r.gate.squeeze_flag:
            continue
        if r.pass_count < MIN_SIGNAL_COUNT:
            continue
        out.append(r)
        if len(out) >= n:
            break
    return out


def long_top(res: ScreenResult, n: int = EMBED_TOP_N) -> list[ScreenRow]:
    """TOP-N longable by the symmetric score; capitulating or sub-signal names
    are never padded in. Deterministic (score desc, ticker asc tiebreak)."""
    cands = [
        r for r in res.ranked
        # HARD requirements (the symmetric criteria the engine's long read
        # already uses — never invented here): z-oversold, Hurst mean-revert,
        # NO capitulation (falling knife) and squeeze CLEAR (a name in an
        # up-squeeze blow-off is momentum, not a mean-revert long). Without
        # these, overbought / trending / squeezing names would leak into the
        # LONG top — noise, never longable. funding≤0 stays a soft component
        # (score/ count), exactly as in the engine's long_read.
        if r.data_ok and r.long.z_oversold and r.long.mean_reverting
        and not r.long.capitulating
        and not (r.gate is not None and r.gate.squeeze_flag)
        and long_component_count(r) >= MIN_SIGNAL_COUNT
    ]
    cands.sort(key=lambda r: (-long_score(r), r.ticker))
    return cands[:n]


# ─── Compact formatting (ONE line per name — no per-gate multiline) ──────────
def _fund_sign_compact(g) -> str:
    """Funding SIGN only ("+", "−", "0", "n/d") — the embed is one-line compact,
    no per-gate ✅/❌ marks (those live in /unlockcheck's full detail)."""
    if g is None or g.funding_sign is None:
        return "n/d"
    if g.funding_sign > 0:
        return "+"
    if g.funding_sign < 0:
        return "−"
    return "0"


def _compact_line(idx: int, r: ScreenRow, count: int) -> str:
    g = r.gate
    sq = "clear" if (g and not g.squeeze_flag) else ("SQUEEZE" if g else "n/d")
    fund = _fund_sign_compact(g)
    return (
        f"{idx:>2}. {r.ticker:<8} {count}/5 · {r.venue_label} · "
        f"z {_fmt_z(g.z if g else None)} · H {_fmt_hurst(g.hurst if g else None)} · "
        f"sq {sq} · fund {fund}"
    )


def format_embedded_screener(res: ScreenResult, n: int = EMBED_TOP_N,
                             telemetry_blocks: Optional[dict[str, str]] = None,
                             telemetry_note: Optional[str] = None) -> str:
    """Render the COMPACT 15+15 block for /reporte. No long tail, ever.

    ``telemetry_blocks`` (R-SCREEN-TELEMETRY): pre-rendered compact telemetry
    keyed by ticker, attached DIRECTLY under each 5/5 GO candidate in the SHORT
    top — identical block to the one /unlockcheck attaches (same renderer, same
    ScreenResult → same strings). ``None`` (default) keeps the legacy block
    byte-identical. Strings are pre-rendered by the caller so this module never
    imports telemetry."""
    shorts = short_top(res, n)
    longs = long_top(res, n)
    n_go = sum(1 for r in res.ranked if r.is_go_candidate)

    lines = [
        "🔭 SCREENER EMBEBIDO — universo completo, 5-gates R-SCREEN",
        f"{res.ts_utc} · universo {res.universe_size} perps · 5/5 GO: {n_go}",
        "(mismo motor que /unlockcheck — acá solo top-15 por lado, detalle completo en /unlockcheck)",
        "",
        SHORT_HEADER,
    ]
    if telemetry_blocks is not None and telemetry_note:
        lines.append(f"  ({telemetry_note})")
    if not shorts:
        lines.append("  0 candidatos con señal (≥3/5 sin squeeze) ahora.")
    else:
        if len(shorts) < n:
            lines.append(f"  (solo {len(shorts)} con señal)")
        for i, r in enumerate(shorts, start=1):
            lines.append(_compact_line(i, r, r.pass_count))
            # R-SCREEN-TELEMETRY: attach the compact telemetry block under each
            # GO candidate (5/5) only — never context names.
            if telemetry_blocks is not None and r.is_go_candidate:
                tb = telemetry_blocks.get(r.ticker)
                if tb:
                    lines.append(tb)
    if telemetry_blocks is not None and n_go == 0:
        lines.append("  📟 telemetría: sin candidatos GO (nada que adjuntar)")

    lines += ["", LONG_HEADER, f"  {LONG_DISCLAIMER}"]
    if not longs:
        lines.append("  0 candidatos con señal (≥3 componentes sin capitulación) ahora.")
    else:
        if len(longs) < n:
            lines.append(f"  (solo {len(longs)} con señal)")
        for i, r in enumerate(longs, start=1):
            lines.append(_compact_line(i, r, long_component_count(r)))
    return "\n".join(lines)


async def build_embedded_screener_block(n: int = EMBED_TOP_N) -> Optional[str]:
    """Compute (SAME engine call as /unlockcheck: ``compute_screen``, pure read,
    advance_state=False) + format the compact block, with R-SCREEN-TELEMETRY
    attached under each 5/5 GO candidate. NEVER raises — returns None on any
    failure so /reporte is never broken by the screener; a telemetry failure
    degrades to the block WITHOUT telemetry rather than dropping the section."""
    try:
        res = await compute_screen(advance_state=False)
    except Exception:  # noqa: BLE001
        log.exception("embedded screener block failed (non-fatal)")
        return None
    # Telemetry is best-effort: render_go_telemetry never raises, but guard the
    # import/call anyway so the screener section survives any telemetry fault.
    tel_blocks: dict[str, str] = {}
    tel_note: Optional[str] = None
    try:
        from modules import telemetry as _tel
        tel_blocks, tel_note, _ = await _tel.render_go_telemetry(res)
    except Exception:  # noqa: BLE001
        log.exception("embedded screener telemetry failed (non-fatal)")
        tel_blocks, tel_note = {}, None
    try:
        return format_embedded_screener(res, n, telemetry_blocks=tel_blocks,
                                        telemetry_note=tel_note)
    except Exception:  # noqa: BLE001
        log.exception("embedded screener format failed (non-fatal)")
        return None
