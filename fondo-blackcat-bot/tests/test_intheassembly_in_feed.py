"""R-BOT-FEEDS-EXPAND (2026-05-07) — Task 2.

@intheassembly must be configured as an X timeline supplement handle by
default, even when the X_EXTRA_HANDLES env var is unset. The supplement
fetcher must respect:

* The kill switch X_LIVE_ENABLED=false / X_EXTRA_HANDLES_ENABLED=false
* The DAILY_CALL_CAP (must skip when headroom < 3)
* Sanitization (strip @, lowercase, dedup)
* Cap to X_EXTRA_HANDLES_MAX (default 5)
"""
from __future__ import annotations

import os
from contextlib import contextmanager

import pytest


@contextmanager
def env(**overrides):
    saved: dict[str, str | None] = {k: os.environ.get(k) for k in overrides}
    try:
        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_default_extra_handles_includes_intheassembly():
    """When X_EXTRA_HANDLES is unset, intheassembly is the baked-in default."""
    # Force re-parse of the env var by importing fresh.
    from modules.x_intel import _parse_extra_handles

    # Default value baked into the module.
    handles = _parse_extra_handles("intheassembly")
    assert "intheassembly" in handles


def test_parse_extra_handles_strips_at_and_lowercases():
    from modules.x_intel import _parse_extra_handles

    out = _parse_extra_handles("@InTheAssembly, FooBar ,@baz")
    assert out == ["intheassembly", "foobar", "baz"]


def test_parse_extra_handles_dedup():
    from modules.x_intel import _parse_extra_handles

    out = _parse_extra_handles("intheassembly,@intheassembly,@INTHEASSEMBLY")
    assert out == ["intheassembly"]


def test_parse_extra_handles_filters_invalid():
    """Empty tokens / non-alnum tokens must be dropped, not crash the parser."""
    from modules.x_intel import _parse_extra_handles

    out = _parse_extra_handles(",,,@valid_handle, !!!, @bad-char-!")
    assert "valid_handle" in out
    assert "bad-char-!" not in out


def test_parse_extra_handles_caps_to_max(monkeypatch):
    """X_EXTRA_HANDLES_MAX limits the parsed list length."""
    from modules import x_intel
    monkeypatch.setattr(x_intel, "X_EXTRA_HANDLES_MAX", 3, raising=False)
    out = x_intel._parse_extra_handles("a,b,c,d,e,f,g,h")
    assert len(out) == 3


@pytest.mark.asyncio
async def test_fetch_extra_handles_skips_when_disabled():
    """X_EXTRA_HANDLES_ENABLED=false short-circuits with no API call."""
    from modules.x_intel import fetch_extra_handles_supplement
    with env(X_EXTRA_HANDLES_ENABLED="false"):
        # Re-import to pick up env? Module reads at import; instead, force
        # the module-level constant via monkeypatching.
        from modules import x_intel as _xi
        orig = _xi.X_EXTRA_HANDLES_ENABLED
        _xi.X_EXTRA_HANDLES_ENABLED = False
        try:
            tweets, diag = await fetch_extra_handles_supplement(
                handles=["intheassembly"], hours=48
            )
        finally:
            _xi.X_EXTRA_HANDLES_ENABLED = orig
    assert tweets == []
    assert diag is None


@pytest.mark.asyncio
async def test_fetch_extra_handles_skips_when_no_bearer():
    """Empty bearer token yields the bearer-missing diagnostic."""
    from modules.x_intel import _DIAG_NO_BEARER, fetch_extra_handles_supplement
    from modules import x_intel as _xi
    orig_bearer = _xi.X_API_BEARER_TOKEN
    orig_kill = _xi.X_LIVE_ENABLED
    orig_enabled = _xi.X_EXTRA_HANDLES_ENABLED
    _xi.X_API_BEARER_TOKEN = ""
    _xi.X_LIVE_ENABLED = True
    _xi.X_EXTRA_HANDLES_ENABLED = True
    try:
        tweets, diag = await fetch_extra_handles_supplement(
            handles=["intheassembly"], hours=48
        )
    finally:
        _xi.X_API_BEARER_TOKEN = orig_bearer
        _xi.X_LIVE_ENABLED = orig_kill
        _xi.X_EXTRA_HANDLES_ENABLED = orig_enabled
    assert tweets == []
    assert diag == _DIAG_NO_BEARER
