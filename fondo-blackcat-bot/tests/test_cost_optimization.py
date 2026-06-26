"""R-COST (2026-06-26) — pin the /reporte cost-reduction contract.

Three structural levers cut the per-/reporte LLM bill without touching the
main FULL ANALYSIS quality:

1. ``tesis_update`` no longer routes to Sonnet — it is a STRUCTURED task
   (Haiku-first, Gemini fallback), because it only re-structures an
   already-written Sonnet report into JSON component statuses.
2. ``slim_x_intel_for_llm`` caps the X timeline to top-N by engagement and
   drops the duplicate by-username map before it enters the LLM context.
3. The thesis update receives only the compact deterministic prefix blocks,
   not the full raw JSON dump (asserted indirectly via the slim contract +
   the router tier here; the strip itself is exercised by analysis tests).

These tests must stay green so a future refactor cannot silently re-introduce
the second expensive Sonnet leg or the double-serialized X timeline.
"""
from __future__ import annotations

import json

from modules.llm_router import (
    TASK_TIER,
    TaskType,
    build_cached_system,
    _system_to_text,
    _cost_for,
    _anthropic_cache_usage,
)
from modules.x_intel import slim_x_intel_for_llm
from modules.intel_slim import slim_intel_for_llm


# ── Lever 1: tesis_update is no longer a CRITICAL (Sonnet) task ──────────────

def test_tesis_update_is_structured_not_critical():
    assert TASK_TIER["tesis_update"] == TaskType.STRUCTURED
    assert TASK_TIER["tesis_update"] != TaskType.CRITICAL


def test_main_report_still_critical_sonnet():
    # The actual analysis must NOT be downgraded.
    assert TASK_TIER["reporte"] == TaskType.CRITICAL
    assert TASK_TIER["tesis"] == TaskType.CRITICAL
    assert TASK_TIER["kill"] == TaskType.CRITICAL
    assert TASK_TIER["decision_query"] == TaskType.CRITICAL


# ── Lever 2: X timeline slimming ─────────────────────────────────────────────

def _fake_x_payload(n: int = 120) -> dict:
    tweets = []
    for i in range(n):
        tweets.append(
            {
                "username": f"acct{i}",
                "name": f"Name {i}",
                "text": "x" * 280,
                "created_at": "2026-06-26T00:00:00Z",
                "_engagement": n - i,  # already sorted desc
                "metrics": {
                    "like_count": n - i,
                    "retweet_count": 1,
                    "reply_count": 0,
                },
            }
        )
    by_user = {}
    for t in tweets:
        by_user.setdefault(t["username"], []).append(t)
    return {
        "status": "ok",
        "source": "x_api_list",
        "tweets": tweets,
        "data": by_user,            # the duplicate that must be dropped
        "accounts_scanned": n,      # legacy alias that must be dropped
        "accounts": n,
        "total": n,
        "hours": 48,
        "canonical_total": 185,
        "canonical_active": 90,
        "canonical_inactive": 95,
    }


def test_slim_caps_to_top_n():
    out = slim_x_intel_for_llm(_fake_x_payload(120), top_n=40)
    assert out["shown_to_llm"] == 40
    assert len(out["tweets"]) == 40
    # keeps the highest-engagement tweets (sorted desc → first 40)
    assert out["tweets"][0]["username"] == "acct0"
    assert out["total"] == 120  # original count preserved for context


def test_slim_drops_duplicate_by_user_map():
    out = slim_x_intel_for_llm(_fake_x_payload(50), top_n=40)
    assert "data" not in out
    assert "accounts_scanned" not in out
    assert "total_tweets" not in out
    assert out["_llm_slim"] is True


def test_slim_tweet_fields_are_lean():
    out = slim_x_intel_for_llm(_fake_x_payload(10), top_n=40)
    t = out["tweets"][0]
    assert set(t.keys()) == {
        "username", "name", "text", "created_at",
        "engagement", "likes", "retweets",
    }
    # the nested full metrics dict is gone
    assert "metrics" not in t


def test_slim_passes_through_non_ok_payloads():
    err = {"status": "error", "error": "ratelimit"}
    assert slim_x_intel_for_llm(err) is err
    assert slim_x_intel_for_llm(None) is None


def test_slim_handles_short_window_without_padding():
    out = slim_x_intel_for_llm(_fake_x_payload(7), top_n=40)
    assert out["shown_to_llm"] == 7
    assert len(out["tweets"]) == 7


# ── R-COST2 Lever 3: intel30 payload slimming ────────────────────────────────

def _fake_fred_payload() -> dict:
    """Real fred_api.format_for_telegram shape, with raw multi-row series."""
    return {
        "_global_error": None,
        "series": [
            {"id": "VIXCLS", "name": "VIX", "fecha": "2026-06-26", "valor": 14.21, "_error": None},
            {"id": "DGS10", "name": "10Y Treasury Yield (%)", "fecha": "2026-06-26", "valor": 4.273, "_error": None},
            {"id": "T10Y2Y", "name": "10Y-2Y Spread (%)", "fecha": "2026-06-26", "valor": 0.52, "_error": None},
            {"id": "DTWEXBGS", "name": "DXY (broad)", "fecha": "2026-06-26", "valor": 121.55, "_error": None},
            {"id": "SOFR", "name": "SOFR (%)", "fecha": "2026-06-26", "valor": 4.31, "_error": None},
            {"id": "FEDFUNDS", "name": "Fed Funds (%)", "fecha": "2026-06-26", "valor": 4.33, "_error": None},
        ],
    }


def _fake_merged_intel() -> dict:
    return {
        "x_intel": {"status": "ok", "tweets": [], "_llm_slim": True},
        "gmail_intel": {"status": "ok", "unread": 3},
        "intel30": {
            "fred": _fake_fred_payload(),
            "unknown_src": {"raw": [1, 2, 3], "huge": "z" * 500},
        },
    }


def test_slim_intel_replaces_known_source_with_digest():
    merged = _fake_merged_intel()
    out = slim_intel_for_llm(merged)
    fred_slim = out["intel30"]["fred"]
    # raw multi-row series gone, replaced by the compact telegram digest
    assert "series" not in fred_slim
    assert "_llm_digest" in fred_slim
    digest = fred_slim["_llm_digest"]
    # signal preserved: the curated macro levels still present
    assert "VIX" in digest and "14.21" in digest
    assert "SOFR" in digest and "Fed Funds" in digest


def test_slim_intel_keeps_unknown_source_raw():
    out = slim_intel_for_llm(_fake_merged_intel())
    # no module owns "unknown_src" → kept verbatim (graceful degrade)
    assert out["intel30"]["unknown_src"] == {"raw": [1, 2, 3], "huge": "z" * 500}
    assert out["intel30"]["_llm_slim"] is True
    assert "fred" in out["intel30"]["_llm_note"]


def test_slim_intel_passes_other_keys_through():
    merged = _fake_merged_intel()
    out = slim_intel_for_llm(merged)
    assert out["x_intel"] == merged["x_intel"]
    assert out["gmail_intel"] == merged["gmail_intel"]


def test_slim_intel_does_not_mutate_original():
    merged = _fake_merged_intel()
    _ = slim_intel_for_llm(merged)
    # original intel30.fred still carries its raw series untouched
    assert "series" in merged["intel30"]["fred"]
    assert "_llm_digest" not in merged["intel30"]["fred"]


def test_slim_intel_shrinks_payload():
    merged = _fake_merged_intel()
    out = slim_intel_for_llm(merged)
    raw_sz = len(json.dumps(merged["intel30"]["fred"], default=str))
    slim_sz = len(json.dumps(out["intel30"]["fred"], default=str))
    assert slim_sz < raw_sz


def test_slim_intel_passthrough_non_dict():
    assert slim_intel_for_llm(None) is None
    assert slim_intel_for_llm("x") == "x"


def test_slim_intel_no_intel30_key_is_safe():
    out = slim_intel_for_llm({"x_intel": {"a": 1}})
    assert out == {"x_intel": {"a": 1}}


# ── R-COST2 Lever 4: prompt caching (cache_control) wiring ───────────────────

def test_build_cached_system_marks_stable_prefix():
    blocks = build_cached_system("STABLE SYSTEM PROMPT", "volatile state")
    assert isinstance(blocks, list) and len(blocks) == 2
    assert blocks[0]["text"] == "STABLE SYSTEM PROMPT"
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    # the volatile suffix is NOT cached
    assert "cache_control" not in blocks[1]
    assert blocks[1]["text"] == "volatile state"


def test_build_cached_system_without_dynamic():
    blocks = build_cached_system("ONLY STABLE")
    assert len(blocks) == 1
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


def test_system_to_text_flattens_blocks_for_gemini():
    blocks = build_cached_system("AAA", "BBB")
    assert _system_to_text(blocks) == "AAA\nBBB"
    # plain strings pass through unchanged (legacy callers)
    assert _system_to_text("plain") == "plain"


def test_cost_for_accounts_for_cache_read_and_write():
    # 1M cached-read tokens at Sonnet input rate ($3) should bill ~10% = $0.30
    read_cost = _cost_for("claude-sonnet-4-6", 0, 0, cache_read=1_000_000)
    assert abs(read_cost - 0.30) < 1e-6
    # 1M cache-write tokens bill 1.25× = $3.75
    write_cost = _cost_for("claude-sonnet-4-6", 0, 0, cache_creation=1_000_000)
    assert abs(write_cost - 3.75) < 1e-6
    # plain input still bills full rate
    base = _cost_for("claude-sonnet-4-6", 1_000_000, 0)
    assert abs(base - 3.0) < 1e-6


def test_anthropic_cache_usage_reads_fields():
    class _U:
        cache_read_input_tokens = 4142
        cache_creation_input_tokens = 0

    class _R:
        usage = _U()

    assert _anthropic_cache_usage(_R()) == (4142, 0)

    class _NoUsage:
        pass

    assert _anthropic_cache_usage(_NoUsage()) == (0, 0)
