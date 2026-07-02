"""R-BOT-DEFINITIVE-2 T3 — deterministic Telegram/Gmail intel render tests.

Mission spec coverage: normal render, empty feeds, telethon_disabled error
dict, gmail error dict, truncation. Plus tier grouping, OTROS dedup vs tiers,
media/link stripping and never-raises guarantees.
"""
from __future__ import annotations

from modules.intel_render import (
    _clean,
    format_gmail_intel_block,
    format_telegram_intel_block,
)


def _legacy_ok():
    return {
        "status": "ok",
        "data": {
            "tier1": [
                {
                    "channel": "Tree of Alpha",
                    "messages": [
                        {"text": "BTC breaks 100k https://t.co/abc",
                         "date": "2026-07-02T10:00:00+00:00"},
                        {"text": "[photo] chart update",
                         "date": "2026-07-02T11:00:00+00:00"},
                        {"text": "ETH ETF inflows accelerate",
                         "date": "2026-07-02T12:00:00+00:00"},
                        {"text": "older msg 4", "date": "2026-07-02T08:00:00+00:00"},
                    ],
                },
            ],
            "tier2": [],
            "tier3": [
                {"channel": "Whale Alert", "messages": [
                    {"text": "1000 BTC moved to Binance",
                     "date": "2026-07-02T09:30:00+00:00"},
                ]},
            ],
        },
    }


def _unread_ok():
    return {
        "status": "ok",
        "data": [
            # handle IN a tier → must be deduped out of OTROS
            {"channel": "Tree of Alpha", "handle": "@treeofalpha", "messages": [
                {"text": "dup msg", "date": "2026-07-02T10:00:00+00:00"},
            ]},
            {"channel": "Random Chan", "handle": "@randomchan", "messages": [
                {"text": "geopolitical escalation update",
                 "date": "2026-07-02T07:00:00+00:00"},
            ]},
        ],
    }


def _patch_channels(monkeypatch):
    import config
    monkeypatch.setattr(config, "CHANNELS", {
        "tier1": [{"name": "Tree of Alpha", "handle": "@treeofalpha"}],
        "tier2": [],
        "tier3": [{"name": "Whale Alert", "handle": "@whale_alert_io"}],
    }, raising=False)


# ─── Telegram: normal render ─────────────────────────────────────────────────
def test_telegram_normal_render_tiers_and_otros(monkeypatch):
    _patch_channels(monkeypatch)
    out = format_telegram_intel_block(_legacy_ok(), _unread_ok())
    assert "📨 TELEGRAM INTEL — 24H" in out
    assert "▪️ TIER 1" in out and "▪️ TIER 3" in out
    assert "▪️ TIER 2" not in out                      # empty tier hidden
    assert "Tree of Alpha (4 msgs)" in out
    assert "▪️ OTROS (unread scan)" in out
    assert "Random Chan" in out
    # tier-covered handle deduped from OTROS:
    assert out.count("Tree of Alpha") == 1
    # totals: 4 + 1 (tier) + 1 (otros, dup skipped from render but counted) = 6
    assert "Totales:" in out and "canales" in out


def test_telegram_max_3_msgs_newest_first(monkeypatch):
    _patch_channels(monkeypatch)
    out = format_telegram_intel_block(_legacy_ok(), {"status": "ok", "data": []})
    # 4 messages in channel but only 3 rendered; oldest ("older msg 4") dropped.
    assert "older msg 4" not in out
    assert "ETH ETF inflows accelerate" in out
    # link stripped:
    assert "https://" not in out
    # media placeholder stripped but caption kept:
    assert "[photo]" not in out and "chart update" in out


# ─── Telegram: error / empty paths ───────────────────────────────────────────
def test_telegram_both_telethon_disabled():
    err = {"status": "error", "error": "telethon_disabled"}
    out = format_telegram_intel_block(err, err)
    assert "n/d — telethon_disabled" in out
    assert "feeds Telegram no disponibles" in out


def test_telegram_partial_error_renders_warning(monkeypatch):
    _patch_channels(monkeypatch)
    out = format_telegram_intel_block(
        {"status": "error", "error": "flood_wait"}, _unread_ok())
    assert "⚠️ tiers n/d (flood_wait)" in out
    assert "Random Chan" in out


def test_telegram_empty_feeds():
    out = format_telegram_intel_block(
        {"status": "ok", "data": {}}, {"status": "ok", "data": []})
    assert "Sin mensajes nuevos en las últimas 24h." in out


def test_telegram_never_raises_on_garbage():
    assert isinstance(format_telegram_intel_block(None, None), str)
    assert isinstance(format_telegram_intel_block("x", 42), str)


# ─── Gmail ───────────────────────────────────────────────────────────────────
def test_gmail_normal_render_and_totals():
    gmail = {"status": "ok", "emails": [
        {"from": "alerts@hyperliquid.xyz", "subject": "Margin call warning",
         "snippet": "Your position is approaching the maintenance threshold",
         "date": "Thu, 2 Jul 2026 10:00:00"},
        {"from": "news@defillama.com", "subject": "",
         "snippet": "", "date": ""},
    ]}
    out = format_gmail_intel_block(gmail)
    assert "📧 GMAIL INTEL" in out
    assert "alerts@hyperliquid.xyz — Margin call warning" in out
    assert "approaching the maintenance threshold" in out
    assert "(sin asunto)" in out
    assert "Totales: 2 procesados · 2 archivados" in out


def test_gmail_zero_unread():
    assert "Sin emails sin leer." in format_gmail_intel_block(
        {"status": "ok", "emails": []})


def test_gmail_error_dict():
    out = format_gmail_intel_block(
        {"status": "error", "error": "imap auth failed"})
    assert "n/d — imap auth failed" in out


def test_gmail_never_raises_on_garbage():
    assert isinstance(format_gmail_intel_block(None), str)
    assert isinstance(format_gmail_intel_block([1, 2]), str)


# ─── Truncation ──────────────────────────────────────────────────────────────
def test_clean_truncates_at_limit():
    long = "palabra " * 100
    out = _clean(long, 200)
    assert len(out) <= 200
    assert out.endswith("…")


def test_telegram_message_truncated_at_200(monkeypatch):
    _patch_channels(monkeypatch)
    legacy = {"status": "ok", "data": {"tier1": [
        {"channel": "Tree of Alpha", "messages": [
            {"text": "x" * 500, "date": "2026-07-02T10:00:00+00:00"},
        ]}], "tier2": [], "tier3": []}}
    out = format_telegram_intel_block(legacy, {"status": "ok", "data": []})
    line = next(l for l in out.split("\n") if "xxx" in l)
    body = line.split("] ", 1)[1] if "] " in line else line.split("· ", 1)[1]
    assert len(body) <= 200 and body.endswith("…")


def test_gmail_snippet_truncated_at_150():
    gmail = {"status": "ok", "emails": [
        {"from": "a@b.c", "subject": "s", "snippet": "y" * 400, "date": ""}]}
    out = format_gmail_intel_block(gmail)
    snippet_line = next(l for l in out.split("\n") if l.strip().startswith("y"))
    assert len(snippet_line.strip()) <= 150
