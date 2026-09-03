"""R-RAILWAY-VARS (2026-09-02) — el bloque de autoactualizacion decia que
faltaba algo que nadie tenia que cargar, y no decia que faltaba lo que si.

LOS TRES DEFECTOS QUE ESTE ARCHIVO CIERRA
=========================================

1. EL "Y/O" ERA UNA CONJETURA CON FORMATO DE HALLAZGO.
   La linea era "falta GITHUB_TOKEN y/o GITHUB_REPO". El veredicto se calculaba
   con ``token and repo``, pero el MENSAJE se elegia mirando solo el token. Con
   el token puesto y el repo ausente la salida quedaba en "❌ push a GitHub
   (via GITHUB_TOKEN)": una cruz roja al lado de una variable presente, sin
   nombrar nada que faltara. Y con el token ausente nombraba GITHUB_REPO
   estuviera o no. En los dos casos el texto afirmaba mas de lo que sabia.

2. GITHUB_REPO NO LO CONSUME NINGUN CAMINO DE PUSH.
   Se leia en un solo lugar de todo el repo: el chequeo que lo exigia. El push
   de backups usa GITHUB_BACKUP_REPO; el del reconciler usa el remoto `origin`
   del propio clon. O sea que cargar GITHUB_REPO no habilitaba nada, y NO
   cargarlo mantenia la cruz roja para siempre. Consecuencia practica y cara:
   poner el token solo, que es lo unico que de verdad hace falta, NO habria
   puesto la linea en verde — y el diagnostico habria echado la culpa a otro
   lado.

3. EL REDEPLOY MOSTRABA UNA CRUZ ROJA PERMANENTE POR ALGO OPCIONAL.
   RAILWAY_TOKEN solo sirve para forzar un redeploy sin push. Todos los deploys
   reales de las ultimas rondas entraron por push a master, sin ningun token.
   Una falla que nunca fue una falla entrena a ignorar el panel entero.
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
    for v in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT", "BOT_GITHUB_PAT",
              "GITHUB_REPO", "RAILWAY_TOKEN", "RAILWAY_API_TOKEN",
              "RAILWAY_GIT_COMMIT_SHA"):
        monkeypatch.delenv(v, raising=False)


# ─── 1. el motivo nombra exactamente lo que falta ───────────────────────────

def test_sin_token_nombra_el_token_y_nada_mas(diag):
    out = diag._b_autoactualizacion()
    assert out["puede_pushear"] is False
    assert out["falta"] == ["GITHUB_TOKEN"], (
        "el unico faltante real es el token; nombrar tambien GITHUB_REPO manda "
        "a cargar una variable que ningun camino de push lee")


def test_con_el_token_puesto_el_push_queda_habilitado(diag, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "no-importa-el-valor")
    out = diag._b_autoactualizacion()
    assert out["puede_pushear"] is True, (
        "con el token cargado el push tiene que quedar verde. Si siguiera "
        "exigiendo GITHUB_REPO, pegar el token —lo unico que de verdad hace "
        "falta— no cambiaria nada y el panel echaria la culpa a otro lado")
    assert out["falta"] == []


def test_el_mensaje_no_puede_contradecir_al_veredicto(diag, monkeypatch):
    """El defecto exacto: cruz roja al lado de una variable presente."""
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    d = {"autoactualizacion": diag._b_autoactualizacion()}
    linea = next(l for l in diag.format_diagnosis(d).split("\n")
                 if "push a GitHub" in l)
    assert "y/o" not in linea, "volvio la conjetura impresa como hallazgo"
    if linea.lstrip().startswith("\u274c"):
        pytest.fail(f"veredicto rojo con el token presente: {linea!r}")


def test_sin_token_la_linea_dice_que_variable_cargar(diag):
    d = {"autoactualizacion": diag._b_autoactualizacion()}
    linea = next(l for l in diag.format_diagnosis(d).split("\n")
                 if "push a GitHub" in l)
    assert "GITHUB_TOKEN" in linea and "GITHUB_REPO" not in linea, (
        f"la linea tiene que nombrar solo lo que falta de verdad: {linea!r}")


# ─── 2. el repo destino siempre se conoce ───────────────────────────────────

def test_el_repo_se_conoce_aun_sin_la_variable(diag):
    out = diag._b_autoactualizacion()
    assert out["repo"], (
        "el destino del push es la identidad del repo, no una decision de "
        "entorno: no puede quedar en None y bloquear el push")
    assert out["repo"].count("/") == 1
    assert out["repo_origen"] in ("origin", "default")


def test_la_variable_gana_si_esta_puesta(diag, monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", "otro/destino")
    out = diag._b_autoactualizacion()
    assert (out["repo"], out["repo_origen"]) == ("otro/destino", "GITHUB_REPO")


@pytest.mark.parametrize("url,esperado", [
    ("https://github.com/blackcatdefi/pear-monitor-bot.git",
     "blackcatdefi/pear-monitor-bot"),
    ("git@github.com:blackcatdefi/pear-monitor-bot.git",
     "blackcatdefi/pear-monitor-bot"),
    ("https://x-access-token:ghp_SECRETO@github.com/blackcatdefi/pear-monitor-bot.git",
     "blackcatdefi/pear-monitor-bot"),
])
def test_parseo_del_remoto(diag, monkeypatch, url, esperado):
    import subprocess

    class _R:
        stdout = url
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    assert diag._repo_desde_origin() == esperado


@pytest.mark.parametrize("url", [
    # Armado mal hecho: el userinfo quedo DESPUES del host. Es el unico caso
    # que discrimina, y por eso es el que se testea. Con la URL bien armada el
    # split por "github.com/" ya descarta el token solo, asi que un test con
    # ese formato pasaria haya o no haya guarda: seria verde y no protegeria
    # nada. Aca el pedazo de la derecha es
    # "x-access-token:ghp_...@owner/repo": tiene UNA sola '/', o sea que la
    # unica condicion que existia antes (count("/") == 1) lo daba por bueno y
    # devolvia el PAT a /diagnostico y a los logs.
    "https://github.com/x-access-token:ghp_ESTO_NO_SALE@owner/repo.git",
    "https://github.com/ghp_ESTO_NO_SALE@owner/repo",
    "ssh://github.com:ghp_ESTO_NO_SALE@owner/repo.git",
])
def test_un_remoto_mal_armado_nunca_filtra_el_token(diag, monkeypatch, url):
    """Preferimos no saber el repo a publicar un secreto.

    El destino del push tiene un default seguro, asi que devolver None no
    rompe nada: el bloque cae en REPO_POR_DEFECTO. Filtrar el PAT, en cambio,
    no tiene vuelta atras — sale por /diagnostico y queda en los logs.
    """
    import subprocess

    class _R:
        stdout = url
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())

    out = diag._repo_desde_origin()
    assert out is None, (
        f"un candidato con credenciales adentro tiene que descartarse entero, "
        f"no publicarse como si fuera owner/repo: {out!r}")
    assert "ghp_ESTO_NO_SALE" not in repr(diag._b_autoactualizacion())


def test_el_remoto_bien_armado_con_token_da_el_repo_limpio(diag, monkeypatch):
    """El formato que usamos de verdad: token adelante del host, repo detras."""
    import subprocess

    class _R:
        stdout = "https://x-access-token:ghp_ESTO_NO_SALE@github.com/o/r.git"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())

    assert diag._repo_desde_origin() == "o/r"
    assert "ghp_ESTO_NO_SALE" not in repr(diag._b_autoactualizacion())


def test_un_remoto_ilegible_no_rompe_el_bloque(diag, monkeypatch):
    import subprocess

    def _boom(*a, **k):
        raise OSError("no hay git")
    monkeypatch.setattr(subprocess, "run", _boom)
    out = diag._b_autoactualizacion()
    assert out["repo"] == diag.REPO_POR_DEFECTO
    assert out["repo_origen"] == "default"


# ─── 3. el redeploy automatico no es una falla ──────────────────────────────

def test_el_autodeploy_observado_cuenta_como_redeploy(diag, monkeypatch):
    """RAILWAY_GIT_COMMIT_SHA lo inyecta Railway solo si ella construyo este
    deploy desde un push: es evidencia, no suposicion."""
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "bde59e9")
    out = diag._b_autoactualizacion()
    assert out["autodeploy_por_push"] is True
    assert out["puede_redeployar"] is True, (
        "todos los deploys reales entraron por push y sin RAILWAY_TOKEN; "
        "pintar eso de rojo es una falla que nunca fue una falla, y entrena a "
        "ignorar el panel entero")

    d = {"autoactualizacion": out}
    linea = next(l for l in diag.format_diagnosis(d).split("\n")
                 if "redeploy en Railway" in l)
    assert "automatico por push" in linea


def test_sin_token_y_sin_evidencia_el_redeploy_no_se_declara_sano(diag):
    out = diag._b_autoactualizacion()
    assert out["puede_redeployar"] is False, (
        "sin RAILWAY_TOKEN y sin ninguna senal de que el auto-deploy exista, "
        "declararlo sano seria inventar la capacidad")


def test_el_token_de_railway_gana_si_esta(diag, monkeypatch):
    monkeypatch.setenv("RAILWAY_TOKEN", "x")
    out = diag._b_autoactualizacion()
    assert out["railway_token_var"] == "RAILWAY_TOKEN"
    assert out["puede_redeployar"] is True


# ─── 4. la regla de siempre ─────────────────────────────────────────────────

def test_ningun_valor_de_secreto_sale_jamas(diag, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secretito_que_no_debe_salir")
    monkeypatch.setenv("RAILWAY_TOKEN", "rw_tampoco_este")
    out = diag._b_autoactualizacion()
    plano = repr(out) + diag.format_diagnosis({"autoactualizacion": out})
    assert "ghp_secretito_que_no_debe_salir" not in plano
    assert "rw_tampoco_este" not in plano
    assert out["github_token_var"] == "GITHUB_TOKEN"
