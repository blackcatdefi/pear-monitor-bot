"""R-INTEGRITY-FIX (P0.1) — subject-resolution regression guards.

Reproduces the 2026-06-05 21:57 UTC live false-positive: an INTEGRITY-HALT
mis-attributed a *Zcash/Orchard* "unlimited mint" rumor to BTC, purely because
BTC was a held position with adverse PnL at scan time. The fix binds a rumor to
the asset it actually NAMES (alias-resolved, proximity-aware), and only fires a
per-position STOP when the resolved subject is held + adverse + NOT blocklisted.
"""
from __future__ import annotations

import importlib

import modules.integrity_halt as ih
from modules.integrity_halt import (
    scan_integrity,
    scan_integrity_signals,
    integrity_aliases,
    raise_flags,
    get_active_flags,
    dismiss,
)


def _pos(coin, upnl):
    return {"coin": coin, "side": "LONG", "unrealized_pnl": upnl}


def _tg(text, channel="AIXBT Daily Reports"):
    return {"status": "ok", "data": [
        {"channel": channel, "handle": "daily",
         "messages": [{"date": "2026-06-05", "text": text}]},
    ]}


# The exact production-shaped rumor blob: a long daily report that mentions BTC
# (held, adverse) in one place and the Zcash bug in another.
LIVE_BLOB = (
    "Daily wrap: BTC chops near 105k as funding cools and SOL holds support. "
    "Meanwhile, Zcash bug crisis deepens — the Orchard pool flaw allowed "
    "unlimited minting since 2022, devs scrambling. Risk-off into the weekend."
)


# ── (a) the exact live shape — must NOT flag BTC/SOL, must resolve to ZEC ─────
def test_live_zcash_blob_does_not_flag_btc_or_sol():
    positions = [_pos("BTC", -3400.0), _pos("SOL", -900.0)]
    scan = scan_integrity(positions, _tg(LIVE_BLOB))
    flagged = {h.asset for h in scan.hits}
    assert "BTC" not in flagged
    assert "SOL" not in flagged
    assert scan.hits == []  # nothing fires: subject ZEC is blocklisted
    # ZEC surfaces as a blocklisted info note (no action), never a STOP.
    blocked = {n.asset for n in scan.notes if n.reason == "blocklisted"}
    assert "ZEC" in blocked


def test_live_blob_via_signals_wrapper_returns_no_hits():
    positions = [_pos("BTC", -3400.0), _pos("SOL", -900.0)]
    assert scan_integrity_signals(positions, _tg(LIVE_BLOB)) == []


# ── (b) a genuine rumor on a held non-blocklisted asset still fires + persists ─
def test_genuine_held_nonblocklisted_fires_and_persists(monkeypatch, tmp_path):
    db = tmp_path / "intel.db"
    import modules.intel_memory as im
    monkeypatch.setattr(im, "DB_PATH", str(db))

    intel = _tg("ARB delisting + hack rumor confirmed by multiple sources")
    scan = scan_integrity([_pos("ARB", -250.0)], intel)
    assert [h.asset for h in scan.hits] == ["ARB"]

    raise_flags(scan.hits)
    active = {f["asset"] for f in get_active_flags()}
    assert "ARB" in active
    # Re-surfaces on a later run until dismissed.
    active2 = {f["asset"] for f in get_active_flags()}
    assert "ARB" in active2
    assert dismiss("ARB") is True
    assert "ARB" not in {f["asset"] for f in get_active_flags()}


# ── (c) multiple held positions, rumor names only one → only that one flags ───
def test_only_named_held_asset_flags():
    positions = [_pos("BTC", -100.0), _pos("ARB", -100.0)]
    intel = _tg("ARB sequencer backdoor exploit — funds at risk")
    scan = scan_integrity(positions, intel)
    assert [h.asset for h in scan.hits] == ["ARB"]
    assert "BTC" not in {h.asset for h in scan.hits}


# ── (d) unresolved rumor (no known asset named) → no per-position STOP ────────
def test_unresolved_rumor_fires_nothing():
    positions = [_pos("BTC", -100.0), _pos("SOL", -100.0)]
    intel = _tg("massive exploit / infinite mint discovered on FOOBARXYZ")
    scan = scan_integrity(positions, intel)
    assert scan.hits == []
    assert any(n.reason == "unresolved" for n in scan.notes)


def test_unresolved_does_not_attach_to_adverse_position():
    # Even with several held-adverse positions, an unnamed rumor attaches to none.
    positions = [_pos("BTC", -5000.0), _pos("SOL", -5000.0), _pos("HYPE", -5000.0)]
    intel = _tg("there is some hack and an exploit somewhere in defi today")
    assert scan_integrity_signals(positions, intel) == []


# ── (e) alias resolution — names map to tickers, case/boundary aware ──────────
def test_alias_map_seeds_zcash_orchard_and_fund_assets():
    al = integrity_aliases()
    assert al.get("zcash") == "ZEC"
    assert al.get("orchard") == "ZEC"
    assert al.get("bitcoin") == "BTC"
    assert al.get("solana") == "SOL"
    assert al.get("hype") == "HYPE"


def test_name_resolves_held_asset_case_insensitive():
    # "Bitcoin" (name) resolves to BTC even though the ticker isn't written.
    intel = _tg("Bitcoin core devs confirm a critical consensus exploit tonight")
    scan = scan_integrity([_pos("BTC", -100.0)], intel)
    assert [h.asset for h in scan.hits] == ["BTC"]


def test_orchard_name_resolves_to_zec_and_is_suppressed():
    intel = _tg("Orchard pool backdoor — unlimited mint possible")
    scan = scan_integrity([_pos("BTC", -100.0)], intel)
    assert scan.hits == []  # ZEC blocklisted, BTC not the subject


def test_word_boundary_alias_no_substring_hit():
    # "hyped" must not trigger the HYPE alias; no asset → unresolved, no STOP.
    intel = _tg("the market is super hyped about this exploit narrative")
    scan = scan_integrity([_pos("HYPE", -100.0)], intel)
    assert scan.hits == []


def test_alias_env_extension(monkeypatch):
    monkeypatch.setenv("INTEGRITY_ASSET_ALIASES", "fartcoin:FART")
    import config as cfg
    importlib.reload(cfg)
    importlib.reload(ih)
    try:
        al = ih.integrity_aliases()
        assert al.get("fartcoin") == "FART"
        intel = _tg("Fartcoin contract has a backdoor mint exploit")
        scan = ih.scan_integrity([{"coin": "FART", "side": "LONG", "unrealized_pnl": -10.0}], intel)
        assert [h.asset for h in scan.hits] == ["FART"]
    finally:
        monkeypatch.delenv("INTEGRITY_ASSET_ALIASES", raising=False)
        importlib.reload(cfg)
        importlib.reload(ih)


# ── (f) shielded subject (non-blocklisted) — no auto-clear, /haltclear works ──
def test_shielded_nonblocklisted_no_auto_clear_until_dismiss(monkeypatch, tmp_path):
    db = tmp_path / "intel.db"
    import modules.intel_memory as im
    monkeypatch.setattr(im, "DB_PATH", str(db))

    # XMR (Monero) is shielded but NOT blocklisted → fires.
    intel = _tg("Monero double-spend exploit rumor, devs silent")
    scan = scan_integrity([_pos("XMR", -700.0)], intel)
    assert [h.asset for h in scan.hits] == ["XMR"]
    assert scan.hits[0].shielded is True
    raise_flags(scan.hits)

    # A later CLEAN run (no rumor) must NOT auto-clear the shielded flag.
    clean = scan_integrity([_pos("XMR", -700.0)], _tg("XMR up 3% on the day"))
    raise_flags(clean.hits)  # nothing new
    assert "XMR" in {f["asset"] for f in get_active_flags()}

    # Only explicit dismissal (/haltclear) clears it.
    assert dismiss("XMR") is True
    assert "XMR" not in {f["asset"] for f in get_active_flags()}
