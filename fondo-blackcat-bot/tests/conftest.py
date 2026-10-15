"""Shared pytest fixtures for R-FINAL bug-fix modules.

Adds the project root (one level above ``tests/``) to ``sys.path`` so the
``auto.*`` package can be imported in tests without installing it.
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_http_host_cooldowns():
    """R-LIVE-FIX: el cooldown por host de utils.http es estado de proceso.
    Sin este reset, un 429 simulado en un test castigaria al host en el
    siguiente (todos usan https://x.test) y la suite dependeria del orden."""
    try:
        from utils import http as _uh
        _uh.reset_cooldowns()
    except Exception:  # noqa: BLE001
        pass
    # R-RONDA-OCT #2: hl_client keeps a per-type 5xx cooldown and a candle
    # cache of its own — same reasoning, same reset.
    try:
        import sys as _sys
        _hc = _sys.modules.get("modules.hl_client")
        if _hc is not None:
            _hc.reset_cooldowns()
            _hc.clear_candle_cache()
    except Exception:  # noqa: BLE001
        pass
    yield
    try:
        from utils import http as _uh
        _uh.reset_cooldowns()
    except Exception:  # noqa: BLE001
        pass
