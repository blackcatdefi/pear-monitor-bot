"""R-BOT-FINAL (2026-09-02) — /health y /diagnostico no pueden contradecirse
sobre cuanto hace que el proceso esta vivo.

El bug que cierra este archivo se vio en produccion: sobre el MISMO proceso,
/diagnostico reportaba "up 21h" y /health reportaba "Uptime proceso: 0m", y seis
minutos despues "6m". El numero de /health no era el uptime del bot: era el
tiempo transcurrido desde la PRIMERA invocacion de /health.

La causa era una linea que parecia inofensiva:

    _PROCESS_START = time.monotonic()   # a nivel de modulo, en heartbeat.py

Un reloj capturado al importar un modulo solo mide desde el boot si el modulo se
importa en el boot. bot.py importa heartbeat DENTRO de cmd_health (import
perezoso), asi que el cronometro arrancaba cuando alguien escribia /health.

Por que importa mas de lo que parece: un uptime de 0m es exactamente el sintoma
de un crash-loop. Este bug lo fabricaba cuando no existia y, peor, lo habria
tapado si existiera — /health siempre habria dicho "recien arranque",
estuviera el bot reiniciandose en loop o llevara tres semanas arriba. Un numero
plausible que nadie cuestiona es peor que un error.

La regla que queda clavada aca: hay UN solo reloj de boot,
modules.version_info.START_TIME, fijado a nivel de modulo en un modulo que bot.py
si importa en el boot. Todo comando que publique uptime lo deriva de ahi. Si el
numero llegara a estar mal, va a estar mal en los tres comandos a la vez, que es
la unica forma de que se note.
"""
from __future__ import annotations

import os
import re
import sys
import time

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from modules import heartbeat  # noqa: E402
from modules import version_info  # noqa: E402


def test_health_lee_el_reloj_de_boot_y_no_uno_propio(monkeypatch):
    """Con el proceso arriba hace 21h, /health tiene que decir 21h.

    Este es el test que le falla al codigo viejo: con _PROCESS_START capturado
    en el import de heartbeat, mover START_TIME no cambiaba nada y la funcion
    devolvia "0m" igual.
    """
    monkeypatch.setattr(version_info, "START_TIME", time.time() - 21 * 3600 - 60)
    out = heartbeat._uptime_str()
    assert out == "21h 1m", (
        f"/health dijo {out!r} para un proceso que lleva 21h arriba: no esta "
        "leyendo el reloj de boot, esta midiendo desde su propio import")


def test_los_dos_comandos_no_pueden_dar_numeros_distintos(monkeypatch):
    """Misma funcion detras de /health y de /version: no hay dos verdades."""
    for horas in (0, 3, 49):
        monkeypatch.setattr(version_info, "START_TIME", time.time() - horas * 3600)
        assert heartbeat._uptime_str() == version_info.format_uptime(), (
            f"con {horas}h de uptime /health y /version discrepan")


def test_heartbeat_no_vuelve_a_capturar_un_reloj_al_importarse():
    """Guarda estructural: que nadie reponga el cronometro a nivel de modulo.

    No alcanza con que el valor de hoy sea correcto — la forma del bug era una
    sola linea de asignacion en el cuerpo del modulo, y volver a escribirla es
    trivial. Se prohibe la forma, no solo el resultado.
    """
    assert not hasattr(heartbeat, "_PROCESS_START"), (
        "volvio el cronometro propio de heartbeat; el uptime tiene que salir "
        "de version_info.START_TIME")

    src = (heartbeat.__file__ or "")
    with open(src, "r", encoding="utf-8") as fh:
        cuerpo = fh.read()
    # Solo lineas de codigo: los comentarios de este round NOMBRAN el patron
    # prohibido a proposito para explicarlo, y no deben disparar la guarda.
    codigo = "\n".join(
        ln for ln in cuerpo.splitlines() if not ln.lstrip().startswith("#"))
    assert not re.search(r"^\s*\w+\s*=\s*time\.(monotonic|time)\(\)", codigo,
                         re.M), (
        "hay un reloj capturado a nivel de modulo en heartbeat.py: si el modulo "
        "se importa perezoso, ese reloj mide desde el primer comando, no desde "
        "el boot")


def test_el_boot_clock_vive_en_un_modulo_que_bot_importa_en_el_boot():
    """La regla anterior solo sirve si version_info SI se importa temprano.

    Si algun dia alguien hace perezoso tambien el import de version_info, el
    reloj unico se corrompe igual que el anterior y este archivo entero dejaria
    de proteger nada. Asi que se verifica el import, no la intencion.
    """
    with open(os.path.join(_ROOT, "bot.py"), "r", encoding="utf-8") as fh:
        bot_src = fh.read()
    top = []
    for ln in bot_src.splitlines():
        if ln and not ln[0].isspace():
            top.append(ln)
    assert any(re.match(r"(from|import)\s+modules\.version_info\b", ln)
               or re.match(r"from\s+modules\.version_info\s+import\b", ln)
               for ln in top), (
        "version_info dejo de importarse a nivel de modulo en bot.py: "
        "START_TIME ya no marca el boot y el uptime vuelve a ser una mentira")
