"""Gmail intel module — reads ALL unread emails, marks read, then post-acts.

Uses IMAP with Gmail App Password for simplicity (full-mailbox rights, so the
Trash operation needs no extra OAuth scope — there is no OAuth here at all).
Env vars needed:
  GMAIL_EMAIL — Gmail address (e.g. blackcatdefi@gmail.com)
  GMAIL_APP_PASSWORD — App Password from Google Account settings
  EMAIL_POST_ACTION — trash (default) | archive (legacy) | none
  EMAIL_TRASH_DRY_RUN — 1 → count trash candidates, archive instead (legacy)
  EMAIL_EXCERPT_CHARS — excerpt length per email in the report (default 800)

R-MAIL-CONTENT-TRASHFIX contracts (supersede the Phase C optimistic count):

1. VERIFIED TRASH. The account's real Trash folder is resolved at runtime from
   the IMAP LIST \\Trash special-use attribute (Spanish Gmail exposes
   "[Gmail]/Papelera", NOT "[Gmail]/Trash" — the hardcoded name made COPY
   return ('NO', [TRYCREATE ...]) which imaplib does NOT raise, so messages
   silently degraded to archive). Every COPY return status is checked, and
   after expunge each message is looked up in the Trash folder by its
   X-GM-MSGID. ``post_action_count`` = VERIFIED-in-Trash count only; failures
   surface in ``trash_failed`` for the caller to alert on. No optimistic
   labels ever.

2. CONTENT BEFORE TRASH. Body extraction (full message, text/plain preferred,
   HTML stripped to text) happens in the read loop, strictly BEFORE any
   post-action runs. The report is built from ``emails_data`` which is fully
   populated before the first COPY, so content is always captured before a
   message is trashed.

3. The post-action touches EXACTLY and ONLY the message uids that were
   successfully processed (appended to emails_data). Pattern mirrors
   scan_telegram_unread: read → extract → mark read → post-act → return dict.
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import logging
import re
from email.header import decode_header
from html import unescape
from html.parser import HTMLParser
from typing import Any

from config import (
    EMAIL_EXCERPT_CHARS,
    EMAIL_POST_ACTION,
    EMAIL_TRASH_DRY_RUN,
    GMAIL_APP_PASSWORD,
    GMAIL_EMAIL,
)

log = logging.getLogger(__name__)

# Fallbacks only if LIST yields no \Trash folder (never expected on Gmail).
_TRASH_FALLBACKS = ("[Gmail]/Trash", "[Gmail]/Papelera")


def _decode_header(raw: str | None) -> str:
    """Decode an email header that may be encoded (RFC 2047)."""
    if not raw:
        return ""
    parts = decode_header(raw)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return " ".join(decoded)


# ─── body extraction + cleaning (Part 2) ────────────────────────────────────

class _HTMLTextExtractor(HTMLParser):
    """Strip tags → text. Skips script/style; <br>/<p>/<div> become newlines."""

    _SKIP = {"script", "style", "head", "title"}
    _BREAK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):  # noqa: D102
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BREAK:
            self._chunks.append("\n")

    def handle_endtag(self, tag):  # noqa: D102
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self._BREAK:
            self._chunks.append("\n")

    def handle_data(self, data):  # noqa: D102
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def _html_to_text(html: str) -> str:
    """Best-effort HTML → plain text (never raises)."""
    try:
        p = _HTMLTextExtractor()
        p.feed(html)
        p.close()
        return p.text()
    except Exception:  # noqa: BLE001
        # Degrade: regex tag strip, still unescape entities.
        return unescape(re.sub(r"<[^>]+>", " ", html))


# Invisible characters newsletters (Bloomberg et al.) pad bodies with.
_INVISIBLE_RE = re.compile(r"[\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad\u034f]")

# Boilerplate lines dropped from excerpts (case-insensitive, per line).
_BOILERPLATE_RES = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"view (this|in|it in).*browser",
        r"view (this )?(email|message) (in|as a) ",
        r"^unsubscribe\b",
        r"\bunsubscribe (here|now|from)",
        r"^subscribe\b|\bsubscribe (here|now|today|to get)\b",
        r"to get this newsletter",
        r"was this (email|newsletter) forwarded",
        r"sign up (here|now|for|to receive)",
        r"^follow us\b|^connect with us\b",
        r"^(facebook|twitter|x|instagram|linkedin|youtube|telegram|tiktok)$",
        r"^https?://\S+$",  # bare URL lines (tracking/social)
        r"^\[?image[:\]]|^\[img\]",
        r"all rights reserved",
        r"^copyright\b|^©|\(c\) \d{4}",
        r"privacy policy|terms of service|terms and conditions",
        r"you (are receiving|received) this (email|message|newsletter)",
        r"update your (email )?preferences",
        r"manage (your )?(subscription|preferences)",
        r"^this (email|message) was sent to",
        r"^add us to your address book",
        r"^click here\b",
        r"^advertisement$|^sponsored( content)?$|^paid post$",
    )
]

_SENTENCE_END_RE = re.compile(r"[.!?…]['\")\]]?(?=\s|$)")

# Angle-bracket URL tokens: Bloomberg text/plain wraps every tracking link in
# <https://sli.bloomberg.com/click?...>, often in long consecutive runs that
# used to eat most of the 800-char excerpt budget. Stripped from the raw text
# BEFORE any substantive-content measuring. [^<>]* deliberately spans
# newlines: mail clients hard-wrap long URLs inside the brackets.
_ANGLE_URL_RE = re.compile(r"<\s*https?://[^<>]*>")
_URL_TOKEN_RE = re.compile(r"https?://\S+")
_ALNUM_RE = re.compile(r"[A-Za-z0-9]")


def _clean_body_text(text: str) -> str:
    """Cleaning pipeline: invisible chars → URL tokens → boilerplate → ws."""
    if not text:
        return ""
    text = _INVISIBLE_RE.sub("", text).replace("\u00a0", " ").replace("\r", "")
    # Strip angle-bracket URL tokens globally (they can span wrapped lines).
    text = _ANGLE_URL_RE.sub(" ", text)
    kept: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if any(rx.search(line) for rx in _BOILERPLATE_RES):
            continue
        # URL-only lines (incl. several URLs separated by punctuation): after
        # removing URL tokens nothing alphanumeric remains → zero content,
        # drop. These arrive in consecutive runs in newsletter link farms.
        if _URL_TOKEN_RE.search(line) and not _ALNUM_RE.search(_URL_TOKEN_RE.sub("", line)):
            continue
        line = line.strip()
        if not line or not _ALNUM_RE.search(line):
            # Leftover bare punctuation once URL tokens are gone.
            continue
        # Collapse runs of spaces/tabs inside the line.
        kept.append(re.sub(r"[ \t]{2,}", " ", line))
    return "\n".join(kept)


def _cut_at_sentence(text: str, limit: int) -> str:
    """Cut ``text`` to ≤ limit chars, preferring the last sentence boundary."""
    if len(text) <= limit:
        return text
    window = text[:limit]
    best_end = -1
    for m in _SENTENCE_END_RE.finditer(window):
        best_end = m.end()
    if best_end > limit // 3:  # sentence cut only if not degenerately short
        return window[:best_end].rstrip()
    # Fallback: cut at last word boundary with ellipsis.
    cut = window.rsplit(" ", 1)[0].rstrip()
    return (cut or window).rstrip() + "…"


def _make_excerpt(body: str, limit: int) -> str:
    """First substantive content of the cleaned body, ≤ limit chars."""
    cleaned = _clean_body_text(body)
    if not cleaned:
        return ""
    # Flow lines into a single paragraph-ish text for the excerpt.
    flowed = re.sub(r"\s*\n\s*", " ", cleaned).strip()
    return _cut_at_sentence(flowed, max(80, int(limit)))


def _get_body(msg: email.message.Message) -> str:
    """Extract full body text: prefer text/plain; else HTML stripped to text."""
    html_body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disp = part.get("Content-Disposition") or ""
            if "attachment" in disp:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if ct == "text/plain":
                return text
            if ct == "text/html" and not html_body:
                html_body = text
        return _html_to_text(html_body) if html_body else ""
    payload = msg.get_payload(decode=True)
    if payload:
        charset = msg.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")
        if msg.get_content_type() == "text/html":
            return _html_to_text(text)
        return text
    return ""


# ─── post-action (Part 1: verified trash) ───────────────────────────────────

def _resolve_post_action() -> tuple[str, bool]:
    """(action, dry_run) — invalid EMAIL_POST_ACTION falls back to trash."""
    action = EMAIL_POST_ACTION if EMAIL_POST_ACTION in ("trash", "archive", "none") else "trash"
    dry_run = bool(EMAIL_TRASH_DRY_RUN) and action == "trash"
    return action, dry_run


_LIST_TRASH_RE = re.compile(
    rb"\\Trash[^)]*\)\s+\"?(?P<delim>[^\"\s]+)\"?\s+\"?(?P<name>[^\"]+?)\"?\s*$"
)


def resolve_trash_folder(imap) -> str | None:
    """Resolve the account's REAL Trash folder from LIST special-use attrs.

    Root cause of the silent-archive bug: Spanish Gmail has NO
    "[Gmail]/Trash" — the folder is "[Gmail]/Papelera". COPY to a missing
    folder returns ('NO', [TRYCREATE...]) which imaplib does not raise.
    """
    try:
        typ, rows = imap.list()
        if typ == "OK" and rows:
            for row in rows:
                if not isinstance(row, bytes):
                    continue
                if b"\\Trash" not in row:
                    continue
                m = _LIST_TRASH_RE.search(row)
                if m:
                    return m.group("name").decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        log.exception("resolve_trash_folder: LIST failed")
    # Fallback: probe known names via STATUS (does not create anything).
    for cand in _TRASH_FALLBACKS:
        try:
            typ, _ = imap.status(_quote_mailbox(cand), "(MESSAGES)")
            if typ == "OK":
                return cand
        except Exception:  # noqa: BLE001
            continue
    return None


def _quote_mailbox(name: str) -> str:
    """imaplib does not quote mailbox args — names with spaces need quotes."""
    if name.startswith('"'):
        return name
    if " " in name:
        return f'"{name}"'
    return name


def _iter_fetch_blobs(data) -> "list[bytes]":
    """Flatten an imaplib FETCH response into byte blobs.

    imaplib data items are bytes rows, ``None`` placeholders, or tuples when
    the response carries a literal — for tuples, all bytes parts are joined so
    the row header (which holds the sequence number and X-GM-MSGID) survives.
    """
    blobs: list[bytes] = []
    for item in data or []:
        if item is None:
            continue
        if isinstance(item, bytes):
            blobs.append(item)
        elif isinstance(item, tuple):
            parts = [p for p in item if isinstance(p, bytes)]
            if parts:
                blobs.append(b" ".join(parts))
    return blobs


_FETCH_ROW_SEQ_RE = re.compile(rb"^\s*(\d+)\s+\(")
_GM_MSGID_RE = re.compile(rb"X-GM-MSGID\s+(\d+)")
_UID_RE = re.compile(rb"\bUID\s+(\d+)")


def _parse_uid_rows(data) -> dict[bytes, tuple[bytes | None, str | None]]:
    """Parse a FETCH response → {seq: (uid, gm_msgid)} strictly per-row.

    Same per-row discipline as ``_parse_gm_msgid_rows``: sequence number,
    UID and X-GM-MSGID are all read from the row's own header, so Gmail's
    interleaved untagged rows can never shift the attribution.
    """
    result: dict[bytes, tuple[bytes | None, str | None]] = {}
    for blob in _iter_fetch_blobs(data):
        for line in blob.splitlines():
            seq_m = _FETCH_ROW_SEQ_RE.match(line)
            if not seq_m:
                continue
            uid_m = _UID_RE.search(line)
            mid_m = _GM_MSGID_RE.search(line)
            if uid_m or mid_m:
                result[seq_m.group(1)] = (
                    uid_m.group(1) if uid_m else None,
                    mid_m.group(1).decode() if mid_m else None,
                )
    return result


def _parse_gm_msgid_rows(data) -> dict[bytes, str]:
    """Parse a (possibly multi-uid) X-GM-MSGID FETCH response → {seq: msgid}.

    Gmail interleaves untagged responses at batch scale: the same FETCH data
    list can carry FLAGS-update rows (from earlier ``store \\Seen`` calls),
    ``None`` placeholders and literal tuples between the rows we asked for.
    Parsing is strictly PER ROW: the sequence number is taken from the row's
    own header, never assumed from request order — so an interleaved row for
    another message can never be attributed to the wrong uid.
    """
    result: dict[bytes, str] = {}
    for blob in _iter_fetch_blobs(data):
        for line in blob.splitlines():
            seq_m = _FETCH_ROW_SEQ_RE.match(line)
            if not seq_m:
                continue
            mid_m = _GM_MSGID_RE.search(line)
            if mid_m:
                result[seq_m.group(1)] = mid_m.group(1).decode()
    return result


def _uid_key(uid) -> bytes:
    return uid if isinstance(uid, bytes) else str(uid).encode()


def _fetch_gm_msgids(imap, uids: list) -> dict[bytes, str]:
    """Batch-fetch Gmail X-GM-MSGIDs for many uids in ONE round trip.

    One FETCH per uid at batch scale is what broke verification (25 trashed /
    9 verified): Gmail's interleaved untagged responses got consumed by the
    wrong in-flight command and individual fetches came back empty → false
    "no X-GM-MSGID". A single multi-uid FETCH parsed per-row is immune to
    ordering; any uid still missing afterwards gets one per-uid retry.
    """
    result: dict[bytes, str] = {}
    if not uids:
        return result
    wanted = [_uid_key(u) for u in uids]
    try:
        typ, data = imap.fetch(b",".join(wanted), "(X-GM-MSGID)")
        if typ == "OK":
            rows = _parse_gm_msgid_rows(data)
            for key in wanted:
                if key in rows:
                    result[key] = rows[key]
    except Exception:  # noqa: BLE001
        log.warning("batch X-GM-MSGID fetch failed for %d uids", len(wanted))
    # Per-uid retry for anything the batch response did not cover.
    for uid in uids:
        key = _uid_key(uid)
        if key not in result:
            single = _fetch_gm_msgid(imap, uid)
            if single:
                result[key] = single
    return result


def _fetch_uid_gm_msgids(
    imap, seqs: list
) -> dict[bytes, tuple[bytes | None, str | None]]:
    """Batch-resolve (UID, X-GM-MSGID) for sequence numbers in ONE round trip.

    MUST run BEFORE any mutation: COPY-to-Trash strips \\Inbox on Gmail and
    the server interleaves untagged EXPUNGEs, so sequence numbers SHIFT
    mid-loop — a seq-addressed COPY then hits the WRONG message (confirmed
    live: alternating messages ended archived instead of trashed). Every
    mutating command afterwards must address messages by immutable UID.
    """
    result: dict[bytes, tuple[bytes | None, str | None]] = {}
    if not seqs:
        return result
    wanted = [_uid_key(s) for s in seqs]
    try:
        typ, data = imap.fetch(b",".join(wanted), "(UID X-GM-MSGID)")
        if typ == "OK":
            rows = _parse_uid_rows(data)
            for key in wanted:
                if key in rows:
                    result[key] = rows[key]
    except Exception:  # noqa: BLE001
        log.warning("batch UID/X-GM-MSGID fetch failed for %d seqs", len(wanted))
    # Per-seq retry for anything the batch response did not cover.
    for key in wanted:
        if key in result and result[key][0] is not None:
            continue
        try:
            typ, data = imap.fetch(key, "(UID X-GM-MSGID)")
            if typ == "OK":
                rows = _parse_uid_rows(data)
                if key in rows:
                    result[key] = rows[key]
                elif len(rows) == 1:
                    result[key] = next(iter(rows.values()))
        except Exception:  # noqa: BLE001
            log.warning("UID/X-GM-MSGID retry failed for seq %s", key)
    return result


def _fetch_gm_msgid(imap, uid) -> str | None:
    """Gmail X-GM-MSGID for a single message (retry path of the batch)."""
    try:
        typ, data = imap.fetch(uid, "(X-GM-MSGID)")
        if typ != "OK" or not data:
            return None
        rows = _parse_gm_msgid_rows(data)
        if rows:
            # Prefer the row matching this uid; tolerate servers that omit
            # the echo by falling back to the single row present.
            key = _uid_key(uid)
            if key in rows:
                return rows[key]
            if len(rows) == 1:
                return next(iter(rows.values()))
    except Exception:  # noqa: BLE001
        log.warning("X-GM-MSGID fetch failed for uid %s", uid)
    return None


def _apply_post_action(
    imap, processed_uids: list, action: str, dry_run: bool
) -> tuple[int, list[str]]:
    """Apply the post action to EXACTLY the given uids.

    Returns (verified_count, failures). For action=trash (live) the count is
    the number of messages VERIFIED present in the Trash folder after the
    move (X-GM-MSGID lookup). ``failures`` carries per-uid reasons for the
    caller to log/alert — nothing is masked.
    """
    failures: list[str] = []
    if not processed_uids or action == "none":
        return 0, failures

    if action == "archive" or (action == "trash" and dry_run):
        # Gmail archive = remove the INBOX label = \Deleted + EXPUNGE on
        # INBOX. (The old COPY to "[Gmail]/All Mail" was a no-op at best and
        # a localized-name failure at worst — every message is already in
        # All Mail on Gmail.)
        acted = 0
        for uid in processed_uids:
            try:
                typ, _ = imap.store(uid, "+FLAGS", "\\Deleted")
                if typ == "OK":
                    acted += 1
                else:
                    failures.append(f"uid {uid}: archive store returned {typ}")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"uid {uid}: archive store error {exc}")
        imap.expunge()
        return acted, failures

    # ── action == "trash" (live) — verified move ────────────────────────────
    trash = resolve_trash_folder(imap)
    if not trash:
        # Hard failure: do NOT degrade to archive. Leave messages in INBOX
        # (read) and surface the error.
        failures.append("trash folder not resolvable via LIST \\Trash")
        return 0, failures

    trash_arg = _quote_mailbox(trash)
    # Resolve (UID, X-GM-MSGID) for ALL messages in ONE round trip BEFORE any
    # mutation: on Gmail, COPY-to-Trash strips \Inbox and untagged EXPUNGEs
    # shift sequence numbers mid-loop, so every command below addresses
    # messages by immutable UID only (R-MAIL-POLISH).
    seq_map = _fetch_uid_gm_msgids(imap, processed_uids)
    pending: list[tuple[Any, str | None]] = []  # (seq, gm_msgid) copied OK
    for seq in processed_uids:
        uid, gm_msgid = seq_map.get(_uid_key(seq), (None, None))
        if not uid:
            # Never fall back to seq-addressed COPY: after any earlier copy
            # the seq may point at a DIFFERENT message. Fail loudly instead.
            failures.append(f"uid {seq}: no UID resolvable; NOT trashed")
            continue
        try:
            typ, resp = imap.uid("COPY", uid, trash_arg)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"uid {seq}: COPY error {exc}")
            continue
        if typ != "OK":
            # e.g. ('NO', [b'[TRYCREATE] No folder ...']) — the silent killer.
            failures.append(f"uid {seq}: COPY {typ} {resp!r}")
            continue
        try:
            imap.uid("STORE", uid, "+FLAGS", "\\Deleted")
        except Exception as exc:  # noqa: BLE001
            log.warning("store \\Deleted failed for uid %s: %s", seq, exc)
            # Copy landed in Trash; message may remain visible in INBOX too.
        pending.append((seq, gm_msgid))

    imap.expunge()

    # Verification pass: ONE batched X-GM-MSGID listing of the Trash folder,
    # then set membership. Per-message ``SEARCH X-GM-MSGID`` desyncs against
    # Gmail's untagged EXISTS updates exactly like per-uid FETCH did
    # (confirmed live: alternating false "NOT in trash" on present messages).
    verified = 0
    trash_ids: set[str] = set()
    trash_selected = False
    try:
        typ, _ = imap.select(trash_arg, readonly=True)
        trash_selected = typ == "OK"
        if trash_selected:
            typ, data = imap.search(None, "ALL")
            if typ == "OK" and data and data[0]:
                trash_ids = set(
                    _fetch_gm_msgids(imap, data[0].split()).values()
                )
    except Exception as exc:  # noqa: BLE001
        trash_selected = False
        failures.append(f"verify: cannot select {trash}: {exc}")
    for seq, gm_msgid in pending:
        if not trash_selected:
            failures.append(f"uid {seq}: unverified (trash select failed)")
        elif not gm_msgid:
            failures.append(f"uid {seq}: unverified (no X-GM-MSGID)")
        elif gm_msgid in trash_ids:
            verified += 1
        else:
            failures.append(f"uid {seq}: NOT in trash after move")
    return verified, failures


def _scan_inbox_sync(max_emails: int = 200) -> dict[str, Any]:
    """Synchronous IMAP scan of ALL unread emails. Marks read + post-acts."""
    if not GMAIL_EMAIL or not GMAIL_APP_PASSWORD:
        return {"status": "error", "error": "GMAIL_EMAIL or GMAIL_APP_PASSWORD not configured"}

    try:
        # Connect to Gmail IMAP
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        imap.login(GMAIL_EMAIL, GMAIL_APP_PASSWORD)
        imap.select("INBOX")

        # Search for ALL unread emails (no date filter)
        _, msg_ids = imap.search(None, "(UNSEEN)")

        if not msg_ids or not msg_ids[0]:
            imap.logout()
            return {"status": "ok", "emails": [], "count": 0, "note": "no unread emails"}

        ids = msg_ids[0].split()
        if len(ids) > max_emails:
            ids = ids[-max_emails:]  # Most recent N

        emails_data: list[dict[str, Any]] = []
        processed_uids: list = []  # ONLY uids fully processed get the post-action

        for uid in ids:
            try:
                _, data = imap.fetch(uid, "(RFC822)")
                if not data or not data[0]:
                    continue
                raw = data[0][1]
                if isinstance(raw, bytes):
                    msg = email.message_from_bytes(raw)
                else:
                    continue

                subject = _decode_header(msg.get("Subject"))
                sender = _decode_header(msg.get("From"))
                date_str = msg.get("Date", "")
                body = _get_body(msg)

                emails_data.append({
                    "subject": subject,
                    "from": sender,
                    "date": date_str,
                    # Part 2: substantive excerpt for the report (content is
                    # captured HERE, before any post-action can run).
                    "excerpt": _make_excerpt(body, EMAIL_EXCERPT_CHARS),
                    # Legacy short snippet kept for downstream consumers.
                    "snippet": body[:500].strip() if body else "",
                })

                # Mark as read
                imap.store(uid, "+FLAGS", "\\Seen")
                processed_uids.append(uid)

            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to process email %s: %s", uid, exc)
                continue

        # Post-action on EXACTLY the processed uids — runs strictly AFTER all
        # content extraction above (order contract 2.4).
        action, dry_run = _resolve_post_action()
        acted, failures = _apply_post_action(imap, processed_uids, action, dry_run)
        for f in failures:
            log.warning("gmail post-action failure: %s", f)

        imap.logout()

        return {
            "status": "ok",
            "emails": emails_data,
            "count": len(emails_data),
            "post_action": action,
            "post_action_dry_run": dry_run,
            # trash live → VERIFIED-in-Trash count. Never optimistic.
            "post_action_count": acted if action != "none" else 0,
            "trash_failed": len(failures) if action == "trash" and not dry_run else 0,
            "post_action_failures": failures,
        }

    except imaplib.IMAP4.error as exc:
        log.exception("IMAP auth/connection failed")
        return {"status": "error", "error": f"IMAP error: {exc}"}
    except Exception as exc:  # noqa: BLE001
        log.exception("Gmail scan failed")
        return {"status": "error", "error": str(exc)}


async def scan_gmail_unread(max_emails: int = 200) -> dict[str, Any]:
    """Async wrapper — runs IMAP scan in a thread to avoid blocking."""
    return await asyncio.to_thread(_scan_inbox_sync, max_emails)
