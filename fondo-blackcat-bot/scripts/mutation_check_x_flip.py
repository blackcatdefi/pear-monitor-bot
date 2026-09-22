#!/usr/bin/env python3
"""R-X-FLIP mutation check — proves tests/test_x_flip.py actually bites.

A green suite proves nothing on its own: a test that asserts a tautology is
green forever. The 27-day X blackout survived a 1500-test suite precisely
because nothing in it ever asserted "depletion flips the backend".

So every contract pinned by tests/test_x_flip.py is broken here ON PURPOSE,
one at a time, in the real source file. The run must go RED for each. Any
mutation that leaves the suite green is a hole in the net and this script
exits non-zero naming it.

Usage:  python3 scripts/mutation_check_x_flip.py
        (from fondo-blackcat-bot/; never run it in a dirty tree — it edits
        source files in place and restores them in a finally block.)
"""
from __future__ import annotations

import os
import subprocess
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
TEST = "tests/test_x_flip.py"

# (label, relative path, needle, replacement)
MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "latch precedence removed from backend_selected() "
        "(THE original bug: a stale env var re-pins a dead backend)",
        "modules/x_provider.py",
        '    if official_depleted():\n        return "twitterapi_io"\n',
        "",
    ),
    (
        "402 no longer counts as depletion (DEPLETION_STATUSES emptied)",
        "modules/x_provider.py",
        "DEPLETION_STATUSES = (402,)",
        "DEPLETION_STATUSES = ()",
    ),
    (
        "the latch is not persisted (mark_official_depleted becomes a no-op)",
        "modules/x_provider.py",
        "def mark_official_depleted(status: int, reason: str = \"\") -> None:",
        "def mark_official_depleted(status: int, reason: str = \"\") -> None:\n    return None",
    ),
    (
        "the official client stops latching the 402 it just read off the wire",
        "modules/x_intel.py",
        "if _xp.is_depletion_response(resp.status_code, body_json):",
        "if False and _xp.is_depletion_response(resp.status_code, body_json):",
    ),
    (
        "same-run retry deleted (the flip would only help the NEXT /reporte)",
        "modules/x_intel.py",
        "if tweets is None and x_provider.provider_active():",
        "if False and tweets is None and x_provider.provider_active():",
    ),
    (
        "console top-up advice re-inserted into the 402 diagnostic",
        "modules/x_intel.py",
        "\"API: switching the timeline to twitterapi.io. Refunding it is not wanted.\"",
        "\"API: switching the timeline to twitterapi.io. Top-up at console.x.com.\"",
    ),
    (
        "a stale timeline is never flagged degraded",
        "modules/x_staleness.py",
        '"degraded": empty or h > window,',
        '"degraded": False,',
    ),
    (
        "/timeline goes back to the hardcoded '(last 48h)' header",
        "templates/timeline.py",
        "f\"{x_staleness.header_line(x_intel, now)}\\n\"",
        "f\"\\U0001f426 X Timeline (last 48h) — {now}\\n\"",
    ),
    (
        "/reporte section title hardcoded to 48H regardless of age "
        "(the literal shape of the incident)",
        "bot.py",
        "    if not st.get(\"degraded\"):",
        "    if True:",
    ),
    (
        "/reporte stops consulting the staleness verdict at all",
        "bot.py",
        "        title = x_section_title(x_intel, hours=48)",
        "        title = \"\\U0001f4e1 X TIMELINE \\u2014 48H\"",
    ),
    (
        "the module-level x_store import is removed again "
        "(the NameError that silently demoted /reporte to the legacy mirror)",
        "modules/x_intel.py",
        "from modules import x_store\n\nfrom modules.intel_memory import (",
        "from modules.intel_memory import (",
    ),
]


def _run_suite() -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q", "-o", "asyncio_mode=auto"],
        cwd=REPO, capture_output=True, text=True,
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
