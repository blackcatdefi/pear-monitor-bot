"""R-UNIFIED-LIQ Phase C — Gmail post-action (trash) acceptance tests.

Locked-in contracts:
  1. EMAIL_POST_ACTION trash|archive|none (default trash, invalid → trash);
     EMAIL_TRASH_DRY_RUN=1 archives instead but reports trash candidates.
  2. The post-action touches EXACTLY and ONLY the successfully processed
     message uids — a uid whose fetch fails is never trashed/archived.
  3. Report totals line: "N procesados y enviados a papelera" with matching
     counts (live), DRY-RUN candidate line, legacy archive line preserved.
"""
from __future__ import annotations

from typing import Any

from modules import gmail_intel as gi
from modules.intel_render import format_gmail_intel_block


def _raw_email(subject: str) -> bytes:
    return (
        f"From: Sender <s@example.com>\r\nSubject: {subject}\r\n"
        f"Date: Mon, 10 Aug 2026 10:00:00 +0000\r\n"
        f"Content-Type: text/plain\r\n\r\nbody {subject}\r\n"
    ).encode()


class _FakeIMAP:
    """Minimal imaplib stand-in recording copy/store calls per uid."""

    def __init__(self, uids: list[bytes], fail_fetch: set[bytes] = frozenset()):
        self._uids = uids
        self._fail_fetch = set(fail_fetch)
        self.copies: list[tuple[bytes, str]] = []
        self.stores: list[tuple[bytes, str]] = []
        self.expunged = False
        self.logged_out = False

    def login(self, *a): return "OK", []
    def select(self, *a): return "OK", []

    def search(self, charset, query):
        return "OK", [b" ".join(self._uids)]

    def fetch(self, uid, spec):
        if uid in self._fail_fetch:
            raise RuntimeError("fetch boom")
        return "OK", [(b"1 (RFC822)", _raw_email(f"mail-{uid.decode()}"))]

    def copy(self, uid, mailbox):
        self.copies.append((uid, mailbox))
        return "OK", []

    def store(self, uid, op, flags):
        self.stores.append((uid, flags))
        return "OK", []

    def expunge(self):
        self.expunged = True
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


# ── 1. trash live: only processed uids touched ──────────────────────────────

def test_trash_only_processed_uids(monkeypatch):
    fake = _FakeIMAP([b"1", b"2", b"3"], fail_fetch={b"2"})
    res = _run_scan(monkeypatch, fake, action="trash")
    assert res["status"] == "ok" and res["count"] == 2
    assert res["post_action"] == "trash"
    assert res["post_action_dry_run"] is False
    assert res["post_action_count"] == 2
    trash_copies = [u for u, mb in fake.copies if mb == "[Gmail]/Trash"]
    assert trash_copies == [b"1", b"3"]          # uid 2 NEVER trashed
    assert all(mb == "[Gmail]/Trash" for _, mb in fake.copies)
    deleted = [u for u, fl in fake.stores if fl == "\\Deleted"]
    assert deleted == [b"1", b"3"]
    assert fake.expunged and fake.logged_out


def test_trash_dry_run_archives_and_counts(monkeypatch):
    fake = _FakeIMAP([b"7", b"8"])
    res = _run_scan(monkeypatch, fake, action="trash", dry=True)
    assert res["post_action"] == "trash"
    assert res["post_action_dry_run"] is True
    assert res["post_action_count"] == 2
    assert all(mb == "[Gmail]/All Mail" for _, mb in fake.copies)  # NO trash
    assert not any(mb == "[Gmail]/Trash" for _, mb in fake.copies)


def test_archive_legacy_and_none(monkeypatch):
    fake = _FakeIMAP([b"1"])
    res = _run_scan(monkeypatch, fake, action="archive")
    assert res["post_action"] == "archive" and res["post_action_count"] == 1
    assert fake.copies == [(b"1", "[Gmail]/All Mail")]

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
    assert fake.copies == [(b"1", "[Gmail]/Trash")]


# ── 2. report totals line ───────────────────────────────────────────────────

def _gmail_dict(n: int, **extra) -> dict[str, Any]:
    return {
        "status": "ok",
        "emails": [
            {"subject": f"s{i}", "from": "a@b.c", "date": "", "snippet": ""}
            for i in range(n)
        ],
        "count": n,
        **extra,
    }


def test_render_trash_live_matching_counts():
    out = format_gmail_intel_block(_gmail_dict(
        3, post_action="trash", post_action_dry_run=False, post_action_count=3))
    assert "Totales: 3 procesados y enviados a papelera" in out


def test_render_trash_mismatch_flagged():
    out = format_gmail_intel_block(_gmail_dict(
        3, post_action="trash", post_action_dry_run=False, post_action_count=2))
    assert "2 enviados a papelera" in out and "⚠️ 1 sin acción" in out


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
