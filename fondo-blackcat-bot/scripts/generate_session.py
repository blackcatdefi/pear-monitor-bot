"""Generate a Telethon StringSession locally for use in Railway.

Usage (run on your laptop, NOT on Railway):

    pip install telethon python-dotenv
    export TELEGRAM_API_ID=...
    export TELEGRAM_API_HASH=...
    python scripts/generate_session.py

You'll be asked for your phone number and the login code (sent via Telegram).
At the end the script prints the StringSession — copy that as TELETHON_SESSION
in Railway env vars.
"""
from __future__ import annotations

import os

from telethon.sessions import StringSession
from telethon.sync import TelegramClient


def main() -> None:
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    print("Connecting... follow the prompts (phone, code, optional 2FA password).")
    with TelegramClient(StringSession(), api_id, api_hash) as client:
        session = client.session.save()
        print("\n========== TELETHON_SESSION ==========")
        print(session)
        print("======================================\n")
        print("Copy the line above into Railway as TELETHON_SESSION.")


if __name__ == "__main__":
    main()
