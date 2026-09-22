"""R-X-FLIP (2026-09-22) — the X feed went blind for 27 days. These tests pin
the four contracts that would have prevented it.

INCIDENT
--------
/reporte on 22-sep rendered posts from 26-aug under the header "X Timeline
(last 48h)". The live call had been returning HTTP 402 (official Console out
of credit) since then. R-BURN-CREDITS was supposed to burn the prepaid balance
and flip to twitterapi.io at the $0.50 floor; it never flipped, because the
flip was decided from a MODELLED balance and nobody ever read the 402 off the
wire.

CONTRACTS PINNED HERE
  1. A 402 (and a credits/spend-cap 403) latches the official backend as dead,
     durably, and the latch outranks EVERY other input — including an explicit
     X_FETCH_BACKEND=official.
  2. The flip takes effect in the SAME run: the fetch that discovers the
     depletion comes back with live posts from the provider, not a stale cache.
  3. The bot never advises topping up console.x.com or raising the spend cap.
  4. A timeline older than its window renders as DEGRADED with its real age,
     never under a "last 48h" header.

Every assertion here is mutation-checked by scripts/mutation_check_x_flip.py:
each contract is broken in the source on purpose and the run must go red.
"""
from __future__ import annotations

import json
import os
import re
import types
from datetime import datetime, timedelta, timezone

import httpx as _httpx_real
import pytest

from modules import x_provider as xp
from modules import x_staleness as xs

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _fake_httpx(client_cls):
    """A per-module httpx stand-in.

    x_intel and x_provider import the SAME httpx module object, so patching
    ``xi.httpx.AsyncClient`` also rewires the provider (and vice versa): the
    two transports become indistinguishable and a 402-on-official test would
    silently exercise the provider twice. Replacing the module *reference*
    inside each module keeps the two wires separate.
    """
    return types.SimpleNamespace(
        AsyncClient=client_cls,
        TimeoutException=_httpx_real.TimeoutException,
        HTTPError=_httpx_real.HTTPError,
        RequestError=_httpx_real.RequestError,
    )


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture()
def isolated_store(monkeypatch, tmp_path):
    """x_store + intel_memory pointed at a throwaway DB."""
    from modules import x_store, intel_memory
    db = str(tmp_path / "intel_memory.db")
    monkeypatch.setattr(x_store, "DB_PATH", db)
    monkeypatch.setattr(intel_memory, "DB_PATH", db, raising=False)
    monkeypatch.setenv("X_OFFICIAL_CREDITS_SINCE", "2026-08-12T00:00:00Z")
    xp.clear_official_depleted()
    return monkeypatch


def _fmt_provider_ts(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%a %b %d %H:%M:%S %z %Y"
    )


def _ptweet(tid: int, hours_ago: float, user: str = "acc1"):
    return {
        "id": str(tid), "text": f"t{tid}", "createdAt": _fmt_provider_ts(hours_ago),
        "author": {"userName": user, "name": user.upper(), "isBlueVerified": True},
        "likeCount": 1, "retweetCount": 0, "replyCount": 0, "quoteCount": 0,
    }


class _Resp:
    def __init__(self, payload, status=200, text=""):
        self._payload, self.status_code, self.text = payload, status, text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


# ═══ CONTRACT 1: observed depletion latches and outranks everything ═════════

def test_402_is_a_depletion_signal_and_403_alone_is_not():
    assert xp.is_depletion_response(402) is True
    assert xp.is_depletion_response(402, {}) is True
    # A bare 403 is a PERMISSIONS bug — different failure, different fix.
    assert xp.is_depletion_response(403, {"title": "Forbidden"}) is False
    # …but a 403 that names the spend cap / credits IS depletion.
    assert xp.is_depletion_response(403, {"title": "SpendCapReached"}) is True
    assert xp.is_depletion_response(403, {"type": "credits-exhausted"}) is True
    # Auth and rate limits never latch.
    assert xp.is_depletion_response(401) is False
    assert xp.is_depletion_response(429) is False
    assert xp.is_depletion_response(200) is False


def test_latch_survives_and_outranks_explicit_official_env(isolated_store):
    """THE regression. A stale X_FETCH_BACKEND=official must NOT be able to
    pin the bot to a backend that answers Payment Required."""
    isolated_store.setenv("X_PROVIDER_API_KEY", "k")
    isolated_store.setenv("X_FETCH_BACKEND", "official")
    assert xp.backend_selected() == "official"      # before the 402

    xp.mark_official_depleted(402, "credits depleted")

    assert xp.official_depleted() is True
    assert xp.backend_selected() == "twitterapi_io"  # env var overruled
    assert xp.provider_active() is True
    assert xp.backend_name() == "twitterapi_io"


def test_latch_outranks_a_modelled_balance_that_still_shows_money(isolated_store):
    """The exact 27-day failure: bookkeeping says $19 left, the wire says 402.
    The wire wins."""
    isolated_store.setenv("X_PROVIDER_API_KEY", "k")
    isolated_store.delenv("X_FETCH_BACKEND", raising=False)
    isolated_store.setenv("X_OFFICIAL_CREDITS_USD", "19")
    assert xp.official_credits_remaining() == pytest.approx(19.0)
    assert xp.backend_selected() == "official"

    xp.mark_official_depleted(402, "credits depleted")

    # The estimate is untouched and still wrong — and no longer decides.
    assert xp.official_credits_remaining() == pytest.approx(19.0)
    assert xp.backend_selected() == "twitterapi_io"


def test_latch_is_durable_across_a_process_restart(isolated_store):
    """Persisted in x_fetch_state on the Railway volume: a redeploy must not
    resurrect the dead backend."""
    isolated_store.setenv("X_PROVIDER_API_KEY", "k")
    xp.mark_official_depleted(402, "credits depleted")

    # Simulate a fresh process: nothing in memory, only the volume.
    from modules import x_store
    raw = x_store.get_state(xp.OFFICIAL_DEPLETED_KEY)
    assert raw, "the latch must be persisted, not process-local"
    assert json.loads(raw)["status"] == 402
    assert xp.official_depleted() is True


def test_latch_keeps_the_original_timestamp_when_re_latched(isolated_store):
    xp.mark_official_depleted(402, "first")
    first = xp.official_depleted_state()
    xp.mark_official_depleted(402, "second")
    assert xp.official_depleted_state()["ts"] == first["ts"]
    assert xp.official_depleted_state()["reason"] == "first"


def test_new_funding_epoch_clears_the_latch(isolated_store):
    """The ONLY escape hatch: the owner re-funds the Console and moves
    X_OFFICIAL_CREDITS_SINCE forward. Explicit owner action, never an accident."""
    isolated_store.setenv("X_PROVIDER_API_KEY", "k")
    xp.mark_official_depleted(402, "credits depleted")
    assert xp.official_depleted() is True

    isolated_store.setenv("X_OFFICIAL_CREDITS_SINCE", "2026-10-01T00:00:00Z")
    assert xp.official_depleted() is False
    isolated_store.setenv("X_FETCH_BACKEND", "official")
    assert xp.backend_selected() == "official"


@pytest.mark.asyncio
async def test_official_402_latches_from_the_wire(isolated_store, monkeypatch):
    """The official client itself must recognise the 402 — no caller can forget."""
    from modules import x_intel as xi

    monkeypatch.setattr(xi, "X_LIST_ID", "123")
    monkeypatch.setattr(xi, "X_API_BEARER_TOKEN", "bearer")
    monkeypatch.setattr(xi, "X_LIVE_ENABLED", True)
    monkeypatch.setattr(xi, "record_x_api_call", lambda *a, **k: None)

    class _Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return False
        async def get(self, *a, **k):
            return _Resp({"title": "PaymentRequired"}, status=402,
                         text="payment required")

    monkeypatch.setattr(xi, "httpx", _fake_httpx(_Client))
    assert xp.official_depleted() is False

    tweets, diag = await xi.fetch_timeline_via_list(hours=48, caller="t")

    assert tweets is None and "402" in diag
    assert xp.official_depleted() is True, (
        "a 402 on the wire must latch the official backend as depleted"
    )


# ═══ CONTRACT 2: the flip serves live posts in the SAME run ═════════════════

@pytest.mark.asyncio
async def test_402_flips_and_serves_fresh_posts_in_the_same_run(
    isolated_store, monkeypatch, tmp_path,
):
    """The run that discovers the depletion must come back with LIVE posts.
    Flipping only on the next /reporte is the 27-day blackout in miniature."""
    from modules import x_intel as xi, x_store

    monkeypatch.setenv("X_PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("X_LIST_ID", "2046698139873378486")
    monkeypatch.setenv("X_FETCH_BACKEND", "official")   # stale pin, as in prod
    monkeypatch.setenv("X_OFFICIAL_CREDITS_USD", "19")  # estimate still wrong
    monkeypatch.setattr(xi, "X_LIST_ID", "2046698139873378486")
    monkeypatch.setattr(xi, "X_API_BEARER_TOKEN", "bearer")
    monkeypatch.setattr(xi, "X_LIVE_ENABLED", True)
    monkeypatch.setattr(xi, "X_EXTRA_HANDLES", [])
    monkeypatch.setattr(xi, "record_x_api_call", lambda *a, **k: None)
    monkeypatch.setattr(xi, "save_x_timeline_payload", lambda *a, **k: None)
    monkeypatch.setattr(xp, "_record", lambda *a, **k: None)
    monkeypatch.setattr(xp, "_member_cache_path",
                        lambda: str(tmp_path / "members.json"))

    seen = {"official": 0, "provider": 0}

    class _OfficialClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return False
        async def get(self, *a, **k):
            seen["official"] += 1
            return _Resp({"title": "PaymentRequired"}, status=402, text="nope")

    class _ProviderClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return False
        async def get(self, url, params=None, headers=None):
            seen["provider"] += 1
            return _Resp({"tweets": [_ptweet(7001, 0.4), _ptweet(7002, 1.2)],
                          "has_next_page": False, "next_cursor": ""})

    monkeypatch.setattr(xi, "httpx", _fake_httpx(_OfficialClient))
    monkeypatch.setattr(xp, "httpx", _fake_httpx(_ProviderClient))

    payload = await xi.fetch_x_intel(hours=48, caller="test")

    assert seen["official"] == 1, "official tried once"
    assert seen["provider"] >= 1, "provider must serve in the same run"
    assert payload.get("live_error") is None, "this run is NOT degraded"
    assert payload.get("fetched_new") == 2
    assert payload.get("backend_switched") is True
    assert payload.get("backend") == "twitterapi_io"
    assert x_store.get_since_id() == "7002"
    assert xp.official_depleted() is True


@pytest.mark.asyncio
async def test_after_the_flip_no_official_call_is_ever_made_again(
    isolated_store, monkeypatch, tmp_path,
):
    from modules import x_intel as xi

    monkeypatch.setenv("X_PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("X_LIST_ID", "2046698139873378486")
    monkeypatch.setenv("X_FETCH_BACKEND", "official")
    monkeypatch.setattr(xi, "X_LIVE_ENABLED", True)
    monkeypatch.setattr(xi, "X_EXTRA_HANDLES", [])
    monkeypatch.setattr(xi, "save_x_timeline_payload", lambda *a, **k: None)
    monkeypatch.setattr(xp, "_record", lambda *a, **k: None)
    monkeypatch.setattr(xp, "_member_cache_path",
                        lambda: str(tmp_path / "members.json"))
    xp.mark_official_depleted(402, "credits depleted")   # already flipped

    async def _forbidden(*a, **k):
        raise AssertionError("official api.x.com must never be called again "
                             "once depletion has been observed")

    monkeypatch.setattr(xi, "fetch_timeline_via_list", _forbidden)

    class _ProviderClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return False
        async def get(self, url, params=None, headers=None):
            return _Resp({"tweets": [_ptweet(8001, 0.2)],
                          "has_next_page": False, "next_cursor": ""})

    monkeypatch.setattr(xp, "httpx", _fake_httpx(_ProviderClient))
    payload = await xi.fetch_x_intel(hours=48, caller="test")
    assert payload.get("fetched_new") == 1


@pytest.mark.asyncio
async def test_flip_without_a_provider_key_degrades_honestly(
    isolated_store, monkeypatch,
):
    """No key = no live fetch possible. The run must degrade with a visible
    error — never pretend, never invent a key."""
    from modules import x_intel as xi

    monkeypatch.delenv("X_PROVIDER_API_KEY", raising=False)
    monkeypatch.setattr(xi, "X_LIST_ID", "123")
    monkeypatch.setattr(xi, "X_API_BEARER_TOKEN", "bearer")
    monkeypatch.setattr(xi, "X_LIVE_ENABLED", True)
    monkeypatch.setattr(xi, "X_EXTRA_HANDLES", [])
    monkeypatch.setattr(xi, "record_x_api_call", lambda *a, **k: None)

    class _Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return False
        async def get(self, *a, **k):
            return _Resp({"title": "PaymentRequired"}, status=402, text="nope")

    monkeypatch.setattr(xi, "httpx", _fake_httpx(_Client))
    payload = await xi.fetch_x_intel(hours=48, caller="test")

    assert payload.get("live_error"), "the failure must be visible"
    assert xp.official_depleted() is True
    st = xp.backend_status()
    assert st["selected"] == "twitterapi_io"
    assert st["provider_key_set"] is False
    assert "X_PROVIDER_API_KEY" in st["faltantes"]
    assert "X_PROVIDER_API_KEY" in st["bloqueo"]


def test_backend_status_names_the_missing_key_instead_of_hiding_it(
    isolated_store, monkeypatch,
):
    monkeypatch.delenv("X_PROVIDER_API_KEY", raising=False)
    monkeypatch.setenv("X_LIST_ID", "2046698139873378486")
    monkeypatch.delenv("X_FETCH_BACKEND", raising=False)
    st = xp.backend_status()
    assert st["faltantes"] == ["X_PROVIDER_API_KEY"]
    assert st["list_id_set"] is True

    monkeypatch.setenv("X_PROVIDER_API_KEY", "k")
    monkeypatch.setenv("X_LIST_ID", "")
    st = xp.backend_status()
    assert st["faltantes"] == ["X_LIST_ID"]
    assert "X_LIST_ID" in st["bloqueo"]


# ═══ CONTRACT 3: no advice to keep paying the official API ══════════════════

_FORBIDDEN = (
    re.compile(r"console\.x\.com", re.I),
    re.compile(r"auto[- ]?recharge", re.I),
    re.compile(r"VISA\s*4463", re.I),
    re.compile(r"top[- ]?up at", re.I),
    re.compile(r"increase\s+spend\s+cap", re.I),
)


def _python_sources():
    skip = {".git", "__pycache__", ".pytest_cache", "tests"}
    # The mutation harness quotes the forbidden advice as the payload it
    # injects to prove this very test bites. It is test infrastructure, not
    # something the bot can ever say to BCD.
    skip_files = {os.path.join(_REPO, "scripts", "mutation_check_x_flip.py")}
    for root, dirs, files in os.walk(_REPO):
        dirs[:] = [d for d in dirs if d not in skip]
        for f in files:
            if f.endswith(".py") and os.path.join(root, f) not in skip_files:
                yield os.path.join(root, f)


def test_no_official_topup_advice_anywhere():
    """The fund LEFT the official API. No code path may tell the owner to
    recharge it or to raise its spend cap — not in a diagnostic, not in a
    template, not in a comment that a future edit could promote to a string."""
    offenders: list[str] = []
    for path in _python_sources():
        with open(path, "r", encoding="utf-8") as fh:
            body = fh.read()
        for pat in _FORBIDDEN:
            for m in pat.finditer(body):
                line = body[:m.start()].count("\n") + 1
                offenders.append(
                    f"{os.path.relpath(path, _REPO)}:{line}: {m.group(0)}"
                )
    assert not offenders, (
        "the bot must never advise recharging the official X API:\n"
        + "\n".join(offenders)
    )


def test_402_diagnostic_states_the_switch_not_a_payment():
    from modules import x_intel as xi
    d = xi._DIAG_402
    assert "402" in d
    assert "twitterapi.io" in d
    assert "console.x.com" not in d.lower()
    assert "top-up" not in d.lower()


# ═══ CONTRACT 4: an old timeline renders DEGRADED with its real age ═════════

def _payload_with_age(hours_ago: float, window: int = 48):
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    t = {"id": "1", "username": "acc1", "text": "x", "created_at": ts,
         "metrics": {"like_count": 1}}
    return {"status": "ok", "hours": window, "tweets": [t],
            "data": {"acc1": [t]}, "accounts_scanned": 1, "total_tweets": 1,
            "total": 1}


def test_fresh_timeline_keeps_the_48h_header():
    from templates.timeline import format_timeline
    out = format_timeline(_payload_with_age(3.0))
    assert "X Timeline (last 48h)" in out
    assert "DEGRADADO" not in out


def test_27_day_old_timeline_never_renders_under_a_48h_header():
    """THE incident: 26-aug posts printed as "last 48h" on 22-sep."""
    from templates.timeline import format_timeline
    out = format_timeline(_payload_with_age(27 * 24 + 5))
    assert "(last 48h)" not in out, "a 27-day-old feed cannot claim 48h"
    assert "DEGRADADO" in out
    assert "27d 5h" in out, "the banner must carry the REAL age"


def test_boundary_just_over_the_window_is_degraded():
    from templates.timeline import format_timeline
    assert "DEGRADADO" not in format_timeline(_payload_with_age(47.5))
    assert "DEGRADADO" in format_timeline(_payload_with_age(48.5))


def test_empty_timeline_is_degraded_not_healthy():
    st = xs.staleness({"status": "ok", "hours": 48, "tweets": [], "data": {}})
    assert st["degraded"] is True and st["empty"] is True


def test_age_text_is_human_and_exact():
    assert xs.format_age(0.5) == "30min"
    assert xs.format_age(3.5) == "3h 30min"
    assert xs.format_age(27 * 24 + 5) == "27d 5h"
    assert xs.format_age(None) == "n/d"


def test_staleness_reads_either_payload_shape():
    p = _payload_with_age(10.0)
    assert xs.age_hours(p) == pytest.approx(10.0, abs=0.05)
    by_user_only = {"hours": 48, "data": p["data"]}
    assert xs.age_hours(by_user_only) == pytest.approx(10.0, abs=0.05)


def test_report_banner_announces_the_degradation(isolated_store, monkeypatch):
    """/reporte's first line about X must carry the age, not just a fetch ts."""
    from modules import x_intel as xi
    monkeypatch.setattr(
        xi, "get_store_timeline_payload",
        lambda hours=48: _payload_with_age(27 * 24 + 5),
    )
    banner = xi.cache_banner_for_report()
    assert "DEGRADADO" in banner and "27d 5h" in banner

    monkeypatch.setattr(
        xi, "get_store_timeline_payload", lambda hours=48: _payload_with_age(2.0),
    )
    assert "DEGRADADO" not in xi.cache_banner_for_report()


def test_reporte_section_title_is_computed_from_the_data():
    """The /reporte section title must come from the data, not from a literal.

    This is the exact shape of the incident: the title said "48H" over posts
    from 26-aug. Behavioural, not a source scan — a source scan is green as
    soon as the right identifier appears SOMEWHERE nearby, which is not the
    same as the branch actually driving the title.
    """
    import bot

    fresh = bot.x_section_title(_payload_with_age(2.0), hours=48)
    assert fresh == "\U0001f4e1 X TIMELINE \u2014 48H"
    assert "DEGRADADO" not in fresh

    stale = bot.x_section_title(_payload_with_age(27 * 24 + 5), hours=48)
    assert "DEGRADADO" in stale
    assert "27d 5h" in stale, stale
    assert not stale.startswith("\U0001f4e1"), "a 27-day corpse is not a 48h feed"

    empty = bot.x_section_title({"tweets": []}, hours=48)
    assert "DEGRADADO" in empty and "sin posts" in empty


def test_reporte_uses_the_computed_title_for_both_headers():
    """Normal AND cache-fallback headers must be built from the same verdict —
    the 27-day render came out of the fallback path."""
    src = open(os.path.join(_REPO, "bot.py"), "r", encoding="utf-8").read()
    i = src.index("Section 1: X Timeline")
    block = src[i:i + 2500]
    assert "x_section_title(" in block
    assert '"\\U0001f4e1 X TIMELINE' not in block, (
        "no hardcoded fresh title may survive in the render path"
    )


# ─── Contract 5: no formatter may die on an unbound module name ─────────────
#
# This is the defect that KEPT the blackout invisible for 27 days. `x_store`
# was imported inside four functions of modules/x_intel.py and then read by
# eight more as if it were a module-level name. Every one of those raised
# NameError:
#   * /x_status, /costos_x, /intel_sources → dead commands (BCD saw the error
#     text, the X subsystem state was never readable);
#   * get_cached_timeline() and cache_banner_for_report() → the NameError was
#     caught by a bare `except`, so /reporte silently fell back to the legacy
#     mirror and lost the cache age. That fallback is literally what served
#     the 26-aug tweets under a "48h" header.
#
# A green suite never caught it because no test ever CALLED the formatters.
# These do.

def test_x_intel_binds_x_store_at_module_level():
    """The import must be module-level, not per-function.

    Pinning the import itself (and not only the behaviour below) is what stops
    the regression from coming back the next time someone adds a function that
    reads `x_store`.
    """
    import ast

    src = open(os.path.join(_REPO, "modules", "x_intel.py"), "r",
               encoding="utf-8").read()
    tree = ast.parse(src)
    top_level = set()
    for node in tree.body:  # module body ONLY — nested imports do not count
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                top_level.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                top_level.add((alias.asname or alias.name).split(".")[0])
    assert "x_store" in top_level, (
        "modules/x_intel.py reads x_store from eight functions that do not "
        "import it; without a module-level binding they all raise NameError"
    )


@pytest.mark.parametrize("fname", [
    "format_x_status",        # /x_status
    "format_x_costos",        # /costos_x
    "format_x_costs",
    "format_intel_sources",   # /intel_sources
    "debug_x_status",         # /debug_x
    "get_cached_timeline",
    "cache_banner_for_report",
])
def test_x_formatters_do_not_raise_nameerror(fname):
    """Every X formatter must actually RUN.

    NameError is asserted separately from the generic failure because the two
    have different meanings: a NameError is a missing import (this bug), while
    an OSError would just be a sandbox without the sqlite volume.
    """
    import asyncio

    from modules import x_intel as xi

    fn = getattr(xi, fname)
    try:
        if asyncio.iscoroutinefunction(fn):
            asyncio.get_event_loop_policy().new_event_loop().run_until_complete(fn())
        else:
            fn()
    except NameError as exc:  # the bug
        pytest.fail(f"{fname}() raised NameError: {exc}")
    except Exception:  # noqa: BLE001 — environment, not the contract
        pass


def test_cache_banner_survives_a_store_read_and_reports_age():
    """cache_banner_for_report() swallowed its own NameError and returned a
    banner with no age. A silent banner is how a 27-day corpse passed for live.
    """
    from modules import x_intel as xi

    banner = xi.cache_banner_for_report()
    assert isinstance(banner, str)
    assert "store read failed" not in banner.lower(), (
        "the banner is reporting its own crash instead of the cache age"
    )
