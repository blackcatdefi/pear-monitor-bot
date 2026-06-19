"""Reconcile the private X list against the CANONICAL handle set.

R-XLIST-CANONICAL (2026-06-19):
    The X list "Fondo Black Cat Intel" (X_LIST_ID) is the SINGLE SOURCE OF
    TRUTH for the /reporte "X TIMELINE". BCD maintains the canonical set in
    ``x_accounts.txt`` (185 handles) and in the X List itself. This script
    makes the X List match ``x_accounts.txt`` EXACTLY:

      1. Loads the canonical handles from x_accounts.txt.
      2. Resolves each handle → numeric user id via GET /2/users/by
         (batched 100/req, app-only Bearer). Handles that do not resolve
         (invalid / suspended / renamed) are KEPT in the canonical file and
         REPORTED as unresolved — never silently dropped.
      3. Pages GET /2/lists/:id/members to read the current list set.
      4. Diffs:
           - to_add    = canonical (resolved) ∉ list   → POST members
           - extraneous = list ∉ canonical             → REPORTED, not
             deleted automatically (use --prune to remove them).
      5. With ``--apply`` issues POST /2/lists/:id/members via OAuth 1.0a
         user context (Bearer alone cannot mutate membership), THROTTLED with
         a small sleep and RETRYING on HTTP 429 with exponential backoff.
         Reports how many of the canonical set were added.

OAuth 1.0a env vars required for --apply (set in Railway):
    X_OAUTH_CONSUMER_KEY
    X_OAUTH_CONSUMER_SECRET
    X_OAUTH_ACCESS_TOKEN
    X_OAUTH_ACCESS_TOKEN_SECRET

Reading (resolve + list members) only needs X_API_BEARER_TOKEN.

Usage:
    # Dry run — resolve + diff, write JSON, print summary (no mutation):
    python -m scripts.reconcile_x_list

    # Apply additions via OAuth 1.0a (throttled, 429-retry):
    python -m scripts.reconcile_x_list --apply

    # Also delete list members not in the canonical set (flagged first):
    python -m scripts.reconcile_x_list --apply --prune
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Iterable

import httpx

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001
    pass

BEARER = os.getenv("X_API_BEARER_TOKEN", os.getenv("X_BEARER_TOKEN", "")).strip()
LIST_ID = os.getenv("X_LIST_ID", "").strip()

_HERE = os.path.dirname(os.path.abspath(__file__))
CANONICAL_FILE = os.getenv(
    "X_ACCOUNTS_FILE", os.path.join(os.path.dirname(_HERE), "x_accounts.txt")
)

OUT_ADD = "/tmp/x_list_reconcile_add.json"
OUT_REPORT = "/tmp/x_list_reconcile_report.json"

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) BCDbot/1.0"


def _die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# ─── Canonical handle loader (mirrors modules.x_intel._load_canonical_handles) ──
def load_canonical_handles(path: str = CANONICAL_FILE) -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        _die(f"cannot read canonical file {path}: {e}")
    out: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for tok in line.split(","):
            h = tok.strip().lstrip("@")
            if not h:
                continue
            k = h.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(h)
    return out


def _chunk(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def resolve_handles(handles: list[str]) -> tuple[dict[str, dict], list[str]]:
    """Resolve usernames → {lower_username: {id, username, name}} via Bearer.

    Returns (resolved, unresolved_handles). Unresolved = invalid / suspended /
    renamed handles — KEPT (never dropped), reported to the caller.
    """
    if not BEARER:
        _die("X_API_BEARER_TOKEN not set (needed to resolve usernames)")
    resolved: dict[str, dict] = {}
    unresolved: list[str] = []
    headers = {"Authorization": f"Bearer {BEARER}", "User-Agent": _UA}
    with httpx.Client(timeout=30) as c:
        for batch in _chunk(handles, 100):
            params = {
                "usernames": ",".join(batch),
                "user.fields": "username,name,verified,protected",
            }
            r = c.get("https://api.x.com/2/users/by", params=params, headers=headers)
            if r.status_code == 429:
                _die("resolve hit 429 — wait for rate-limit reset and retry")
            if r.status_code != 200:
                _die(f"resolve HTTP {r.status_code}: {r.text[:300]}")
            payload = r.json()
            for u in payload.get("data") or []:
                resolved[u["username"].lower()] = {
                    "id": u["id"],
                    "username": u["username"],
                    "name": u.get("name", ""),
                    "protected": u.get("protected", False),
                }
            time.sleep(1)  # polite
    for h in handles:
        if h.lower() not in resolved:
            unresolved.append(h)
    return resolved, unresolved


def _page_users(url: str, params: dict, headers: dict, label: str) -> list[dict]:
    out: list[dict] = []
    next_token: str | None = None
    with httpx.Client(timeout=30) as c:
        for _ in range(50):  # hard safety cap
            p = dict(params)
            if next_token:
                p["pagination_token"] = next_token
            r = c.get(url, params=p, headers=headers)
            if r.status_code != 200:
                _die(f"{label} fetch HTTP {r.status_code} — body: {r.text[:400]}")
            payload = r.json()
            out.extend(payload.get("data") or [])
            next_token = (payload.get("meta") or {}).get("next_token")
            if not next_token:
                break
            time.sleep(1)
    return out


def fetch_list_members() -> list[dict]:
    if not LIST_ID:
        _die("X_LIST_ID not set")
    url = f"https://api.x.com/2/lists/{LIST_ID}/members"
    headers = {"Authorization": f"Bearer {BEARER}", "User-Agent": _UA}
    params = {"max_results": 100, "user.fields": "username,name"}
    print(f"Fetching members of list {LIST_ID}...")
    users = _page_users(url, params, headers, "list members")
    print(f"  → {len(users)} current list members")
    return users


def compute_diff(
    resolved: dict[str, dict], members: Iterable[dict]
) -> tuple[list[dict], list[dict]]:
    """to_add = canonical resolved not already in list;
    extraneous = list members not in canonical resolved set."""
    member_by_id = {u["id"]: u for u in members}
    member_ids = set(member_by_id)
    canonical_ids = {u["id"] for u in resolved.values()}
    to_add = [u for u in resolved.values() if u["id"] not in member_ids]
    extraneous = [member_by_id[mid] for mid in member_ids if mid not in canonical_ids]
    return to_add, extraneous


def _oauth_session():
    try:
        from requests_oauthlib import OAuth1Session  # type: ignore
    except ImportError:
        _die("--apply requires `pip install requests-oauthlib`")
    ck = os.getenv("X_OAUTH_CONSUMER_KEY", "")
    cs = os.getenv("X_OAUTH_CONSUMER_SECRET", "")
    at = os.getenv("X_OAUTH_ACCESS_TOKEN", "")
    ats = os.getenv("X_OAUTH_ACCESS_TOKEN_SECRET", "")
    if not all([ck, cs, at, ats]):
        _die(
            "Missing X_OAUTH_* env vars for --apply. Need: X_OAUTH_CONSUMER_KEY, "
            "X_OAUTH_CONSUMER_SECRET, X_OAUTH_ACCESS_TOKEN, "
            "X_OAUTH_ACCESS_TOKEN_SECRET (set them in Railway)."
        )
    from requests_oauthlib import OAuth1Session  # type: ignore

    return OAuth1Session(
        client_key=ck, client_secret=cs,
        resource_owner_key=at, resource_owner_secret=ats,
    )


def apply_adds(
    to_add: list[dict],
    *,
    throttle_s: float = 1.5,
    max_retries: int = 5,
) -> dict:
    """POST each member with throttling + exponential backoff on 429."""
    session = _oauth_session()
    url = f"https://api.x.com/2/lists/{LIST_ID}/members"
    added = 0
    failed: list[dict] = []
    print(f"Applying +{len(to_add)} additions (throttle {throttle_s}s, 429-retry)...")
    for i, u in enumerate(to_add, 1):
        backoff = throttle_s
        ok = False
        recorded = False  # whether this handle's failure was already appended
        for attempt in range(1, max_retries + 1):
            r = session.post(url, json={"user_id": u["id"]})
            if r.status_code == 429:
                wait = max(backoff, 5) * attempt
                print(f"  [{i}/{len(to_add)}] @{u['username']} 429 — backoff {wait:.0f}s "
                      f"(attempt {attempt}/{max_retries})")
                time.sleep(wait)
                backoff *= 2
                continue
            if r.status_code in (200, 201):
                added += 1
                ok = True
                print(f"  [{i}/{len(to_add)}] ADD @{u['username']} → {r.status_code}")
                break
            # Non-retryable (e.g. duplicate, privacy opt-out)
            print(f"  [{i}/{len(to_add)}] @{u['username']} → HTTP {r.status_code}: {r.text[:120]}")
            failed.append({"username": u["username"], "id": u["id"],
                           "status": r.status_code, "body": r.text[:200]})
            recorded = True
            break
        if not ok and not recorded:
            # Loop exhausted all retries on repeated 429s without ever recording.
            print(f"  [{i}/{len(to_add)}] @{u['username']} → exhausted {max_retries} retries (429)")
            failed.append({"username": u["username"], "id": u["id"], "status": 429,
                           "body": "exhausted retries"})
        time.sleep(throttle_s)
    return {"added": added, "failed": failed}


def apply_prune(extraneous: list[dict], *, throttle_s: float = 1.5) -> dict:
    session = _oauth_session()
    removed = 0
    failed = []
    print(f"Pruning -{len(extraneous)} extraneous members...")
    for i, u in enumerate(extraneous, 1):
        r = session.delete(f"https://api.x.com/2/lists/{LIST_ID}/members/{u['id']}")
        if r.status_code in (200, 204):
            removed += 1
            print(f"  [{i}/{len(extraneous)}] REMOVE @{u.get('username')} → {r.status_code}")
        else:
            print(f"  [{i}/{len(extraneous)}] @{u.get('username')} → HTTP {r.status_code}")
            failed.append({"username": u.get("username"), "id": u["id"], "status": r.status_code})
        time.sleep(throttle_s)
    return {"removed": removed, "failed": failed}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Add canonical handles to the list via OAuth 1.0a")
    ap.add_argument("--prune", action="store_true",
                    help="Also delete list members not in the canonical set")
    ap.add_argument("--throttle", type=float, default=1.5,
                    help="Seconds between write calls (default 1.5)")
    args = ap.parse_args()

    handles = load_canonical_handles()
    print(f"Canonical handles (x_accounts.txt): {len(handles)}")
    resolved, unresolved = resolve_handles(handles)
    print(f"  Resolved: {len(resolved)}   Unresolved (kept, reported): {len(unresolved)}")
    if unresolved:
        print("  UNRESOLVED (invalid/suspended/renamed — fix typos in x_accounts.txt):")
        for h in unresolved:
            print(f"    - {h}")

    members = fetch_list_members()
    to_add, extraneous = compute_diff(resolved, members)

    report = {
        "canonical_total": len(handles),
        "resolved": len(resolved),
        "unresolved": unresolved,
        "current_list_members": len(members),
        "to_add": [u["username"] for u in to_add],
        "extraneous_in_list": [u.get("username") for u in extraneous],
    }
    with open(OUT_ADD, "w") as f:
        json.dump(to_add, f, indent=2)
    with open(OUT_REPORT, "w") as f:
        json.dump(report, f, indent=2)

    print("")
    print("=" * 60)
    print(f"Canonical resolved : {len(resolved)} / {len(handles)}")
    print(f"Current list size  : {len(members)}")
    print(f"To ADD             : {len(to_add)}")
    print(f"Extraneous in list : {len(extraneous)} "
          f"{'(NOT deleted without --prune)' if extraneous else ''}")
    print(f"Expected final size: {len(resolved)}")
    print(f"Reports: {OUT_ADD} / {OUT_REPORT}")
    print("=" * 60)

    if extraneous and not args.prune:
        print("\n⚠️ Pre-existing members NOT in the canonical set were found and "
              "are FLAGGED above. Re-run with --prune to remove them.")

    if args.apply:
        res = apply_adds(to_add, throttle_s=args.throttle)
        print(f"\nAdded {res['added']}/{len(to_add)} (failures: {len(res['failed'])})")
        if args.prune and extraneous:
            pres = apply_prune(extraneous, throttle_s=args.throttle)
            print(f"Pruned {pres['removed']}/{len(extraneous)}")
        print("Verify via GET /2/lists/{id}/members count.")
    else:
        print("\nDry run complete. Re-run with --apply to mutate the list "
              "(requires X_OAUTH_* user-context credentials).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
