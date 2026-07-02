"""R-BOT-DEFINITIVE-2 T4 — integrity-halt price-action false-positive filter.

Mission spec: if a matched message ALSO matches ≥1 price-action pattern AND
does NOT match a real-event pattern, downgrade the HALT to an informational
note ("price-action context, not an integrity event"). Real-event patterns
ALWAYS win.
"""
from __future__ import annotations

from modules.integrity_halt import (
    build_notes_block,
    is_price_action_context,
    scan_integrity,
)

HELD = [{"coin": "HYPE", "unrealized_pnl": -1500.0}]
ALIASES = {"hyperliquid": "HYPE", "hype": "HYPE"}

# Representative ZordXBT-style trader commentary: matches the integrity
# keyword «rug» but is pure price-action talk about a held asset.
ZORD_MSG = (
    "$HYPE price action looks bearish, struggling to hold support at the "
    "yearly low — chart shows a clear downtrend, feels like a slow rug if "
    "this level breaks"
)


def _scan(text: str):
    intel = {"data": [{"channel": "ZordXBT", "messages": [{"text": text}]}]}
    return scan_integrity(HELD, intel, blocklist=set(), alias_map=ALIASES)


# ─── 1. Trader commentary downgrades: NO STOP, info note instead ─────────────
def test_zordxbt_price_action_message_downgrades():
    scan = _scan(ZORD_MSG)
    assert scan.hits == []                       # NO STOP raised
    pa = [n for n in scan.notes if n.reason == "price_action"]
    assert len(pa) == 1
    assert pa[0].asset == "HYPE"
    block = build_notes_block(scan.notes)
    assert "price-action context, not an integrity event" in block
    assert "STOP" not in block


def test_is_price_action_context_pure_helper():
    assert is_price_action_context(ZORD_MSG) is True
    assert is_price_action_context("HYPE protocol exploited, funds drained") is False
    assert is_price_action_context("") is False
    assert is_price_action_context(None) is False


# ─── 2. Real event still HALTs ───────────────────────────────────────────────
def test_real_exploit_still_halts():
    scan = _scan("HYPE protocol exploited, funds drained from the bridge")
    assert len(scan.hits) == 1
    assert scan.hits[0].asset == "HYPE"


# ─── 3. Both patterns present → real-event WINS → HALT ───────────────────────
def test_both_patterns_real_event_wins():
    scan = _scan(
        "HYPE chart looks bearish at support, but the protocol was hacked "
        "and funds drained — insolvency risk"
    )
    assert len(scan.hits) == 1
    assert scan.hits[0].asset == "HYPE"
    assert not any(n.reason == "price_action" for n in scan.notes)


# ─── 4. Price-action with unresolvable subject → generic note, no STOP ───────
def test_price_action_unresolved_subject_generic_note():
    scan = _scan("this chart is struggling at support, total rug vibes")
    assert scan.hits == []
    pa = [n for n in scan.notes if n.reason == "price_action"]
    assert len(pa) == 1 and pa[0].asset is None


# ─── 5. Non-price-action, non-event message keeps old behaviour ──────────────
def test_plain_rumor_without_price_action_unchanged():
    scan = _scan("rumor: hyperliquid team can secretly print unlimited supply")
    assert len(scan.hits) == 1 and scan.hits[0].asset == "HYPE"
