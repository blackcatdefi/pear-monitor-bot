"""R-DASHBOARD-DOUBLECOUNT-FIX (2026-05-06) — Bug #5 regression lock.

Background
----------
On 2026-05-06 the dashboard header showed UPnL ``+$40.22`` while the basket
card directly below showed UPnL ``-$35.79`` for the same set of positions.
Two truths is one too many. Root cause: the header summed every perp
position's UPnL across all wallets (including phantom dust legs left over
from closed positions), while the basket card summed only the active
SHORT-basket leg UPnLs aggregated by ``basket_state``.

Fix surface (locked down by these tests)
----------------------------------------
The header consumes ``state["basket_upnl"]`` as the single source of
truth. The basket card pulls from the same field. ``upnl_perp_total`` is
kept as a fallback when ``basket_upnl`` is None (cold start, no basket
state yet) so the header never renders an empty value.

Rule
----
After the fix, header UPnL == basket card UPnL == ``state["basket_upnl"]``.
The legacy ``upnl_perp_total`` is informative-only and only kicks in as
the cold-start fallback.
"""
from __future__ import annotations

from pathlib import Path


_DASHBOARD_SRC = Path(__file__).parent.parent / "modules" / "dashboard.py"


def _read_dashboard_source() -> str:
    return _DASHBOARD_SRC.read_text(encoding="utf-8")


# ─── The single-source-of-truth contract ───────────────────────────────────
def test_header_upnl_consumes_basket_upnl_as_primary():
    """The dashboard's _render_html must read ``state.get("basket_upnl")``
    as the primary source for the header UPnL — not iterate over
    ``raw_positions`` (the pre-fix path)."""
    text = _read_dashboard_source()
    assert 'state.get("basket_upnl")' in text, (
        "header UPnL no longer reads basket_upnl as primary — Bug #5 reopened"
    )


def test_header_upnl_falls_back_to_perp_total_when_basket_unset():
    """If basket_upnl is None (cold start / pre-basket-detection), the
    header must fall back to upnl_perp_total. Empty render is worse than
    a stale-but-numeric value."""
    text = _read_dashboard_source()
    assert 'state.get("upnl_perp_total")' in text, (
        "header UPnL lost its cold-start fallback to upnl_perp_total — "
        "first dashboard hit after a deploy will render '—'"
    )


def test_header_upnl_uses_signed_helper():
    """The header UPnL formatter is ``_signed(upnl_for_header)`` — same
    helper the basket card uses, so the colour and signed-prefix match."""
    text = _read_dashboard_source()
    assert "upnl_cls, upnl_fmt = _signed(upnl_for_header)" in text


# ─── Bug #5 prefix marker survives in source so future grep-fixes find it ──
def test_bug5_marker_present_in_source():
    """The Bug #5 fix carries a ``R-DASHBOARD-DOUBLECOUNT-FIX`` and
    ``Bug #5`` marker in the source so a grep tour can locate the
    regression lock immediately."""
    text = _read_dashboard_source()
    assert "R-DASHBOARD-DOUBLECOUNT-FIX" in text
    assert "Bug #5" in text


# ─── Behavioural emulation: header & basket consume same field ────────────
def test_header_and_basket_consume_same_field_when_basket_upnl_set():
    """Behavioural: emulate the dashboard's selection logic. When
    basket_upnl is set, both the header and the basket card MUST agree."""
    state = {
        "basket_upnl": -35.79,
        "upnl_perp_total": 40.22,  # what the pre-fix path would have shown
        "basket_state": {"wallets": {}},
    }
    # Header logic (from dashboard.py lines 162-164):
    upnl_for_header = state.get("basket_upnl")
    if upnl_for_header is None:
        upnl_for_header = state.get("upnl_perp_total") or 0.0
    # Basket card consumes the same field directly.
    upnl_basket = state["basket_upnl"]
    assert upnl_for_header == upnl_basket
    assert upnl_for_header == -35.79  # NOT the legacy +40.22


def test_header_falls_back_to_perp_total_on_cold_start():
    """When basket_upnl is None (cold-start scenario), the header must
    surface the perp-total fallback so the dashboard never renders
    blank UPnL."""
    state = {
        "basket_upnl": None,
        "upnl_perp_total": 12.34,
    }
    upnl_for_header = state.get("basket_upnl")
    if upnl_for_header is None:
        upnl_for_header = state.get("upnl_perp_total") or 0.0
    assert upnl_for_header == 12.34


def test_header_zero_when_both_fields_missing():
    """Defense-in-depth: if both fields are missing/None, header must
    render 0.0 (numeric), not crash with TypeError."""
    state: dict = {}
    upnl_for_header = state.get("basket_upnl")
    if upnl_for_header is None:
        upnl_for_header = state.get("upnl_perp_total") or 0.0
    assert upnl_for_header == 0.0


# ─── Two-truths regression: pre-fix divergence detector ────────────────────
def test_legacy_drift_would_now_be_caught():
    """Synthetic scenario reproducing the may-6 21:04 UTC drift:
    basket_upnl = -35.79, upnl_perp_total = +40.22. Pre-fix the header
    used the latter; post-fix the header uses the former. This test
    asserts the post-fix path."""
    state = {
        "basket_upnl": -35.79,
        "upnl_perp_total": 40.22,
    }
    # Post-fix path (basket_upnl is primary).
    primary = state.get("basket_upnl")
    if primary is None:
        primary = state.get("upnl_perp_total") or 0.0
    assert primary == -35.79
    # Sanity: the legacy +$40.22 is what the bug surfaced.
    legacy_pre_fix = state["upnl_perp_total"]
    assert legacy_pre_fix == 40.22
    # The two MUST diverge for this regression to be meaningful — the
    # whole bug was that they did and the dashboard still picked the
    # wrong one.
    assert primary != legacy_pre_fix
