"""Normalizacion canonica de timestamps guardados como TEXT en SQLite.

POR QUE EXISTE ESTE MODULO
--------------------------
SQLite no tiene tipo fecha. Todo timestamp es TEXT y toda comparacion
(`>=`, `<=`, `BETWEEN`) es una comparacion de STRINGS, byte a byte.

En este repo conviven dos formatos incompatibles:

    a) ``DEFAULT CURRENT_TIMESTAMP``  ->  "2026-09-01 14:03:22"
    b) ``datetime.isoformat()``       ->  "2026-09-01T14:03:22+00:00"

El caracter que los separa es el septimo: espacio (0x20) contra 'T' (0x54).
Como 0x20 < 0x54, TODA fila escrita con el formato (a) se ordena por DEBAJO de
un corte escrito con el formato (b) del mismo instante. La consulta no falla,
no loguea nada: simplemente devuelve menos filas de las que existen, o cero.

Esto ya mordio a este repo: ``x_api_calls.ts`` se escribia con (a) y
``posts_fetched_today()`` comparaba contra (b), asi que devolvia 0 SIEMPRE y el
presupuesto de la API de X se creia intacto cuando ya estaba consumido.

COMO SE USA
-----------
Dos piezas que van SIEMPRE juntas: si normalizas un lado tenes que normalizar
el otro, o creas el mismo bug al reves.

    from utils.tsnorm import TS_NORM, ts_bound

    cur.execute("SELECT * FROM t WHERE " + TS_NORM.format(col="ts") + " >= ?",
                (ts_bound(since),))

CUANDO **NO** USARLO
--------------------
Si la columna se escribe SIEMPRE con ``.isoformat()`` y se compara SIEMPRE
contra ``.isoformat()``, ya es consistente y normalizar solo agrega ruido (y
rompe el indice, ver abajo). Este modulo es para columnas de formato MIXTO o
de formato (a). Ejemplo real: ``intel_memory.timestamp_utc`` es isoformat puro
de punta a punta y NO debe pasar por aca.

COSTO
-----
Envolver la columna en funciones impide que SQLite use el indice sobre ella:
la comparacion pasa de index seek a full scan. En las tablas de este repo
(miles de filas, no millones) es irrelevante. Si alguna crece, la solucion no
es sacar la normalizacion sino unificar el formato en la escritura.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

__all__ = ["TS_NORM", "ts_norm", "ts_bound"]

# Expresion SQL que lleva cualquiera de los dos formatos a "YYYY-MM-DD HH:MM:SS".
# substr(...,1,19) descarta offset de zona y microsegundos; replace cambia la
# 'T' por espacio. El orden importa: primero recortar, despues reemplazar.
TS_NORM = "replace(substr({col},1,19),'T',' ')"


def ts_norm(col: str = "ts") -> str:
    """Devuelve la expresion SQL que normaliza la columna ``col``.

    Preferir esta funcion sobre ``TS_NORM.format(col=...)`` cuando el nombre de
    la columna viene de una variable, para que quede un solo lugar que sepa la
    forma de la expresion.
    """
    return TS_NORM.format(col=col)


def ts_bound(value: Any) -> str:
    """Lleva un datetime / string ISO al MISMO formato que produce ``TS_NORM``.

    Es la contraparte obligatoria de ``TS_NORM``: normaliza el lado Python de
    la comparacion. Devuelve "" para entradas vacias, que el llamador debe
    tratar como "sin corte" en vez de pasarlo a la consulta (comparar contra ""
    haria pasar todas las filas).
    """
    if isinstance(value, datetime):
        value = value.isoformat()
    s = str(value or "").strip()
    if not s:
        return ""
    # Mismo recorte que hace TS_NORM en SQL, en el mismo orden.
    return s.replace("T", " ").rstrip("Z").split("+")[0][:19]
