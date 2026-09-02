"""R-BOT-FINAL (2026-09-02) — un registro de salud que solo sabe anotar
fracasos no es un registro de salud.

QUE PASO EN PRODUCCION
======================
La ronda anterior instrumento 89 handlers del money path con
``health_registry.swallowed()`` y dio el trabajo por terminado. Pero nadie
llamaba nunca a ``mark_ok()``: en todo el codigo de produccion habia CERO
llamadas. Resultado, leido en /diagnostico contra el bot vivo: los 14
subsistemas decian "ultimo ok nunca" mientras el mismo reporte, tres bloques mas
abajo, mostraba "sync hace 4h" y "ultimo backup hace 18h".

O sea: el bot funcionaba y su panel de salud no tenia forma de enterarse.

Eso deja tres agujeros, y los tres son de la misma familia que el resto de la
ronda — una senal que parece informacion y no lo es:

  * ``stale_subsystems()`` devolvia SIEMPRE los 14, asi que "rancio" no
    distinguia nada.
  * un subsistema realmente muerto era indistinguible de uno impecable.
  * ``last_ok_utc`` — la pregunta que cinco rondas seguidas no se pudieron
    contestar, "¿cuando fue la ultima vez que esto anduvo?" — nunca se escribia.

LO QUE ESTE ARCHIVO CLAVA
=========================
1. El punto de entrada de cada subsistema del catalogo esta decorado, asi que un
   exito se registra solo. Sin esto el fix se pierde en el proximo refactor.
2. Un exito NO borra una degradacion ocurrida DENTRO de la misma operacion. Este
   es el punto delicado: si fetch_market_data() devuelve un dict pero adentro un
   swallowed() ya anoto que CoinGecko contesto 429, marcar ok al salir borraria
   exactamente el aviso que hay que dar. Seria un fix que reintroduce el bug
   original con mejor caligrafia.
3. Devolver ``{"ok": False}`` sin levantar tampoco es un exito. run_backup() no
   levanta nunca: reporta el fracaso en el valor de retorno.
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@pytest.fixture()
def hr(tmp_path, monkeypatch):
    """health_registry apuntado a una DB descartable."""
    from modules import health_registry as _hr
    monkeypatch.setattr(_hr, "DB_PATH", str(tmp_path / "intel_memory.db"))
    _hr._deg_seq.clear()
    _hr._self_error = 0
    return _hr


# ─── 1. los exitos se registran ─────────────────────────────────────────────

def test_una_operacion_exitosa_deja_last_ok(hr):
    @hr.tracked("market", "prueba")
    def corre():
        return {"btc": 1}

    assert hr.status("market").get("last_ok_utc") is None, "arranque sucio"
    corre()
    st = hr.status("market")
    assert st["ok"] is True
    assert st["last_ok_utc"], (
        "el subsistema corrio bien y el registro sigue diciendo 'nunca': es el "
        "bug de produccion exacto — 14 subsistemas en 'ultimo ok nunca' con el "
        "bot andando")
    assert hr.all_status()["subsystems"]["market"]["stale_hours"] is not None


async def test_tambien_registra_exitos_de_funciones_async(hr):
    @hr.tracked("portfolio", "prueba_async")
    async def corre():
        return []

    await corre()
    assert hr.status("portfolio")["last_ok_utc"]


def test_un_exito_limpia_una_degradacion_vieja(hr):
    hr.mark_degraded("funding", "se cayo ayer")
    assert hr.is_degraded("funding")

    @hr.tracked("funding", "prueba")
    def corre():
        return {"BTC": 0.001}

    corre()
    assert not hr.is_degraded("funding"), (
        "la salud es el estado actual, no un historial de agravios")


# ─── 2. un exito NO puede tapar una degradacion de la misma operacion ───────

def test_el_exito_no_borra_lo_que_se_trago_adentro(hr):
    """El caso que vuelve peligroso al fix si se hace ingenuamente."""

    @hr.tracked("market", "fetch_market_data")
    def corre():
        try:
            raise RuntimeError("coingecko 429")
        except RuntimeError:
            hr.swallowed("market", "precios")
        return {"btc": None}   # devolvio algo plausible, como siempre

    corre()
    st = hr.status("market")
    assert st["ok"] is False, (
        "la operacion 'termino bien' pero adentro se comio un 429 y devolvio "
        "precios incompletos; marcarla sana borra justo el aviso que hay que "
        "dar y reintroduce el bug con mejor caligrafia")
    assert "429" in st["detail"]


def test_una_excepcion_marca_degradado_y_sigue_viajando(hr):
    @hr.tracked("vault", "prueba")
    def corre():
        raise ValueError("hl caida")

    with pytest.raises(ValueError):
        corre()
    assert hr.is_degraded("vault"), "el decorador tiene que anotar el fracaso"
    assert "hl caida" in hr.status("vault")["detail"]


async def test_una_excepcion_async_tampoco_se_traga(hr):
    @hr.tracked("ledger", "prueba")
    async def corre():
        raise KeyError("fills")

    with pytest.raises(KeyError):
        await corre()
    assert hr.is_degraded("ledger")


# ─── 3. ok=False devuelto tampoco es exito ──────────────────────────────────

def test_devolver_ok_false_sin_levantar_no_cuenta_como_exito(hr):
    """run_backup() no levanta: informa el fracaso en el valor de retorno."""

    @hr.tracked("backup", "run_backup")
    def corre():
        return {"ok": False, "reason": "tar_fail: disco lleno"}

    corre()
    st = hr.status("backup")
    assert st["ok"] is False, (
        "el backup fallo y lo dijo en el return; tomar eso como exito seria "
        "exactamente el error que este registro existe para cazar")
    assert "disco lleno" in st["detail"]


def test_devolver_ok_true_si_cuenta(hr):
    @hr.tracked("backup", "run_backup")
    def corre():
        return {"ok": True, "tarball": "x.tar.gz"}

    corre()
    assert hr.status("backup")["ok"] is True


# ─── 4. la cobertura del catalogo no se pierde en el proximo refactor ───────

def test_todo_subsistema_del_catalogo_tiene_quien_le_registre_exitos():
    """Guarda estructural: si alguien agrega un subsistema al catalogo y no
    instrumenta su punto de entrada, vuelve el 'ultimo ok nunca' para ese
    subsistema y nadie se entera hasta leer /diagnostico en produccion."""
    import re
    from modules.health_registry import SUBSYSTEMS

    decorados: set[str] = set()
    for base in ("modules", "auto", "utils"):
        d = os.path.join(_ROOT, base)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.endswith(".py"):
                continue
            with open(os.path.join(d, fn), "r", encoding="utf-8") as fh:
                for m in re.finditer(r"@health_registry\.tracked\(\s*[\"'](\w+)[\"']",
                                     fh.read()):
                    decorados.add(m.group(1))

    faltan = sorted(set(SUBSYSTEMS) - decorados)
    assert not faltan, (
        f"subsistemas sin ningun punto de entrada instrumentado: {faltan}. "
        "Van a reportar 'ultimo ok nunca' para siempre, aunque funcionen "
        "perfecto — que es el bug que este archivo cierra.")

    sobran = sorted(decorados - set(SUBSYSTEMS))
    assert not sobran, (
        f"hay tracked() con nombres fuera del catalogo: {sobran}; no van a "
        "aparecer etiquetados en /diagnostico")
