"""P0.3 — geopol bucket relevance routing.

Regression for the ISW/CTP "Iran/MENA" bucket being polluted with unrelated
headlines (Ivory Coast football, Kenya Ebola, US vote-a-rama) when the
canonical CTP feed 403s and the code falls back to a GENERAL wire feed.
Non-canonical feeds must be filtered to bucket keywords; a bucket with no
on-topic items is dropped, never mislabelled.
"""
from __future__ import annotations

from modules.intel30 import isw_ctp

MENA = "Geopol Iran/MENA"
RU = "Geopol Russia/Ukraine"
GENERAL = "https://www.aljazeera.com/xml/rss/all.xml"
CANONICAL = "https://www.criticalthreats.org/rss.xml"

NOISE = [
    {"title": "Ivory Coast qualify for the World Cup", "link": "", "date": ""},
    {"title": "Kenya reports first Ebola case of the year", "link": "", "date": ""},
    {"title": "US Senate holds marathon vote-a-rama", "link": "", "date": ""},
]
IRAN_ITEM = {"title": "Iran says nuclear talks have stalled", "link": "", "date": ""}


def test_general_feed_filters_out_unrelated_headlines():
    items = NOISE + [IRAN_ITEM]
    kept = isw_ctp._filter_relevant(MENA, items, GENERAL)
    assert kept == [IRAN_ITEM]


def test_general_feed_all_noise_yields_empty():
    kept = isw_ctp._filter_relevant(MENA, NOISE, GENERAL)
    assert kept == []


def test_canonical_feed_passes_through_unfiltered():
    # Canonical CTP is topical by construction — do not over-filter it.
    kept = isw_ctp._filter_relevant(MENA, NOISE + [IRAN_ITEM], CANONICAL)
    assert len(kept) == len(NOISE) + 1


def test_russia_bucket_keeps_ukraine_item_only():
    items = NOISE + [{"title": "Russian forces shell Kharkiv overnight", "link": "", "date": ""}]
    kept = isw_ctp._filter_relevant(RU, items, GENERAL)
    assert len(kept) == 1
    assert "Kharkiv" in kept[0]["title"]


def test_empty_bucket_is_dropped_not_mislabelled():
    # A bucket that resolved to no relevant items must NOT render under the
    # Iran/MENA heading nor print a raw error string.
    data = {"feeds": [
        {"label": MENA, "items": [], "source": None, "_error": "no_relevant@aljazeera"},
        {"label": RU, "items": [{"title": "Russia update", "link": "", "date": ""}],
         "source": CANONICAL, "_error": None},
    ]}
    out = isw_ctp.format_for_telegram(data)
    assert "Iran/MENA" not in out
    assert "no_relevant" not in out
    assert "Russia/Ukraine" in out
