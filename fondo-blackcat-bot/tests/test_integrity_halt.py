"""R-AUDIT2-P1.3 — INTEGRITY-HALT detector (ZEC-born rule).

A credibility/integrity signal on a HELD asset with NEGATIVE UPnL raises a
🛑 STOP-accumulation flag (MANUAL REVIEW, never an auto-action). A held asset
with positive UPnL, or no integrity keyword, never raises. Shielded/opaque
assets never auto-clear on "no confirmation".
"""
from __future__ import annotations

from modules.integrity_halt import (
    scan_integrity_signals,
    build_integrity_block,
    stop_line,
    shielded_assets,
)

# Exact STOP wording is asset-agnostic (formatter). XMR is shielded but NOT
# blocklisted, so it is the canonical "STOP still fires" example post-R-AUDIT2
# (ZEC is now permanently blocklisted → its rumors are suppressed, see below).
EXACT = ("STOP accumulation on ZEC: integrity rumor + adverse PnL. "
         "Do NOT DCA/add/average. Await news. Never catch a falling knife.")
EXACT_XMR = ("STOP accumulation on XMR: integrity rumor + adverse PnL. "
             "Do NOT DCA/add/average. Await news. Never catch a falling knife.")


def _pos(coin, upnl):
    return {"coin": coin, "side": "LONG", "unrealized_pnl": upnl}


def _tg(text):
    """A telegram-unread-shaped feed carrying one message."""
    return {"status": "ok", "data": [
        {"channel": "ZachXBT", "handle": "investigations",
         "messages": [{"date": "2026-06-05", "text": text}]},
    ]}


def test_doublespend_on_held_negative_raises_exact_wording():
    intel = _tg("BREAKING: possible double-spend undetectable on Monero, devs silent")
    hits = scan_integrity_signals([_pos("XMR", -1200.0)], intel)
    assert len(hits) == 1
    assert hits[0].asset == "XMR"
    assert hits[0].shielded is True
    block = build_integrity_block(
        [{"asset": "XMR", "keyword": hits[0].keyword, "excerpt": hits[0].excerpt,
          "source": hits[0].source, "shielded": True}]
    )
    assert EXACT_XMR in block
    assert stop_line("ZEC") == EXACT       # formatter is asset-agnostic
    assert stop_line("XMR") == EXACT_XMR
    assert "🛑" in block
    assert "NO se auto-limpia" in block  # shielded nuance surfaced


def test_positive_upnl_does_not_raise():
    intel = _tg("rumor of an exploit / double-spend on ZEC")
    assert scan_integrity_signals([_pos("ZEC", +500.0)], intel) == []


def test_no_keyword_does_not_raise():
    intel = _tg("ZEC up 4% today, healthy volume, nothing unusual")
    assert scan_integrity_signals([_pos("ZEC", -800.0)], intel) == []


def test_keyword_but_asset_not_held_does_not_raise():
    intel = _tg("massive exploit / infinite mint discovered on FOOBAR")
    assert scan_integrity_signals([_pos("ZEC", -800.0)], intel) == []


def test_x_feed_shape_is_scanned():
    intel = {"status": "ok", "tweets": [
        {"username": "zachxbt", "text": "XMR backdoor — insolvency risk, get out"},
    ]}
    hits = scan_integrity_signals([_pos("XMR", -100.0)], intel)
    assert len(hits) == 1 and hits[0].asset == "XMR"


def test_whole_word_match_avoids_substring_false_positive():
    # "ZECASH" should NOT match the ZEC ticker (word-boundary guard).
    intel = _tg("ZECASHX token exploit rumor")  # not our ZEC
    assert scan_integrity_signals([_pos("ZEC", -100.0)], intel) == []


def test_non_shielded_asset_flags_too():
    intel = _tg("ARB delisting + hack rumor circulating")
    hits = scan_integrity_signals([_pos("ARB", -50.0)], intel)
    assert len(hits) == 1
    assert hits[0].asset == "ARB"
    assert hits[0].shielded is False


def test_shielded_set_contains_privacy_coins():
    s = shielded_assets()
    assert "ZEC" in s and "XMR" in s


def test_empty_block_when_no_flags():
    assert build_integrity_block([]) == ""
    assert build_integrity_block(None) == ""


def test_disabled_via_env(monkeypatch):
    monkeypatch.setenv("INTEGRITY_HALT_ENABLED", "false")
    intel = _tg("ZEC double-spend exploit")
    assert scan_integrity_signals([_pos("ZEC", -1.0)], intel) == []


def test_extra_keyword_via_env(monkeypatch):
    monkeypatch.setenv("INTEGRITY_HALT_KEYWORDS", "ponzi")
    intel = _tg("XMR looks like a ponzi now")
    hits = scan_integrity_signals([_pos("XMR", -1.0)], intel)
    assert len(hits) == 1 and hits[0].keyword == "ponzi"
