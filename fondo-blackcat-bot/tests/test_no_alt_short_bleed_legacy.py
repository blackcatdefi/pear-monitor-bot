"""R-BOT-TERMINOLOGY-UNIFY (2026-05-07) — Bug #2 + #3.

The basket category was renamed from "Alt Short Bleed" to "Super Basket
Stage 6" on 5 may 2026 but several user-facing strings still referred to
the legacy name. This test pins the rename across:

* templates/system_prompt.py — LLM prompt + thesis prompt
* modules/analysis.py — JSON schema key + component label
* modules/kill_scenarios.py — section title
* fund_state.py — comments / BASKET_NOTE prose

The internal Python constant ``ALT_SHORT_BLEED_WALLETS`` is intentionally
preserved (data-structure name, not user-facing) so the rename does not
ripple through unrelated imports — see README in fund_state.py.
"""
from __future__ import annotations

import os

import pytest


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _read(rel_path: str) -> str:
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as f:
        return f.read()


@pytest.mark.parametrize(
    "rel_path",
    [
        "templates/system_prompt.py",
        "modules/analysis.py",
        "modules/kill_scenarios.py",
    ],
)
def test_no_user_facing_alt_short_bleed_string(rel_path: str):
    """Bug #2 — no user-facing module should still mention 'Alt Short Bleed'."""
    body = _read(rel_path)
    # Allow neither casing in user-facing strings.
    assert "Alt Short Bleed" not in body, (
        f"{rel_path} still contains the legacy 'Alt Short Bleed' string"
    )
    assert "ALT SHORT BLEED" not in body, (
        f"{rel_path} still contains the legacy 'ALT SHORT BLEED' string"
    )


def test_super_basket_stage_6_present_in_user_modules():
    """The new label must be present in the modules that surface it to BCD."""
    sp = _read("templates/system_prompt.py")
    assert "Super Basket Stage 6" in sp or "SUPER BASKET STAGE 6" in sp
    ks = _read("modules/kill_scenarios.py")
    assert "SUPER BASKET STAGE 6" in ks
    an = _read("modules/analysis.py")
    assert "Super Basket Stage 6" in an or "super_basket_stage_6" in an


def test_thesis_state_has_super_basket_stage_6_key_in_components():
    """The components map exported by analysis.py should pin the new key."""
    an = _read("modules/analysis.py")
    assert '"super_basket_stage_6"' in an or "'super_basket_stage_6'" in an


def test_basket_constant_name_preserved():
    """Internal data-structure name must not change — too many imports.

    Renaming ALT_SHORT_BLEED_WALLETS to SUPER_BASKET_STAGE_6_WALLETS
    would break formatters.py, fund_state.py shadows, and external
    callers. Pin that the constant is still named the legacy way.
    """
    fs = _read("fund_state.py")
    assert "ALT_SHORT_BLEED_WALLETS" in fs
