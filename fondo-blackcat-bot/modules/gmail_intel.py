"""Gmail intel module — reads unread emails and marks them as read.

Uses IMAP with Gmail App Password for simplicity.
Env vars needed:
  GMAIL_EMAIL       — Gmail address (e.g. blackcatdefi@gmail.com)
  GMAIL_APP_PASSWORD — App Password from Google Account settings

Pattern mirrors scan_telegram_unread: read → extract → mark read → return dict.
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import logging
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from typing import Any

from config import GMAIL_APP_PASSWORD, GMAIL_EMAIL

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


def _scan_inbox_sync(hours: int = 24, max_emails: int = 50) -> dict[str, Any]:
    """Synchronous IMAP scan of unread emails in the last N hours."""
    if not GMAIL_EMAIL or not GMAIL_APP_PASSWORD:
        return {"status": "error", "error": "GMAIL_EMAIL or GMAIL_APP_PASSWORD not configured"}

    try:
        # Connect to Gmail IMAP
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        imap.login(GMAIL_EMAIL, GMAIL_APP_PASSWORD)
        imap.select("INBOX")

        # Search for unread emails from the last N hours
        since_date = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%d-%b-%Y")
        _, msg_ids = imap.search(None, f'(UNSEEN SINCE "{since_date}")')

        if not msg_ids or not msg_ids[0]:
            imap.logout()
            return {"status": "ok", "emails": [], "count": 0, "note": "no unread emails"}

        ids = msg_ids[0].split()
        if len(ids) > max_emails:
            ids = ids[-max_emails:]  # Most recent N

        emails_data: list[dict[str, Any]] = []

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

                # Mark as read (add SEEN flag)
                imap.store(uid, "+FLAGS", "\\Seen")

            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to process email %s: %s", uid, exc)
                continue

        imap.logout()

        return {
            "status": "ok",
            "emails": emails_data,
            "count": len(emails_data),
        }

    except imaplib.IMAP4.error as exc:
        log.exception("IMAP auth/connection failed")
        return {"status": "error", "error": f"IMAP error: {exc}"}
    except Exception as exc:  # noqa: BLE001
        log.exception("Gmail scan failed")
        return {"status": "error", "error": str(exc)}


async def scan_gmail_unread(hours: int = 24, max_emails: int = 50) -> dict[str, Any]:
    """Async wrapper — runs IMAP scan in a thread to avoid blocking."""
    return await asyncio.to_thread(_scan_inbox_sync, hours, max_emails)
