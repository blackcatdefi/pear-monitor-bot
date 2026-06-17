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

    R-DASHBOARD-RABBY-PARITY (2026-05-06): ``total_equity_usd`` field
    added for the Rabby parity headline. ``net_total_usd`` (post-leverage
    exposure) excludes stables by design — see R-DASHBOARD-SPOT-FIX —
    but Rabby's "Total" line counts stables as fund equity (because
    they ARE — they're cash sitting in our wallets), so the dashboard
    needs a separate top-line that matches Rabby pixel-for-pixel.

        total_equity_usd = net_total_usd + spot_stables_usd
                         = (hl_collateral - hl_debt) + perp_equity
                           + spot_non_stable + spot_stables

    Pear Protocol staked balance is added on top via ``pear_staked_usd``
    (env-driven static value or, in the future, on-chain reader).
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
    # R-DASHBOARD-RABBY-PARITY: external DeFi positions (Pear Protocol
    # staked, etc.) that don't show up in HL/perp/spot endpoints. Surfaced
    # as a separate line and folded into ``total_equity_usd``.
    pear_staked_usd: float = 0.0
    # R-VAULTDEP (2026-05-30): fund capital deposited INTO HL vaults (e.g.
    # "Systemic Strategies HyperGrowth"). Lives under the vault address, NOT
    # in any fund wallet, so the wallet/Rabby-parity total omits it. Read
    # live via modules.vault_deposits (userVaultEquities, keyless). Surfaced
    # as its own line and folded into ``total_equity_usd``. NEVER added to
    # perp margin or wallet USDC (no double-count).
    vault_deposits_usd: float = 0.0
    # R-WALLET-FIX (2026-06-06): TRUE borrowed liability from HyperLiquid
    # Portfolio Margin (USDC borrowed against spot HYPE collateral). The HYPE
    # collateral is counted GROSS in ``spot_non_stable_usd`` and the perp
    # account value is counted in ``perp_equity_usd``; this borrow is the debt
    # side that nets them down to the real Rabby/DeBank net worth. SUBTRACTED
    # from ``total_equity_usd``. Distinct from ``hl_debt_usd`` (deprecated
    # HyperLend flywheel) — this is the live native lending borrow.
    spot_borrow_usd: float = 0.0
    # R-PEAR-ASSET-INTEGRATION (2026-06-17): PEAR is the fund's SECOND asset,
    # held as stPEAR on Pear Protocol (Arbitrum) and read LIVE on-chain
    # (balance × live PEAR price). These carry the underlying detail so every
    # surface can print "PEAR (2º activo): {bal} stPEAR × ${px} = ${val}".
    pear_staked_balance: float = 0.0  # stPEAR units (== PEAR at 1:1)
    pear_staked_price: float = 0.0  # live PEAR/USD
    # ``pear_staked_known`` is False when the on-chain balance OR the price
    # feed failed: PEAR then contributes 0 to equity and renders "n/d" — NEVER
    # a stale/fabricated value. Defaults True for backward-compat with callers
    # that pre-date the live reader (their static value is treated as known).
    pear_staked_known: bool = True
    # R-PEAR-ASSET-INTEGRATION: set True when the negative-equity guard fired
    # (a six-figure-collateralised PM account cannot have negative net worth —
    # so a negative raw total means the parity/price feed lagged). When True
    # the headline uses the oracle computation and flags the feed as stale.
    parity_stale: bool = False

    @property
    def spot_non_usdc_usd(self) -> float:
        """Backward-compat alias for the pre-R-DASHBOARD-SPOT-FIX field name.

        Older test fixtures and any external callers that still reach for
        ``spot_non_usdc_usd`` get the corrected non-stable value
        transparently. New code should use ``spot_non_stable_usd``.
        """
        return self.spot_non_stable_usd

    @property
    def total_equity_usd(self) -> float:
        """Total fund equity — matches Rabby's "Total" headline.

        Includes stables (cash equivalent), external DeFi positions
        (Pear staked) AND fund capital deposited INTO HL vaults
        (vault_deposits) on top of the post-leverage NET. This is the
        number BCD wants to see at the top of the dashboard.

        R-PEAR-ASSET-INTEGRATION (2026-06-17): when ``parity_stale`` is set
        (the negative-equity guard fired — see ``compute_net_capital``), the
        raw Rabby formula produced a nonsense negative because the collateral
        price feed or the borrow feed lagged. A PM account collateralised in
        six figures of HYPE cannot have negative net worth (the borrow is
        always sub-collateralised, else liquidated). In that case prefer the
        oracle-based computation: keep the live-valued collateral + assets and
        drop the suspect borrow over-subtraction, clamped at 0.
        """
        oracle_equity = (
            self.net_total_usd
            + self.spot_stables_usd
            + self.pear_staked_usd
            + self.vault_deposits_usd
        )
        if self.parity_stale:
            return max(oracle_equity, 0.0)
        return oracle_equity - self.spot_borrow_usd


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
        # R-DASHBOARD-RABBY-PARITY: external DeFi (Pear Protocol staked).
        "pear_staked_total",
        # R-VAULTDEP: fund capital deposited INTO HL vaults.
        "vault_deposits_total",
        # R-WALLET-FIX: TRUE borrowed liability (PM USDC borrow).
        "spot_borrow_total",
        # R-PEAR-ASSET-INTEGRATION: live stPEAR balance + price detail.
        "pear_staked_balance",
        "pear_staked_price",
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
        pear_staked = f["pear_staked_total"]
        vault_deposits = f["vault_deposits_total"]
        spot_borrow = f["spot_borrow_total"]
        pear_balance = f["pear_staked_balance"]
        pear_price = f["pear_staked_price"]
        # Default True for backward-compat: a non-zero static value supplied by
        # a legacy caller is treated as known. The live reader passes the flag
        # explicitly (False on fetch failure → n/d).
        pear_known = bool(snap.get("pear_staked_known", True))
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
        # R-DASHBOARD-RABBY-PARITY: Pear staked surfaced via env or future
        # on-chain reader. Defaults to 0 when not present.
        pear_staked = _get("pear_staked_total")
        # R-VAULTDEP: fund capital deposited INTO HL vaults. Defaults to 0.
        vault_deposits = _get("vault_deposits_total")
        # R-WALLET-FIX: TRUE borrowed liability (PM USDC borrow). Defaults 0.
        spot_borrow = _get("spot_borrow_total")
        # R-PEAR-ASSET-INTEGRATION: live stPEAR balance + price detail.
        pear_balance = _get("pear_staked_balance")
        pear_price = _get("pear_staked_price")
        try:
            pear_known = bool(getattr(snap, "pear_staked_known", True))
        except Exception:  # noqa: BLE001
            pear_known = True

    # R-PEAR-ASSET-INTEGRATION: when the live PEAR read failed, PEAR is
    # unknown → it must contribute 0 to equity and render "n/d" (never a
    # stale/fabricated number). Be defensive even if a caller passed a value.
    if not pear_known:
        pear_staked = 0.0

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

    # R-WALLET-FIX (2026-06-06): subtract the real PM borrow. The HYPE
    # collateral funding that borrow is already in ``spot`` (gross) and the
    # borrowed dollars deployed into perp are already in ``perp`` — so the
    # liability must be netted out once here to land on Rabby/DeBank net worth.
    total_equity = net + stables + pear_staked + vault_deposits - spot_borrow

    # R-PEAR-ASSET-INTEGRATION (2026-06-17): kill the "-$800 / Rabby parity"
    # artifact. A PM account collateralised in six figures of HYPE cannot have
    # negative net worth — the USDC borrow is always sub-collateralised (else
    # liquidated). So a NEGATIVE total while the live collateral base is
    # clearly positive means the price feed (HYPE oracle → spot ~0) or the
    # borrow feed lagged. Flag the parity feed stale; the total_equity_usd
    # property then prefers the oracle computation (borrow excluded, clamped).
    collateral_base = hl_coll + spot + perp  # gross, pre-borrow
    # Fire when the raw total is negative AND the PM borrow cannot be
    # reconciled against the live collateral: either the six-figure collateral
    # dwarfs the (small) negative — the classic "-$800" lag — OR the borrow
    # exceeds all visible collateral, which is impossible in a live PM account
    # and means the collateral price feed collapsed (HYPE oracle → spot ~0).
    parity_stale = (
        total_equity < 0.0
        and spot_borrow > 0.0
        and (collateral_base > abs(total_equity) or spot_borrow > collateral_base)
    )
    if parity_stale:
        log.warning(
            "capital_calc: PARITY-STALE guard fired — raw total=%.2f negative "
            "while collateral_base=%.2f, borrow=%.2f. Using oracle computation.",
            total_equity,
            collateral_base,
            spot_borrow,
        )
    log.info(
        "capital_calc: hl_coll=%.2f hl_debt=%.2f hl_net=%.2f perp=%.2f "
        "spot_non_stable=%.2f spot_stables=%.2f pear_staked=%.2f "
        "vault_deposits=%.2f spot_borrow=%.2f upnl=%.2f -> net=%.2f "
        "total_equity=%.2f gross=%.2f",
        hl_coll,
        hl_debt,
        hl_net,
        perp,
        spot,
        stables,
        pear_staked,
        vault_deposits,
        spot_borrow,
        upnl,
        net,
        total_equity,
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
        pear_staked_usd=pear_staked,
        vault_deposits_usd=vault_deposits,
        spot_borrow_usd=spot_borrow,
        pear_staked_balance=pear_balance,
        pear_staked_price=pear_price,
        pear_staked_known=pear_known,
        parity_stale=parity_stale,
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

    R-DASHBOARD-RABBY-PARITY (2026-05-06): top line is now
    ``TOTAL EQUITY`` (matches Rabby) with NET as a sub-line below.

    Layout::

        💰 TOTAL EQUITY: $36.6K  (Rabby parity)
        ├─ NET (post-leverage): $33.6K
        │   ├─ HL net (col-debt): $30.8K
        │   ├─ Perp account: $2.7K
        │   └─ Spot non-stable: $44
        ├─ Spot stables (cash equiv): $1.7K
        └─ Pear Protocol staked: $1.2K

        UPnL perp (already en perp account): +$231

        Gross exposure: $79.0K  (leverage incluido — informativo)
        ├─ HL collateral: $73.2K
        └─ HL debt: -$45.3K
    """
    lines: list[str] = []
    # ── Top line: total equity (Rabby parity) ─────────────────────────────
    _parity_tag = (
        "  ⚠️ parity feed STALE — usando cómputo oracle"
        if getattr(net, "parity_stale", False)
        else "  (Rabby parity)"
    )
    lines.append(
        f"💰 TOTAL EQUITY: {_fmt_usd(net.total_equity_usd)}{_parity_tag}"
    )
    lines.append(
        f"├─ NET (post-leverage): {_fmt_usd(net.net_total_usd)}"
    )
    lines.append(f"│   ├─ HL net (col-debt): {_fmt_usd(net.hl_net_usd)}")
    lines.append(f"│   ├─ Perp account: {_fmt_usd(net.perp_equity_usd)}")
    lines.append(
        f"│   └─ Spot non-stable: {_fmt_usd(net.spot_non_stable_usd)}"
    )
    # Optional sibling sub-lines (only rendered when > $0.01). The LAST
    # present sibling gets the └─ connector. R-VAULTDEP adds the HL vault
    # deposits line here so it's visibly folded into TOTAL EQUITY.
    # R-PEAR-ASSET-INTEGRATION: PEAR is the fund's 2nd asset, surfaced as a
    # first-class line with its live balance × price (or "n/d" on read fail).
    _siblings: list[str] = []
    if net.spot_stables_usd > 0.01:
        _siblings.append(
            f"Spot stables (cash equiv): {_fmt_usd(net.spot_stables_usd)}"
        )
    if getattr(net, "pear_staked_known", True):
        if net.pear_staked_usd > 0.01:
            _bal = getattr(net, "pear_staked_balance", 0.0) or 0.0
            _px = getattr(net, "pear_staked_price", 0.0) or 0.0
            if _bal > 0 and _px > 0:
                _siblings.append(
                    f"PEAR (2º activo): {_bal:,.0f} stPEAR × ${_px:.5f} "
                    f"= {_fmt_usd(net.pear_staked_usd)}"
                )
            else:
                _siblings.append(
                    f"PEAR (2º activo): {_fmt_usd(net.pear_staked_usd)}"
                )
    else:
        _siblings.append(
            "PEAR (2º activo): n/d (lectura on-chain/precio falló — excluido del equity)"
        )
    if net.vault_deposits_usd > 0.01:
        _siblings.append(f"Vault Deposits (HL): {_fmt_usd(net.vault_deposits_usd)}")
    for _i, _entry in enumerate(_siblings):
        _tee = "└─" if _i == len(_siblings) - 1 else "├─"
        lines.append(f"{_tee} {_entry}")
    # R-WALLET-FIX (2026-06-06): show the PM borrow as an explicit liability
    # netted out of TOTAL EQUITY, so the headline can never silently overstate.
    if net.spot_borrow_usd > 0.01:
        lines.append(
            f"⚠️ Deuda PM (USDC borrowed): -{_fmt_usd(net.spot_borrow_usd)}  "
            "(restada del TOTAL EQUITY)"
        )
    lines.append("")
    lines.append(
        f"UPnL perp (already en perp account): "
        f"{_fmt_signed(net.upnl_perp_usd)}"
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

    R-DASHBOARD-RABBY-PARITY (2026-05-06): top line is now ``TOTAL EQUITY``
    matching Rabby's headline. NET (post-leverage exposure) is a sub-line
    with HL/perp/spot-non-stable breakdown. ``Spot stables`` and
    ``Pear Protocol staked`` are sibling sub-lines folded into the headline.
    Gross exposure stays as the informative footer (leverage view).

    ``fmt_compact_usd`` and ``signed`` are passed in so the auto module
    stays decoupled from the dashboard's escape/format helpers.

    ``upnl_cls`` / ``upnl_fmt`` are optional pre-formatted (class, value)
    pair if the caller already computed them in its colour scheme; if not
    provided, they're derived from ``net.upnl_perp_usd``.
    """
    if upnl_cls is None or upnl_fmt is None:
        upnl_cls, upnl_fmt = signed(net.upnl_perp_usd)

    # R-DASHBOARD-RABBY-PARITY: optional sub-lines, only shown when > $0.01
    # so empty buckets don't pollute the card.
    extra_lines: list[str] = []
    if net.spot_stables_usd > 0.01:
        extra_lines.append(
            f"<p>&nbsp;&nbsp;Spot stables (cash equiv): "
            f"{fmt_compact_usd(net.spot_stables_usd)}</p>"
        )
    # R-PEAR-ASSET-INTEGRATION: PEAR 2nd asset — live balance × price or n/d.
    if getattr(net, "pear_staked_known", True):
        if net.pear_staked_usd > 0.01:
            _bal = getattr(net, "pear_staked_balance", 0.0) or 0.0
            _px = getattr(net, "pear_staked_price", 0.0) or 0.0
            if _bal > 0 and _px > 0:
                extra_lines.append(
                    f"<p>&nbsp;&nbsp;PEAR (2º activo): "
                    f"{_bal:,.0f} stPEAR × ${_px:.5f} = "
                    f"{fmt_compact_usd(net.pear_staked_usd)}</p>"
                )
            else:
                extra_lines.append(
                    f"<p>&nbsp;&nbsp;PEAR (2º activo): "
                    f"{fmt_compact_usd(net.pear_staked_usd)}</p>"
                )
    else:
        extra_lines.append(
            "<p>&nbsp;&nbsp;PEAR (2º activo): "
            "<span class='dim'>n/d (lectura on-chain/precio falló)</span></p>"
        )
    # R-VAULTDEP: fund capital inside HL vaults (separate from wallets).
    if net.vault_deposits_usd > 0.01:
        extra_lines.append(
            f"<p>&nbsp;&nbsp;Vault Deposits (HL): "
            f"{fmt_compact_usd(net.vault_deposits_usd)}</p>"
        )
    # R-WALLET-FIX (2026-06-06): explicit PM borrow liability line.
    if net.spot_borrow_usd > 0.01:
        extra_lines.append(
            f"<p>&nbsp;&nbsp;⚠️ Deuda PM (USDC borrowed): "
            f"-{fmt_compact_usd(net.spot_borrow_usd)}</p>"
        )
    extra_block = "".join(extra_lines)

    return (
        # ── Headline: TOTAL EQUITY (Rabby parity) ──────────────────────
        f"<p>💰 <strong>TOTAL EQUITY: "
        f"{fmt_compact_usd(net.total_equity_usd)}</strong>"
        f" <span class='dim'>{'⚠️ parity feed stale — oracle' if getattr(net, 'parity_stale', False) else '(Rabby parity)'}</span></p>"
        # ── Sub: NET (post-leverage exposure) ──────────────────────────
        f"<p>&nbsp;&nbsp;NET (post-leverage): "
        f"{fmt_compact_usd(net.net_total_usd)}</p>"
        f"<p>&nbsp;&nbsp;&nbsp;&nbsp;HL net (col-debt): "
        f"{fmt_compact_usd(net.hl_net_usd)}</p>"
        f"<p>&nbsp;&nbsp;&nbsp;&nbsp;Perp account: "
        f"{fmt_compact_usd(net.perp_equity_usd)}</p>"
        f"<p>&nbsp;&nbsp;&nbsp;&nbsp;Spot non-stable: "
        f"{fmt_compact_usd(net.spot_non_stable_usd)}</p>"
        # ── Sibling sub-lines: stables + Pear staked ───────────────────
        f"{extra_block}"
        f"<p>&nbsp;&nbsp;UPnL perp (en perp): "
        f"<span class='{upnl_cls}'>{upnl_fmt}</span></p>"
        f"<p>&nbsp;</p>"
        # ── Footer: gross exposure (informative leverage view) ─────────
        f"<p class='dim'>Gross exposure: {fmt_compact_usd(net.gross_exposure_usd)}"
        f" <span class='dim'>(leverage incluido — informativo)</span></p>"
        f"<p>&nbsp;&nbsp;HL collateral: {fmt_compact_usd(net.hl_collateral_usd)}</p>"
        f"<p>&nbsp;&nbsp;HL debt: -{fmt_compact_usd(net.hl_debt_usd)}</p>"
    )
