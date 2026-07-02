"""R-BOT-DEFINITIVE WI-1 — catalysts engine tests."""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def cats(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import config
    importlib.reload(config)
    import modules.catalysts as catalysts
    importlib.reload(catalysts)
    return catalysts


def test_add_list_delete_roundtrip(cats):
    cid = cats.add_catalyst("2026-06-20", "Test Event", time_utc="14:00", impact="high")
    assert cid is not None
    rows = cats.list_catalysts(now=datetime(2026, 6, 10, tzinfo=timezone.utc))
    assert any(c.name == "Test Event" and c.date_utc == "2026-06-20" for c in rows)
    assert cats.delete_catalyst(cid) is True
    rows = cats.list_catalysts(now=datetime(2026, 6, 10, tzinfo=timezone.utc))
    assert not any(c.name == "Test Event" for c in rows)


def test_add_is_upsert_no_duplicates(cats):
    a = cats.add_catalyst("2026-06-20", "Dup Event", impact="low")
    b = cats.add_catalyst("2026-06-20", "Dup Event", impact="critical")
    assert a == b
    rows = [c for c in cats.list_catalysts(now=datetime(2026, 6, 10, tzinfo=timezone.utc))
            if c.name == "Dup Event"]
    assert len(rows) == 1
    assert rows[0].impact == "critical"  # updated, not duplicated


def test_invalid_date_rejected(cats):
    assert cats.add_catalyst("20-06-2026", "Bad") is None
    assert cats.add_catalyst("2026-06-20", "Bad Time", time_utc="25:99") is None


def test_seed_contains_ticket_events_and_fomc(cats):
    n = cats.seed_catalysts()
    assert n >= 4
    now = datetime(2026, 6, 9, tzinfo=timezone.utc)
    names = {(c.date_utc, c.name) for c in cats.list_catalysts(now=now, limit=60)}
    assert ("2026-06-10", "US CPI") in names
    assert ("2026-06-11", "US PPI") in names
    assert any(d == "2026-06-12" and "SpaceX" in n for d, n in names)
    # Official June FOMC = 2026-06-16/17, decision day 17, WITH dot plot.
    assert ("2026-06-17", "FOMC decision + dot plots (SEP)") in names
    # Idempotent re-seed: no duplicates.
    cats.seed_catalysts()
    rows = [c for c in cats.list_catalysts(now=now, limit=80) if c.name == "US CPI"]
    assert len(rows) == 1


def test_next_catalyst_candidates_window(cats):
    cats.seed_catalysts()
    now = datetime(2026, 6, 9, 18, 0, tzinfo=timezone.utc)
    cands = cats.next_catalyst_candidates(window_hours=72, now=now)
    labels = [c["label"] for c in cands]
    assert "US CPI" in labels          # 2026-06-10 12:30 — inside 72h
    assert "US PPI" in labels          # 2026-06-11 — inside
    assert not any("FOMC" in l for l in labels)  # 06-17 — outside 72h
    # All candidates carry the header contract keys.
    for c in cands:
        assert {"label", "dt", "emoji", "rank"} <= set(c)


def test_header_merges_catalysts_table(cats, monkeypatch):
    """The DESTACADO 'NEXT CATALYST <72h' line reads from the engine table."""
    cats.seed_catalysts()
    import templates.formatters as fmt
    importlib.reload(fmt)
    now = datetime(2026, 6, 9, 18, 0, tzinfo=timezone.utc)
    line = fmt._next_catalyst_for_header(window_hours=72, unlocks=None, now=now)
    assert "ninguno" not in line
    assert "US CPI" in line


def test_header_truly_empty_window(cats):
    import templates.formatters as fmt
    importlib.reload(fmt)
    # Far future date with nothing seeded in window.
    now = datetime(2027, 3, 1, tzinfo=timezone.utc)
    line = fmt._next_catalyst_for_header(window_hours=72, unlocks=None, now=now)
    assert "ninguno" in line


def test_llm_block_deterministic(cats):
    cats.seed_catalysts()
    now = datetime(2026, 6, 9, 18, 0, tzinfo=timezone.utc)
    block = cats.build_llm_catalyst_block(days=7, now=now)
    assert "US CPI" in block and "2026-06-10" in block
    assert "PROHIBIDO" in block  # never from model memory
    # 7-day window from 06-09 includes the 06-16/17 FOMC.
    assert "FOMC" in block


def test_llm_block_empty_is_explicit(cats):
    now = datetime(2027, 5, 1, tzinfo=timezone.utc)
    block = cats.build_llm_catalyst_block(days=7, now=now)
    assert "sin catalysts registrados" in block


def test_handle_setcatalyst_add_del_list(cats):
    # Date must stay in the FUTURE relative to "now": /setcatalyst list
    # filters past events, so a hardcoded date rots. Use now+30d.
    from datetime import timedelta
    fut = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
    out = cats.handle_setcatalyst(
        ["add", fut, "13:30", "Evento", "Manual", "critical"]
    )
    assert "✅" in out and fut in out and "Evento Manual" in out
    listing = cats.handle_setcatalyst(["list"])
    assert "Evento Manual" in listing
    import re
    m = re.search(r"#(\d+) " + re.escape(fut), listing)
    assert m
    out_del = cats.handle_setcatalyst(["del", m.group(1)])
    assert "eliminado" in out_del
    assert "Evento Manual" not in cats.handle_setcatalyst(["list"])


def test_handle_setcatalyst_bad_input(cats):
    assert "Uso" in cats.handle_setcatalyst([])
    assert "inválida" in cats.handle_setcatalyst(["add", "junk", "X"])
    assert "Uso" in cats.handle_setcatalyst(["del", "abc"])


def test_compile_raw_data_injects_catalysts_and_rules(cats, monkeypatch):
    cats.seed_catalysts()
    import templates.formatters as fmt
    importlib.reload(fmt)
    out = fmt.compile_raw_data([], [], {}, None, None)
    assert "REGLAS DURAS DEL FONDO" in out          # WI-8 injection
    assert "CATALYSTS PRÓXIMOS 7 DÍAS" in out        # WI-1 injection
    assert "RAW DATA" in out
