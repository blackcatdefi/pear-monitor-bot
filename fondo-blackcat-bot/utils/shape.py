"""Validacion de la FORMA de una respuesta HTTP antes de creerle.

POR QUE EXISTE ESTE MODULO (clase C4 de R-BOT-DEFINITIVE)
----------------------------------------------------------
El codigo defensivo de este repo estaba escrito contra el error equivocado.
Protegia contra "la peticion fallo" — timeouts, 5xx, conexion cortada — que es
el caso facil, porque levanta una excepcion y se ve. No protegia contra el caso
que de verdad hace dano: **la peticion respondio 200 con un JSON de otra
forma**.

Ese es el bug que convirtio un 429 de ``userFunding`` en funding 0.00 en todas
las patas del fondo. La API contesto 200 con un objeto de error en vez de la
lista esperada; el codigo hizo ``data.get("...", [])``, recibio ``[]``, y el
llamador lo leyo como "no hubo funding en esta ventana". Ninguna excepcion,
ninguna linea de log, un numero equivocado en el reporte.

El patron peligroso, en abstracto::

    items = (data or {}).get("data", [])   # 200 con {"error": ...} -> []
    if not items:
        return {}                          # "no hay datos" (mentira)

La forma vacia y la forma invalida se vuelven indistinguibles. Y son cosas
opuestas: una es informacion, la otra es la ausencia de informacion.

QUE HACEN ESTAS FUNCIONES
-------------------------
Separan los tres casos que el ``.get(k, [])`` juntaba en uno:

    1. forma correcta con datos     -> se devuelven
    2. forma correcta y vacia       -> se devuelve vacio (es una respuesta
                                       valida: de verdad no hubo nada)
    3. forma incorrecta             -> ``UnexpectedShape``, que es un FALLO

El caso 3 se levanta a proposito. En los modulos del money path el llamador ya
tiene un handler instrumentado con ``health_registry.swallowed()``, asi que la
excepcion se convierte sola en "subsistema degradado" visible en /health, en
/diagnostico y como banner en el reporte. No hace falta tocar el flujo: alcanza
con dejar de mentir sobre la forma.
"""
from __future__ import annotations

from typing import Any

__all__ = ["UnexpectedShape", "require_mapping", "require_list", "dig"]


class UnexpectedShape(ValueError):
    """La respuesta llego, pero no tiene la forma que el codigo asume."""


def _preview(data: Any) -> str:
    """Fragmento util para el log sin volcar la respuesta entera.

    Se prioriza el mensaje de error de la API: cuando un 200 trae
    ``{"error": "rate limited"}`` esa cadena es TODA la explicacion, y perderla
    obliga a adivinar. Si no hay mensaje, se muestran las claves presentes, que
    es lo siguiente mas util para entender que llego en vez de lo esperado.
    """
    if isinstance(data, dict):
        for k in ("error", "message", "msg", "detail", "status"):
            v = data.get(k)
            if v:
                return f"{k}={str(v)[:160]}"
        return f"claves={sorted(data)[:8]}"
    return f"{type(data).__name__}={str(data)[:120]}"


def require_mapping(data: Any, *, source: str) -> dict:
    """Exige un objeto JSON. Un dict vacio es valido; otra cosa no."""
    if isinstance(data, dict):
        return data
    raise UnexpectedShape(
        f"{source}: se esperaba un objeto JSON y llego "
        f"{type(data).__name__} ({_preview(data)})")


def require_list(data: Any, *, source: str) -> list:
    """Exige una lista. Una lista vacia es valida; otra cosa no."""
    if isinstance(data, list):
        return data
    raise UnexpectedShape(
        f"{source}: se esperaba una lista y llego "
        f"{type(data).__name__} ({_preview(data)})")


def dig(data: Any, key: str, *, source: str, expect: type = dict) -> Any:
    """Baja un nivel exigiendo que la clave EXISTA y tenga el tipo esperado.

    Reemplaza directamente al ``(data or {}).get(key, [])``. La diferencia es
    que la clave ausente y la clave presente-pero-vacia dejan de ser lo mismo:

        dig(payload, "data", source="coingecko/global")   # falta 'data' -> raise
        payload["data"] == {}                             # presente y vacio -> {}
    """
    obj = require_mapping(data, source=source)
    if key not in obj:
        raise UnexpectedShape(
            f"{source}: falta la clave '{key}' en la respuesta "
            f"({_preview(obj)})")
    val = obj[key]
    if val is None or not isinstance(val, expect):
        raise UnexpectedShape(
            f"{source}: '{key}' es {type(val).__name__} y se esperaba "
            f"{expect.__name__} ({_preview(obj)})")
    return val
