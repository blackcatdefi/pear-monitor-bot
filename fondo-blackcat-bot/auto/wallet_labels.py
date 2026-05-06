"""R-DASHBOARD-RABBY-PARITY (2026-05-06) — canonical wallet labels.

The fund's wallet labels live in two places:

1. Railway env vars ``FUND_WALLET_N_LABEL`` (consumed by ``config.py``).
2. This module — a canonical address-to-label map kept in code so the
   dashboard renders the right names even if the env vars are stale or
   were never updated after a labelling decision.

Rationale
---------
The 6 may 2026 09:09 UTC parity audit against Rabby revealed that the
env-var labels still pointed to dead state ("Alt Short Bleed v4" on
wallets that no longer hold v4 positions, "Reserva histórica" on the
DDS main flywheel). Updating env vars in Railway is the upstream fix
but cache-invalidation between env-var rotation and a fresh deploy
leaves a window where the dashboard renders wrong labels. A code-side
override applied at render time closes that gap.

Use ``apply_wallet_label(addr, fallback_label)`` to translate a wallet
address into its canonical display name. If the address is unknown,
the fallback label (typically the env-var label) is returned unchanged
so the override only kicks in for the addresses we explicitly track.

Categories
----------
* ``Main Flywheel (DDS)`` — the wallet driving the WHYPE/UETH
  flywheel position. Rabby calls this "DDS" (Diamond Dragon Slayer).
* ``Secondary Flywheel (CLOSED)`` — historical secondary flywheel,
  kept for forensic clarity. Will be filtered if capital < ``DUST_USD``.
* ``BlackCatDeFi EVM (Trading)`` — the wallet running the active basket.
* ``DreamCash (WAR TRADE)`` — long-vol commodity hedge.
* ``Dust`` — wallets that historically held positions but now carry
  only floating residue (<$50). Surface them under a single "Dust"
  bucket on the dashboard so they don't clutter the wallets card.
"""
from __future__ import annotations

import os
from typing import Final

# Lower-cased address → canonical label. New wallets MUST be added here
# whenever the fund's structure changes.
CANONICAL_WALLET_LABELS: Final[dict[str, str]] = {
    # DDS — Main Flywheel (WHYPE collateral / UETH debt). Rabby labels
    # this wallet as "DDS"; the bot calls it "Main Flywheel".
    "0xa44e8b9522a5f710e2b63ab790465af2f155b632": "Main Flywheel (DDS)",
    # Historical secondary flywheel — closed by BCD; only appears in
    # the dashboard if it carries non-trivial residual balance.
    "0xcddfcc4e597091d8e395a24738f09bbd8973f22e": "Secondary Flywheel (CLOSED)",
    # Trading wallet — runs the active perp basket on Hyperliquid.
    "0xc7ae23316b47f7e75f455f53ad37873a18351505": "BlackCatDeFi EVM (Trading)",
    # War trade — long commodities / short equities hedge.
    "0x171b7880939d76abbc6b6b2094f54e6636f829a7": "DreamCash (WAR TRADE)",
    # Historical wallet with only dust residue.
    "0x00bb6858ccbfc924a86642d438020155ccb36b64": "Dust",
}

# Dust threshold for the "collapse to one line" UX rule. Anything below
# this in total capital is bucketed under ``Dust:`` on the dashboard.
DUST_THRESHOLD_USD: Final[float] = float(
    os.getenv("DASHBOARD_DUST_USD", "50") or 50
)


def apply_wallet_label(addr: str | None, fallback: str | None = None) -> str:
    """Translate a wallet address into its canonical display label.

    Parameters
    ----------
    addr :
        Wallet address (any case). Returns ``fallback`` if None / empty.
    fallback :
        Label to return if ``addr`` is not in the canonical map. Typically
        the env-var label or a generic placeholder. ``"?"`` is used if
        ``fallback`` is also ``None``.
    """
    if not addr:
        return fallback or "?"
    canonical = CANONICAL_WALLET_LABELS.get(addr.lower())
    if canonical:
        return canonical
    return fallback or "?"


def is_dust(capital_usd: float | int | None) -> bool:
    """True when the wallet should be collapsed under "Dust" on the
    dashboard (capital < ``DUST_THRESHOLD_USD``)."""
    if capital_usd is None:
        return True
    try:
        return float(capital_usd) < DUST_THRESHOLD_USD
    except (TypeError, ValueError):
        return True


def is_canonical_dust_wallet(addr: str | None) -> bool:
    """True when an address is explicitly marked as a dust wallet in
    the canonical map (e.g. 0x00bb…6b64 was historically a basket leg
    but now only carries residue)."""
    if not addr:
        return False
    return CANONICAL_WALLET_LABELS.get(addr.lower(), "") == "Dust"


# R-DASHBOARD-DOUBLECOUNT-FIX (2026-05-06) — Bug #3 + #4.
# Map of wallets that are explicitly retired (CLOSED in canonical label).
# Used by the dashboard to:
#   * route the secondary flywheel render through the "Wallets cerradas
#     (histórico)" collapsible block instead of beside the main flywheel.
#   * skip the "(cached Xh ago)" HF rendering when a wallet is closed —
#     the cached HF is meaningless for a wallet that's no longer active.
# Optional ``CLOSED_AT_ISO`` map captures the human-known closure date so
# the dashboard can render "CLOSED at 2026-04-22, last HF: 1.429" instead
# of an auto-staleness counter.
CLOSED_AT_ISO: Final[dict[str, str]] = {
    # Secondary flywheel — BCD officially closed late April 2026.
    "0xcddfcc4e597091d8e395a24738f09bbd8973f22e": "2026-04-22",
}


def is_closed_wallet(addr: str | None) -> bool:
    """True when the canonical label contains "CLOSED" (case-insensitive).

    Drives the Bug #3 (filter from main render) and Bug #4 (HF render
    branch on closed status) fixes in modules/dashboard.py.
    """
    if not addr:
        return False
    label = CANONICAL_WALLET_LABELS.get(addr.lower(), "")
    return "CLOSED" in label.upper()


def closed_at_iso(addr: str | None) -> str | None:
    """Return the recorded closure date (ISO ``YYYY-MM-DD``) for a wallet,
    or ``None`` if the wallet is not in the closed-wallets map."""
    if not addr:
        return None
    return CLOSED_AT_ISO.get(addr.lower())
