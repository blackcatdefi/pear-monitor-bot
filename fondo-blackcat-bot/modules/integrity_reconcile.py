"""R-INTEGRITY-RECONCILE-FIX (2026-06-07) — persisted-flag RE-RESOLUTION pass.

The bug (live /reporte 2026-06-07 06:59 UTC)
--------------------------------------------
A persisted INTEGRITY-HALT flag (``STOP accumulation on BTC``) raised by an
OLD nearest-ticker resolver off a *Zcash/Orchard* "unlimited mint" rumor kept
rendering forever even though the corrected resolver attributes that exact
rumor to ZEC. R-PM-LIQ's ``reconcile_misattributed`` only reconciled against
rumors firing in the CURRENT scan and keyed on the live excerpt — so once the
originating excerpt rotated out of the feed window (or the live scan fired on a
different keyword such as "exploit"), no matching pair was found and the BTC
flag was orphaned forever.

The fix
-------
This module adds a PERSISTED-FLAG RE-RESOLUTION pass that runs on EVERY scan,
BEFORE rendering, and is COMPLETELY DECOUPLED from the live feed window. For
each OPEN, auto-dismissible flag it re-reads the flag's OWN stored excerpt,
re-runs it through the CURRENT resolver (alias map + distinctive-name
precedence — the exact raise-time code path), and dismisses the flag when:

  * resolved_asset_now != flag.resolved_asset      → "re_resolved_to_other_asset"
  * resolved_asset_now in DCA blocklist OR not held → "asset_blocklisted"
  * resolved_asset_now is None AND conf < CONF_MIN  → "no_identifiable_asset"

Hard guards (a real risk NEVER auto-clears):
  * a flag whose re-resolution still maps to a HELD asset with adverse PnL,
  * a shielded asset flag, and
  * a manually-pinned flag (``auto_dismissible = 0``)
never auto-dismiss.

Integrity keywords are grouped into FAMILIES that all route through the same
alias map, and reconcile/dedup keys on the tuple ``(resolved_asset,
keyword_family)`` rather than a raw excerpt or a single keyword. Where excerpt
comparison is still needed it is normalised (lowercase, punctuation-stripped,
whitespace-collapsed) and compared with rapidfuzz ``token_set_ratio`` (subset
matches treated as the same originating rumor). NEVER raises.
"""
from __future__ import annotations

import logging
import re
import string
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import modules.integrity_halt as ih

log = logging.getLogger(__name__)

# Minimum confidence below which a None-resolution flag is treated as having no
# identifiable subject (and therefore an auto-dismissible orphan).
CONF_MIN = 0.60

# Fuzzy thresholds for "same originating rumor" on normalised excerpts.
_FUZZY_ACCEPT = 90   # >=90 → same rumor outright
_FUZZY_MAYBE = 60    # 60-90 → same rumor ONLY if asset+family also agree
# (<60 → different rumor)

# Integrity-keyword FAMILIES. Every member routes through the SAME alias map;
# reconcile/dedup keys on (resolved_asset, family), not a raw keyword.
INTEGRITY_KEYWORD_FAMILIES: dict[str, list[str]] = {
    "supply_integrity": [
        "unlimited mint", "infinite mint", "counterfeit", "inflation",
        "double-spend", "double spend", "unauthorized mint", "infinite supply",
        "secretly print", "secret print",
    ],
    "exploit": [
        "exploit", "hack", "hacked", "vulnerability", "bug", "flaw",
        "soundness", "backdoor",
    ],
    "crisis": [
        "crisis", "collapse", "depeg", "insolvency", "insolvent", "rug",
        "rugpull", "rug pull", "delisting", "delist",
    ],
}

# keyword(lower) → family, longest keyword first so multi-word phrases win.
_KEYWORD_TO_FAMILY: dict[str, str] = {}
for _fam, _kws in INTEGRITY_KEYWORD_FAMILIES.items():
    for _k in _kws:
        _KEYWORD_TO_FAMILY[_k.lower()] = _fam
_ALL_FAMILY_KEYWORDS = sorted(_KEYWORD_TO_FAMILY.keys(), key=len, reverse=True)


def keyword_family(keyword: str | None) -> str | None:
    """Return the integrity FAMILY a keyword belongs to (or None). NEVER raises."""
    if not keyword:
        return None
    k = str(keyword).strip().lower()
    if k in _KEYWORD_TO_FAMILY:
        return _KEYWORD_TO_FAMILY[k]
    # tolerate stored variants ("unlimited minting" carries "unlimited mint")
    for kw in _ALL_FAMILY_KEYWORDS:
        if kw in k or k in kw:
            return _KEYWORD_TO_FAMILY[kw]
    return None


def _all_keywords() -> list[str]:
    """Union of the integrity_halt default keywords + family keywords."""
    try:
        base = list(ih._keywords())
    except Exception:  # noqa: BLE001
        base = []
    merged = set(base) | set(_ALL_FAMILY_KEYWORDS)
    return sorted(merged, key=len, reverse=True)


_PUNCT_TABLE = str.maketrans({c: " " for c in string.punctuation + "—–…"})


def normalize_excerpt(s: Any) -> str:
    """Lowercase, strip punctuation, collapse whitespace. NEVER raises."""
    try:
        text = str(s or "").lower().translate(_PUNCT_TABLE)
        return " ".join(text.split())
    except Exception:  # noqa: BLE001
        return ""


def fuzzy_ratio(a: Any, b: Any) -> float:
    """token_set_ratio over normalised excerpts (rapidfuzz; difflib fallback).

    Subset matches (one excerpt fully contained in the other) are pinned to
    100 so a truncated/rephrased stored excerpt is recognised as the same
    originating rumor. NEVER raises.
    """
    na, nb = normalize_excerpt(a), normalize_excerpt(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 100.0
    sa, sb = set(na.split()), set(nb.split())
    if sa and sb and (sa <= sb or sb <= sa):
        return 100.0
    try:
        from rapidfuzz import fuzz
        return float(fuzz.token_set_ratio(na, nb))
    except Exception:  # noqa: BLE001 — fallback keeps reconcile working keyless
        import difflib
        # difflib over sorted token sets approximates token_set_ratio.
        ta = " ".join(sorted(sa))
        tb = " ".join(sorted(sb))
        return difflib.SequenceMatcher(None, ta, tb).ratio() * 100.0


def fuzzy_same_rumor(
    a: Any,
    b: Any,
    *,
    asset_a: str | None = None,
    asset_b: str | None = None,
    family_a: str | None = None,
    family_b: str | None = None,
) -> bool:
    """Decide whether two excerpts are the SAME originating rumor.

    >=90 → same outright; 60-90 → same ONLY if asset AND family agree;
    <60 → different. NEVER raises.
    """
    score = fuzzy_ratio(a, b)
    if score >= _FUZZY_ACCEPT:
        return True
    if score >= _FUZZY_MAYBE:
        return (
            asset_a is not None
            and asset_a == asset_b
            and family_a is not None
            and family_a == family_b
        )
    return False


@dataclass(frozen=True)
class ResolveResult:
    resolved_asset: str | None
    keyword_family: str | None
    confidence: float
    matched_keyword: str | None


def _held_tickers(positions: list[dict[str, Any]] | None) -> set[str]:
    out: set[str] = set()
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        coin = str(p.get("coin") or "").upper().strip()
        if coin and coin != "?":
            out.add(coin)
    return out


def reresolve_excerpt(
    excerpt: Any,
    *,
    positions: list[dict[str, Any]] | None = None,
    alias_map: dict[str, str] | None = None,
) -> ResolveResult:
    """Re-run a STORED excerpt through the current resolver.

    Applies distinctive-name precedence (orchard/zcash, ≥4 chars, not a bare
    ticker) AT RECONCILE-TIME exactly as at raise-time: a distinctive name in
    the local window out-ranks the nearest held ticker. Returns the resolved
    asset, its keyword family, a confidence in [0,1] and the matched keyword.
    NEVER raises.
    """
    try:
        text = str(excerpt or "")
        tl = text.lower()
        alias_map = alias_map if alias_map is not None else ih.integrity_aliases()
        held = _held_tickers(positions)
        detect = set(held) | {str(v).upper() for v in alias_map.values()}

        # keyword positions (union of default + family keywords)
        kw_hits: list[tuple[int, str]] = []
        for k in _all_keywords():
            for m in re.finditer(re.escape(k), tl):
                kw_hits.append((m.start(), k))
        matched_kw = min(kw_hits, key=lambda x: x[0])[1] if kw_hits else None
        family = keyword_family(matched_kw)

        # entity mentions: (offset, TICKER, is_distinctive_name)
        mentions: list[tuple[int, str, bool]] = []
        for ticker in detect:
            for pos in ih._word_positions(ticker, tl):
                mentions.append((pos, ticker.upper(), False))
        for name, ticker in alias_map.items():
            distinctive = len(name) >= 4 and name.lower() != str(ticker).lower()
            for pos in ih._word_positions(name, tl):
                mentions.append((pos, str(ticker).upper(), distinctive))

        if not mentions or not kw_hits:
            # No identifiable subject (or not an integrity excerpt at all).
            return ResolveResult(None, family, 0.0, matched_kw)

        kw_positions = [pos for pos, _ in kw_hits]
        chosen: list[tuple[str, bool]] = []
        for kpos in kw_positions:
            near_distinctive = [
                m for m in mentions
                if m[2] and abs(m[0] - kpos) <= ih._DISTINCTIVE_WINDOW
            ]
            pool = near_distinctive or mentions
            nearest = min(pool, key=lambda m: abs(m[0] - kpos))
            chosen.append((nearest[1], nearest[2]))

        # Prefer a distinctive-name resolution; pick the most frequent ticker.
        distinctive_choices = [c for c in chosen if c[1]]
        pool = distinctive_choices or chosen
        counts: dict[str, int] = {}
        for tk, _ in pool:
            counts[tk] = counts.get(tk, 0) + 1
        resolved = max(counts, key=lambda t: counts[t])
        confidence = 0.90 if distinctive_choices else 0.70
        return ResolveResult(resolved, family, confidence, matched_kw)
    except Exception as exc:  # noqa: BLE001
        log.warning("reresolve_excerpt failed: %s", exc)
        return ResolveResult(None, None, 0.0, None)


# ── schema migration (enrich integrity_halt in place; never wipe) ────────────
_EXTRA_COLUMNS = {
    "source_excerpt_norm": "TEXT",
    "keyword_family": "TEXT",
    "resolved_asset": "TEXT",
    "confidence": "REAL",
    "auto_dismissible": "INTEGER DEFAULT 1",
    "dismiss_reason": "TEXT",
    "last_reresolved_at": "TEXT",
}


def _conn():
    conn = ih._conn()  # creates integrity_halt if missing
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(integrity_halt)")}
        for name, decl in _EXTRA_COLUMNS.items():
            if name not in cols:
                conn.execute(f"ALTER TABLE integrity_halt ADD COLUMN {name} {decl}")
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("integrity_halt migration failed: %s", exc)
    return conn


def _dismiss(conn, asset: str, reason: str, res: ResolveResult) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    try:
        cur = conn.execute(
            "UPDATE integrity_halt SET active=0, dismissed_ts=?, dismiss_reason=?, "
            "resolved_asset=?, keyword_family=?, confidence=?, last_reresolved_at=? "
            "WHERE asset=? AND active=1",
            (
                f"{now} (auto-reresolve: {reason})", reason,
                res.resolved_asset, res.keyword_family, res.confidence, now,
                asset.upper(),
            ),
        )
        return cur.rowcount > 0
    except Exception as exc:  # noqa: BLE001
        log.warning("integrity_reconcile dismiss failed: %s", exc)
        return False


def _touch(conn, asset: str, res: ResolveResult) -> None:
    """Record the re-resolution audit fields without changing flag state."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute(
            "UPDATE integrity_halt SET resolved_asset=?, keyword_family=?, "
            "confidence=?, last_reresolved_at=? WHERE asset=? AND active=1",
            (res.resolved_asset, res.keyword_family, res.confidence, now, asset.upper()),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("integrity_reconcile touch failed: %s", exc)


def reconcile_persisted_flags(
    positions: list[dict[str, Any]] | None,
    *,
    plan_assets: set[str] | None = None,
    blocklist: set[str] | None = None,
    alias_map: dict[str, str] | None = None,
    conf_min: float = CONF_MIN,
) -> list[tuple[str, str]]:
    """Re-resolve EVERY open auto-dismissible flag against its OWN excerpt.

    Feed-window independent: a stale flag clears even if its originating rumor
    is no longer in the current feed. Returns ``[(asset, reason), ...]`` for
    every flag auto-dismissed this pass. NEVER raises.
    """
    dismissed: list[tuple[str, str]] = []
    try:
        flags = ih.get_active_flags()
        if not flags:
            return dismissed
        blocklist = {a.upper() for a in (blocklist if blocklist is not None else ih._blocklist())}
        alias_map = alias_map if alias_map is not None else ih.integrity_aliases()
        shielded = ih.shielded_assets()
        held_neg = ih._held_negative(positions)
        held_all = _held_tickers(positions)
        conn = _conn()
        try:
            for f in flags:
                asset = str(f.get("asset") or "").upper()
                if not asset:
                    continue
                # Shielded / manually-pinned flags NEVER auto-clear.
                if f.get("shielded") or asset in shielded:
                    continue
                if f.get("auto_dismissible") == 0:
                    continue

                excerpt = f.get("excerpt") or ""
                res = reresolve_excerpt(
                    excerpt, positions=positions, alias_map=alias_map,
                )
                resolved = res.resolved_asset

                # R-EQUITY-DEDUP-DREAMCASH (2026-07-07): persisted flags whose
                # stored excerpt reads as PRICE-ACTION commentary (trader chart
                # talk — "slow rug", "struggling at support") are false
                # positives and auto-dismiss BEFORE the held-adverse guard.
                # Real-event patterns (exploit/hack/drained/mint/…) always win
                # inside is_price_action_context, so genuine events keep the
                # guard. Without this, a stale PA flag on a held-negative
                # asset (the BTC ZordXBT tweet) was pinned alive forever.
                if excerpt and ih.is_price_action_context(excerpt):
                    if _dismiss(conn, asset, "price_action_commentary", res):
                        dismissed.append((asset, "price_action_commentary"))
                    else:
                        _touch(conn, asset, res)
                    continue

                # GUARD: a genuine flag whose excerpt still maps to a HELD asset
                # with adverse PnL must NEVER auto-clear (even if text drifted).
                if resolved == asset and asset in held_neg:
                    _touch(conn, asset, res)
                    continue

                reason: str | None = None
                if resolved is not None and resolved != asset:
                    reason = "re_resolved_to_other_asset"
                elif resolved is not None and (
                    resolved in blocklist or resolved not in held_all
                ):
                    reason = "asset_blocklisted"
                elif resolved is None and res.confidence < conf_min:
                    reason = "no_identifiable_asset"

                if reason and _dismiss(conn, asset, reason, res):
                    dismissed.append((asset, reason))
                else:
                    _touch(conn, asset, res)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("reconcile_persisted_flags failed: %s", exc)
    return dismissed
