"""Gmail intel module — reads ALL unread emails, marks read, then post-acts.

Uses IMAP with Gmail App Password for simplicity (full-mailbox rights, so the
Trash operation needs no extra OAuth scope — there is no OAuth here at all).
Env vars needed:
  GMAIL_EMAIL — Gmail address (e.g. blackcatdefi@gmail.com)
  GMAIL_APP_PASSWORD — App Password from Google Account settings
  EMAIL_POST_ACTION — trash (default) | archive (legacy) | none
  EMAIL_TRASH_DRY_RUN — 1 → count trash candidates, archive instead (legacy)

R-UNIFIED-LIQ Phase C contract: the post-action touches EXACTLY and ONLY the
message uids that were successfully processed (appended to emails_data).
Pattern mirrors scan_telegram_unread: read → extract → mark read → post-act →
return dict.
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import logging
from email.header import decode_header
from typing import Any

from config import (
    EMAIL_POST_ACTION,
    EMAIL_TRASH_DRY_RUN,
    GMAIL_APP_PASSWORD,
    GMAIL_EMAIL,
)

log = logging.getLogger(__name__)


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


def _get_body(msg: email.message.Message) -> str:
    """Extract plain-text body from a MIME message."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and "attachment" not in (part.get("Content-Disposition") or ""):
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # Fallback: try text/html
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")[:2000]
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


def _resolve_post_action() -> tuple[str, bool]:
    """(action, dry_run) — invalid EMAIL_POST_ACTION falls back to trash."""
    action = EMAIL_POST_ACTION if EMAIL_POST_ACTION in ("trash", "archive", "none") else "trash"
    dry_run = bool(EMAIL_TRASH_DRY_RUN) and action == "trash"
    return action, dry_run


def _apply_post_action(imap, processed_uids: list, action: str, dry_run: bool) -> int:
    """Apply the post action to EXACTLY the given uids. Returns acted count."""
    acted = 0
    for uid in processed_uids:
        try:
            if action == "trash" and not dry_run:
                # Gmail IMAP: copying to Trash moves the message to Trash.
                imap.copy(uid, "[Gmail]/Trash")
                imap.store(uid, "+FLAGS", "\\Deleted")
            elif action == "archive" or (action == "trash" and dry_run):
                # Legacy archive (also the safe behavior during trash dry-run).
                try:
                    imap.copy(uid, "[Gmail]/All Mail")
                except Exception:  # noqa: BLE001
                    pass  # Already in All Mail on Gmail
                imap.store(uid, "+FLAGS", "\\Deleted")
            else:  # none — leave in INBOX (already marked read)
                continue
            acted += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("Post-action %s failed for uid %s: %s", action, uid, exc)
    return acted


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

                # Truncate body to snippet
                snippet = body[:500].strip() if body else ""

                emails_data.append({
                    "subject": subject,
                    "from": sender,
                    "date": date_str,
                    "snippet": snippet,
                })

                # Mark as read
                imap.store(uid, "+FLAGS", "\\Seen")
                processed_uids.append(uid)

            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to process email %s: %s", uid, exc)
                continue

        # R-UNIFIED-LIQ Phase C: post-action on EXACTLY the processed uids.
        action, dry_run = _resolve_post_action()
        acted = _apply_post_action(imap, processed_uids, action, dry_run)

        # Expunge moved messages from INBOX view (no-op for action=none)
        imap.expunge()
        imap.logout()

        return {
            "status": "ok",
            "emails": emails_data,
            "count": len(emails_data),
            "post_action": action,
            "post_action_dry_run": dry_run,
            "post_action_count": acted if action != "none" else 0,
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
