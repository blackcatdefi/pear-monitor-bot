"""Round 16: aiohttp /health endpoint for Railway probes.

Bound to PORT env var (Railway web service) or 8080 by default. Safe to start
on workers too — Railway just won't route external traffic to it. Internal
healthchecks still work.

Exposes:
    GET /health       — JSON status
    GET /             — alias of /health
"""
from __future__ import annotations

import logging
import os
from typing import Optional

try:
    from aiohttp import web
    AIOHTTP_OK = True
except Exception:  # noqa: BLE001
    AIOHTTP_OK = False

from modules.version_info import health_payload

log = logging.getLogger(__name__)

_runner: Optional["web.AppRunner"] = None


async def health_handler(request) -> "web.Response":  # type: ignore[name-defined]
    from commands_registry import COMMANDS  # local import → up-to-date count
    return web.json_response(health_payload(commands_count=len(COMMANDS)))


async def start_health_server(port: Optional[int] = None) -> None:
    """Start the health server on PORT env or fallback. Idempotent."""
    global _runner
    if not AIOHTTP_OK:
        log.warning("aiohttp not available — health server NOT started")
        return
    if _runner is not None:
        log.info("health server already running")
        return

    bind_port = port
    if bind_port is None:
        env_port = os.getenv("PORT", "").strip()
        bind_port = int(env_port) if env_port.isdigit() else 8080

    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_get("/", health_handler)

    # Round 17: optional dashboard at /dashboard?token=XXX
    try:
        from modules.dashboard import dashboard_handler  # local import to avoid cycle
        app.router.add_get("/dashboard", dashboard_handler)
        log.info("dashboard route mounted at /dashboard")
    except Exception:  # noqa: BLE001
        log.exception("dashboard mount failed (non-fatal)")

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", bind_port)
    try:
        await site.start()
        _runner = runner
        log.info("✅ health server listening on 0.0.0.0:%s/health", bind_port)
    except OSError as exc:
        log.warning("health server could not bind to %s: %s", bind_port, exc)


async def stop_health_server() -> None:
    global _runner
    if _runner is not None:
        try:
            await _runner.cleanup()
        except Exception:  # noqa: BLE001
            log.exception("health server cleanup failed")
        _runner = None
