"""P1.9 — data-source health states + LMEC manual-input (/setlmec) state.

Optional-by-design sources (Arkham keyless, ASXN no-API, HypurrScan 404)
must read as OPTIONAL and never count as selftest failures. LMEC weekly TA
inputs that BCD has not entered must read as a clean AWAITING_BCD state, and
/setlmec values must override env/feeds.
"""
from __future__ import annotations

import os

import pytest

from modules import intel_selftest as st
from modules import lmec_state


@pytest.fixture(autouse=True)
def _clean_manual_lmec():
    """LMEC manual inputs persist to a single shared state file (config.DATA_DIR),
    so clear them before AND after every test to avoid cross-test pollution."""
    for k in lmec_state._MANUAL_KEYS:
        lmec_state.set_manual_input(k, None)
    yield
    for k in lmec_state._MANUAL_KEYS:
        lmec_state.set_manual_input(k, None)


def test_optional_sources_never_count_as_failures():
    """Solo lo que de verdad depende de una clave que el fondo no dio de alta."""
    for name, ge in (
        ("arkham_intel", "ARKHAM_API_KEY not set"),
        ("eia_oil", "EIA_API_KEY not set"),
    ):
        entry = st._classify(name, {"_global_error": ge}, 120)
        assert entry["status"] == "OPTIONAL"
        assert entry.get("optional") is True
        assert st._is_healthy(entry) is True


def test_asxn_y_hypurrscan_ya_no_pueden_esconderse_en_opcionales():
    """R-BOT-DEFINITIVE Fase 4.1 — el test que impide repetir el error.

    Las dos fuentes estuvieron meses en OPTIONAL_SOURCES con diagnosticos que
    sonaban definitivos ('no public API', 'endpoint retired upstream') y eran
    los dos falsos: ASXN tiene API en otro host (api-data.asxn.xyz) y
    HypurrScan solo renombro las rutas (estan en su openapi.json). Declararlas
    opcionales convirtio dos bugs nuestros en 'asi es la vida': fallaban todos
    los dias y el selftest daba verde igual.

    Si alguien vuelve a meterlas en la lista para que el selftest deje de
    quejarse, este test se rompe primero.
    """
    assert "asxn_data" not in st.OPTIONAL_SOURCES
    assert "hypurrscan" not in st.OPTIONAL_SOURCES
    for name in ("asxn_data", "hypurrscan"):
        entry = st._classify(name, {"_global_error": "http_404"}, 120)
        assert st._is_healthy(entry) is False, (
            f"{name} volvio a poder fallar en silencio")


def test_un_404_nunca_es_una_fuente_sana():
    """La segunda puerta trasera, independiente de OPTIONAL_SOURCES.

    _classify_inner mapeaba cualquier _global_error que contuviera '404',
    'html_only', 'moved' o 'spa_' a DEGRADED, y DEGRADED estaba en
    HEALTHY_STATUSES. Resultado: una fuente que no devolvio ni un byte
    contaba como sana. Habia DOS mecanismos tapando la misma falla, asi que
    sacar uno solo no alcanzaba.
    """
    for ge in ("http_404@/api/auctions", "html_only@dashboard",
               "spa_no_json", "endpoint moved"):
        entry = st._classify("fred_api", {"_global_error": ge}, 120)
        assert entry["status"] == "UNAVAILABLE", (
            f"'{ge}' volvio a clasificarse como {entry['status']}")
        assert st._is_healthy(entry) is False


def test_degraded_sigue_significando_trajo_datos_incompletos():
    """No se rompe la semantica: DEGRADED sigue existiendo y sigue contando
    como sano, pero solo para respuestas que SI trajeron algo."""
    assert "DEGRADED" in st.HEALTHY_STATUSES
    entry = st._classify("fred_api", {"series": [{"x": 1}], "_partial": "1 de 3"}, 50)
    assert st._is_healthy(entry) is True


def test_toda_fuente_opcional_declara_que_le_falta():
    """Una fuente solo puede ser opcional por FALTA DE CLAVE, y esa clave
    tiene que tener nombre. 'Opcional porque no anda' no es una categoria."""
    import modules.feed_registry as fr
    for name in st.OPTIONAL_SOURCES:
        f = fr.get(name)
        assert bool(f.env_var) or name == "arkham_intel", (
            f"{name} es opcional pero no declara que variable le falta; sin "
            f"eso 'opcional' es solo el lugar donde se esconde una fuente rota")


def test_required_source_failure_counts_against_total():
    entry = st._classify("fred_api", {"_global_error": "connection refused"}, 120)
    assert st._is_healthy(entry) is False


def test_live_source_is_healthy():
    entry = st._classify("fred_api", {"series": [{"x": 1}]}, 50)
    assert entry["status"] == "LIVE"
    assert st._is_healthy(entry) is True


def test_optional_import_fail_still_counts_as_failure():
    # An OPTIONAL source that won't even import is a real failure.
    entry = st._classify("arkham_intel", {}, 0)
    entry["status"] = "IMPORT_FAIL"
    assert st._is_healthy(entry) is False


def test_format_matrix_reports_healthy_tally():
    matrix = {
        "total": 3, "healthy": 3, "failures": [],
        "counts": {"LIVE": 2, "OPTIONAL": 1},
        "rows": [
            {"name": "fred_api", "status": "LIVE", "latency_ms": 10},
            {"name": "arkham_intel", "status": "OPTIONAL", "latency_ms": 10, "optional": True},
        ],
    }
    out = st.format_matrix(matrix)
    assert "sanos 3/3" in out
    assert "sin fuentes requeridas caídas" in out


# ── LMEC manual-input state ──────────────────────────────────────────────
def test_lmec_awaiting_state_when_no_inputs(monkeypatch):
    # No env, no manual, no autofeed → the three BCD-manual legs read clean.
    for k in ("LMEC_MACD_WEEKLY_POSITIVE", "LMEC_RSI_WEEKLY", "LMEC_MA50W_USD",
              "TRADERMAP_BTC_MACD", "TRADERMAP_BTC_RSI", "TRADERMAP_BTC_MA50W"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LMEC_AUTOFEED_ENABLED", "false")
    from modules import lmec_triggers
    res = lmec_triggers.evaluate_lmec_triggers(None)
    by_id = {c["id"]: c for c in res["conditions"]}
    assert by_id["macd_weekly_positive"]["status"] == "AWAITING_BCD"
    assert by_id["rsi_weekly_above_70"]["status"] == "AWAITING_BCD"
    assert "esperando" in by_id["macd_weekly_positive"]["detail"].lower()
    # The clean state must NOT print the raw "unset" env noise.
    assert "unset" not in by_id["rsi_weekly_above_70"]["detail"].lower()


def test_setlmec_value_overrides_into_valida(monkeypatch):
    monkeypatch.delenv("TRADERMAP_BTC_RSI", raising=False)
    from modules import lmec_triggers
    lmec_state.set_manual_input("rsi_weekly", 75.0)
    res = lmec_triggers.evaluate_lmec_triggers(None)
    rsi = next(c for c in res["conditions"] if c["id"] == "rsi_weekly_above_70")
    assert rsi["status"] == "VALIDA"
    assert "75" in rsi["detail"]


def test_setlmec_clear_returns_to_awaiting():
    lmec_state.set_manual_input("macd_weekly_positive", True)
    assert lmec_state.get_manual_inputs().get("macd_weekly_positive") is True
    lmec_state.set_manual_input("macd_weekly_positive", None)
    assert "macd_weekly_positive" not in lmec_state.get_manual_inputs()


def test_setlmec_rejects_unknown_key():
    try:
        lmec_state.set_manual_input("bogus", 1)
        assert False, "expected ValueError"
    except ValueError:
        pass
