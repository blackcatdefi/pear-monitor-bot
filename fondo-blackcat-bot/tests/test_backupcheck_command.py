"""R-BOT-FINAL (2026-09-02) — /backupcheck: el ciclo backup+restauracion
deja de ocurrir una sola vez por dia y siempre en pasado.

QUE PROBLEMA CIERRA
===================
El ciclo completo — comprimir, restaurar en un temporal, correr
integrity_check y comparar los conteos de filas contra las DBs vivas — solo
corria dentro del cron de las 04:00 UTC. La pregunta "¿el backup de anoche
sirve?" tenia respuesta una vez al dia, y siempre sobre algo que ya habia
pasado.

Eso mordio en esta misma ronda. Se arreglo el backup que se reportaba exitoso
sin tablas adentro, se puso el fix en produccion, y no habia forma de mirar el
resultado hasta la madrugada siguiente. Un fix de backups que no se puede
observar hasta 5 horas despues no esta terminado.

LAS DOS COSAS QUE ESTE ARCHIVO CLAVA
====================================
1. /backupcheck corre EXACTAMENTE `run_backup` y despues `verify_latest`, las
   mismas dos funciones que el cron. Un camino de verificacion propio, "de
   prueba", no verificaria el camino real: verificaria a si mismo.
2. Si el backup FALLA, no se verifica el tarball viejo. Restaurar el de ayer
   contestaria "✅ restaurable" — una verdad sobre un archivo que nadie
   pregunto, exactamente en el momento en que el usuario cree estar leyendo el
   estado del backup de hoy.
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class _Msg:
    def __init__(self) -> None:
        self.textos: list[str] = []

    async def reply_text(self, texto, *a, **kw):
        self.textos.append(str(texto))
        return None


class _Update:
    def __init__(self) -> None:
        self.message = _Msg()
        self.effective_user = type("U", (), {"id": 1, "username": "bcd"})()
        self.effective_chat = type("C", (), {"id": 1})()


@pytest.fixture()
def entorno(monkeypatch):
    """bot.py con el envio de mensajes capturado y la autorizacion abierta."""
    import bot as _bot
    from modules import backup_verify as _bv
    from modules import backup_volume as _bvol

    enviados: list[str] = []

    async def _send(update, texto, **kw):
        enviados.append(str(texto))

    monkeypatch.setattr(_bot, "send_long_message", _send)
    return _bot, _bvol, _bv, enviados


def _handler(bot):
    """El handler sin las capas @authorized / @with_error_logging."""
    fn = bot.cmd_backupcheck
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


# ─── 1. registro ────────────────────────────────────────────────────────────

def test_el_comando_esta_registrado_y_apunta_a_un_handler_real():
    import bot
    from commands_registry import COMMANDS
    cmd = next((c for c in COMMANDS if c.command == "backupcheck"), None)
    assert cmd is not None, "/backupcheck no figura en el registro de comandos"
    assert hasattr(bot, cmd.handler_name)
    assert bot.HANDLER_MAP.get("backupcheck") is not None, (
        "declarado en el registro pero sin handler: en Telegram el comando "
        "aparece en el autocompletado y despues no hace nada")


# ─── 2. corre el camino real, no uno paralelo ───────────────────────────────

async def test_usa_las_mismas_dos_funciones_que_el_cron(entorno, monkeypatch):
    bot, bvol, bv, enviados = entorno
    llamadas: list[str] = []

    monkeypatch.setattr(bvol, "run_backup",
                        lambda: (llamadas.append("run_backup"),
                                 {"ok": True, "tarball": "b.tar.gz",
                                  "size_bytes": 3 * 1024 * 1024})[1])
    monkeypatch.setattr(bv, "verify_latest",
                        lambda: (llamadas.append("verify_latest"),
                                 {"ok": True})[1])
    monkeypatch.setattr(bv, "format_for_telegram",
                        lambda: "1,000 filas restauradas vs 1,000 vivas")

    up = _Update()
    await _handler(bot)(up, None)

    assert llamadas == ["run_backup", "verify_latest"], (
        "el comando tiene que correr el mismo par de funciones que el cron de "
        f"las 04:00, y en ese orden; corrio {llamadas}. Un camino propio de "
        "verificacion solo se verifica a si mismo")
    assert any("filas restauradas" in t for t in enviados), (
        "el comando existe para publicar los conteos: sin ellos vuelve a ser "
        "'15 DBs restauradas', que es lo que tambien diria restaurar 15 "
        "sqlites vacias")


async def test_publica_el_veredicto_de_restaurabilidad(entorno, monkeypatch):
    bot, bvol, bv, enviados = entorno
    monkeypatch.setattr(bvol, "run_backup",
                        lambda: {"ok": True, "tarball": "b.tar.gz",
                                 "size_bytes": 1024})
    monkeypatch.setattr(bv, "verify_latest", lambda: {"ok": False})
    monkeypatch.setattr(bv, "format_for_telegram", lambda: "detalle")

    await _handler(bot)(_Update(), None)
    assert any("NO restaurable" in t for t in enviados), (
        "la verificacion fallo y la salida no lo dice en el veredicto")


# ─── 3. un backup fallido NO se tapa verificando el tarball de ayer ─────────

async def test_si_el_backup_falla_no_se_verifica_el_tarball_viejo(
        entorno, monkeypatch):
    """El caso que volveria inutil al comando si se hiciera de forma ingenua."""
    bot, bvol, bv, enviados = entorno
    verificaciones: list[str] = []

    monkeypatch.setattr(bvol, "run_backup",
                        lambda: {"ok": False, "reason": "tar_fail: disco lleno"})
    monkeypatch.setattr(bv, "verify_latest",
                        lambda: (verificaciones.append("x"), {"ok": True})[1])
    monkeypatch.setattr(bv, "format_for_telegram",
                        lambda: "\u2705 hace 19h, todo perfecto")

    await _handler(bot)(_Update(), None)

    assert not verificaciones, (
        "el backup de HOY fallo y se verifico el tarball de AYER: contestaria "
        "'restaurable ✅' — una verdad sobre un archivo que nadie pregunto, "
        "justo cuando el usuario cree estar leyendo el estado de hoy")
    junto = "\n".join(enviados)
    assert "FALLO" in junto and "disco lleno" in junto, (
        "si el backup fallo, el motivo tiene que salir en el mensaje")
    assert "perfecto" not in junto, (
        "se filtro la verificacion vieja en la respuesta de un backup fallido")


# ─── 4. ok=True no significa "todas las DBs entraron sanas" ─────────────────

async def test_una_db_sin_snapshot_consistente_se_avisa(entorno, monkeypatch):
    bot, bvol, bv, enviados = entorno
    monkeypatch.setattr(bvol, "run_backup",
                        lambda: {"ok": True, "tarball": "b.tar.gz",
                                 "size_bytes": 2048,
                                 "db_snapshot_fallos": ["intel_memory.db: locked"]})
    monkeypatch.setattr(bv, "verify_latest", lambda: {"ok": True})
    monkeypatch.setattr(bv, "format_for_telegram", lambda: "detalle")

    await _handler(bot)(_Update(), None)
    junto = "\n".join(enviados)
    assert "intel_memory.db" in junto and "sin snapshot consistente" in junto, (
        "run_backup devuelve ok=True apenas el tarball existe, aunque alguna "
        "DB haya entrado cruda. Callarlo hace que el resto de la salida se lea "
        "como un exito completo cuando no lo es")
