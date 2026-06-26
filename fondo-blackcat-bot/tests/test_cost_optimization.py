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

from modules.llm_router import TASK_TIER, TaskType
from modules.x_intel import slim_x_intel_for_llm


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
