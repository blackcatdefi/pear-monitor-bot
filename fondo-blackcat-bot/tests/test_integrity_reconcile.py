"""R-PM-LIQ / P0.3 (2026-06-06) — self-heal of misattributed persisted flags.

The bug
-------
A pre-R-INTEGRITY-FIX resolver bound a *Zcash/Orchard* "unlimited mint" rumor
to the held BTC leg (nearest-ticker logic) and persisted a BTC INTEGRITY-HALT
flag in SQLite. Because raised flags NEVER auto-clear (shielded-asset safety
invariant), that false "STOP accumulation on BTC" kept rendering on every
/reporte even after the resolver was corrected and the deploy was current —
the root cause was the stale DB row, not a stale deploy.

The fix
-------
``reconcile_misattributed`` runs inside ``run_integrity_halt`` AFTER raising
new hits and BEFORE rendering. It auto-dismisses a persisted flag iff, under
the corrected resolver, the SAME rumor excerpt now resolves to a DIFFERENT
concrete asset — i.e. the old flag is provably a misattribution. Strict guards
preserve the safety invariant: a shielded flag is NEVER auto-cleared, and a
genuine flag (excerpt still resolves to that asset, or to nothing else) stays.
"""
from __future__ import annotations

import modules.intel_memory as im
import modules.integrity_halt as ih
from modules.integrity_halt import (
    IntegrityHit,
    raise_flags,
    get_active_flags,
    reconcile_misattributed,
    run_integrity_halt,
    scan_integrity,
)


LIVE_BLOB = (
    "Daily wrap: BTC chops near 105k as funding cools and SOL holds support. "
    "Meanwhile, Zcash bug crisis deepens — the Orchard pool flaw allowed "
    "unlimited minting since 2022, devs scrambling. Risk-off into the weekend."
)


def _tg(text, channel="AIXBT Daily Reports"):
    return {"status": "ok", "data": [
        {"channel": channel, "handle": "daily",
         "messages": [{"date": "2026-06-05", "text": text}]},
    ]}


def _pos(coin, upnl):
    return {"coin": coin, "side": "LONG", "unrealized_pnl": upnl}


def _zec_excerpt() -> str:
    """The exact excerpt the corrected resolver attaches to the ZEC note."""
    scan = scan_integrity([_pos("BTC", -3400.0)], _tg(LIVE_BLOB))
    notes = [n for n in scan.notes if n.asset == "ZEC"]
    assert notes, "expected a ZEC blocklisted note from the live blob"
    return notes[0].excerpt


# ── 1. The live false BTC flag is auto-dismissed by run_integrity_halt ───────
def test_legacy_btc_flag_self_heals(monkeypatch, tmp_path):
    monkeypatch.setattr(im, "DB_PATH", str(tmp_path / "intel.db"))

    # Simulate the persisted false BTC flag from the OLD resolver: the BTC flag
    # carries the Zcash rumor excerpt (the misattribution fingerprint).
    raise_flags([IntegrityHit(
        asset="BTC", keyword="unlimited mint", excerpt=_zec_excerpt(),
        source="AIXBT Daily Reports", shielded=False,
    )])
    assert "BTC" in {f["asset"] for f in get_active_flags()}

    # The corrected resolver re-reads the SAME rumor → resolves to ZEC.
    block, _newly = run_integrity_halt(
        [_pos("BTC", -3400.0), _pos("SOL", -900.0)], _tg(LIVE_BLOB),
    )
    # The false BTC flag is gone and the block no longer STOPs BTC.
    assert "BTC" not in {f["asset"] for f in get_active_flags()}
    assert "STOP accumulation on BTC" not in block


# ── 2. reconcile returns the dismissed asset; audit trail records re-attrib ──
def test_reconcile_reports_dismissed_and_audit(monkeypatch, tmp_path):
    monkeypatch.setattr(im, "DB_PATH", str(tmp_path / "intel.db"))
    raise_flags([IntegrityHit(
        asset="BTC", keyword="unlimited mint", excerpt=_zec_excerpt(),
        source="AIXBT Daily Reports", shielded=False,
    )])
    scan = scan_integrity([_pos("BTC", -3400.0), _pos("SOL", -900.0)], _tg(LIVE_BLOB))
    dismissed = reconcile_misattributed(scan, get_active_flags())
    assert dismissed == ["BTC"]


# ── 3. A shielded flag is NEVER auto-cleared (safety invariant) ──────────────
def test_shielded_flag_never_auto_clears(monkeypatch, tmp_path):
    monkeypatch.setattr(im, "DB_PATH", str(tmp_path / "intel.db"))
    # ZEC itself is shielded — even if a later excerpt pointed elsewhere, a
    # shielded persisted flag must survive reconciliation.
    raise_flags([IntegrityHit(
        asset="ZEC", keyword="unlimited mint", excerpt=_zec_excerpt(),
        source="AIXBT Daily Reports", shielded=True,
    )])
    # Force a scan whose same excerpt resolves to a different concrete asset.
    scan = scan_integrity([_pos("BTC", -3400.0)], _tg(LIVE_BLOB))
    dismissed = reconcile_misattributed(scan, get_active_flags())
    assert "ZEC" not in dismissed
    assert "ZEC" in {f["asset"] for f in get_active_flags()}


# ── 4. A genuine flag (no competing re-attribution) is NOT dismissed ─────────
def test_genuine_flag_survives_reconcile(monkeypatch, tmp_path):
    monkeypatch.setattr(im, "DB_PATH", str(tmp_path / "intel.db"))
    # A real ARB rumor flag with an excerpt that does NOT reappear in the live
    # Zcash scan → nothing to re-attribute to → must stay active.
    raise_flags([IntegrityHit(
        asset="ARB", keyword="delisting", excerpt="ARB delisting + hack rumor confirmed",
        source="AIXBT Daily Reports", shielded=False,
    )])
    scan = scan_integrity([_pos("BTC", -3400.0), _pos("SOL", -900.0)], _tg(LIVE_BLOB))
    dismissed = reconcile_misattributed(scan, get_active_flags())
    assert "ARB" not in dismissed
    assert "ARB" in {f["asset"] for f in get_active_flags()}


# ── 5. A still-valid flag whose excerpt resolves to ITSELF is NOT dismissed ──
def test_flag_resolving_to_itself_survives(monkeypatch, tmp_path):
    monkeypatch.setattr(im, "DB_PATH", str(tmp_path / "intel.db"))
    # Genuine ARB rumor: raise the flag from a real ARB scan, then reconcile
    # against the SAME ARB scan. The excerpt still resolves to ARB → keep.
    intel = _tg("ARB sequencer backdoor exploit — funds at risk")
    scan = scan_integrity([_pos("ARB", -250.0)], intel)
    raise_flags(scan.hits)
    assert "ARB" in {f["asset"] for f in get_active_flags()}
    dismissed = reconcile_misattributed(scan, get_active_flags())
    assert "ARB" not in dismissed
    assert "ARB" in {f["asset"] for f in get_active_flags()}


# ── 6. reconcile never raises on garbage input ──────────────────────────────
def test_reconcile_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(im, "DB_PATH", str(tmp_path / "intel.db"))
    scan = scan_integrity([_pos("BTC", -100.0)], _tg(LIVE_BLOB))
    assert reconcile_misattributed(scan, None) == []
    assert reconcile_misattributed(scan, []) == []
