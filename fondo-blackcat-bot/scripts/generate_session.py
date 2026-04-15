"""Generate a Telethon StringSession once, locally.

Usage:
    cd fondo-blackcat-bot
    pip install -r requirements.txt
    python scripts/generate_session.py

Prompts for phone code (SMS/Telegram) and prints a StringSession to paste
into Railway as TELETHON_SESSION.
"""
from __future__ import annotations

import os
import sys

from telethon.sessions import StringSession
from telethon.sync import TelegramClient


def main() -> int:
    api_id = int(os.environ.get("TELEGRAM_API_ID") or input("TELEGRAM_API_ID: "))
    api_hash = os.environ.get("TELEGRAM_API_HASH") or input("TELEGRAM_API_HASH: ")
    with TelegramClient(StringSession(), api_id, api_hash) as client:
        me = client.get_me()
        print(f"\nLogged in as: {me.first_name} (@{me.username})  id={me.id}")
        print("\n===== TELETHON_SESSION =====")
        print(client.session.save())
        print("============================\n")
        print("Copiá ese string y pegalo como TELETHON_SESSION en Railway.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
