"""R-MAIL-CONTENT-TRASHFIX Part 1 — VERIFIED Gmail trash acceptance tests.

Locked-in contracts (supersede the Phase C optimistic-count tests):
  1. The Trash folder is resolved at runtime from LIST \\Trash special-use
     attrs (Spanish Gmail = "[Gmail]/Papelera"; hardcoded "[Gmail]/Trash"
     returned ('NO', [TRYCREATE...]) which imaplib does NOT raise → silent
     archive). COPY return status is checked; NO → failure surfaced.
  2. After the move, each message is verified present in Trash via
     X-GM-MSGID search; ``post_action_count`` = VERIFIED count only, and
     failures surface in ``trash_failed`` / ``post_action_failures``.
  3. The post-action touches EXACTLY and ONLY the successfully processed
     uids — a uid whose fetch fails is never trashed/archived.
  4. archive (and trash dry-run) = \\Deleted + EXPUNGE on INBOX only — no
     COPY to a (localized, possibly missing) All Mail folder.
"""
from __future__ import annotations

from typing import Any

from modules import gmail_intel as gi
from modules.intel_render import format_gmail_intel_block

TRASH_ES = "[Gmail]/Papelera"

_LIST_ROWS_ES = [
    b'(\\HasNoChildren) "/" "INBOX"',
    b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
    b'(\\Drafts \\HasNoChildren) "/" "[Gmail]/Borradores"',
    b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Papelera"',
    b'(\\All \\HasNoChildren) "/" "[Gmail]/Todos"',
]


def _raw_email(subject: str) -> bytes:
    return (
        f"From: Sender <s@example.com>\r\nSubject: {subject}\r\n"
        f"Date: Mon, 10 Aug 2026 10:00:00 +0000\r\n"
        f"Content-Type: text/plain\r\n\r\nbody {subject}\r\n"
    ).encode()


class _FakeIMAP:
    """imaplib stand-in with localized folders + X-GM-MSGID semantics."""

    def __init__(
        self,
        uids: list[bytes],
        fail_fetch: set[bytes] = frozenset(),
        copy_fails: set[bytes] = frozenset(),
        verify_missing: set[bytes] = frozenset(),
        list_rows: list[bytes] | None = None,
    ):
        self._uids = uids
        self._fail_fetch = set(fail_fetch)
        self._copy_fails = set(copy_fails)
        self._verify_missing = set(verify_missing)
        self._list_rows = _LIST_ROWS_ES if list_rows is None else list_rows
        self._selected = "INBOX"
        self._trash_msgids: set[str] = set()
        self.copies: list[tuple[bytes, str]] = []
        self.stores: list[tuple[bytes, str]] = []
        self.expunges = 0
        self.logged_out = False

    def login(self, *a):
        return "OK", []

    def select(self, mailbox="INBOX", readonly=False):
        name = mailbox.strip('"')
        if name not in ("INBOX", TRASH_ES, "[Gmail]/Todos"):
            return "NO", [b"[TRYCREATE] no folder"]
        self._selected = name
        return "OK", [b"1"]

    def list(self):
        return "OK", self._list_rows

    def status(self, mailbox, what):
        if mailbox.strip('"') == TRASH_ES:
            return "OK", [b"..."]
        return "NO", [b"no folder"]

    def search(self, charset, *criteria):
        if self._selected == TRASH_ES and criteria and criteria[0] == "X-GM-MSGID":
            mid = criteria[1]
            return ("OK", [b"1"]) if mid in self._trash_msgids else ("OK", [b""])
        return "OK", [b" ".join(self._uids)]

    def fetch(self, uid, spec):
        if "X-GM-MSGID" in spec:
            return "OK", [b"1 (X-GM-MSGID 90" + uid + b")"]
        if uid in self._fail_fetch:
            raise RuntimeError("fetch boom")
        return "OK", [(b"1 (RFC822)", _raw_email(f"mail-{uid.decode()}"))]

    def copy(self, uid, mailbox):
        self.copies.append((uid, mailbox))
        if uid in self._copy_fails or mailbox.strip('"') != TRASH_ES:
            # imaplib returns NO, it does NOT raise — the silent killer.
            return "NO", [b"[TRYCREATE] No folder " + mailbox.encode()]
        if uid not in self._verify_missing:
            self._trash_msgids.add((b"90" + uid).decode())
        return "OK", []

    def store(self, uid, op, flags):
        self.stores.append((uid, flags))
        return "OK", []

    def expunge(self):
        self.expunges += 1
        return "OK", []

    def logout(self):
        self.logged_out = True
        return "OK", []


def _run_scan(monkeypatch, fake: _FakeIMAP, *, action="trash", dry=False):
    monkeypatch.setattr(gi, "GMAIL_EMAIL", "x@gmail.com")
    monkeypatch.setattr(gi, "GMAIL_APP_PASSWORD", "app-pass")
    monkeypatch.setattr(gi, "EMAIL_POST_ACTION", action)
    monkeypatch.setattr(gi, "EMAIL_TRASH_DRY_RUN", dry)
    monkeypatch.setattr(gi.imaplib, "IMAP4_SSL", lambda *a, **k: fake)
    return gi._scan_inbox_sync()


# ── 1. trash live: localized folder resolved + verified move ────────────────

def test_trash_resolves_localized_folder_and_verifies(monkeypatch):
    fake = _FakeIMAP([b"1", b"2", b"3"], fail_fetch={b"2"})
    res = _run_scan(monkeypatch, fake, action="trash")
    assert res["status"] == "ok" and res["count"] == 2
    assert res["post_action"] == "trash"
    assert res["post_action_dry_run"] is False
    # ONLY processed uids touched, ALL copies to the REAL (Spanish) folder.
    assert [u for u, _ in fake.copies] == [b"1", b"3"]  # uid 2 NEVER trashed
    assert all(mb.strip('"') == TRASH_ES for _, mb in fake.copies)
    # Trash = COPY-to-Trash endpoint, NOT bare remove-from-INBOX: \Deleted
    # only AFTER a successful COPY.
    deleted = [u for u, fl in fake.stores if fl == "\\Deleted"]
    assert deleted == [b"1", b"3"]
    # Verified count — verification ran against the Trash folder.
    assert res["post_action_count"] == 2
    assert res["trash_failed"] == 0 and res["post_action_failures"] == []
    assert fake.expunges >= 1 and fake.logged_out


def test_trash_copy_no_surfaces_failure_not_masked(monkeypatch):
    """COPY 'NO' (the TRYCREATE bug) must surface — never silent archive."""
    fake = _FakeIMAP([b"1", b"2"], copy_fails={b"2"})
    res = _run_scan(monkeypatch, fake, action="trash")
    assert res["post_action_count"] == 1          # only the verified one
    assert res["trash_failed"] == 1
    assert any("COPY" in f for f in res["post_action_failures"])
    # Failed uid must NOT get \Deleted (would archive it silently).
    deleted = [u for u, fl in fake.stores if fl == "\\Deleted"]
    assert b"2" not in deleted and b"1" in deleted


def test_trash_verification_miss_counts_as_failure(monkeypatch):
    """COPY OK but message not found in Trash → unverified, surfaced."""
    fake = _FakeIMAP([b"1", b"2"], verify_missing={b"2"})
    res = _run_scan(monkeypatch, fake, action="trash")
    assert res["post_action_count"] == 1
    assert res["trash_failed"] == 1
    assert any("NOT in trash" in f for f in res["post_action_failures"])


def test_trash_folder_unresolvable_hard_stop(monkeypatch):
    """No \\Trash folder resolvable → ZERO deletions, everything surfaced."""
    fake = _FakeIMAP([b"1"], list_rows=[b'(\\HasNoChildren) "/" "INBOX"'])
    fake.status = lambda *a, **k: ("NO", [b"no"])  # kill fallback probe too
    res = _run_scan(monkeypatch, fake, action="trash")
    assert res["post_action_count"] == 0
    assert res["trash_failed"] >= 1
    assert fake.copies == []
    assert not any(fl == "\\Deleted" for _, fl in fake.stores)


# ── 2. dry-run + legacy archive: NO copy, \Deleted+expunge only ─────────────

def test_trash_dry_run_archives_and_counts(monkeypatch):
    fake = _FakeIMAP([b"7", b"8"])
    res = _run_scan(monkeypatch, fake, action="trash", dry=True)
    assert res["post_action"] == "trash"
    assert res["post_action_dry_run"] is True
    assert res["post_action_count"] == 2
    assert fake.copies == []                       # archive = no COPY at all
    deleted = [u for u, fl in fake.stores if fl == "\\Deleted"]
    assert deleted == [b"7", b"8"] and fake.expunges == 1


def test_archive_legacy_and_none(monkeypatch):
    fake = _FakeIMAP([b"1"])
    res = _run_scan(monkeypatch, fake, action="archive")
    assert res["post_action"] == "archive" and res["post_action_count"] == 1
    assert fake.copies == []

    fake2 = _FakeIMAP([b"1"])
    res2 = _run_scan(monkeypatch, fake2, action="none")
    assert res2["post_action"] == "none" and res2["post_action_count"] == 0
    assert fake2.copies == []
    # marked read but NOT deleted
    assert (b"1", "\\Seen") in fake2.stores
    assert not any(fl == "\\Deleted" for _, fl in fake2.stores)


def test_invalid_action_falls_back_to_trash(monkeypatch):
    fake = _FakeIMAP([b"1"])
    res = _run_scan(monkeypatch, fake, action="banana")
    assert res["post_action"] == "trash"


# ── 3. resolve_trash_folder / quoting unit pins ─────────────────────────────

def test_resolve_trash_folder_spanish_and_english():
    fake = _FakeIMAP([])
    assert gi.resolve_trash_folder(fake) == TRASH_ES

    class _EN(_FakeIMAP):
        def list(self):
            return "OK", [b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"']

    assert gi.resolve_trash_folder(_EN([])) == "[Gmail]/Trash"


def test_quote_mailbox_spaces():
    assert gi._quote_mailbox("[Gmail]/Papelera") == "[Gmail]/Papelera"
    assert gi._quote_mailbox("[Gmail]/All Mail") == '"[Gmail]/All Mail"'


# ── 4. report totals line ───────────────────────────────────────────────────

def _gmail_dict(n: int, **extra) -> dict[str, Any]:
    return {
        "status": "ok",
        "emails": [
            {"subject": f"s{i}", "from": "a@b.c", "date": "", "excerpt": f"cuerpo {i}."}
            for i in range(n)
        ],
        "count": n,
        **extra,
    }


def test_render_trash_live_verified_counts():
    out = format_gmail_intel_block(_gmail_dict(
        3, post_action="trash", post_action_dry_run=False, post_action_count=3))
    assert "Totales: 3 procesados · 3 verificados en papelera" in out
    # The optimistic label is dead.
    assert "y enviados a papelera" not in out


def test_render_trash_mismatch_flagged():
    out = format_gmail_intel_block(_gmail_dict(
        3, post_action="trash", post_action_dry_run=False, post_action_count=2,
        trash_failed=1))
    assert "2 verificados en papelera" in out
    assert "⚠️ 1 NO verificados" in out


def test_render_dry_run_candidates():
    out = format_gmail_intel_block(_gmail_dict(
        4, post_action="trash", post_action_dry_run=True, post_action_count=4))
    assert "papelera DRY-RUN (4 candidatos" in out


def test_render_legacy_archive_line_preserved():
    assert "Totales: 2 procesados · 2 archivados" in \
        format_gmail_intel_block(_gmail_dict(2))
    assert "Totales: 2 procesados · 2 archivados" in \
        format_gmail_intel_block(_gmail_dict(
            2, post_action="archive", post_action_count=2))
    assert "sin acción posterior" in \
        format_gmail_intel_block(_gmail_dict(1, post_action="none"))
