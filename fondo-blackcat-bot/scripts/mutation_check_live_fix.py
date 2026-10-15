#!/usr/bin/env python3
"""R-LIVE-FIX mutation check — proves tests/test_live_fix.py actually bites.

Every contract pinned by tests/test_live_fix.py is broken here ON PURPOSE,
one at a time, in the real source file. The run must go RED for each. Any
mutation that leaves the suite green is a hole in the net and this script
exits non-zero naming it.

Usage:  python3 scripts/mutation_check_live_fix.py
        (from fondo-blackcat-bot/; never run it in a dirty tree — it edits
        source files in place and restores them in a finally block.)
"""
from __future__ import annotations

import os
import subprocess
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
TEST = "tests/test_live_fix.py"

# (label, relative path, needle, replacement)
MUTATIONS: list[tuple[str, str, str, str]] = [
    # ── utils/http.py: cooldown por host ──────────────────────────────────
    (
        "cooldown check removed: a host in 429 penalty gets hit again "
        "(THE storm: every call re-tried CoinGecko during the penalty)",
        "utils/http.py",
        "    restante = cooldown_remaining(url)\n    if restante > 0:",
        "    restante = 0.0\n    if restante > 0:",
    ),
    (
        "429 no longer recorded at host level (_note_429 becomes a no-op)",
        "utils/http.py",
        "    st[\"strikes\"] = float(st.get(\"strikes\", 0)) + 1\n",
        "    return 0.0\n    st[\"strikes\"] = float(st.get(\"strikes\", 0)) + 1\n",
    ),
    (
        "penalty stops growing with consecutive 429s (exponent frozen)",
        "utils/http.py",
        "castigo = COOLDOWN_BASE_SEC * (2 ** (int(st[\"strikes\"]) - 1))",
        "castigo = COOLDOWN_BASE_SEC",
    ),
    (
        "penalty cap removed",
        "utils/http.py",
        "    castigo = min(castigo, COOLDOWN_MAX_SEC)\n",
        "",
    ),
    (
        "Retry-After ignored for the host cooldown",
        "utils/http.py",
        "    if pedido is not None and pedido > 0:\n        castigo = pedido\n",
        "    if False:\n        castigo = pedido\n",
    ),
    (
        "a 2xx no longer resets the strike counter",
        "utils/http.py",
        "                _note_success(url)\n",
        "",
    ),
    (
        "429 without Retry-After is retried in-call again (2s/4s guessing)",
        "utils/http.py",
        "                if _retry_after_sec(exc) is None:\n",
        "                if False:\n",
    ),
    # ── modules/market.py: HL primero ─────────────────────────────────────
    (
        "CoinGecko price overwrites the HL oracle price for core assets",
        "modules/market.py",
        "        if sym in out:\n            # HL ya puso el precio; CoinGecko solo enriquece.",
        "        if False:\n            # HL ya puso el precio; CoinGecko solo enriquece.",
    ),
    (
        "HL oracle no longer consulted at all (back to CoinGecko-primary)",
        "modules/market.py",
        "    oracle = _hl_oracle_map()\n",
        "    oracle = {}\n",
    ),
    (
        "24h change taken from CoinGecko even when HL prevDayPx exists",
        "modules/market.py",
        "            if out[sym].get(\"change_24h\") is None:\n                out[sym][\"change_24h\"] = entry.get(\"usd_24h_change\")",
        "            if True:\n                out[sym][\"change_24h\"] = entry.get(\"usd_24h_change\")",
    ),
    (
        "missing core price no longer declared (silent market degradation)",
        "modules/market.py",
        "    if core_missing:\n        # Esto SI es money path",
        "    if False:\n        # Esto SI es money path",
    ),
    (
        "CoinGecko failure degrades market even when HL covered the core",
        "modules/market.py",
        "    if gecko_err:\n        # Constancia",
        "    if gecko_err:\n        health_registry.swallowed(\"market\", \"coingecko\")\n        # Constancia",
    ),
    (
        "negative cache removed: a failed CoinGecko call is retried next tick",
        "modules/market.py",
        "    if _cache_get(fail_key, GECKO_FAIL_TTL_SEC) is not None:\n        raise RuntimeError(",
        "    if False:\n        raise RuntimeError(",
    ),
    (
        "CoinGecko price call goes back to 3 in-call retries",
        "modules/market.py",
        "params=params,\n                                              max_retries=1)",
        "params=params,\n                                              max_retries=3)",
    ),
    (
        "price assembly TTL back to 60s",
        "modules/market.py",
        "PRICES_TTL_SEC = 300",
        "PRICES_TTL_SEC = 60",
    ),
    (
        "/global failure returns {} again (n/d indistinguishable from a miss)",
        "modules/market.py",
        "        _cache_set(\"gecko_global:fail\", True)\n        log.warning(\"CoinGecko global unavailable",
        "        return {}\n        log.warning(\"CoinGecko global unavailable",
    ),
    (
        "unlock BTC.D goes back to its own raw CoinGecko GET",
        "modules/unlock_monitor.py",
        "        from modules.market import coingecko_global\n        g = await coingecko_global()",
        "        g = {}",
    ),
    # ── bot.py: cadencia ──────────────────────────────────────────────────
    (
        "dashboard proactive refresh back to 30s",
        "bot.py",
        "DASHBOARD_PROACTIVE_REFRESH_DEFAULT_SEC = 300",
        "DASHBOARD_PROACTIVE_REFRESH_DEFAULT_SEC = 30",
    ),
    # ── modules/hl_borrow_lend.py: forma del endpoint + procedencia ───────
    (
        "back to the parameterless borrowLendReserveState (the 422)",
        "modules/hl_borrow_lend.py",
        "ALL_RESERVES_REQUEST: dict[str, Any] = {\"type\": \"allBorrowLendReserveStates\"}",
        "ALL_RESERVES_REQUEST: dict[str, Any] = {\"type\": \"borrowLendReserveState\"}",
    ),
    (
        "indexed pairs [idx, state] no longer parsed",
        "modules/hl_borrow_lend.py",
        "        if isinstance(row, (list, tuple)) and len(row) == 2:\n            # Forma vigente: [token_index, state]",
        "        if False:\n            # Forma vigente: [token_index, state]",
    ),
    (
        "unknown token index gets a guessed name instead of being skipped",
        "modules/hl_borrow_lend.py",
        "            name = index_map.get(idx, \"\")\n            state = row[1]",
        "            name = index_map.get(idx, f\"TOKEN{idx}\")\n            state = row[1]",
    ),
    (
        "endpoint failure no longer declared in health_registry (silent again)",
        "modules/hl_borrow_lend.py",
        "        health_registry.swallowed(\"pm_state\", f\"get_collateral_ltv_map: {err[:120]}\")\n",
        "",
    ),
    (
        "200-without-LTV treated as success",
        "modules/hl_borrow_lend.py",
        "        raise ValueError(\n            f\"allBorrowLendReserveStates sin LTV usable \"",
        "        return dict(_cache[\"ltv\"])\n        raise ValueError(\n            f\"allBorrowLendReserveStates sin LTV usable \"",
    ),
    (
        "provenance never set to hl_live on success",
        "modules/hl_borrow_lend.py",
        "            _set_provenance(\"hl_live\", fetched_at=now)\n",
        "",
    ),
    (
        "failure provenance lies: stays hl_live / loses the error",
        "modules/hl_borrow_lend.py",
        "        if _cache[\"ltv\"]:\n            _set_provenance(\"hl_cache\", error=err)\n        else:\n            _set_provenance(\"none\", error=err)\n",
        "",
    ),
    # ── portfolio_margin: etiqueta en el PMState y en el panel ────────────
    (
        "PMState.ltv_source blanked (panel prints config LTV as measured)",
        "modules/portfolio_margin.py",
        "    ltv_source = _ltv_source_for(ltv_map)\n",
        "    ltv_source = \"\"\n",
    ),
    (
        "config fallback labelled like a live reading",
        "modules/portfolio_margin.py",
        "    return f\"CONFIG — HL no leido ({err})\"",
        "    return \"HL live\"",
    ),
    (
        "render drops the provenance next to the LTV",
        "modules/portfolio_margin.py",
        "        + (f\" · {pm.ltv_source}\" if pm.ltv_source else \"\")\n",
        "",
    ),
]


def _run_suite() -> tuple[bool, str]:
    env = dict(os.environ)
    env.setdefault("TZ", "UTC")
    env.setdefault("TELEGRAM_BOT_TOKEN", "x")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q", "-o", "asyncio_mode=auto",
         "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True, env=env,
    )
    return proc.returncode == 0, (proc.stdout or "")[-400:]


def main() -> int:
    os.chdir(REPO)

    green, tail = _run_suite()
    if not green:
        print("BASELINE IS RED — fix the suite before mutating:\n" + tail)
        return 2
    print("baseline: GREEN\n")

    survivors: list[str] = []
    for label, rel, needle, repl in MUTATIONS:
        path = os.path.join(REPO, rel)
        with open(path, "r", encoding="utf-8") as fh:
            original = fh.read()
        if needle not in original:
            survivors.append(f"{label}\n      (needle not found in {rel} — "
                             f"the mutation could not be applied; update this script)")
            print(f"  SKIP  {rel}: needle not found — {label}")
            continue
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original.replace(needle, repl, 1))
            still_green, out = _run_suite()
        finally:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original)
        if still_green:
            survivors.append(label)
            print(f"  SURVIVED  {rel}: {label}")
        else:
            print(f"  killed    {rel}: {label}")

    print()
    green_again, tail = _run_suite()
    if not green_again:
        print("SOURCE NOT RESTORED CLEANLY — check `git diff`:\n" + tail)
        return 2

    if survivors:
        print(f"{len(survivors)} MUTATION(S) SURVIVED — the suite does not "
              f"pin these contracts:")
        for s in survivors:
            print(f"  - {s}")
        return 1

    print(f"all {len(MUTATIONS)} mutations killed — suite restored GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
