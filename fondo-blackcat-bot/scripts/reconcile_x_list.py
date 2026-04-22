"""Reconcile the private X list with BCD's following set.

Goal (Round 12 post-mortem):
    The "Fondo Black Cat Intel" private list ballooned to ~600 members after
    the Round 9 browser-side bulk-add was run multiple times. BCD only
    follows ~211 accounts actively. We want the list = BCD's following set
    so the X list endpoint returns relevant tweets and the projected cost
    stays close to the theoretical floor ($0.25/1K tweets × expected volume).

What this script does:
    1. Pages /2/users/:id/following to build the canonical follow set.
    2. Pages /2/lists/:list_id/members to build the current list set.
    3. Diffs: accounts to ADD to list (follows ∉ list) and accounts to
       REMOVE from list (list ∉ follows).
    4. Writes the two diff lists to /tmp/x_list_reconcile_{add,remove}.json
       and prints a summary.
    5. If invoked with `--apply`, issues POST /2/lists/:list_id/members and
       DELETE /2/lists/:list_id/members/:user_id calls with small sleeps.
       Requires OAuth 1.0a user-context credentials (Bearer alone cannot
       mutate list membership). Env vars:
           X_OAUTH_CONSUMER_KEY
           X_OAUTH_CONSUMER_SECRET
           X_OAUTH_ACCESS_TOKEN
           X_OAUTH_ACCESS_TOKEN_SECRET

IMPORTANT — do NOT run until spend cap resets (2026-05-16) OR user
aumenta el spend cap manualmente en developer.x.com. Reading the follow
set + list members is cheap (user-timeline endpoints are Owned Reads),
but each response still counts against the cap.

Usage:
    # Dry run (just computes the diffs):
    python -m scripts.reconcile_x_list

    # Apply the diffs via OAuth 1.0a user context (requires extra env vars):
    python -m scripts.reconcile_x_list --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Iterable

import httpx
from dotenv import load_dotenv

load_dotenv()

BEARER = os.getenv("X_API_BEARER_TOKEN", "").strip()
LIST_ID = os.getenv("X_LIST_ID", "").strip()
# BCD's X user id (confirmed 2026-04-21 during list-populate flow)
BCD_USER_ID = os.getenv("X_OWNER_USER_ID", "1397263268691992576").strip()

OUT_ADD = "/tmp/x_list_reconcile_add.json"
OUT_REMOVE = "/tmp/x_list_reconcile_remove.json"


def _die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _page_users(url: str, params: dict, headers: dict, label: str) -> list[dict]:
    """Paginate a users-returning endpoint. Returns [{id, username, name}...]."""
    out: list[dict] = []
    next_token: str | None = None
    with httpx.Client(timeout=30) as c:
        for page in range(50):  # hard safety cap
            p = dict(params)
            if next_token:
                p["pagination_token"] = next_token
            r = c.get(url, params=p, headers=headers)
            if r.status_code != 200:
                _die(f"{label} fetch HTTP {r.status_code} — body: {r.text[:400]}")
            payload = r.json()
            out.extend(payload.get("data") or [])
            meta = payload.get("meta") or {}
            next_token = meta.get("next_token")
            if not next_token:
                break
            time.sleep(1)  # polite
    return out


def fetch_following() -> list[dict]:
    if not BEARER:
        _die("X_API_BEARER_TOKEN not set")
    url = f"https://api.x.com/2/users/{BCD_USER_ID}/following"
    headers = {"Authorization": f"Bearer {BEARER}"}
    params = {"max_results": 1000, "user.fields": "username,name,verified"}
    print(f"Fetching following for user {BCD_USER_ID}...")
    users = _page_users(url, params, headers, "following")
    print(f"  → {len(users)} follows")
    return users


def fetch_list_members() -> list[dict]:
    if not LIST_ID:
        _die("X_LIST_ID not set")
    url = f"https://api.x.com/2/lists/{LIST_ID}/members"
    headers = {"Authorization": f"Bearer {BEARER}"}
    params = {"max_results": 100, "user.fields": "username,name,verified"}
    print(f"Fetching members of list {LIST_ID}...")
    users = _page_users(url, params, headers, "list members")
    print(f"  → {len(users)} current list members")
    return users


def compute_diff(follows: Iterable[dict], members: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    follow_ids = {u["id"]: u for u in follows}
    member_ids = {u["id"]: u for u in members}
    to_add = [follow_ids[uid] for uid in follow_ids if uid not in member_ids]
    to_remove = [member_ids[uid] for uid in member_ids if uid not in follow_ids]
    return to_add, to_remove


def apply_diff(to_add: list[dict], to_remove: list[dict]) -> None:
    """Mutate list membership via OAuth 1.0a user context.

    Bearer tokens cannot add/remove members — X requires user-context auth.
    """
    try:
        from requests_oauthlib import OAuth1Session  # type: ignore
    except ImportError:
        _die("--apply requires `pip install requests-oauthlib`")

    ck = os.getenv("X_OAUTH_CONSUMER_KEY", "")
    cs = os.getenv("X_OAUTH_CONSUMER_SECRET", "")
    at = os.getenv("X_OAUTH_ACCESS_TOKEN", "")
    ats = os.getenv("X_OAUTH_ACCESS_TOKEN_SECRET", "")
    if not all([ck, cs, at, ats]):
        _die("Missing X_OAUTH_* env vars for --apply (need consumer + access token pair)")

    session = OAuth1Session(
        client_key=ck,
        client_secret=cs,
        resource_owner_key=at,
        resource_owner_secret=ats,
    )

    print(f"Applying: +{len(to_add)} adds, -{len(to_remove)} removes")
    for i, u in enumerate(to_add, 1):
        r = session.post(
            f"https://api.x.com/2/lists/{LIST_ID}/members",
            json={"user_id": u["id"]},
        )
        print(f"  [{i}/{len(to_add)}] ADD @{u.get('username')} → HTTP {r.status_code}")
        time.sleep(0.5)
    for i, u in enumerate(to_remove, 1):
        r = session.delete(
            f"https://api.x.com/2/lists/{LIST_ID}/members/{u['id']}"
        )
        print(f"  [{i}/{len(to_remove)}] REMOVE @{u.get('username')} → HTTP {r.status_code}")
        time.sleep(0.5)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Mutate list membership (requires OAuth 1.0a env)")
    args = ap.parse_args()

    follows = fetch_following()
    members = fetch_list_members()
    to_add, to_remove = compute_diff(follows, members)

    with open(OUT_ADD, "w") as f:
        json.dump(to_add, f, indent=2)
    with open(OUT_REMOVE, "w") as f:
        json.dump(to_remove, f, indent=2)

    print("")
    print("═" * 60)
    print(f"Summary — current list size: {len(members)}")
    print(f"  Follows (BCD):         {len(follows)}")
    print(f"  To ADD (follows not in list):     {len(to_add)}")
    print(f"  To REMOVE (list not in follows):  {len(to_remove)}")
    print(f"  Final list size after apply:      {len(follows)}")
    print(f"  Diff files written: {OUT_ADD} / {OUT_REMOVE}")
    print("═" * 60)

    if args.apply:
        apply_diff(to_add, to_remove)
        print("Apply complete. Verify via /2/lists/{id}/members count.")
    else:
        print("\nDry run complete. Re-run with --apply to mutate the list.")
        print("NOTE: --apply requires OAuth 1.0a user-context credentials.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
