"""R-RAILWAY-VARS (2026-09-02) — inventario de claves del servicio.

POR QUE EXISTE ESTE BLOQUE
==========================

Sin credencial de Railway no hay forma de abrir el panel de Variables ni de
leerlo por API. Durante cinco rondas seguidas la pregunta "¿que claves estan
cargadas en el servicio?" se contesto de dos maneras, las dos malas: por
conjetura, o leyendo un deploy_history escrito meses antes. La respuesta de
primera mano existe en un solo lugar —adentro del proceso que corre en
Railway— y hasta ahora ese lugar no la publicaba.

LA REGLA QUE NO SE NEGOCIA
==========================

Se publica el NOMBRE y un booleano. El valor no se lee, no se corta, no se
hashea, no se muestra ni en parte. Un prefijo tambien es un secreto: `ghp_`
mas cuatro caracteres ya es informacion que un atacante no tenia. Los tests de
abajo tratan esto como el invariante central, no como un detalle.
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@pytest.fixture()
def diag():
    from modules import diagnostics as _d
    return _d


@pytest.fixture(autouse=True)
def _entorno_limpio(monkeypatch):
    for v in ("GITHUB_TOKEN", "GITHUB_REPO", "FRED_API_KEY", "ARKHAM_API_KEY"):
        monkeypatch.delenv(v, raising=False)


# ─── 1. el inventario contesta la pregunta que motivo el bloque ─────────────

def test_estan_las_cuatro_que_importan(diag):
    nombres = [k["nombre"] for k in diag._b_claves()["claves"]]
    assert nombres == ["GITHUB_TOKEN", "GITHUB_REPO",
                       "FRED_API_KEY", "ARKHAM_API_KEY"], (
        "el inventario existe para contestar por las cuatro claves del "
        "servicio; si se cae una, la pregunta vuelve a contestarse por "
        "conjetura justo para esa")


def test_ausente_es_ausente_y_presente_es_presente(diag, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "lo-que-sea")
    est = {k["nombre"]: k["presente"] for k in diag._b_claves()["claves"]}
    assert est == {"GITHUB_TOKEN": False, "GITHUB_REPO": False,
                   "FRED_API_KEY": True, "ARKHAM_API_KEY": False}


def test_una_variable_vacia_cuenta_como_ausente(diag, monkeypatch):
    """Crear la variable y dejarla en blanco es el error de tipeo tipico.

    Un ✅ ahi mandaria a buscar el problema a cualquier otro lado durante horas,
    que es exactamente el costo que este bloque existe para evitar.
    """
    monkeypatch.setenv("ARKHAM_API_KEY", "   ")
    est = {k["nombre"]: k["presente"] for k in diag._b_claves()["claves"]}
    assert est["ARKHAM_API_KEY"] is False


# ─── 2. la regla que no se negocia ─────────────────────────────────────────

def test_ningun_valor_sale_jamas_ni_en_parte(diag, monkeypatch):
    secretos = {
        "GITHUB_TOKEN": "ghp_AAAAAAAAAAAAAAAAAAAA",
        "GITHUB_REPO": "duenio-secreto/repo-secreto",
        "FRED_API_KEY": "fred_BBBBBBBBBBBBBBBBBBBB",
        "ARKHAM_API_KEY": "ark_CCCCCCCCCCCCCCCCCCCC",
    }
    for k, v in secretos.items():
        monkeypatch.setenv(k, v)

    out = diag._b_claves()
    plano = repr(out) + diag.format_diagnosis({"claves": out})

    for k, v in secretos.items():
        assert v not in plano, f"salio el valor entero de {k}"
        # Un prefijo tambien es un secreto: cuatro caracteres del PAT ya son
        # informacion que antes no estaba publicada.
        assert v[:8] not in plano, (
            f"salio un prefijo del valor de {k}: mostrar 'ghp_AAAA…' se siente "
            f"seguro y no lo es")


def test_el_bloque_no_lee_el_valor_mas_alla_de_saber_si_esta(diag, monkeypatch):
    """Guarda estructural: cada clave aporta exactamente nombre, para y un
    booleano. Si algun dia alguien agrega "primeros_4" o "longitud", este test
    lo frena antes del deploy — no despues, mirando los logs."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_lo_que_sea")
    for k in diag._b_claves()["claves"]:
        assert set(k) == {"nombre", "para", "presente"}
        assert isinstance(k["presente"], bool)


# ─── 3. se ve en el reporte, que es donde se lee ────────────────────────────

def test_la_linea_nombra_la_clave_y_su_veredicto(diag, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    texto = diag.format_diagnosis({"claves": diag._b_claves()})
    lineas = {l.split("—")[0].strip(): l for l in texto.split("\n")
              if "API_KEY" in l or "GITHUB_" in l}

    tok = next(l for k, l in lineas.items() if "GITHUB_TOKEN" in k)
    fred = next(l for k, l in lineas.items() if "FRED_API_KEY" in k)
    assert tok.lstrip().startswith("\u2705"), (
        f"la clave presente tiene que verse presente: {tok!r}")
    assert fred.lstrip().startswith("\u274c"), (
        f"la clave ausente tiene que verse ausente: {fred!r}")


def test_el_bloque_esta_registrado_y_entra_en_el_diagnostico(diag):
    assert diag.BLOQUES["claves"] is diag._b_claves, (
        "un bloque que no esta en BLOQUES no corre nunca: el inventario "
        "quedaria escrito y sin publicar, que es igual a no existir")
