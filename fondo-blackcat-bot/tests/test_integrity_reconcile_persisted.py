"""R-INTEGRITY-RECONCILE-FIX (2026-06-07) — persisted-flag re-resolution pass.

Reproduces the live 2026-06-07 06:59 UTC failure: a persisted false
"STOP accumulation on BTC" flag raised off a Zcash/Orchard "unlimited mint"
rumor that survived R-PM-LIQ's feed-window-coupled reconcile because the
originating excerpt rotated out of the feed (or the live scan fired on a
different keyword). The new pass re-reads each flag's OWN stored excerpt,
re-resolves it against the current resolver, and clears orphaned flags —
independent of the live feed window.
"""
from __future__ import annotations

import modules.intel_memory as im
import modules.integrity_halt as ih
from modules.integrity_halt import (
    IntegrityHit,
    raise_flags,
    get_active_flags,
    run_integrity_halt,
)
from modules.integrity_reconcile import (
    CONF_MIN,
    INTEGRITY_KEYWORD_FAMILIES,
    keyword_family,
    normalize_excerpt,
    fuzzy_ratio,
    fuzzy_same_rumor,
    reresolve_excerpt,
    reconcile_persisted_flags,
)


# The Zcash/Orchard "unlimited mint" excerpt the OLD resolver mis-bound to BTC.
ZEC_EXCERPT = (
    "Daily wrap: BTC chops near 105k as funding cools and SOL holds support. "
    "Meanwhile, Zcash bug crisis deepens — the Orchard pool flaw allowed "
    "unlimited minting since 2022, devs scrambling."
)


def _pos(coin, upnl):
    return {"coin": coin, "side": "LONG", "unrealized_pnl": upnl}


def _db(monkeypatch, tmp_path):
    monkeypatch.setattr(im, "DB_PATH", str(tmp_path / "intel.db"))


# ── T1. feed-rotation orphan: persisted BTC flag self-heals to ZEC ───────────
def test_t1_feed_rotation_orphan_btc_flag_clears(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    # The OLD resolver persisted a BTC flag carrying the Zcash excerpt.
    raise_flags([IntegrityHit(
        asset="BTC", keyword="unlimited mint", excerpt=ZEC_EXCERPT,
        source="AIXBT Daily Reports", shielded=False,
    )])
    assert "BTC" in {f["asset"] for f in get_active_flags()}

    # A SUBSEQUENT scan whose live feed NO LONGER contains the Zcash excerpt —
    # it fires on a totally different "exploit" headline. The feed-coupled
    # reconcile would find nothing to pair; the persisted re-resolution pass
    # re-reads the BTC flag's OWN excerpt and clears it.
    dismissed = reconcile_persisted_flags([_pos("BTC", -3400.0), _pos("SOL", -900.0)])
    assert ("BTC", "re_resolved_to_other_asset") in dismissed
    assert "BTC" not in {f["asset"] for f in get_active_flags()}


def test_t1_via_run_integrity_halt_with_unrelated_feed(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    raise_flags([IntegrityHit(
        asset="BTC", keyword="unlimited mint", excerpt=ZEC_EXCERPT,
        source="AIXBT Daily Reports", shielded=False,
    )])
    # Live feed is now an UNRELATED story (no Zcash excerpt in-window).
    unrelated = {"status": "ok", "data": [{
        "channel": "AIXBT Daily Reports", "handle": "daily",
        "messages": [{"date": "2026-06-07", "text": "ETH gas spikes; market calm."}],
    }]}
    block, _newly = run_integrity_halt([_pos("BTC", -3400.0)], unrelated)
    assert "STOP accumulation on BTC" not in block
    assert "BTC" not in {f["asset"] for f in get_active_flags()}


# ── T2. keyword-family join: raise on "unlimited mint", reresolve via "exploit"
def test_t2_keyword_family_join(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    # Excerpt names Zcash/Orchard and carries an "exploit" keyword (a DIFFERENT
    # family than the raise-time "unlimited mint") — both must route to ZEC.
    excerpt = "Zcash Orchard exploit confirmed — shielded pool soundness broken."
    raise_flags([IntegrityHit(
        asset="BTC", keyword="unlimited mint", excerpt=excerpt,
        source="AIXBT", shielded=False,
    )])
    res = reresolve_excerpt(excerpt, positions=[_pos("BTC", -100.0)])
    assert res.resolved_asset == "ZEC"
    assert res.keyword_family == "exploit"
    assert keyword_family("unlimited mint") == "supply_integrity"
    assert keyword_family("exploit") == "exploit"
    dismissed = reconcile_persisted_flags([_pos("BTC", -100.0)])
    assert ("BTC", "re_resolved_to_other_asset") in dismissed


# ── T3. genuine flag on a HELD asset with adverse PnL is NEVER dismissed ─────
def test_t3_genuine_held_adverse_flag_protected(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    genuine = "Bitcoin core exploit: a consensus bug enables double-spend on BTC."
    raise_flags([IntegrityHit(
        asset="BTC", keyword="exploit", excerpt=genuine,
        source="AIXBT", shielded=False,
    )])
    # Re-resolution maps to BTC (held, adverse) → guard keeps it.
    res = reresolve_excerpt(genuine, positions=[_pos("BTC", -5000.0)])
    assert res.resolved_asset == "BTC"
    dismissed = reconcile_persisted_flags([_pos("BTC", -5000.0)])
    assert all(a != "BTC" for a, _ in dismissed)
    assert "BTC" in {f["asset"] for f in get_active_flags()}


# ── T4. distinctive-name precedence at reconcile-time (orchard → ZEC) ────────
def test_t4_distinctive_name_precedence_at_reconcile(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    # BTC is the nearest held ticker, but "orchard" is a distinctive name → ZEC.
    excerpt = "BTC steady. Orchard pool flaw enables unlimited mint."
    res = reresolve_excerpt(excerpt, positions=[_pos("BTC", -100.0)])
    assert res.resolved_asset == "ZEC"
    assert res.confidence >= 0.90


# ── T5. no-attribution fallback: generic crisis → dismissed ──────────────────
def test_t5_no_identifiable_asset(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    excerpt = "Broad market crisis and collapse fears across risk assets today."
    raise_flags([IntegrityHit(
        asset="BTC", keyword="crisis", excerpt=excerpt,
        source="AIXBT", shielded=False,
    )])
    res = reresolve_excerpt(excerpt, positions=[_pos("BTC", -100.0)])
    assert res.resolved_asset is None
    assert res.confidence < CONF_MIN
    dismissed = reconcile_persisted_flags([_pos("BTC", -100.0)])
    assert ("BTC", "no_identifiable_asset") in dismissed


# ── T6. excerpt-drift fuzzy: truncated/rephrased == same rumor ───────────────
def test_t6_excerpt_drift_fuzzy():
    canonical = ZEC_EXCERPT
    truncated = "Zcash bug crisis deepens — the Orchard pool flaw allowed unlimited minting"
    assert fuzzy_ratio(truncated, canonical) >= 90  # subset → same rumor
    assert fuzzy_same_rumor(truncated, canonical)
    rephrased = "Orchard pool flaw in Zcash allowed unlimited minting since 2022 (bug crisis)"
    assert fuzzy_ratio(rephrased, canonical) >= 60
    assert fuzzy_same_rumor(
        rephrased, canonical,
        asset_a="ZEC", asset_b="ZEC", family_a="exploit", family_b="exploit",
    )
    # An unrelated excerpt is NOT the same rumor.
    assert not fuzzy_same_rumor("ETH gas spikes; market calm and bullish.", canonical)


# ── T7. idempotency: no dup flags, never resurrect a dismissed flag ──────────
def test_t7_idempotency(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    raise_flags([IntegrityHit(
        asset="BTC", keyword="unlimited mint", excerpt=ZEC_EXCERPT,
        source="AIXBT Daily Reports", shielded=False,
    )])
    # raise twice → ON CONFLICT(asset) → still exactly one row.
    raise_flags([IntegrityHit(
        asset="BTC", keyword="unlimited mint", excerpt=ZEC_EXCERPT,
        source="AIXBT Daily Reports", shielded=False,
    )])

    first = reconcile_persisted_flags([_pos("BTC", -100.0)])
    assert ("BTC", "re_resolved_to_other_asset") in first
    assert "BTC" not in {f["asset"] for f in get_active_flags()}

    # Second pass: the flag is already dismissed and must NOT resurrect.
    second = reconcile_persisted_flags([_pos("BTC", -100.0)])
    assert second == []
    assert "BTC" not in {f["asset"] for f in get_active_flags()}


# ── families exposed for callers / config parity ─────────────────────────────
def test_keyword_families_shape():
    assert set(INTEGRITY_KEYWORD_FAMILIES) == {"supply_integrity", "exploit", "crisis"}
    assert "unlimited mint" in INTEGRITY_KEYWORD_FAMILIES["supply_integrity"]
    assert "exploit" in INTEGRITY_KEYWORD_FAMILIES["exploit"]
    assert "crisis" in INTEGRITY_KEYWORD_FAMILIES["crisis"]


def test_normalize_excerpt():
    assert normalize_excerpt("  Zcash —  BUG!!  crisis  ") == "zcash bug crisis"
