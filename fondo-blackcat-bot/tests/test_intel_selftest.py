"""R-PERFECT Phase 3 #9 — intel_selftest classifier + matrix tests."""
from __future__ import annotations

import sys


def _import_with_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    import modules.intel_selftest as its  # noqa: WPS433
    importlib.reload(its)
    return its


def test_classify_live(tmp_path, monkeypatch):
    its = _import_with_tmp(tmp_path, monkeypatch)
    out = its._classify("foo", {"series": [{"valor": 1}]}, 100)
    assert out["status"] == "LIVE"


def test_classify_graceful_no_key(tmp_path, monkeypatch):
    its = _import_with_tmp(tmp_path, monkeypatch)
    out = its._classify("foo", {"_status": "GRACEFUL_NO_KEY"}, 50)
    assert out["status"] == "GRACEFUL_NO_KEY"


def test_una_fuente_que_no_trajo_nada_no_puede_quedar_en_estado_sano(
        tmp_path, monkeypatch):
    """R-BOT-DEFINITIVE Fase 4.1 — este test ANTES exigia lo contrario.

    Pedia que `spa_only_no_data` clasificara como DEGRADED, y DEGRADED esta
    dentro de HEALTHY_STATUSES. Es decir: el test estaba fijando la segunda
    puerta trasera por la que hypurrscan devolvia 404 todos los dias y el
    selftest seguia dando verde.

    DEGRADED significa "trajo datos, pero incompletos". Si hay _global_error
    no trajo NADA, y eso es UNAVAILABLE se llame como se llame el error. La
    unica excepcion es la falta de API key, que es una decision de config y no
    una falla de la fuente.
    """
    its = _import_with_tmp(tmp_path, monkeypatch)
    for err in ("spa_only_no_data", "html_only", "moved", "http_404@/api/auctions"):
        out = its._classify("foo", {"_global_error": err}, 50)
        assert out["status"] not in its.HEALTHY_STATUSES, (
            f"'{err}' quedo como estado sano ({out['status']}): una fuente que "
            f"no devolvio ni una fila esta contando como fuente que anda")
        assert out["status"] == "UNAVAILABLE"


def test_la_falta_de_api_key_si_se_distingue_de_una_fuente_caida(
        tmp_path, monkeypatch):
    """No tener key es una decision nuestra; que la fuente no responda no.
    Mezclarlas haria que configurar una key se viera igual que una caida."""
    its = _import_with_tmp(tmp_path, monkeypatch)
    out = its._classify("foo", {"_global_error": "EIA_API_KEY not set"}, 50)
    assert out["status"] == "GRACEFUL_NO_KEY"


def test_classify_unavailable(tmp_path, monkeypatch):
    its = _import_with_tmp(tmp_path, monkeypatch)
    out = its._classify("foo", {"_global_error": "connection refused"}, 5000)
    assert out["status"] == "UNAVAILABLE"


def test_classify_empty_when_no_rows(tmp_path, monkeypatch):
    its = _import_with_tmp(tmp_path, monkeypatch)
    out = its._classify("foo", {"series": []}, 10)
    assert out["status"] == "EMPTY"


def test_format_matrix_renders(tmp_path, monkeypatch):
    its = _import_with_tmp(tmp_path, monkeypatch)
    matrix = {
        "ts_utc": 0,
        "rows": [{"name": "fred_api", "status": "LIVE", "latency_ms": 100, "reason": "ok"}],
        "counts": {"LIVE": 1},
        "total": 1,
    }
    out = its.format_matrix(matrix)
    assert "Selftest" in out
    assert "fred_api" in out
    assert "LIVE" in out


def test_format_source_status_no_snapshot(tmp_path, monkeypatch):
    its = _import_with_tmp(tmp_path, monkeypatch)
    out = its.format_source_status()
    assert "no selftest snapshot" in out
