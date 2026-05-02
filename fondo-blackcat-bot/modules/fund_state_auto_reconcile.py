"""Round 18 — Auto-apply fund_state reconcile via Telegram inline buttons.

When the existing reconciler detects a STATUS_OUT_OF_SYNC (or any other
discrepancy whose suggested_basket_v5_status field is set), wrap the
alert with an InlineKeyboard "Apply / Ignore" pair so BCD can authorise
the rewrite + git push from his phone.

This module exposes:
    build_keyboard_for(d) → InlineKeyboardMarkup
    persist_pending(d) → str (pending_id stored in SQLite)
    handle_callback(query) → (callback handler logic)

Auth: only the configured TELEGRAM_CHAT_ID owner can approve.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, ContextTypes

from config import DATA_DIR, TELEGRAM_CHAT_ID

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "auto_reconcile.db")


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute(
        """CREATE TABLE IF NOT EXISTS pending_reconcile (
            id TEXT PRIMARY KEY,
            ts_utc TEXT NOT NULL,
            type TEXT NOT NULL,
            wallet TEXT,
            suggested_status TEXT,
            payload_json TEXT NOT NULL,
            resolved INTEGER DEFAULT 0,
            resolution TEXT,
            resolved_at TEXT
        )"""
    )
    return c


def is_enabled() -> bool:
    return os.getenv("AUTO_RECONCILE_APPLY_ENABLED", "true").strip().lower() != "false"


def persist_pending(discrepancy: Any) -> str:
    """Save a discrepancy as pending and return a callback-friendly id."""
    pending_id = uuid.uuid4().hex[:10]
    try:
        payload = asdict(discrepancy)
    except Exception:
        payload = dict(getattr(discrepancy, "__dict__", {}))
    with _conn() as c:
        c.execute(
            "INSERT INTO pending_reconcile(id,ts_utc,type,wallet,suggested_status,payload_json) "
            "VALUES (?,?,?,?,?,?)",
            (
                pending_id,
                datetime.now(timezone.utc).isoformat(),
                payload.get("type") or "?",
                (payload.get("wallet") or "")[:10],
                payload.get("suggested_basket_v5_status"),
                json.dumps(payload, default=str),
            ),
        )
    return pending_id


def load_pending(pending_id: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute(
            "SELECT id,ts_utc,type,wallet,suggested_status,payload_json,resolved "
            "FROM pending_reconcile WHERE id=?",
            (pending_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "ts_utc": row[1],
        "type": row[2],
        "wallet": row[3],
        "suggested_status": row[4],
        "payload": json.loads(row[5]) if row[5] else {},
        "resolved": int(row[6] or 0),
    }


def mark_resolved(pending_id: str, resolution: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE pending_reconcile SET resolved=1,resolution=?,resolved_at=? WHERE id=?",
            (resolution, datetime.now(timezone.utc).isoformat(), pending_id),
        )


def build_keyboard_for(pending_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("\u2705 Apply", callback_data=f"reconcile_apply_{pending_id}"),
            InlineKeyboardButton("\u274c Ignore", callback_data=f"reconcile_ignore_{pending_id}"),
        ]]
    )


async def handle_callback(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle reconcile_apply_<id> / reconcile_ignore_<id> button clicks."""
    query = update.callback_query
    if query is None:
        return
    chat = update.effective_chat
    if chat is None or str(chat.id) != str(TELEGRAM_CHAT_ID):
        await query.answer("\u26d4 Not authorised.", show_alert=True)
        return

    data = query.data or ""
    if data.startswith("reconcile_apply_"):
        pending_id = data.replace("reconcile_apply_", "")
        action = "apply"
    elif data.startswith("reconcile_ignore_"):
        pending_id = data.replace("reconcile_ignore_", "")
        action = "ignore"
    else:
        await query.answer()
        return

    pending = load_pending(pending_id)
    if pending is None:
        await query.answer("Pending no encontrado.", show_alert=False)
        return
    if pending["resolved"]:
        await query.answer("Ya resuelto.", show_alert=False)
        return

    if action == "ignore":
        mark_resolved(pending_id, "ignored")
        await query.answer("Ignorado.")
        try:
            await query.edit_message_text(
                (query.message.text or "") + "\n\n\u274c Ignorado por BCD."
            )
        except Exception:
            pass
        return

    # Apply path
    suggested = pending.get("suggested_status")
    if not suggested:
        await query.answer("Sin status sugerido para aplicar.", show_alert=True)
        return

    try:
        from modules.fund_state_reconciler import apply_basket_v5_status
    except Exception:
        await query.answer("Reconciler no disponible.", show_alert=True)
        return

    await query.answer("Aplicando…")
    try:
        result = apply_basket_v5_status(suggested)
    except Exception as exc:  # noqa: BLE001
        log.exception("apply_basket_v5_status failed")
        try:
            await query.edit_message_text(
                (query.message.text or "") + f"\n\n\u274c Apply failed: {exc}"
            )
        except Exception:
            pass
        mark_resolved(pending_id, "error")
        return

    mark_resolved(pending_id, "applied" if result.get("ok") else "apply_error")
    icon = "\u2705" if result.get("ok") else "\u26a0\ufe0f"
    pushed = "pushed" if result.get("pushed") else "NO push"
    suffix = (
        f"\n\n{icon} Apply → {suggested} ({pushed}). "
        f"{result.get('message','')[:200]}"
    )
    try:
        await query.edit_message_text((query.message.text or "") + suffix)
    except Exception:
        pass


def get_callback_handler() -> CallbackQueryHandler:
    return CallbackQueryHandler(handle_callback, pattern=r"^reconcile_(apply|ignore)_")


def format_with_keyboard_message(d: Any) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build (text, keyboard) tuple from a Discrepancy. Keyboard is None if no
    suggested status (only STATUS_OUT_OF_SYNC currently has actionable apply).
    """
    if not is_enabled():
        return ("", None)
    suggested = getattr(d, "suggested_basket_v5_status", None)
    if not suggested:
        return ("", None)

    pending_id = persist_pending(d)
    text = (
        "\U0001f504 DISCREPANCIA DETECTADA en fund_state.py\n"
        f"Type: {getattr(d, 'type', '?')}\n"
        f"Wallet: {getattr(d, 'wallet', '?')}\n"
        f"Detail: {getattr(d, 'detail', '')[:300]}\n"
        f"Suggested: BASKET_V5_STATUS \u2192 {suggested}\n\n"
        "Apply automatically? (commit + push)"
    )
    return (text, build_keyboard_for(pending_id))
