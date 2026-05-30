"""R-VAULTDEP — track BCD's own capital deposited INTO HyperLiquid vaults.

The fund deposited $5,000 USDC into the HL vault "Systemic Strategies
HyperGrowth" (vault ``0xd6e5…5b42``). That capital lives under the *vault*
address, not in any fund wallet, so the wallet/Rabby-parity equity total
silently omitted it (~$5K under-count). This module reads the depositor's
live equity inside each configured vault and folds the total into TOTAL
EQUITY as its own line item — never double-counted against perp margin or
wallet USDC.

WHY ``userVaultEquities`` (not ``vaultDetails.followers``)
----------------------------------------------------------
``vaultDetails`` returns only the TOP ~100 followers by equity (min ~$19K on
HyperGrowth at 2026-05-30). A ~$5K deposit is below that cutoff and never
appears in the ``followers`` array. ``userVaultEquities`` returns the
depositor's own equity directly, keyed by wallet, regardless of rank::

    POST https://api.hyperliquid.xyz/info
    {"type":"userVaultEquities","user":"0xc7ae…1505"}
    -> [{"vaultAddress":"0xd6e5…5b42","equity":"5062.32","lockedUntilTimestamp":…}, …]

Keyless, read-only, non-custodial: no private key, no agent wallet, no
custody, no fund movement. Same trust surface as the existing public vault
stats reads.

Robustness contract
-------------------
``fetch_vault_deposits()`` NEVER raises. On total failure it returns
``ok=False`` (renderers show "n/a (vault read failed)"; the equity total
contributes 0 so the report neither crashes nor inflates). Null / missing
fields are coerced safely. Results are cached in-memory for 45s.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

try:
    from config import BLACKCAT_VAULT_DEPOSITS, HYPERLIQUID_API
except Exception:  # noqa: BLE001 — keep importable in isolated tests
    BLACKCAT_VAULT_DEPOSITS = []
    HYPERLIQUID_API = "https://api.hyperliquid.xyz"

log = logging.getLogger(__name__)

_INFO_URL = f"{HYPERLIQUID_API}/info"
# Browser UA — some HL edge nodes 1010 a bare urllib UA (CF challenge).
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_HTTP_TIMEOUT_SEC = 8.0
_CACHE_TTL_SEC = 45.0

# In-memory cache only (never browser storage). {"ts": epoch, "result": …}
_cache: dict[str, Any] = {"ts": 0.0, "result": None}


@dataclass(frozen=True)
class VaultDeposit:
    """One configured deposit and its live valuation."""

    label: str
    vault_address: str
    depositor_address: str
    cost_basis_usd: float
    equity_usd: float
    pnl_usd: float
    locked_until_ts: int  # epoch ms (0 = unknown / unlocked)
    found: bool  # True iff the depositor actually holds equity in this vault


@dataclass(frozen=True)
class VaultDepositsResult:
    """Aggregate result for all configured deposits.

    ``ok`` is False only when there ARE configured deposits but every read
    failed (so renderers can show "n/a" without crashing). With zero
    configured deposits ``ok`` is True and ``total_usd`` is 0.
    """

    ok: bool
    total_usd: float
    deposits: list[VaultDeposit] = field(default_factory=list)
    error: str | None = None


def _post_user_vault_equities(depositor: str) -> list[dict]:
    """POST userVaultEquities for one depositor. Raises on transport error."""
    body = json.dumps(
        {"type": "userVaultEquities", "user": depositor}
    ).encode()
    req = urllib.request.Request(
        _INFO_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": _UA,
        },
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as r:
        data = json.load(r)
    return data if isinstance(data, list) else []


def _safe_float(v: Any) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def fetch_vault_deposits(force: bool = False) -> VaultDepositsResult:
    """Read every configured deposit's live equity. NEVER raises.

    Groups config entries by depositor so each wallet is queried once even
    if it holds multiple configured vaults. Cached for 45s.
    """
    now = time.time()
    if (
        not force
        and _cache["result"] is not None
        and (now - _cache["ts"]) < _CACHE_TTL_SEC
    ):
        return _cache["result"]  # type: ignore[return-value]

    entries = list(BLACKCAT_VAULT_DEPOSITS or [])
    if not entries:
        result = VaultDepositsResult(ok=True, total_usd=0.0, deposits=[])
        _cache.update(ts=now, result=result)
        return result

    # Query each unique depositor once → {depositor_lower: {vault_lower: row}}
    depositors = {str(e.get("depositor_address", "")).lower() for e in entries}
    depositors.discard("")
    equities_by_depositor: dict[str, dict[str, dict]] = {}
    queries_ok = 0
    last_err: str | None = None
    for dep in depositors:
        try:
            rows = _post_user_vault_equities(dep)
            by_vault: dict[str, dict] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                va = str(row.get("vaultAddress", "")).lower()
                if va:
                    by_vault[va] = row
            equities_by_depositor[dep] = by_vault
            queries_ok += 1
        except Exception as e:  # noqa: BLE001 — robustness contract
            last_err = f"{type(e).__name__}: {e}"
            log.warning("vault_deposits: query failed for %s — %s", dep, last_err)

    deposits: list[VaultDeposit] = []
    total = 0.0
    for e in entries:
        va = str(e.get("vault_address", "")).lower()
        dep = str(e.get("depositor_address", "")).lower()
        label = str(e.get("label") or "Vault deposit")
        cost_basis = _safe_float(e.get("cost_basis"))
        row = equities_by_depositor.get(dep, {}).get(va)
        if row is not None:
            equity = _safe_float(row.get("equity"))
            locked = _safe_int(row.get("lockedUntilTimestamp"))
            deposits.append(
                VaultDeposit(
                    label=label,
                    vault_address=va,
                    depositor_address=dep,
                    cost_basis_usd=cost_basis,
                    equity_usd=equity,
                    pnl_usd=equity - cost_basis,
                    locked_until_ts=locked,
                    found=True,
                )
            )
            total += equity
        else:
            # Either the query failed, or the depositor isn't in this vault.
            deposits.append(
                VaultDeposit(
                    label=label,
                    vault_address=va,
                    depositor_address=dep,
                    cost_basis_usd=cost_basis,
                    equity_usd=0.0,
                    pnl_usd=0.0,
                    locked_until_ts=0,
                    found=False,
                )
            )

    # ok=False only when we have entries but EVERY depositor query failed.
    ok = queries_ok > 0
    result = VaultDepositsResult(
        ok=ok,
        total_usd=total if ok else 0.0,
        deposits=deposits,
        error=None if ok else (last_err or "all vault reads failed"),
    )
    _cache.update(ts=now, result=result)
    return result


def get_vault_deposits_total(force: bool = False) -> float:
    """Live USD total of all tracked vault deposits. 0.0 on failure (safe)."""
    try:
        r = fetch_vault_deposits(force=force)
        return float(r.total_usd) if r.ok else 0.0
    except Exception as e:  # noqa: BLE001 — never break a caller
        log.warning("get_vault_deposits_total failed: %s", e)
        return 0.0


def _fmt_usd_exact(v: float) -> str:
    return f"${v:,.0f}"


def _fmt_signed_exact(v: float) -> str:
    if v > 0:
        return f"+${v:,.0f}"
    if v < 0:
        return f"-${abs(v):,.0f}"
    return "$0"


def _fmt_lockup(locked_until_ts: int) -> str:
    """Human lockup hint. Empty string when unlocked/unknown."""
    if locked_until_ts <= 0:
        return ""
    try:
        # HL lockedUntilTimestamp is epoch milliseconds.
        dt = datetime.fromtimestamp(locked_until_ts / 1000.0, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return ""
    if dt <= datetime.now(timezone.utc):
        return ""
    return f" | 🔒 hasta {dt.strftime('%Y-%m-%d')}"


def format_vault_deposits_telegram(
    result: VaultDepositsResult | None = None,
) -> str:
    """Telegram detail block for the capital section. Never raises.

    Empty string when nothing is configured (so the report stays clean).
    On read failure: a single explicit "n/a" line — never a crash.
    """
    try:
        if result is None:
            result = fetch_vault_deposits()
    except Exception:  # noqa: BLE001
        return "🏦 Vault Deposits (HL): n/a (vault read failed)"

    if not result.ok:
        return "🏦 Vault Deposits (HL): n/a (vault read failed)"

    if not result.deposits:
        return ""  # nothing configured → render nothing

    lines: list[str] = []
    lines.append(
        "🏦 VAULT DEPOSITS (capital DENTRO de protocolo HL — "
        "separado de balances de wallet)"
    )
    n = len(result.deposits)
    for i, d in enumerate(result.deposits):
        tee = "└─" if i == n - 1 else "├─"
        if not d.found:
            lines.append(f"{tee} {d.label}: n/a (depositante no encontrado)")
            continue
        pnl_txt = f"PnL {_fmt_signed_exact(d.pnl_usd)} vs {_fmt_usd_exact(d.cost_basis_usd)}"
        lines.append(
            f"{tee} {d.label}: {_fmt_usd_exact(d.equity_usd)}  "
            f"({pnl_txt}{_fmt_lockup(d.locked_until_ts)})"
        )
    return "\n".join(lines)
