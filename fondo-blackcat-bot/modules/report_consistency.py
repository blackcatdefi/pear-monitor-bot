"""R-REPORTE-LIVE (2026-06-03) FIX 3 — header/body self-consistency pass.

Final deterministic pass over the generated report BEFORE it is sent. Drops
(or neutralises) body lines that contradict the fund's current venue/state:

  1. Venue truth — when the flywheel is migrated to Portfolio Margin
     (HyperLend CLOSED), any body line that presents a LIVE HyperLend health
     factor / flywheel kHYPE-UETH pair trade is a contradiction and is dropped.
  2. Cycle-accumulation protection — any line that suggests
     closing/reducing/exiting a coin currently tagged CYCLE_ACCUMULATION on
     bearish grounds is dropped (the drawdown is the thesis).

NEVER raises: on any error the original text is returned unchanged (the report
must always send).
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

log = logging.getLogger(__name__)

# Phrases that indicate a LIVE HyperLend HF / flywheel claim in the body.
_HL_LIVE_PATTERNS = [
    re.compile(r"hyperlend.*\bhf\b", re.IGNORECASE),
    re.compile(r"\bhf\b.*hyperlend", re.IGNORECASE),
    re.compile(r"flywheel.*\bhf\b", re.IGNORECASE),
    re.compile(r"\bhf\b\s*[=:]\s*\d", re.IGNORECASE),
    re.compile(r"health\s*factor\s*[=:]?\s*\d", re.IGNORECASE),
    re.compile(r"khype\s*/?\s*ueth", re.IGNORECASE),
    re.compile(r"pair\s*trade.*ueth", re.IGNORECASE),
]

# Bearish close/reduce verbs.
_CLOSE_RE = re.compile(
    r"\b(cerrar|cerr[aá]|reducir|reduc[íi]|salir|sal[íi]|liquidar|close|exit|reduce)\b",
    re.IGNORECASE,
)
# Bearish-environment justifications.
_BEARISH_RE = re.compile(
    r"\b(bearish|bajista|capitulaci[oó]n|capitulation|cvd|downtrend|tendencia\s+bajista|"
    r"miedo|fear|p[aá]nico|panic|cae|cayendo|desplome|dump|sell[\s-]?off|risk[\s-]?off)\b",
    re.IGNORECASE,
)


def _is_live_hyperlend_line(line: str) -> bool:
    return any(p.search(line) for p in _HL_LIVE_PATTERNS)


def _mentions_cycle_coin(line: str, cycle_coins: Iterable[str]) -> str | None:
    up = line.upper()
    for c in cycle_coins:
        c = (c or "").upper()
        if not c:
            continue
        if re.search(rf"\b{re.escape(c)}\b", up):
            return c
    return None


def enforce_consistency(
    report_text: str,
    *,
    flywheel_deprecated: bool = True,
    cycle_coins: Iterable[str] | None = None,
) -> tuple[str, list[str]]:
    """Return ``(clean_text, dropped_lines)``. NEVER raises.

    ``flywheel_deprecated`` True (default) → drop live-HyperLend-HF body lines.
    ``cycle_coins`` → coins tagged CYCLE_ACCUMULATION this run; lines that
    suggest a bearish close/reduce on one of them are dropped.
    """
    try:
        cycle_coins = list(cycle_coins or [])
        out_lines: list[str] = []
        dropped: list[str] = []
        for raw in report_text.splitlines():
            line = raw
            stripped = line.strip()

            # Never touch the analysis-provider footer / section dividers.
            if not stripped:
                out_lines.append(line)
                continue

            # Rule 1 — live HyperLend HF / flywheel pair-trade contradiction.
            if flywheel_deprecated and _is_live_hyperlend_line(stripped):
                # Keep explicit "CERRADO / closed / migrado" mentions — those
                # are CORRECT (they affirm the venue truth), drop the rest.
                low = stripped.lower()
                if not any(
                    kw in low for kw in ("cerrad", "closed", "migrad", "deprecad", "stale", "no live", "no disponible")
                ):
                    dropped.append(stripped)
                    continue

            # Rule 2 — bearish close suggestion on a cycle-accumulation coin.
            if cycle_coins:
                coin = _mentions_cycle_coin(stripped, cycle_coins)
                if coin and _CLOSE_RE.search(stripped) and _BEARISH_RE.search(stripped):
                    dropped.append(stripped)
                    continue

            out_lines.append(line)

        clean = "\n".join(out_lines)
        if dropped:
            note = (
                "\n\n⚠️ NOTA DE CONSISTENCIA (auto): se removieron "
                f"{len(dropped)} línea(s) que contradecían el estado actual del "
                "fondo (HyperLend CERRADO / no cerrar acumulación de ciclo en "
                "entorno bearish)."
            )
            clean = clean + note
        return clean, dropped
    except Exception:  # noqa: BLE001
        log.exception("enforce_consistency failed — returning original report")
        return report_text, []
