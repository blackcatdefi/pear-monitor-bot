"""R-AUDIT2-P1.3 — INTEGRITY-HALT detector (born from the ZEC liquidation).

Scans the intel feeds (Telegram + X) for integrity / credibility signals tied
to a HELD position or DCA-plan asset. When such a signal hits a HELD asset
whose position UPnL is NEGATIVE (the trade is going against us), it raises a
🛑 INTEGRITY-HALT flag for BCD:

    "STOP accumulation on [asset]: integrity rumor + adverse PnL. Do NOT
     DCA/add/average. Await news. Never catch a falling knife."

This is a MANUAL-REVIEW alert ONLY — never an auto-action of any kind. The bot
never closes, reduces, or trades; it surfaces the rumor + adverse PnL so BCD
decides.

Critical nuance (shielded / opaque assets — privacy coins, anything where an
exploit may be invisible on-chain): the flag must NOT be auto-cleared by
"no confirmed exploit yet." Absence of on-chain confirmation is NOT safety. A
raised flag clears ONLY on explicit BCD dismissal (``dismiss``) or a clear
positive resolution recorded by BCD — never automatically, and never just
because the signal didn't recur on a later run. To enforce that, raised flags
are PERSISTED (SQLite) and re-surfaced every run until dismissed.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# ── Config (env-extensible, safe defaults) ──────────────────────────────────
_DEFAULT_KEYWORDS = (
    "exploit", "bug", "double-spend", "double spend", "infinite mint",
    "unlimited mint", "counterfeit", "undetectable", "unrecoverable narrative",
    "insolvency", "insolvent", "hack", "hacked", "depeg", "delisting",
    "delist", "infinite supply", "backdoor", "secretly print", "secret print",
    "rug", "rugpull", "rug pull",
)

# Assets with opaque / shielded accounting where an exploit may be invisible
# on-chain — their flags NEVER auto-clear on "no confirmation".
_DEFAULT_SHIELDED = ("ZEC", "XMR", "DASH", "SCRT", "ROSE", "ARRR", "FIRO", "BEAM")


def _enabled() -> bool:
    return (os.getenv("INTEGRITY_HALT_ENABLED", "true") or "true").lower() == "true"


def _keywords() -> list[str]:
    raw = os.getenv("INTEGRITY_HALT_KEYWORDS", "") or ""
    extra = [k.strip().lower() for k in raw.split(",") if k.strip()]
    kws = list(_DEFAULT_KEYWORDS) + extra
    # Longest first so multi-word phrases match before their sub-words.
    return sorted(set(kws), key=len, reverse=True)


def shielded_assets() -> set[str]:
    raw = os.getenv("INTEGRITY_SHIELDED_ASSETS", "") or ""
    extra = {a.strip().upper() for a in raw.split(",") if a.strip()}
    return {a.upper() for a in _DEFAULT_SHIELDED} | extra


@dataclass(frozen=True)
class IntegrityHit:
    asset: str
    keyword: str
    excerpt: str
    source: str
    shielded: bool


@dataclass(frozen=True)
class IntegrityNote:
    """A suppressed / informational rumor — NEVER a per-position STOP.

    reason ∈ {"blocklisted", "not_held", "unresolved"}.
    """
    asset: str | None
    keyword: str
    excerpt: str
    source: str
    reason: str


@dataclass(frozen=True)
class IntegrityScan:
    hits: list[IntegrityHit]
    notes: list[IntegrityNote]


# ── Feed text harvesting (robust to every intel shape we pass) ───────────────
_TEXT_KEYS = ("text", "message", "snippet", "title", "body", "summary", "content")


def _harvest_texts(node: Any, source: str = "intel", out: list | None = None) -> list[tuple[str, str]]:
    """Recursively collect (source, text) from any nested intel structure.

    Handles telegram (data: list/dict of channels → messages[{text}]),
    x_intel (tweets:[{text}] / data by_user), gmail, etc. NEVER raises.
    """
    if out is None:
        out = []
    try:
        if isinstance(node, str):
            if node.strip():
                out.append((source, node))
        elif isinstance(node, dict):
            # capture text-ish leaf values at this level
            label = node.get("channel") or node.get("handle") or node.get("username") or source
            for k, v in node.items():
                if k in _TEXT_KEYS and isinstance(v, str) and v.strip():
                    out.append((str(label), v))
                else:
                    _harvest_texts(v, str(label) if isinstance(label, str) else source, out)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _harvest_texts(item, source, out)
    except Exception:  # noqa: BLE001
        pass
    return out


def _held_negative(positions: list[dict[str, Any]] | None) -> dict[str, float]:
    """Map ``{COIN: upnl}`` for HELD positions whose UPnL is NEGATIVE."""
    held: dict[str, float] = {}
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        coin = str(p.get("coin") or "").upper().strip()
        if not coin or coin == "?":
            continue
        try:
            upnl = float(p.get("unrealized_pnl") if p.get("unrealized_pnl") is not None
                         else p.get("unrealizedPnl") or 0.0)
        except (TypeError, ValueError):
            upnl = 0.0
        # Keep the most-negative read if a coin appears across wallets.
        if upnl < 0 and (coin not in held or upnl < held[coin]):
            held[coin] = upnl
    return held


def _word_positions(needle: str, text_lower: str) -> list[int]:
    """All whole-word match start offsets of ``needle`` in ``text_lower``.

    Case-insensitive (caller lowers) and word-boundary aware so a ticker like
    ZEC never matches inside ZECASH and a name like ETH never matches ethernet.
    """
    pat = rf"(?<![A-Za-z0-9]){re.escape(needle.lower())}(?![A-Za-z0-9])"
    return [m.start() for m in re.finditer(pat, text_lower)]


def _asset_in_text(asset: str, text_lower: str) -> bool:
    """Whole-word ticker match (case-insensitive), avoids substring noise."""
    return bool(_word_positions(asset, text_lower))


def integrity_aliases() -> dict[str, str]:
    """name(lower) → TICKER(upper) resolution map (config-driven, env-extensible)."""
    try:
        from config import INTEGRITY_ASSET_ALIASES
        return {str(k).lower(): str(v).upper() for k, v in dict(INTEGRITY_ASSET_ALIASES).items()}
    except Exception:  # noqa: BLE001 — never let config break a scan
        return {
            "bitcoin": "BTC", "btc": "BTC", "ethereum": "ETH", "eth": "ETH",
            "solana": "SOL", "sol": "SOL", "hyperliquid": "HYPE", "hype": "HYPE",
            "zcash": "ZEC", "zec": "ZEC", "orchard": "ZEC", "monero": "XMR", "xmr": "XMR",
        }


def _blocklist() -> set[str]:
    try:
        from config import CYCLE_DCA_BLOCKLIST
        return {a.upper() for a in CYCLE_DCA_BLOCKLIST}
    except Exception:  # noqa: BLE001
        return {"ZEC"}


# How far (chars) a distinctive protocol/asset NAME may sit from an integrity
# keyword and still out-rank a merely co-mentioned held ticker. Generous enough
# to span a full sentence/tweet clause, tight enough that an unrelated coin three
# sentences away never captures the signal.
_DISTINCTIVE_WINDOW = 220


def _resolve_subjects(
    text_lower: str,
    kw_positions: list[int],
    *,
    held_tickers: set[str],
    alias_map: dict[str, str],
) -> set[str]:
    """Bind each integrity-keyword occurrence to the asset it actually NAMES.

    Subject resolution is proximity-aware but NOT naive-nearest. A *distinctive*
    name — a protocol/project word like ``orchard``/``zcash``/``sapling`` (≥4
    chars, not merely a bare ticker) — is a high-confidence subject anchor: when
    one sits within ``_DISTINCTIVE_WINDOW`` of the keyword it takes PRECEDENCE
    over any held ticker (e.g. BTC) that merely happens to sit closer in the same
    multi-coin blob. This kills the live false BTC STOP that fired off a
    Zcash/Orchard "unlimited mint" rumor: ``unlimited mint``/``orchard``/``zcash``
    hard-resolve to ZEC regardless of which held coin is textually nearest.

    A held coin only captures a keyword when NO distinctive name is in range —
    i.e. the rumor genuinely names that held asset (by ticker or its own name).
    Detects held HL tickers and project/coin NAMES via the alias map. Returns the
    set of resolved tickers, or empty when the text names no known asset
    (fail-closed: an unresolved rumor must NOT attach to any held position).
    """
    # mention = (offset, TICKER, is_distinctive_name)
    mentions: list[tuple[int, str, bool]] = []
    for ticker in held_tickers:
        for pos in _word_positions(ticker, text_lower):
            mentions.append((pos, ticker.upper(), False))
    for name, ticker in alias_map.items():
        distinctive = len(name) >= 4 and name.lower() != str(ticker).lower()
        for pos in _word_positions(name, text_lower):
            mentions.append((pos, ticker.upper(), distinctive))
    if not mentions:
        return set()
    subjects: set[str] = set()
    for kpos in kw_positions:
        # PRECEDENCE: a distinctive asset/protocol NAME within the local window
        # wins outright over a nearer bare held ticker. Only when no distinctive
        # name is in range does the rumor fall back to nearest-overall (so a
        # genuine "$BTC exploit" with no other named asset still binds BTC).
        near_distinctive = [
            m for m in mentions if m[2] and abs(m[0] - kpos) <= _DISTINCTIVE_WINDOW
        ]
        pool = near_distinctive or mentions
        nearest = min(pool, key=lambda m: abs(m[0] - kpos))
        subjects.add(nearest[1])
    return subjects


def scan_integrity(
    positions: list[dict[str, Any]] | None,
    intel: Any,
    *,
    plan_assets: set[str] | None = None,
    blocklist: set[str] | None = None,
    alias_map: dict[str, str] | None = None,
) -> IntegrityScan:
    """Resolve each integrity rumor to the asset it NAMES, then classify it.

    A per-position STOP (``IntegrityHit``) fires ONLY for a resolved subject
    that is (i) currently held with NEGATIVE UPnL and (ii) NOT in the DCA
    blocklist. Subjects that are blocklisted, not held, or unresolvable are
    surfaced as low-severity ``IntegrityNote``s — never a STOP, never a
    MANUAL-REVIEW escalation tied to an unrelated held asset. NEVER raises.
    """
    if not _enabled():
        return IntegrityScan([], [])
    held_neg = _held_negative(positions)
    held_all = {
        str(p.get("coin") or "").upper().strip()
        for p in (positions or []) if isinstance(p, dict) and (p.get("coin") or "").strip()
    }
    held_all.discard("")
    held_all.discard("?")

    blocklist = {a.upper() for a in (blocklist if blocklist is not None else _blocklist())}
    alias_map = alias_map if alias_map is not None else integrity_aliases()
    kws = _keywords()
    shielded = shielded_assets()
    # Detect any named asset: held tickers, alias targets, and plan assets.
    detect_tickers = set(held_all) | {v.upper() for v in alias_map.values()}
    if plan_assets:
        detect_tickers |= {a.upper() for a in plan_assets}

    hits: dict[str, IntegrityHit] = {}
    notes: dict[tuple[str | None, str], IntegrityNote] = {}
    for source, text in _harvest_texts(intel):
        tl = text.lower()
        kw_positions: dict[str, list[int]] = {}
        for k in kws:
            pos = [m.start() for m in re.finditer(re.escape(k), tl)]
            if pos:
                kw_positions[k] = pos
        if not kw_positions:
            continue
        all_kw_pos = [p for plist in kw_positions.values() for p in plist]
        first_kw = min(kw_positions, key=lambda k: min(kw_positions[k]))
        excerpt = text.strip().replace("\n", " ")
        if len(excerpt) > 180:
            excerpt = excerpt[:177] + "…"

        subjects = _resolve_subjects(
            tl, all_kw_pos, held_tickers=detect_tickers, alias_map=alias_map
        )
        if not subjects:
            # Fail-closed: a keyword with no resolvable subject NEVER attaches
            # to a held position. At most a single generic info note.
            key = (None, "unresolved")
            if key not in notes:
                notes[key] = IntegrityNote(
                    asset=None, keyword=first_kw, excerpt=excerpt,
                    source=str(source), reason="unresolved",
                )
            continue
        for asset in subjects:
            if asset in hits:
                continue
            if asset in blocklist:
                notes.setdefault((asset, "blocklisted"), IntegrityNote(
                    asset=asset, keyword=first_kw, excerpt=excerpt,
                    source=str(source), reason="blocklisted",
                ))
                continue
            if asset not in held_neg:
                notes.setdefault((asset, "not_held"), IntegrityNote(
                    asset=asset, keyword=first_kw, excerpt=excerpt,
                    source=str(source), reason="not_held",
                ))
                continue
            hits[asset] = IntegrityHit(
                asset=asset, keyword=first_kw, excerpt=excerpt,
                source=str(source), shielded=(asset in shielded),
            )
    # A held+adverse subject that genuinely fires must not also linger as a note.
    notes = {k: v for k, v in notes.items() if k[0] not in hits}
    return IntegrityScan(list(hits.values()), list(notes.values()))


def scan_integrity_signals(
    positions: list[dict[str, Any]] | None,
    intel: Any,
    *,
    plan_assets: set[str] | None = None,
    blocklist: set[str] | None = None,
    alias_map: dict[str, str] | None = None,
) -> list[IntegrityHit]:
    """Backward-compatible wrapper — returns only the fireable STOP hits."""
    return scan_integrity(
        positions, intel, plan_assets=plan_assets,
        blocklist=blocklist, alias_map=alias_map,
    ).hits


# ── Persistence (raised flags never auto-clear) ──────────────────────────────
def _conn():
    from modules.intel_memory import _get_conn
    conn = _get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS integrity_halt (
            asset TEXT PRIMARY KEY,
            keyword TEXT,
            excerpt TEXT,
            source TEXT,
            shielded INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            raised_ts TEXT,
            dismissed_ts TEXT
        )
        """
    )
    conn.commit()
    return conn


def raise_flags(hits: list[IntegrityHit]) -> list[str]:
    """Persist hits as ACTIVE flags. Returns assets newly raised this run."""
    newly: list[str] = []
    if not hits:
        return newly
    try:
        conn = _conn()
        now = datetime.now(timezone.utc).isoformat()
        for h in hits:
            row = conn.execute(
                "SELECT active FROM integrity_halt WHERE asset=?", (h.asset,)
            ).fetchone()
            already_active = bool(row and row["active"])
            conn.execute(
                "INSERT INTO integrity_halt "
                "(asset, keyword, excerpt, source, shielded, active, raised_ts) "
                "VALUES (?,?,?,?,?,1,?) "
                "ON CONFLICT(asset) DO UPDATE SET "
                "  keyword=excluded.keyword, excerpt=excluded.excerpt, "
                "  source=excluded.source, shielded=excluded.shielded, active=1, "
                "  raised_ts=COALESCE(integrity_halt.raised_ts, excluded.raised_ts)",
                (h.asset, h.keyword, h.excerpt, h.source, int(h.shielded), now),
            )
            if not already_active:
                newly.append(h.asset)
        conn.commit()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("raise_flags failed: %s", exc)
    return newly


def get_active_flags() -> list[dict[str, Any]]:
    try:
        conn = _conn()
        rows = conn.execute(
            "SELECT asset, keyword, excerpt, source, shielded, raised_ts "
            "FROM integrity_halt WHERE active=1 ORDER BY raised_ts ASC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.warning("get_active_flags failed: %s", exc)
        return []


def dismiss(asset: str, *, resolution: str = "BCD dismissal") -> bool:
    """Explicit BCD dismissal / positive resolution — the ONLY way to clear."""
    try:
        conn = _conn()
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "UPDATE integrity_halt SET active=0, dismissed_ts=? WHERE asset=? AND active=1",
            (f"{now} ({resolution})", asset.upper()),
        )
        conn.commit()
        cleared = cur.rowcount > 0
        conn.close()
        return cleared
    except Exception as exc:  # noqa: BLE001
        log.warning("dismiss failed: %s", exc)
        return False


# ── Rendering ────────────────────────────────────────────────────────────────
def stop_line(asset: str) -> str:
    """The exact STOP wording (single source of truth)."""
    return (
        f"STOP accumulation on {asset}: integrity rumor + adverse PnL. "
        f"Do NOT DCA/add/average. Await news. Never catch a falling knife."
    )


def build_integrity_block(active_flags: list[dict[str, Any]] | None) -> str:
    """Render the 🛑 INTEGRITY-HALT block. Empty string when no active flags."""
    active_flags = active_flags or []
    if not active_flags:
        return ""
    lines = [
        "🛑 INTEGRITY-HALT — MANUAL REVIEW (NUNCA auto-acción; decide BCD)",
    ]
    for f in active_flags:
        asset = str(f.get("asset") or "?").upper()
        lines.append(f"🛑 {stop_line(asset)}")
        kw = f.get("keyword")
        src = f.get("source")
        if kw:
            lines.append(f"   señal: «{kw}» — fuente: {src}")
        exc = f.get("excerpt")
        if exc:
            lines.append(f"   “{exc}”")
        if f.get("shielded"):
            lines.append(
                "   ⚠ activo opaco/shielded: la AUSENCIA de exploit confirmado "
                "NO es seguridad — el flag NO se auto-limpia (solo lo cierra BCD)."
            )
    return "\n".join(lines)


def build_notes_block(notes: list[IntegrityNote] | None) -> str:
    """Render low-severity informational lines for suppressed rumors.

    These are NEVER STOPs — they only acknowledge that a rumor was seen and
    consciously not acted on (blocklisted / not held / unresolvable).
    """
    notes = notes or []
    if not notes:
        return ""
    lines: list[str] = []
    for n in notes:
        if n.reason == "blocklisted":
            lines.append(
                f"ℹ️ Rumor de integridad notado para {n.asset} «{n.keyword}» "
                f"(en blocklist DCA / fuera del plan) — sin acción."
            )
        elif n.reason == "not_held":
            lines.append(
                f"ℹ️ Rumor de integridad notado para {n.asset} «{n.keyword}» "
                f"(no en posición) — sin acción."
            )
        else:  # unresolved
            lines.append(
                f"ℹ️ Rumor de integridad sin activo identificable «{n.keyword}» "
                f"— sin acción (no se atribuye a ninguna posición)."
            )
    return "\n".join(lines)


def _norm_excerpt(s: Any) -> str:
    """Normalise an excerpt for cross-run equality (whitespace + case)."""
    return " ".join(str(s or "").split()).lower()


def reconcile_misattributed(
    scan: IntegrityScan,
    active_flags: list[dict[str, Any]] | None,
) -> list[str]:
    """Self-heal flags that a SUPERSEDED subject-resolver mis-attributed.

    A flag raised by an older resolver (e.g. the pre-R-INTEGRITY-FIX nearest-
    ticker logic that bound a Zcash/Orchard "unlimited mint" rumor to the held
    BTC leg) lingers in SQLite forever because raised flags never auto-clear.
    Now that the distinctive-name resolver re-reads the SAME rumor excerpt and
    correctly attributes it to a DIFFERENT concrete asset (ZEC), that old flag
    is provably a misattribution — not a genuine unresolved risk — so we dismiss
    it with an audit-trail resolution.

    Strict guards (never clear a real risk):
      * the flag's asset is NOT in the current scan's fireable hits, AND
      * the flag is NOT shielded (shielded flags never auto-clear, ever), AND
      * the SAME excerpt now resolves to a DIFFERENT, concrete (non-None) asset
        in the current scan (hit OR note).

    Returns the list of assets auto-dismissed. NEVER raises.
    """
    dismissed: list[str] = []
    try:
        flags = active_flags or []
        if not flags:
            return dismissed
        hit_assets = {h.asset.upper() for h in scan.hits}
        shielded = shielded_assets()
        # excerpt(normalised) → set of concrete assets it now resolves to.
        excerpt_assets: dict[str, set[str]] = {}
        for item in list(scan.hits) + list(scan.notes):
            asset = getattr(item, "asset", None)
            if not asset:
                continue
            excerpt_assets.setdefault(
                _norm_excerpt(getattr(item, "excerpt", "")), set()
            ).add(str(asset).upper())
        for f in flags:
            asset = str(f.get("asset") or "").upper()
            if not asset or asset in hit_assets:
                continue
            if asset in shielded or f.get("shielded"):
                continue
            others = excerpt_assets.get(_norm_excerpt(f.get("excerpt")), set())
            if others and others != {asset} and any(a != asset for a in others):
                if dismiss(
                    asset,
                    resolution="auto-reconcile: rumor re-attributed to "
                    + ",".join(sorted(a for a in others if a != asset)),
                ):
                    dismissed.append(asset)
    except Exception as exc:  # noqa: BLE001
        log.warning("reconcile_misattributed failed: %s", exc)
    return dismissed


def run_integrity_halt(
    positions: list[dict[str, Any]] | None,
    intel: Any,
    *,
    plan_assets: set[str] | None = None,
) -> tuple[str, list[str]]:
    """Scan → persist → reconcile → render. Returns (block, newly_raised).

    NEVER raises — integrity scanning must never break /reporte. The STOP
    block (persisted, MANUAL-REVIEW) is followed by any low-severity info
    lines for rumors that were consciously suppressed. Before rendering, any
    persisted flag that the (now-corrected) resolver re-attributes to a
    different concrete asset is auto-dismissed (self-heal of misattribution).
    """
    try:
        scan = scan_integrity(positions, intel, plan_assets=plan_assets)
        newly = raise_flags(scan.hits)
        # Self-heal BEFORE reading active flags so a misattributed legacy flag
        # (e.g. a false BTC STOP off a Zcash rumor) is gone from the rendered
        # block the moment the corrected resolver re-reads the same excerpt.
        reconcile_misattributed(scan, get_active_flags())
        # R-INTEGRITY-RECONCILE-FIX (2026-06-07): a feed-window-INDEPENDENT pass
        # that re-reads each persisted flag's OWN stored excerpt and dismisses it
        # when the corrected resolver no longer attributes it to a held asset.
        # This clears orphaned flags (e.g. the false BTC STOP off the Zcash
        # "unlimited mint" rumor) even after the originating excerpt has rotated
        # out of the live feed — the exact 2026-06-07 production failure.
        try:
            from modules.integrity_reconcile import reconcile_persisted_flags
            reconcile_persisted_flags(positions, plan_assets=plan_assets)
        except Exception as _exc:  # noqa: BLE001 — never break /reporte
            log.warning("reconcile_persisted_flags wiring failed: %s", _exc)
        block = build_integrity_block(get_active_flags())
        notes_block = build_notes_block(scan.notes)
        if notes_block:
            block = f"{block}\n{notes_block}" if block else notes_block
        return block, newly
    except Exception as exc:  # noqa: BLE001
        log.warning("run_integrity_halt failed: %s", exc)
        return "", []
