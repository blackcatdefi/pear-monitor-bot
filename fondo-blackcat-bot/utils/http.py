"""Async HTTP helper with retry/backoff."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


async def request_json(
    method: str,
    url: str,
    *,
    max_retries: int = 3,
    base_backoff: float = 2.0,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
    **kwargs: Any,
) -> Any:
    """Perform an HTTP request and return parsed JSON, with exponential backoff."""
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(method, url, **kwargs)
                resp.raise_for_status()
                return resp.json()
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            last_exc = exc
            wait = base_backoff * (2 ** attempt)
            log.warning(
                "HTTP %s %s failed (attempt %d/%d): %s — retrying in %ss",
                method, url, attempt + 1, max_retries, exc, wait,
            )
            await asyncio.sleep(wait)
    assert last_exc is not None
    raise last_exc


async def get_json(url: str, **kwargs: Any) -> Any:
    return await request_json("GET", url, **kwargs)


async def post_json(url: str, json_body: Any, **kwargs: Any) -> Any:
    return await request_json("POST", url, json=json_body, **kwargs)
