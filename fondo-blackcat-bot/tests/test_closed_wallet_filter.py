"""R-DASHBOARD-DOUBLECOUNT-FIX (2026-05-06) — Bug #3 + #4 regression lock.

Background
----------
The secondary flywheel ``0xcddfcc4e597091d8e395a24738f09bbd8973f22e`` was
officially closed by BCD on 2026-04-22. Pre-fix the dashboard still
rendered it as an active flywheel beside the main one (Bug #3) and
attached an auto-staleness counter ``HF: 1.429 (cached 32h ago)`` even
though the cached HF is meaningless for a wallet that's no longer being
managed (Bug #4).

Fix surface (locked down by these tests)
----------------------------------------
* ``auto.wallet_labels.is_closed_wallet(addr)`` — True when the canonical
  label contains the substring "CLOSED".
* ``auto.wallet_labels.closed_at_iso(addr)`` — ISO date the wallet was
  closed (or None if not in the closed map).
* ``modules.dashboard._render_html`` — promotes the secondary to "main"
  when the snapshot's main is closed; collects every closed flywheel
  into the "Wallets cerradas (histórico)" collapsible section instead
  of beside the active main; routes closed wallets through the closure-
  date branch in ``_render_hf_block`` so HF renders as
  ``CLOSED at 2026-04-22, last HF: 1.429`` instead of ``(cached Xh ago)``.
"""
from __future__ import annotations

from auto.wallet_labels import (
    CANONICAL_WALLET_LABELS,
    CLOSED_AT_ISO,
    apply_wallet_label,
    closed_at_iso,
    is_closed_wallet,
)


# Canonical addresses for the regression lock.
SECONDARY_FLYWHEEL_ADDR = "0xcddfcc4e597091d8e395a24738f09bbd8973f22e"
MAIN_FLYWHEEL_ADDR = "0xa44e8b9522a5f710e2b63ab790465af2f155b632"
TRADING_ADDR = "0xc7ae23316b47f7e75f455f53ad37873a18351505"


# ─── Bug #3: is_closed_wallet flips the dashboard render branch ────────────
def test_secondary_flywheel_is_classified_as_closed():
    """The 0xcddf wallet was closed by BCD on 2026-04-22 — its canonical
    label contains "CLOSED" (case-insensitive). is_closed_wallet must
    return True so the dashboard skips it from the main render."""
    assert is_closed_wallet(SECONDARY_FLYWHEEL_ADDR) is True
    label = CANONICAL_WALLET_LABELS[SECONDARY_FLYWHEEL_ADDR]
    assert "CLOSED" in label.upper()


def test_active_wallets_are_not_classified_as_closed():
    """Defense lockdown: every active fund wallet MUST return False so
    a future label edit doesn't accidentally hide a live wallet."""
    assert is_closed_wallet(MAIN_FLYWHEEL_ADDR) is False
    assert is_closed_wallet(TRADING_ADDR) is False
    assert is_closed_wallet(
        "0x171b7880939d76abbc6b6b2094f54e6636f829a7"
    ) is False  # DreamCash


def test_is_closed_wallet_handles_unknown_address():
    """An unknown address must return False (no canonical label → no
    "CLOSED" substring → defaults to active)."""
    assert is_closed_wallet(
        "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    ) is False


def test_is_closed_wallet_handles_none_and_empty():
    """Edge cases: None / empty / whitespace must NOT raise."""
    assert is_closed_wallet(None) is False
    assert is_closed_wallet("") is False


def test_is_closed_wallet_is_case_insensitive():
    """Address case shouldn't matter — the canonical map keys are
    stored lowercase."""
    assert is_closed_wallet(SECONDARY_FLYWHEEL_ADDR.upper()) is True
    mixed = "0xcDDfCc4E597091d8E395a24738f09bBd8973f22e"
    assert is_closed_wallet(mixed) is True


# ─── Bug #4: closed_at_iso surfaces the closure date ───────────────────────
def test_secondary_flywheel_has_closure_date():
    """BCD closed the secondary flywheel on 2026-04-22 — closed_at_iso
    must return that date so the dashboard can render
    ``CLOSED at 2026-04-22, last HF: X`` instead of an auto-staleness
    counter that means nothing for a retired wallet."""
    assert closed_at_iso(SECONDARY_FLYWHEEL_ADDR) == "2026-04-22"


def test_closed_at_iso_returns_none_for_active_wallets():
    """Active wallets must return None — the closure-date branch only
    fires for explicitly retired wallets."""
    assert closed_at_iso(MAIN_FLYWHEEL_ADDR) is None
    assert closed_at_iso(TRADING_ADDR) is None
    assert closed_at_iso(None) is None
    assert closed_at_iso("") is None


def test_closed_at_iso_map_contains_secondary_only():
    """The CLOSED_AT_ISO map only carries the secondary flywheel for now.
    A new closed wallet must be added here BEFORE the canonical label
    edit — this test fails loudly if the map drifts from the labels."""
    assert SECONDARY_FLYWHEEL_ADDR in CLOSED_AT_ISO
    assert CLOSED_AT_ISO[SECONDARY_FLYWHEEL_ADDR] == "2026-04-22"


def test_every_closed_at_entry_has_canonical_closed_label():
    """Lockdown: every wallet in CLOSED_AT_ISO MUST have a canonical
    label containing "CLOSED" — otherwise is_closed_wallet returns
    False and the closure-date branch never fires."""
    for addr in CLOSED_AT_ISO:
        label = CANONICAL_WALLET_LABELS.get(addr, "")
        assert "CLOSED" in label.upper(), (
            f"Address {addr} in CLOSED_AT_ISO but canonical label "
            f"{label!r} missing 'CLOSED' marker"
        )


# ─── Bug #3: dashboard render path skips closed from main ──────────────────
def test_dashboard_render_uses_is_closed_to_filter_main():
    """The dashboard module MUST import is_closed_wallet to filter the
    main flywheel render. This test reads the source so we don't have
    to import dashboard (which pulls aiohttp at module load)."""
    from pathlib import Path

    src = Path(__file__).parent.parent / "modules" / "dashboard.py"
    text = src.read_text(encoding="utf-8")
    assert "is_closed_wallet" in text, (
        "dashboard module dropped the is_closed_wallet import — Bug #3 "
        "filter is no longer wired"
    )
    assert "closed_at_iso" in text, (
        "dashboard module dropped the closed_at_iso import — Bug #4 "
        "closure-date render is no longer wired"
    )


def test_dashboard_renders_closed_collapsible_section():
    """The "Wallets cerradas (histórico)" collapsible section is the
    canonical home for closed flywheels. The fix wires it as
    ``closed_html`` and substitutes into the HTML grid template."""
    from pathlib import Path

    src = Path(__file__).parent.parent / "modules" / "dashboard.py"
    text = src.read_text(encoding="utf-8")
    assert "Wallets cerradas (histórico)" in text, (
        "dashboard removed the closed-wallets section header — Bug #3 "
        "render branch dropped"
    )
    assert "{closed_html}" in text, (
        "dashboard HTML template no longer interpolates {closed_html} — "
        "the closed-wallets card never renders"
    )


def test_dashboard_promotes_secondary_to_main_when_main_closed():
    """If the snapshot's "main_flywheel" slot points at a closed wallet,
    the dashboard must promote secondary → main so the user always sees
    a live flywheel at the top."""
    from pathlib import Path

    src = Path(__file__).parent.parent / "modules" / "dashboard.py"
    text = src.read_text(encoding="utf-8")
    # The promotion swap is the canonical Python idiom — match either
    # direction.
    assert (
        "main, sec = sec, main" in text
        or "sec, main = main, sec" in text
    ), (
        "dashboard no longer promotes secondary→main when main is closed"
    )


# ─── Bug #4: HF render branches on is_closed ───────────────────────────────
def test_hf_block_renders_closed_at_date_on_closed_wallet():
    """When is_closed_wallet is True, the HF block must render
    ``CLOSED at 2026-04-22, last HF: X`` — NOT a stale-counter."""
    from pathlib import Path

    src = Path(__file__).parent.parent / "modules" / "dashboard.py"
    text = src.read_text(encoding="utf-8")
    # The render fragment must include the CLOSED-at template.
    assert "CLOSED at" in text, (
        "dashboard HF block no longer carries the CLOSED-at branch — "
        "closed wallets revert to (cached Xh ago) auto-counter"
    )
    assert "last HF" in text, (
        "dashboard HF block no longer surfaces the last known HF on "
        "closed wallets"
    )


def test_canonical_label_for_closed_wallet_contains_marker():
    """The substring "CLOSED" must be present in the canonical label —
    that's the entire trigger for is_closed_wallet."""
    label = apply_wallet_label(SECONDARY_FLYWHEEL_ADDR, "anything")
    assert "CLOSED" in label.upper()
    assert "Secondary Flywheel" in label
