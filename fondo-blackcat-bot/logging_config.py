"""Logging configuration module for the bot.

Reduces verbosity of noisy third-party HTTP libraries (httpx, httpcore,
urllib3) so Railway logs no longer print every Telegram bot URL at INFO
level (which leaks the bot token in plain text and adds heartbeat noise).

Each logger's level is overridable via env var so we can flip back to
INFO/DEBUG without a redeploy:

    LOG_LEVEL_HTTPX     (default WARNING)
    LOG_LEVEL_HTTPCORE  (default WARNING)
    LOG_LEVEL_URLLIB3   (default WARNING)

Auto-configures on module import — just `import logging_config` once at
process startup, before any HTTP requests fire.

R19 — 2026-04-29
"""
from __future__ import annotations

import logging
import os

_LOGGERS_AND_ENV: tuple[tuple[str, str], ...] = (
    ("httpx", "LOG_LEVEL_HTTPX"),
    ("httpcore", "LOG_LEVEL_HTTPCORE"),
    ("urllib3", "LOG_LEVEL_URLLIB3"),
)


def _resolve_level(env_value: str | None, default: str = "WARNING") -> int:
    """Map an env-string ('INFO', 'warning', '20', '') to a logging level int.

    Falls back to WARNING on anything unparseable so a typo in Railway can
    never silence the whole bot.
    """
    if not env_value:
        env_value = default
    candidate = env_value.strip().upper()
    # Numeric override (e.g. '20')
    if candidate.isdigit():
        return int(candidate)
    level = getattr(logging, candidate, None)
    if isinstance(level, int):
        return level
    return logging.WARNING


def configure_logging() -> dict[str, int]:
    """Apply the per-logger levels and return the resolved mapping.

    Idempotent — safe to call multiple times.
    """
    applied: dict[str, int] = {}
    for logger_name, env_key in _LOGGERS_AND_ENV:
        level = _resolve_level(os.environ.get(env_key))
        logging.getLogger(logger_name).setLevel(level)
        applied[logger_name] = level
    return applied


# Auto-configure on first import (kill switch: set env var to INFO/DEBUG).
_APPLIED = configure_logging()


__all__ = ["configure_logging"]
