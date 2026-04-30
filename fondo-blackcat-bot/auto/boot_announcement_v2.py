"""R-FINAL — boot announcement v2 (dedup-aware wrapper).

Replaces the direct call to ``boot_announcement.announce_boot`` from bot.py
with a wrapper that consults ``auto.boot_dedup`` first. Existing
``boot_announcement.announce_boot`` body is reused unchanged — we only add
the dedup gate around it.

Usage in bot.py (replace the legacy import):
    from auto.boot_announcement_v2 import announce_boot

Bug fixed: 5 identical boot messages within 5 minutes on apr-30 2026 due
to Railway cold restarts.
"""
from __future__ import annotations

import logging

from auto import boot_dedup
from boot_announcement import announce_boot as _legacy_announce_boot

logger = logging.getLogger(__name__)


async def announce_boot(bot) -> None:
    """Boot announcement, gated by ``boot_dedup``.

    Behavior:
      - If the dedup module is disabled or this is the first boot in the
        suppression window → call the legacy announcer and persist the
        timestamp.
      - Otherwise log a single suppression line and return.
    """
    if not boot_dedup.should_announce():
        logger.info(
            "boot_announcement_v2: suppressed (dedup window active)"
        )
        return
    try:
        await _legacy_announce_boot(bot)
    except Exception:  # noqa: BLE001
        logger.exception(
            "boot_announcement_v2: legacy announce_boot failed"
        )
        # Do NOT mark_announced if the underlying send failed — let the
        # next restart retry.
        return
    boot_dedup.mark_announced()
