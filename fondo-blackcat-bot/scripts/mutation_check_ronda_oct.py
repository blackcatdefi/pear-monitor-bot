#!/usr/bin/env python3
"""R-RONDA-OCT mutation check — proves the round's tests actually bite.

Covers items 1 (debt carry + RETIRO PERSONAL) and 2 (HL cooldown + candle
cache). Each contract is broken ON PURPOSE, one at a time, in the real source
file; the matching test file must go RED. A mutation that leaves the tests
green is a hole in the net and this script exits non-zero naming it.

Usage:  python3 scripts/mutation_check_ronda_oct.py
        (from fondo-blackcat-bot/; never run it in a dirty tree — it edits
        source files in place and restores them in a finally block.)
"""
from __future__ import annotations

import os
import subprocess
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
T_DEUDA = "tests/test_ronda_oct_deuda.py"
T_HL = "tests/test_ronda_oct_hl_cooldown.py"

# (label, test file, relative source path, needle, replacement)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ── item 1a: debt_carry ────────────────────────────────────────────────
    (
        "APR read from the wrong token (not USDC = 0)",
        T_DEUDA, "modules/debt_carry.py",
        "USDC_TOKEN_INDEX = 0\n", "USDC_TOKEN_INDEX = 150\n",
    ),
    (
        "APR assumed: config fallback printed as HL live",
        T_DEUDA, "modules/debt_carry.py",
        'return {"apr": _APR_FALLBACK, "source": "config", "age_s": None, "error": err}',
        'return {"apr": _APR_FALLBACK, "source": "hl_live", "age_s": None, "error": err}',
    ),
    (
        "APR failure no longer degrades the subsystem",
        T_DEUDA, "modules/debt_carry.py",
        '        health_registry.swallowed(SUBSYSTEM, f"get_usdc_borrow_apr: {err[:120]}")\n',
        "",
    ),
    (
        "percent-form APR not normalised (5.0 taken as 500%)",
        T_DEUDA, "modules/debt_carry.py",
        "            if v > 1.5:\n                v = v / 100.0\n",
        "            if False:\n                v = v / 100.0\n",
    ),
    (
        "accrual uses the CURRENT debt instead of the one observed at the previous tick",
        T_DEUDA, "modules/debt_carry.py",
        'base = float(acc.get("last_debt") if acc.get("last_debt") is not None else d)',
        "base = d",
    ),
    (
        "accrual not divided by a year (interest per second = APR)",
        T_DEUDA, "modules/debt_carry.py",
        "delta = base * apr * ((now_ms - last) / 1000.0) / _YEAR_SEC",
        "delta = base * apr * ((now_ms - last) / 1000.0) / 86400.0",
    ),
    (
        "accrued total not persisted (always restarts at the delta)",
        T_DEUDA, "modules/debt_carry.py",
        'total = float(acc.get("accrued_total") or 0.0) + delta',
        "total = delta",
    ),
    (
        "min tick interval ignored (every PM read writes a tick)",
        T_DEUDA, "modules/debt_carry.py",
        "if last > 0 and not force and (now_ms - last) < _MIN_TICK_SEC * 1000:",
        "if False:",
    ),
    (
        "since-mark accumulators return the whole history",
        T_DEUDA, "modules/debt_carry.py",
        'return float(acc.get("accrued_total") or 0.0) - float(mark["accrued_total"] or 0.0)',
        'return float(acc.get("accrued_total") or 0.0)',
    ),
    (
        "per-day figure computed on the wrong base",
        T_DEUDA, "modules/debt_carry.py",
        'out["per_day"] = d * out["apr"] / 365.0',
        'out["per_day"] = d * out["apr"] / 360.0',
    ),
    (
        "gap >48h no longer flagged",
        T_DEUDA, "modules/debt_carry.py",
        "            out[\"gap_flag\"] = True\n",
        "            out[\"gap_flag\"] = False\n",
    ),
    (
        "CONFIG label dropped from the panel line",
        T_DEUDA, "modules/debt_carry.py",
        'return f"CONFIG — HL no leido ({info.get(\'error\') or \'?\'})"',
        'return "HL live"',
    ),
    (
        "snapshot reports ok even on CONFIG fallback",
        T_DEUDA, "modules/debt_carry.py",
        'out["ok"] = info["source"] != "config"',
        'out["ok"] = True',
    ),
    # ── item 1b: retiros_personales ────────────────────────────────────────
    (
        "bridge filter accepts any destination",
        T_DEUDA, "modules/retiros_personales.py",
        '        if str(d.get("destination", "")).lower() != BRIDGE_ADDR:\n            continue\n',
        "",
    ),
    (
        "bridge filter accepts any token",
        T_DEUDA, "modules/retiros_personales.py",
        '        if str(d.get("token", "")).upper() != "HYPE":\n            continue\n',
        "",
    ),
    (
        "bridge filter accepts sends from other wallets",
        T_DEUDA, "modules/retiros_personales.py",
        '        if str(d.get("user", "")).lower() != w:\n            continue\n',
        "",
    ),
    (
        "match window ignored (buys from any time count)",
        T_DEUDA, "modules/retiros_personales.py",
        "        if t < t0 or t > t1:\n            continue\n",
        "",
    ),
    (
        "sells counted as buys",
        T_DEUDA, "modules/retiros_personales.py",
        "        if not is_buy:\n            continue\n",
        "",
    ),
    (
        "quantity tolerance ignored (any buy explains any send)",
        T_DEUDA, "modules/retiros_personales.py",
        "if qty > 0 and abs(qty - withdrawn) / withdrawn <= QTY_TOL:",
        "if qty > 0:",
    ),
    (
        "a fill can be reused for two sends",
        T_DEUDA, "modules/retiros_personales.py",
        "        if k in used:\n            continue\n",
        "",
    ),
    (
        "fee not folded into USDC borrowed",
        T_DEUDA, "modules/retiros_personales.py",
        'float(m["usdc"]) + float(m["fee_usdc"]), float(m["hype_bought"]),',
        'float(m["usdc"]), float(m["hype_bought"]),',
    ),
    (
        "events stored twice (dedup by hash removed)",
        T_DEUDA, "modules/retiros_personales.py",
        '    fresh = [s for s in sends if s["hash"] not in known]\n',
        "    fresh = list(sends)\n",
    ),
    (
        "scan cursor never advances",
        T_DEUDA, "modules/retiros_personales.py",
        '    _meta_set(f"{META_SCAN_CURSOR}:{w}", str(max_ts if max_ts > 0 else now_ms))\n',
        "",
    ),
    (
        "window query off by one (inclusive start)",
        T_DEUDA, "modules/retiros_personales.py",
        'q = "SELECT * FROM retiros_personales WHERE ts_ms>? AND ts_ms<=?"',
        'q = "SELECT * FROM retiros_personales WHERE ts_ms>=? AND ts_ms<=?"',
    ),
    (
        "PPC exclusion keys empty (retiro buys leak into PPC)",
        T_DEUDA, "modules/retiros_personales.py",
        "    for r in all_rows(wallet):\n        try:\n            out.update(json.loads(r.get(\"fill_keys\") or \"[]\"))",
        "    for r in []:\n        try:\n            out.update(json.loads(r.get(\"fill_keys\") or \"[]\"))",
    ),
    (
        "withdraw-only send not stored",
        T_DEUDA, "modules/retiros_personales.py",
        '    return {"kind": KIND_WITHDRAW_ONLY, "hype_bought": 0.0, "usdc": 0.0,',
        '    return {"kind": KIND_BORROW_BUY_WITHDRAW, "hype_bought": 0.0, "usdc": 0.0,',
    ),
    (
        "hype_acquisition no longer skips retiro fills",
        T_DEUDA, "modules/hype_acquisition.py",
        "        if _excluded and _rp_key is not None and _rp_key(f) in _excluded:",
        "        if False:",
    ),
    (
        "PM panel hides the withdrawn stack",
        T_DEUDA, "modules/portfolio_margin.py",
        "    if retiros_hype is not None and retiros_hype > 0:",
        "    if False:",
    ),
    (
        "delta attribution residual hidden",
        T_DEUDA, "modules/report_delta.py",
        "    if abs(resto) >= 1.0:\n        parts.append(f\"{resto:+,.0f} sin atribuir\")",
        "    if False:\n        parts.append(f\"{resto:+,.0f} sin atribuir\")",
    ),
    (
        "delta attribution line never rendered",
        T_DEUDA, "modules/report_delta.py",
        "            debt_attribution_line(previous.get(\"pm_debt\"), current.get(\"pm_debt\"),\n                                  attribution),\n",
        "",
    ),
    # ── item 2: hl_client cooldown + candle cache ──────────────────────────
    (
        "429 retried in-call again (storm)",
        T_HL, "modules/hl_client.py",
        "                if resp.status_code == 429:\n                    last_exc = RuntimeError(\"HTTP 429\")\n                    _note_host_429(getattr(resp, \"headers\", None))\n                    break\n",
        "                if resp.status_code == 429:\n                    last_exc = RuntimeError(\"HTTP 429\")\n                    await asyncio.sleep(_backoff_delay(attempt))\n                    continue\n",
    ),
    (
        "cooldown check removed on the async path (network touched during penalty)",
        T_HL, "modules/hl_client.py",
        "    if cooling_down(ptype):\n        stale = _serve_stale(key, ptype)\n        if stale is not None:\n            return stale\n        raise _cooldown_error(ptype)\n\n    # In-flight dedupe",
        "    if False:\n        stale = _serve_stale(key, ptype)\n        if stale is not None:\n            return stale\n        raise _cooldown_error(ptype)\n\n    # In-flight dedupe",
    ),
    (
        "cooldown check removed on the sync path",
        T_HL, "modules/hl_client.py",
        "    if cooling_down(ptype):\n        stale = _serve_stale(key, ptype)\n        if stale is not None:\n            return stale\n        raise _cooldown_error(ptype)\n\n    last_exc: Exception | None = None\n    attempts_5xx = 0\n    for attempt in range(_MAX_RETRIES):\n        wait = _acquire_budget(ptype)\n        if wait > 0:\n            time.sleep",
        "    if False:\n        stale = _serve_stale(key, ptype)\n        if stale is not None:\n            return stale\n        raise _cooldown_error(ptype)\n\n    last_exc: Exception | None = None\n    attempts_5xx = 0\n    for attempt in range(_MAX_RETRIES):\n        wait = _acquire_budget(ptype)\n        if wait > 0:\n            time.sleep",
    ),
    (
        "Retry-After ignored for HL",
        T_HL, "modules/hl_client.py",
        "        if asked is None or asked <= 0:\n            penalty =",
        "        if True:\n            penalty =",
    ),
    (
        "HL penalty stops escalating",
        T_HL, "modules/hl_client.py",
        "penalty = min(_COOLDOWN_BASE_SEC * (2 ** max(0, strikes - 1)), _COOLDOWN_MAX_SEC)",
        "penalty = _COOLDOWN_BASE_SEC",
    ),
    (
        "success no longer clears the host cooldown",
        T_HL, "modules/hl_client.py",
        "                _note_host_ok()\n                _TYPE_COOLDOWN.pop(ptype, None)\n                if not fut.done():",
        "                _TYPE_COOLDOWN.pop(ptype, None)\n                if not fut.done():",
    ),
    (
        "5xx storm never cools the type",
        T_HL, "modules/hl_client.py",
        "                    if attempts_5xx >= _MAX_RETRIES_5XX:\n                        _note_type_5xx(ptype)\n                        break\n",
        "                    if attempts_5xx >= _MAX_RETRIES_5XX:\n                        break\n",
    ),
    (
        "5xx retried without bound",
        T_HL, "modules/hl_client.py",
        "_MAX_RETRIES_5XX = int(os.getenv(\"HL_CLIENT_MAX_RETRIES_5XX\", \"2\") or 2)",
        "_MAX_RETRIES_5XX = 99",
    ),
    (
        "candleSnapshot weight treated as light (2)",
        T_HL, "modules/hl_client.py",
        "    if payload_type in _WEIGHT_LIGHT:\n        return 2\n    return int(_WEIGHT_HEAVY.get(payload_type, 20))",
        "    return 2",
    ),
    (
        "weight budget never blocks",
        T_HL, "modules/hl_client.py",
        "            return (weight - self.tokens) / self.rate\n",
        "            return 0.0\n",
    ),
    (
        "budget wait not applied before the request",
        T_HL, "modules/hl_client.py",
        "            wait = _acquire_budget(ptype)\n            if wait > 0:\n                await asyncio.sleep(wait + random.uniform(0, 0.05))\n",
        "            wait = _acquire_budget(ptype)\n            if False:\n                await asyncio.sleep(wait + random.uniform(0, 0.05))\n",
    ),
    (
        "candle cache never hits (every request refetches)",
        T_HL, "modules/hl_client.py",
        "    if ent is not None and (now - ent[0]) <= ttl and len(ent[1]) >= bars:\n        return list(ent[1][-bars:])\n",
        "",
    ),
    (
        "candle cache ignores TTL (stale series forever)",
        T_HL, "modules/hl_client.py",
        "    if ent is not None and (now - ent[0]) <= ttl and len(ent[1]) >= bars:",
        "    if ent is not None and len(ent[1]) >= bars:",
    ),
    (
        "longer request no longer covers the cached length",
        T_HL, "modules/hl_client.py",
        "    want = max(bars, len(ent[1]) if ent is not None else 0)\n",
        "    want = bars\n",
    ),
    (
        "candle endTime back to now_ms (payload cache never hits)",
        T_HL, "modules/hl_client.py",
        "    end_ms = (int(now * 1000) // 60_000) * 60_000 + 60_000\n",
        "    end_ms = int(now * 1000) + 1\n",
    ),
    (
        "unexpected candle shape returned as empty list",
        T_HL, "modules/hl_client.py",
        "    if not isinstance(data, list):\n        raise ValueError(f\"hl_client.get_candles: shape inesperado para {coin} {interval}\")\n",
        "    if not isinstance(data, list):\n        data = []\n",
    ),
    (
        "screener keeps fetching during a cooldown",
        T_HL, "modules/universal_screener.py",
        "            if _hl_cooling_down():\n                _COOLDOWN_SKIPS[\"n\"] += 1\n                break\n",
        "",
    ),
    (
        "unlock monitor bypasses the shared candle cache",
        T_HL, "modules/unlock_monitor.py",
        "            from modules.hl_client import get_candles\n            candles = await get_candles(coin, \"4h\", bars + 4)\n",
        "            candles = await _hl_post({\"type\": \"candleSnapshot\", \"req\": {\"coin\": coin, \"interval\": \"4h\", \"startTime\": 0, \"endTime\": int(time.time() * 1000)}})\n",
    ),
]


def _run(test: str) -> tuple[bool, str]:
    env = dict(os.environ)
    env.setdefault("TELEGRAM_BOT_TOKEN", "x")
    env.setdefault("TZ", "UTC")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", test, "-q", "-o", "asyncio_mode=auto",
         "-p", "no:cacheprovider", "-p", "no:randomly", "-x"],
        cwd=REPO, capture_output=True, text=True, env=env,
    )
    return proc.returncode == 0, (proc.stdout or "")[-400:]


def main() -> int:
    os.chdir(REPO)
    for t in (T_DEUDA, T_HL):
        green, tail = _run(t)
        if not green:
            print(f"BASELINE IS RED for {t} — fix the suite before mutating:\n" + tail)
            return 2
    print("baseline: GREEN\n")

    survivors: list[str] = []
    for label, test, rel, needle, repl in MUTATIONS:
        path = os.path.join(REPO, rel)
        with open(path, "r", encoding="utf-8") as fh:
            original = fh.read()
        if original.count(needle) != 1:
            survivors.append(f"{label}\n      (needle count {original.count(needle)} in {rel} — "
                             f"the mutation could not be applied; update this script)")
            print(f"  SKIP  {rel}: needle not unique — {label}")
            continue
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original.replace(needle, repl, 1))
            still_green, _ = _run(test)
        finally:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original)
        if still_green:
            survivors.append(label)
            print(f"  SURVIVED  {rel}: {label}")
        else:
            print(f"  killed    {rel}: {label}")

    print()
    for t in (T_DEUDA, T_HL):
        green_again, tail = _run(t)
        if not green_again:
            print("SOURCE NOT RESTORED CLEANLY — check `git diff`:\n" + tail)
            return 2

    if survivors:
        print(f"{len(survivors)} MUTATION(S) SURVIVED — the suite does not pin these contracts:")
        for s in survivors:
            print(f"  - {s}")
        return 1

    print(f"all {len(MUTATIONS)} mutations killed — suite restored GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
