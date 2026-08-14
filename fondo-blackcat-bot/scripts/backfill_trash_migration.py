#!/usr/bin/env python3
"""R-MAIL-CONTENT-TRASHFIX 1.4 — one-shot backfill: archived → Trash.

The Phase C bot archived (instead of trashing) every processed email because
COPY to the hardcoded "[Gmail]/Trash" failed silently on a Spanish-localized
account ("[Gmail]/Papelera"). This migrates EXACTLY that population to Trash:

Candidate = message in the \\All folder ([Gmail]/Todos) that is
  * INTERNALDATE >= SINCE (default 11-Aug-2026 — Phase C deploy date),
  * \\Seen (the bot marks every processed email read),
  * NOT labeled \\Inbox (i.e. archived), and
  * NOT \\Sent / \\Draft / \\Trash / \\Spam.

Usage:
  GMAIL_EMAIL=... GMAIL_APP_PASSWORD=... python3 backfill_trash_migration.py            # DRY-RUN
  GMAIL_EMAIL=... GMAIL_APP_PASSWORD=... python3 backfill_trash_migration.py --execute  # migrate

Dry-run logs the full candidate list (From / Subject / Date) and count.
Execute copies each candidate to the resolved Trash folder, checks the COPY
status, and verifies each X-GM-MSGID is present in Trash afterwards.
Nothing outside the candidate set is ever touched.
"""
from __future__ import annotations

import email
import imaplib
import os
import re
import sys
from email.header import decode_header

SINCE = os.getenv("BACKFILL_SINCE", "11-Aug-2026")
EXCLUDE_LABELS = (b"\\Inbox", b"\\Sent", b"\\Draft", b"\\Trash", b"\\Spam", b"\\Junk")


def _dec(raw):
    if not raw:
        return ""
    out = []
    for data, cs in decode_header(raw):
        out.append(data.decode(cs or "utf-8", "replace") if isinstance(data, bytes) else data)
    return " ".join(out)


def main() -> int:
    execute = "--execute" in sys.argv
    user, pwd = os.environ["GMAIL_EMAIL"], os.environ["GMAIL_APP_PASSWORD"]
    im = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    im.login(user, pwd)

    # Resolve special-use folders from LIST attributes (localization-proof).
    typ, rows = im.list()
    all_folder = trash_folder = None
    for row in rows or []:
        m = re.search(rb'\)\s+"[^"]*"\s+"?([^"]+?)"?\s*$', row)
        if not m:
            continue
        name = m.group(1).decode()
        if b"\\All" in row:
            all_folder = name
        elif b"\\Trash" in row:
            trash_folder = name
    if not all_folder or not trash_folder:
        print(f"FATAL: cannot resolve folders (all={all_folder} trash={trash_folder})")
        return 1
    q = lambda n: f'"{n}"' if " " in n else n
    print(f"All-folder={all_folder}  Trash={trash_folder}  since={SINCE}  "
          f"mode={'EXECUTE' if execute else 'DRY-RUN'}")

    im.select(q(all_folder))
    typ, data = im.search(None, f"(SINCE {SINCE} SEEN)")
    seqs = data[0].split() if data and data[0] else []
    candidates = []  # (seq, gm_msgid, from, subject, date)
    for seq in seqs:
        typ, fd = im.fetch(seq, "(X-GM-LABELS X-GM-MSGID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        if typ != "OK" or not fd:
            continue
        meta = b""
        hdr = b""
        for item in fd:
            if isinstance(item, tuple):
                meta += item[0] or b""
                hdr += item[1] or b""
            elif isinstance(item, bytes):
                meta += item
        if any(lbl in meta for lbl in EXCLUDE_LABELS):
            continue
        mm = re.search(rb"X-GM-MSGID (\d+)", meta)
        gm = mm.group(1).decode() if mm else None
        msg = email.message_from_bytes(hdr)
        candidates.append((seq, gm, _dec(msg.get("From")), _dec(msg.get("Subject")),
                           msg.get("Date", "")))

    print(f"\nCANDIDATES: {len(candidates)}")
    for _, gm, frm, sub, dt in candidates:
        print(f"  · [{gm}] {frm} — {sub} ({dt})")

    if not execute:
        im.logout()
        print("\nDRY-RUN only — nothing touched. Re-run with --execute to migrate.")
        return 0

    moved, failed = 0, []
    for seq, gm, frm, sub, _ in candidates:
        typ, resp = im.copy(seq, q(trash_folder))
        if typ != "OK":
            failed.append(f"{sub}: COPY {typ} {resp!r}")
            continue
        im.store(seq, "+FLAGS", "\\Deleted")
        moved += 1
    im.expunge()

    # Verify in Trash by X-GM-MSGID.
    verified = 0
    im.select(q(trash_folder), readonly=True)
    for seq, gm, frm, sub, _ in candidates:
        if not gm:
            continue
        typ, hits = im.search(None, "X-GM-MSGID", gm)
        if typ == "OK" and hits and hits[0] and hits[0].split():
            verified += 1
        else:
            failed.append(f"{sub}: NOT verified in Trash")
    im.logout()
    print(f"\nRESULT: moved={moved} verified={verified} failed={len(failed)}")
    for f in failed:
        print(f"  ⚠️ {f}")
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
