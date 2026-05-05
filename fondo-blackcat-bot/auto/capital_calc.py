"""R-DASH — Bug B fix: single source of truth for fund NET capital.

Background
----------
The dashboard's Capital block was rendering ``Total: $79.0K`` as the first
line — the sum of HL collateral + perp account value + spot non-USDC. That
number is **gross exposure**: it includes the leveraged collateral the fund
borrowed against. Reading it as "what the fund owns" double-counts the debt
side. The 1 may 2026 13:31 UTC snapshot showed $79.0K Total / $73.2K HL
collateral / $45.3K HL debt — a casual reader would assume the fund had ~$79K
liquid, when in reality it had ~$33.85K.

This module replaces the misleading top-line with a proper NET calculation
and exposes the gross/leverage figures purely as informative breakdowns.

NET formula
-----------
::

    NET = (HL_collateral - HL_debt) + perp_equity + spot_non_stable

UPnL is **not** added separately. Hyperliquid's Unified Account already
folds unrealised P&L into ``marginSummary.accountValue`` (== ``perp_equity``
in our snapshot), so adding it again would double-count. The
``upnl_perp_usd`` field is exposed as informative breakdown only.

R-DASHBOARD-SPOT-FIX (2026-05-05)
---------------------------------
The historical ``spot_non_usdc`` field included USDT0/USDH/USDT/DAI even
though those are stablecoins — pegged to USD, NOT exposure. The 5 may
2026 12:13 UTC snapshot rendered "Spot non-USDC: $1.7K" when the real
non-stable bag was $43.59 (USOL + HYPE dust); the inflated $1.7K was
USDT0 + USDH cash equivalent.

Fix: the field is renamed ``spot_non_stable_usd`` (semantically correct)
and a separate ``spot_stables_usd`` bucket tracks cash equivalents.
``net_total_usd`` consumes only ``spot_non_stable_usd`` — stables are NOT
exposure but ARE part of total fund equity, so they appear under the
informative breakdown as "Spot stables (cash equiv)".

The pre-fix ``Total`` formula remains available as ``gross_exposure_usd``,
labelled clearly as "leverage included" in the rendered output.

Public API
----------
``compute_net_capital(snap_or_dict) -> NetCapital``
    Accepts either a ``modules.portfolio_snapshot.PortfolioSnapshot`` or a
    flat dict with the canonical totals (``hl_collateral_total``,
    ``hl_debt_total``, ``perp_equity_total``, ``spot_usd_total``,
    ``upnl_perp_total``). Returns the structured ``NetCapital``.

``format_net_capital_telegram(net) -> str``
    Plain-text block for ``/reporte`` (Telegram).

``render_net_capital_html(net, fmt_compact_usd, fmt_signed) -> str``
    HTML fragment for the dashboard's Capital card. Helpers are injected
    so we don't import dashboard internals from here.

Both renderers consume the same ``NetCapital`` instance — that is the
single-source-of-truth guarantee.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NetCapital:
    """Authoritative breakdown of the fund's capital.

    R-DASHBOARD-SPOT-FIX (2026-05-05): ``spot_non_usdc_usd`` was renamed
    to ``spot_non_stable_usd``. ``spot_non_usdc_usd`` is kept as an alias
    property for any older callers that still reach for the old name.
    A new ``spot_stables_usd`` field carries the cash-equivalent bucket
    (USDT0, USDH, USDC-when-idle, etc.).
    """

    # Top-line: what the fund effectively owns (post-leverage).
    net_total_usd: float
    # HL net = collateral - debt (the "real" flywheel position).
    hl_net_usd: float
    # Perp equity = sum of marginSummary.accountValue across all wallets,
    # which already INCLUDES unrealized PnL under Hyperliquid Unified Account.
    perp_equity_usd: float
    # Spot non-stable: HYPE, kHYPE, PEAR, USOL, etc. — real market exposure
    # valued at current price (entry-notional fallback).
    spot_non_stable_usd: float
    # Spot stables: USDT0, USDH, USDC-when-idle, USDE, USDHL, sUSDe, USR, DAI.
    # Cash equivalent — included in fund equity but NOT in "exposure".
    spot_stables_usd: float
    # Informative breakdown — already folded inside perp_equity_usd.
    upnl_perp_usd: float
    # Informative gross exposure — pre-leverage view.
    gross_exposure_usd: float
    # Raw HL gross figures — exposed for the "leverage included" footer.
    hl_collateral_usd: float
    hl_debt_usd: float

    @property
    def spot_non_usdc_usd(self) -> float:
        """Backward-compat alias for the pre-R-DASHBOARD-SPOT-FIX field name.

        Older test fixtures and any external callers that still reach for
        ``spot_non_usdc_usd`` get the corrected non-stable value
        transparently. New code should use ``spot_non_stable_usd``.
        """
        return self.spot_non_stable_usd


def _coerce_floats(d: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key in (
        "hl_collateral_total",
        "hl_debt_total",
        "perp_equity_total",
        # R-DASHBOARD-SPOT-FIX: spot_usd_total semantically means
        # NON-STABLE only post-fix. Callers that pre-date the fix still
        # supply this key; their values were already mis-aggregated upstream
        # (the bug) — once portfolio_snapshot.py and formatters.py are
        # updated, the value arriving here is correctly stable-free.
        "spot_usd_total",
        "spot_stables_total",
        "upnl_perp_total",
    ):
        try:
            out[key] = float(d.get(key) or 0.0)
        except (TypeError, ValueError):
            out[key] = 0.0
    return out


def compute_net_capital(snap: Any) -> NetCapital:
    """Compute NET / gross capital from a snapshot or flat-dict totals.

    Accepts:

    * ``modules.portfolio_snapshot.PortfolioSnapshot`` — uses its totals
      directly.
    * ``dict`` with ``hl_collateral_total`` / ``hl_debt_total`` /
      ``perp_equity_total`` / ``spot_usd_total`` / ``upnl_perp_total``.
    * Anything with the equivalent attributes (duck-typed).

    Returns a ``NetCapital`` dataclass — never raises on missing keys
    (treated as zero) so it is safe to call from rendering hot paths even
    when an upstream fetcher is partially failing.
    """
    if isinstance(snap, dict):
        f = _coerce_floats(snap)
        hl_coll = f["hl_collateral_total"]
        hl_debt = f["hl_debt_total"]
        perp = f["perp_equity_total"]
        spot = f["spot_usd_total"]
        stables = f["spot_stables_total"]
        upnl = f["upnl_perp_total"]
    else:
        def _get(name: str) -> float:
            try:
                return float(getattr(snap, name, 0.0) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        hl_coll = _get("hl_collateral_total")
        hl_debt = _get("hl_debt_total")
        perp = _get("perp_equity_total")
        spot = _get("spot_usd_total")
        # R-DASHBOARD-SPOT-FIX: snapshot now exposes spot_stables_total.
        # Falls back to 0.0 when the source pre-dates the fix (Snap fixtures).
        stables = _get("spot_stables_total")
        upnl = _get("upnl_perp_total")

    hl_net = hl_coll - hl_debt
    # NET = post-leverage capital exposure. UPnL is NOT added separately
    # because ``perp`` (marginSummary.accountValue) already includes it
    # under Hyperliquid Unified Account. See portfolio_snapshot.py.
    # R-DASHBOARD-SPOT-FIX: ``spot`` is non-stable only. Stables are NOT
    # part of NET (per BCD directive: "son cash equivalente, no exposure").
    # They appear as a separate informative line and their value is also
    # captured in ``total_equity_usd`` below for total fund net worth.
    net = hl_net + perp + spot
    # GROSS = pre-leverage view (the old "Total" line). Kept informative.
    gross = hl_coll + perp + spot

    log.info(
        "capital_calc: hl_coll=%.2f hl_debt=%.2f hl_net=%.2f perp=%.2f "
        "spot_non_stable=%.2f spot_stables=%.2f upnl=%.2f -> net=%.2f gross=%.2f",
        hl_coll,
        hl_debt,
        hl_net,
        perp,
        spot,
        stables,
        upnl,
        net,
        gross,
    )

    return NetCapital(
        net_total_usd=net,
        hl_net_usd=hl_net,
        perp_equity_usd=perp,
        spot_non_stable_usd=spot,
        spot_stables_usd=stables,
        upnl_perp_usd=upnl,
        gross_exposure_usd=gross,
        hl_collateral_usd=hl_coll,
        hl_debt_usd=hl_debt,
    )


def _fmt_usd(v: float) -> str:
    """Compact USD formatter used by the Telegram block.

    Avoids depending on ``templates.formatters`` from this auto/* module.
    """
    av = abs(v)
    if av >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if av >= 1_000:
        return f"${v / 1_000:.1f}K"
    if av >= 1:
        return f"${v:,.0f}"
    return f"${v:.2f}"


def _fmt_signed(v: float) -> str:
    if v > 0:
        return f"+{_fmt_usd(v)}"
    if v < 0:
        return f"-{_fmt_usd(abs(v))}"
    return _fmt_usd(0.0)


def format_net_capital_telegram(net: NetCapital) -> str:
    """Plain-text block for ``/reporte`` (top of POSICIONES section).

    R-DASHBOARD-SPOT-FIX (2026-05-05): ``Spot non-USDC`` line was
    renamed to ``Spot non-stable`` (semantically accurate — USDT0/USDH
    are also stablecoins) and a ``Spot stables (cash equiv)`` line is
    appended when the bucket is non-trivial.

    Layout::

        💰 NET CAPITAL: $34.5K  (post-leverage)
        ├─ HL net (col-debt): $31.6K
        ├─ Perp account: $2.9K
        ├─ Spot non-stable: $43.59
        └─ UPnL perp (en perp): +$231.59

        Spot stables (cash equiv): $1.6K
        ├─ (USDT0, USDH, USDC-idle, etc.)

        Gross exposure: $79.0K  (leverage incluido — informativo)
        ├─ HL collateral: $73.2K
        └─ HL debt: -$45.3K
    """
    lines: list[str] = []
    lines.append(
        f"💰 NET CAPITAL: {_fmt_usd(net.net_total_usd)}  (post-leverage)"
    )
    lines.append(f"├─ HL net (col-debt): {_fmt_usd(net.hl_net_usd)}")
    lines.append(f"├─ Perp account: {_fmt_usd(net.perp_equity_usd)}")
    lines.append(f"├─ Spot non-stable: {_fmt_usd(net.spot_non_stable_usd)}")
    lines.append(
        f"└─ UPnL perp (en perp): {_fmt_signed(net.upnl_perp_usd)}"
    )
    lines.append("")
    # R-DASHBOARD-SPOT-FIX: stables surfaced separately (not in NET).
    if net.spot_stables_usd > 0.01:
        lines.append(
            f"Spot stables (cash equiv): {_fmt_usd(net.spot_stables_usd)}"
        )
        lines.append("")
    lines.append(
        f"Gross exposure: {_fmt_usd(net.gross_exposure_usd)}  "
        "(leverage incluido — informativo)"
    )
    lines.append(f"├─ HL collateral: {_fmt_usd(net.hl_collateral_usd)}")
    lines.append(f"└─ HL debt: -{_fmt_usd(net.hl_debt_usd)}")
    return "\n".join(lines)


def render_net_capital_html(
    net: NetCapital,
    fmt_compact_usd,
    signed,
    upnl_cls: str | None = None,
    upnl_fmt: str | None = None,
) -> str:
    """HTML fragment for the dashboard Capital card.

    ``fmt_compact_usd`` and ``signed`` are passed in so the auto module
    stays decoupled from the dashboard's escape/format helpers.

    ``upnl_cls`` / ``upnl_fmt`` are optional pre-formatted (class, value)
    pair if the caller already computed them in its colour scheme; if not
    provided, they're derived from ``net.upnl_perp_usd``.
    """
    if upnl_cls is None or upnl_fmt is None:
        upnl_cls, upnl_fmt = signed(net.upnl_perp_usd)

    # R-DASHBOARD-SPOT-FIX: separate "Spot stables" cash-equivalent block,
    # rendered only when non-trivial. Keeps the Capital card visually
    # uncluttered when the fund has no stablecoins sitting in spot.
    stables_block = ""
    if net.spot_stables_usd > 0.01:
        stables_block = (
            f"<p>&nbsp;</p>"
            f"<p class='dim'>Spot stables (cash equiv): "
            f"{fmt_compact_usd(net.spot_stables_usd)}</p>"
        )

    return (
        f"<p>💰 <strong>NET: {fmt_compact_usd(net.net_total_usd)}</strong>"
        f" <span class='dim'>(post-leverage)</span></p>"
        f"<p class='dim'>Breakdown:</p>"
        f"<p>&nbsp;&nbsp;HL net (col-debt): {fmt_compact_usd(net.hl_net_usd)}</p>"
        f"<p>&nbsp;&nbsp;Perp account: {fmt_compact_usd(net.perp_equity_usd)}</p>"
        f"<p>&nbsp;&nbsp;Spot non-stable: {fmt_compact_usd(net.spot_non_stable_usd)}</p>"
        f"<p>&nbsp;&nbsp;UPnL perp (en perp): "
        f"<span class='{upnl_cls}'>{upnl_fmt}</span></p>"
        f"{stables_block}"
        f"<p>&nbsp;</p>"
        f"<p class='dim'>Gross exposure: {fmt_compact_usd(net.gross_exposure_usd)}"
        f" <span class='dim'>(leverage incluido — informativo)</span></p>"
        f"<p>&nbsp;&nbsp;HL collateral: {fmt_compact_usd(net.hl_collateral_usd)}</p>"
        f"<p>&nbsp;&nbsp;HL debt: -{fmt_compact_usd(net.hl_debt_usd)}</p>"
    )
