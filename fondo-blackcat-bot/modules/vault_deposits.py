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
    from config import (
        BLACKCAT_VAULT_DEPOSITS,
        HYPERLIQUID_API,
        VAULT_AUTODISCOVER,
        VAULT_DUST_USD,
        PM_PRIMARY_WALLET,
        FUND_WALLETS,
    )
except Exception:  # noqa: BLE001 — keep importable in isolated tests
    BLACKCAT_VAULT_DEPOSITS = []
    HYPERLIQUID_API = "https://api.hyperliquid.xyz"
    VAULT_AUTODISCOVER = True
    VAULT_DUST_USD = 1.0
    PM_PRIMARY_WALLET = "0xc7ae23316b47f7e75f455f53ad37873a18351505"
    FUND_WALLETS = {}

log = logging.getLogger(__name__)

_INFO_URL = f"{HYPERLIQUID_API}/info"
# Vault name cache: {vault_address_lower: name}. Resolved lazily via
# vaultDetails (keyless), cached for the process lifetime.
_name_cache: dict[str, str] = {}
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
    """One deposit (configured or auto-discovered) and its live valuation."""

    label: str
    vault_address: str
    depositor_address: str
    cost_basis_usd: float
    equity_usd: float
    pnl_usd: float
    locked_until_ts: int  # epoch ms (0 = unknown / unlocked)
    found: bool  # True iff the depositor actually holds equity in this vault
    # R-PMCORE (2026-06-01): auto-discovered vaults have no configured cost
    # basis. cost_basis_known=False → PnL vs cost is suppressed and the
    # all-time return is computed from the FIRST recorded snapshot instead
    # (see modules.vault_history). auto_discovered tags how we found it.
    cost_basis_known: bool = True
    auto_discovered: bool = False


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


def _short_addr(a: str) -> str:
    a = a or ""
    return (a[:6] + "…" + a[-4:]) if len(a) >= 12 else (a or "?")


def _resolve_vault_name(vault_address: str) -> str:
    """Best-effort human name for a vault via ``vaultDetails`` (keyless).

    Cached for the process lifetime. NEVER raises — falls back to the short
    address on any failure. Separated out so tests can monkeypatch it.
    """
    va = (vault_address or "").lower()
    if not va:
        return "?"
    if va in _name_cache:
        return _name_cache[va]
    name = _short_addr(va)
    try:
        body = json.dumps({"type": "vaultDetails", "vaultAddress": va}).encode()
        req = urllib.request.Request(
            _INFO_URL, data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": _UA},
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as r:
            d = json.load(r)
        nm = (d or {}).get("name") if isinstance(d, dict) else None
        if nm:
            # Strip decorative junk some vaults use (brackets, infinity emoji).
            name = str(nm).replace("[", "").replace("]", "").replace("♾️", "").strip() or _short_addr(va)
    except Exception as e:  # noqa: BLE001 — best-effort
        log.debug("vault name resolve failed for %s: %s", va, e)
    _name_cache[va] = name
    return name


def _fund_depositor_wallets() -> set[str]:
    """Wallets to auto-scan for vault deposits: PM primary + all fund wallets."""
    out: set[str] = set()
    try:
        if PM_PRIMARY_WALLET:
            out.add(PM_PRIMARY_WALLET.lower())
    except Exception:  # noqa: BLE001
        pass
    try:
        for w in (FUND_WALLETS or {}):
            if w:
                out.add(str(w).lower())
    except Exception:  # noqa: BLE001
        pass
    return out


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
    # R-PMCORE (2026-06-01): even with zero configured entries we still
    # auto-discover every vault the fund's wallets are in. With autodiscover
    # off AND no config, there's nothing to do.
    if not entries and not VAULT_AUTODISCOVER:
        result = VaultDepositsResult(ok=True, total_usd=0.0, deposits=[])
        _cache.update(ts=now, result=result)
        return result

    # Depositors to query: configured depositors UNION (if autodiscover on)
    # the fund's own wallets. Query each unique depositor once.
    depositors = {str(e.get("depositor_address", "")).lower() for e in entries}
    depositors.discard("")
    if VAULT_AUTODISCOVER:
        depositors |= _fund_depositor_wallets()
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
    seen: set[tuple[str, str]] = set()
    # ── 1. Configured deposits first (keep their labels + cost basis) ──
    for e in entries:
        va = str(e.get("vault_address", "")).lower()
        dep = str(e.get("depositor_address", "")).lower()
        label = str(e.get("label") or "Vault deposit")
        cost_basis = _safe_float(e.get("cost_basis"))
        seen.add((dep, va))
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
                    cost_basis_known=cost_basis > 0,
                    auto_discovered=False,
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
                    cost_basis_known=cost_basis > 0,
                    auto_discovered=False,
                )
            )

    # ── 2. Auto-discovered deposits (every vault the fund holds, > dust) ──
    if VAULT_AUTODISCOVER:
        for dep, by_vault in equities_by_depositor.items():
            for va, row in by_vault.items():
                if (dep, va) in seen:
                    continue
                seen.add((dep, va))
                equity = _safe_float(row.get("equity"))
                if equity <= VAULT_DUST_USD:
                    continue  # dust / closed — skip
                locked = _safe_int(row.get("lockedUntilTimestamp"))
                deposits.append(
                    VaultDeposit(
                        label=_resolve_vault_name(va),
                        vault_address=va,
                        depositor_address=dep,
                        cost_basis_usd=0.0,
                        equity_usd=equity,
                        pnl_usd=0.0,  # unknown basis → PnL deferred to history
                        locked_until_ts=locked,
                        found=True,
                        cost_basis_known=False,
                        auto_discovered=True,
                    )
                )
                total += equity

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

    # Show found deposits (each vault on its OWN line — never aggregated).
    shown = [d for d in result.deposits if d.found or not getattr(d, "auto_discovered", False)]
    if not any(d.found for d in result.deposits):
        # nothing actually held — keep report clean unless a configured
        # vault explicitly wasn't found (surfaced below).
        if not result.deposits:
            return ""
    lines: list[str] = []
    lines.append(
        "🏦 VAULT DEPOSITS (capital DENTRO de protocolo HL — "
        "cada vault por separado, fuera de los balances de wallet)"
    )
    n = len(shown)
    grand = 0.0
    for i, d in enumerate(shown):
        tee = "└─" if i == n - 1 else "├─"
        if not d.found:
            lines.append(f"{tee} {d.label}: n/a (depositante no encontrado)")
            continue
        grand += d.equity_usd
        if getattr(d, "cost_basis_known", True) and d.cost_basis_usd > 0:
            pnl_txt = (
                f"PnL {_fmt_signed_exact(d.pnl_usd)} vs "
                f"{_fmt_usd_exact(d.cost_basis_usd)}"
            )
        else:
            # Auto-discovered: no configured cost basis → show equity only,
            # evolution/PnL comes from the SQLite history baseline.
            pnl_txt = "costo no configurado"
        lines.append(
            f"{tee} {d.label}: {_fmt_usd_exact(d.equity_usd)}  "
            f"({pnl_txt}{_fmt_lockup(d.locked_until_ts)})"
        )
    if sum(1 for d in shown if d.found) > 1:
        lines.append(f"   Σ vaults: {_fmt_usd_exact(grand)}")
    return "\n".join(lines)
