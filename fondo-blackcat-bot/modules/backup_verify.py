"""R-BOT-DEFINITIVE Fase 5.1 — un backup no verificado no es un backup.

El problema
-----------
`backup_volume.run_backup()` devolvia {"ok": True} en cuanto el tar.gz se
escribia sin excepcion. Eso prueba UNA cosa: que se pudo escribir un archivo.
No prueba nada de lo unico que importa, que es que ese archivo sirva el dia
que haya que restaurarlo. Un tarball puede estar perfecto y contener sqlites
inservibles — es justamente lo que pasaba, porque el tar copiaba los .db
crudos mientras el bot escribia y sin sus -wal (ver `_snapshot_db`).

La unica forma de saber si un backup se puede restaurar es restaurarlo. Este
modulo lo hace de verdad:

    1. agarra el tarball mas nuevo
    2. lo extrae en un directorio temporal (NUNCA sobre DATA_DIR)
    3. abre cada .db y corre PRAGMA integrity_check
    4. lista las tablas y cuenta las filas de cada una
    5. compara esos conteos contra la DB viva
    6. borra el temporal

El paso 5 es el que atrapa el backup "valido pero vacio": un sqlite recien
creado pasa integrity_check con las mejores notas y tiene cero filas. Si el
backup tiene 0 filas donde la viva tiene 40.000, el archivo esta sano y el
backup es basura.

La comparacion es asimetrica a proposito: el backup es mas VIEJO que la DB
viva, asi que backup <= viva es normal. Lo anormal es backup > viva (la viva
perdio datos) o backup muy por debajo (el backup no capturo nada).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except (PermissionError, OSError):
    DATA_DIR = Path("/tmp/intel_data")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

BACKUP_DIR = DATA_DIR / "backup"
VERIFY_LAST_PATH = DATA_DIR / "backup_verify_last.json"

# Cuanto puede achicarse un conteo respecto de la DB viva sin que sea alarma.
# Es generoso a proposito: el backup es de hace horas y muchas tablas rotan.
COBERTURA_MINIMA = 0.5

# Tablas que se vacian solas (caches, dedup con TTL): un conteo bajo en estas
# no dice nada. Se listan explicitamente para no inventar excusas despues.
TABLAS_VOLATILES = {"alert_dedup", "dedup", "rate", "cache", "source_state",
                    "intel_calls", "x_api_calls",
                    # R-BOT-FINAL: subsystem_health es estado ACTUAL, una fila
                    # por subsistema, reescrita en cada corrida. Nunca es
                    # historia y no se restaura: si se pierde, cada subsistema
                    # la vuelve a escribir la primera vez que corre. Ademas se
                    # escribe DESPUES del snapshot (el propio run_backup marca
                    # su exito al terminar), asi que en el tarball siempre va a
                    # ir una version anterior. Compararla contra la viva solo
                    # produce falsas alarmas.
                    "subsystem_health"}


# ── DBs que el fondo NO puede recomputar (Fase 5.2) ─────────────────────────
# Cada entrada dice que pasa si se pierde. Las que se declaran descartables
# tienen que explicar de donde se vuelven a sacar; "es solo un cache" sin
# decir cache de que no alcanza.
DBS_CRITICAS: dict[str, str] = {
    "intel_memory.db": (
        "EL archivo. Adentro viven trade_ledger (ledger_fills, "
        "ledger_funding, ledger_positions), health_registry, x_store, "
        "errors_log y varios mas. Los fills y el funding se pueden re-pedir a "
        "la API de HL, pero SOLO dentro de su horizonte: lo que quedo fuera "
        "de la ventana no vuelve nunca. El track record historico se pierde."),
    "pnl.db": "Serie de PnL realizado. No se recomputa: HL no la guarda.",
    "position_log.db": "Historial de posiciones abiertas/cerradas propias.",
    "snapshots.db": "Fotos de equity para la curva del fondo.",
    "vault_history.db": "Historial de depositos en vaults.",
    "perf_attribution.db": "Atribucion de performance acumulada.",
    "compounding.db": "Estado del detector de compounding.",
    "basket_close.db": "Cierres de canasta detectados (define los ciclos).",
    "auto_reconcile.db": "Historial de reconciliaciones de fund_state.",
    "cost.db": "Costos acumulados de APIs (X, LLM). Sin esto no hay control "
               "de gasto historico.",
}

DBS_DESCARTABLES: dict[str, str] = {
    "alert_dedup.db": "Solo huellas de alertas ya mandadas. Perderlo hace que "
                      "una alerta se repita una vez. Se regenera sola.",
    "intel_rate.db": "Contadores de rate limit del dia por fuente. Se "
                     "resetean solos a medianoche UTC; perderlos solo permite "
                     "unas llamadas de mas en el run siguiente.",
    "intel_source_state.db": "Estado vivo/muerto de cada feed. Se reconstruye "
                             "en el primer run de cada fuente.",
    "source_state.db": "Estado vivo/muerto por feed (nombre legacy del "
                       "anterior). Se reconstruye en el primer run de cada "
                       "fuente; lo unico que se pierde es el historial de "
                       "last_success, que solo sirve para las alertas.",
    "boot_dedup.db": "Dedup de los mensajes de arranque. Si se pierde, el "
                     "bot saluda una vez de mas en el proximo deploy.",
    "scheduler_health.db": "Latidos del scheduler. Solo importa el ultimo, y "
                           "ese lo escribe el propio scheduler a los pocos "
                           "minutos de arrancar.",
    "macro_calendar.db": "Calendario macro. Se re-descarga entero de las "
                         "fuentes publicas (FRED, ForexFactory) en el proximo "
                         "refresh diario.",
    "catalysts.db": "Catalizadores (unlocks, TGEs, eventos). Se re-descargan de "
                    "Tokenomist y del calendario macro en el proximo refresh.",
    "catalyst_alerts.db": "Dedup de alertas de catalizadores ya enviadas. "
                          "Perderlo repite como mucho un aviso por catalizador "
                          "vigente.",
    "cryexc.db": "Cache de intel de exchanges (CryptoExchange). Se vuelve a "
                 "pedir al API en el proximo ciclo de intel; no hay nada propio "
                 "adentro.",
    "predictive_alerts.db": "Estado de las alertas predictivas. Se recalcula "
                            "entero del precio y las posiciones vivas en el "
                            "proximo run.",
    "margin_alerts.db": "Dedup de alertas de margen. El margen real se lee de "
                        "HL cada vez; esto solo evita repetir el mismo aviso.",
    "hf_alerts.db": "Dedup de alertas de health factor. El HF se recalcula de "
                    "la posicion viva en cada consulta; esto solo evita repetir "
                    "el aviso.",
    "trailing_monitor.db": "Estado del trailing stop. Se recalcula del precio "
                           "vivo y del maximo de la serie, que vienen de HL, no "
                           "de aca.",
    "sl_validator.db": "Validaciones de stop loss ya hechas. Se vuelven a "
                       "calcular sobre las posiciones abiertas en la proxima "
                       "corrida.",
    "macro_convergence.db": "Derivada por completo de macro_calendar y de los "
                            "precios; no guarda ni un dato de origen propio.",
    "pre_event_brief.db": "Registro de que briefs pre-evento ya se mandaron. "
                          "Perderlo reenvia como mucho un brief por evento "
                          "pendiente.",
}


def _newest_tarball() -> Path | None:
    if not BACKUP_DIR.exists():
        return None
    tars = sorted(BACKUP_DIR.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime)
    return tars[-1] if tars else None


def _es_volatil(tabla: str) -> bool:
    t = tabla.lower()
    return any(v in t for v in TABLAS_VOLATILES)


def _inspeccionar(path: Path) -> dict[str, Any]:
    """integrity_check + conteo por tabla. Nunca levanta."""
    out: dict[str, Any] = {"integridad": None, "tablas": {}, "error": None}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
    except sqlite3.Error as e:
        out["error"] = f"no abre: {e!s:.60s}"
        return out
    try:
        row = con.execute("PRAGMA integrity_check").fetchone()
        out["integridad"] = row[0] if row else "sin respuesta"
        tablas = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        for t in tablas:
            try:
                n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            except sqlite3.Error:
                n = -1          # -1 = tabla ilegible; NO es lo mismo que 0
            out["tablas"][t] = n
    except sqlite3.Error as e:
        out["error"] = f"lectura: {e!s:.60s}"
    finally:
        con.close()
    return out


def verify_latest(*, tarball: Path | None = None) -> dict[str, Any]:
    """Restaura el backup mas nuevo en un temporal y prueba que sirva."""
    t0 = time.time()
    tar_path = tarball or _newest_tarball()
    if tar_path is None:
        return {"ok": False, "motivo": "no hay ningun tarball en backup/",
                "ts_utc": int(t0), "problemas": ["nunca se corrio un backup"]}

    edad_h = round((time.time() - tar_path.stat().st_mtime) / 3600.0, 1)
    problemas: list[str] = []
    detalle: dict[str, Any] = {}
    tmp = Path(tempfile.mkdtemp(prefix="bkverify_"))
    try:
        try:
            with tarfile.open(tar_path, "r:gz") as tar:
                # Extraccion acotada: nada fuera del temporal, ni symlinks.
                miembros = [m for m in tar.getmembers()
                            if m.isfile() and "/" not in m.name
                            and not m.name.startswith("..")]
                tar.extractall(tmp, members=miembros)
        except (tarfile.TarError, OSError) as e:
            return {"ok": False, "motivo": f"el tarball no se puede abrir: {e!s:.80s}",
                    "tarball": tar_path.name, "ts_utc": int(t0),
                    "problemas": [f"backup ilegible: {tar_path.name}"]}

        dbs = sorted(tmp.glob("*.db"))
        if not dbs:
            problemas.append(
                f"{tar_path.name} no contiene ni una sqlite: el backup existe "
                f"pero no hay nada que restaurar")

        for db in dbs:
            info = _inspeccionar(db)
            vivo = DATA_DIR / db.name
            # Los totales excluyen las tablas volatiles, de los DOS lados. Si
            # se contara todo, el total de la viva incluiria dedups y caches
            # que rotan solos y la comparacion backup/viva dejaria de medir lo
            # unico que interesa: los datos que NO se pueden recomputar.
            info["filas_backup"] = sum(
                v for t, v in info["tablas"].items()
                if v > 0 and not _es_volatil(t))
            info["filas_vivas"] = None
            info["critica"] = db.name in DBS_CRITICAS

            if info["error"]:
                problemas.append(f"{db.name}: {info['error']}")
            elif info["integridad"] != "ok":
                problemas.append(
                    f"{db.name}: integrity_check dice '{info['integridad']}' "
                    f"— el backup NO es restaurable")

            if vivo.exists():
                vivo_info = _inspeccionar(vivo)
                info["filas_vivas"] = sum(
                    v for t, v in vivo_info["tablas"].items()
                    if v > 0 and not _es_volatil(t))
                # Las volatiles se excluyen tambien de "faltan tablas": una
                # tabla de estado creada despues del ultimo backup no es un
                # backup incompleto, es un backup viejo. Y un backup viejo ya
                # se reporta como tal en `edad_horas`.
                faltantes = [t for t in vivo_info["tablas"]
                             if t not in info["tablas"] and not _es_volatil(t)]
                if faltantes:
                    problemas.append(
                        f"{db.name}: al backup le faltan tablas que la DB viva "
                        f"tiene ({', '.join(sorted(faltantes)[:4])})")
                bajas = []
                for t, n_vivo in vivo_info["tablas"].items():
                    if _es_volatil(t) or n_vivo <= 0:
                        continue
                    n_bk = info["tablas"].get(t, 0)
                    if n_bk < n_vivo * COBERTURA_MINIMA:
                        bajas.append(f"{t} {n_bk}/{n_vivo}")
                    elif n_bk > n_vivo:
                        # El backup tiene MAS que la viva: la viva perdio datos.
                        problemas.append(
                            f"{db.name}.{t}: el backup tiene {n_bk} filas y la "
                            f"DB viva {n_vivo} — la viva perdio datos")
                if bajas and db.name in DBS_CRITICAS:
                    problemas.append(
                        f"{db.name}: el backup esta muy por debajo de la DB "
                        f"viva ({'; '.join(bajas[:3])})")
                info["tablas_bajas"] = bajas
            detalle[db.name] = info

        # Fase 5.2 — cobertura: toda DB critica tiene que estar en el tarball.
        presentes = {d.name for d in dbs}
        sin_cubrir = [n for n in DBS_CRITICAS if (DATA_DIR / n).exists()
                      and n not in presentes]
        if sin_cubrir:
            problemas.append(
                f"DBs criticas fuera del backup: {', '.join(sorted(sin_cubrir))}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # R-BOT-FINAL (2026-09-02) — los conteos por DB ya se calculaban y se
    # guardaban en `resumen`, pero no salian por ningun lado: /diagnostico decia
    # "15 DBs restauradas" y punto. "15 DBs restauradas" es exactamente lo que
    # tambien diria una restauracion de 15 sqlites vacios, que es el modo de
    # falla que este modulo existe para detectar. El numero que prueba algo es
    # cuantas filas volvieron, asi que se agrega y se publica.
    filas_bk = sum(int(v.get("filas_backup") or 0) for v in detalle.values())
    filas_vivas = sum(int(v.get("filas_vivas") or 0) for v in detalle.values()
                      if v.get("filas_vivas") is not None)
    res = {
        "ok": not problemas,
        "tarball": tar_path.name,
        "edad_horas": edad_h,
        "dbs_verificadas": len(detalle),
        "filas_restauradas_total": filas_bk,
        "filas_vivas_total": filas_vivas,
        "dbs_criticas_declaradas": len(DBS_CRITICAS),
        "problemas": problemas,
        "detalle": detalle,
        "ts_utc": int(time.time()),
        "iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duracion_s": round(time.time() - t0, 1),
    }
    try:
        liviano = {k: v for k, v in res.items() if k != "detalle"}
        liviano["resumen"] = {
            k: {"integridad": v.get("integridad"),
                "filas_backup": v.get("filas_backup"),
                "filas_vivas": v.get("filas_vivas")}
            for k, v in detalle.items()}
        VERIFY_LAST_PATH.write_text(json.dumps(liviano), encoding="utf-8")
    except OSError as e:
        log.debug("no se pudo escribir backup_verify_last.json: %s", e)
    return res


def last_verification() -> dict[str, Any] | None:
    """Ultima verificacion registrada, o None si nunca se corrio.

    None NO significa 'todo bien': significa que nadie probo nunca si el
    backup sirve. /health tiene que mostrar eso como '❓', no como '✅'.
    """
    if not VERIFY_LAST_PATH.exists():
        return None
    try:
        return json.loads(VERIFY_LAST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def horas_desde_verificacion() -> float | None:
    last = last_verification()
    if not last or not last.get("ts_utc"):
        return None
    return round((time.time() - int(last["ts_utc"])) / 3600.0, 1)


def cobertura() -> dict[str, Any]:
    """Fase 5.2 — toda DB en DATA_DIR esta clasificada, sin huerfanas."""
    presentes = sorted(p.name for p in DATA_DIR.glob("*.db"))
    clasificadas = set(DBS_CRITICAS) | set(DBS_DESCARTABLES)
    return {
        "en_disco": presentes,
        "criticas": sorted(n for n in presentes if n in DBS_CRITICAS),
        "descartables": sorted(n for n in presentes if n in DBS_DESCARTABLES),
        "sin_clasificar": sorted(n for n in presentes if n not in clasificadas),
    }


def format_for_telegram() -> str:
    last = last_verification()
    if not last:
        return ("🧪 *Backup verificado*: nunca. Existen tarballs pero nadie "
                "probo jamas si se pueden restaurar.")
    h = horas_desde_verificacion()
    edad = f"{h:.0f}h" if h is not None else "?"
    # Los conteos van en las DOS ramas: si la verificacion fallo, saber cuantas
    # filas volvieron contra cuantas hay vivas es justo lo que dice si el
    # problema es cosmetico o si el backup esta vacio.
    fbk = last.get("filas_restauradas_total")
    fviv = last.get("filas_vivas_total")
    if fbk is None:
        conteo = ""   # verificacion vieja, de antes de que esto se guardara
    else:
        pct = (f" ({100.0 * fbk / fviv:.0f}% de las vivas)"
               if isinstance(fviv, int) and fviv > 0 else "")
        conteo = f"\n  · {fbk:,} filas restauradas vs {fviv:,} vivas{pct}"
    top = last.get("resumen") or {}
    mayores = sorted(
        ((k, v.get("filas_backup") or 0, v.get("filas_vivas"))
         for k, v in top.items() if isinstance(v, dict)),
        key=lambda x: -int(x[1]))[:3]
    if mayores:
        conteo += "\n  · " + " · ".join(
            f"{k.replace('.db','')} {n:,}/{m if m is not None else '?'}"
            for k, n, m in mayores)
    if last.get("ok"):
        return (f"🧪 *Backup verificado* ✅ hace {edad}\n"
                f"  · `{last.get('tarball','?')}` "
                f"({last.get('edad_horas','?')}h de antiguedad)\n"
                f"  · {last.get('dbs_verificadas',0)} DBs restauradas, "
                f"integridad ok y conteos coherentes con las vivas"
                f"{conteo}")
    probs = last.get("problemas") or []
    lines = [f"🧪 *Backup verificado* ❌ hace {edad} — {len(probs)} problema(s)"]
    for p in probs[:5]:
        lines.append(f"  · {p}")
    if conteo:
        lines.append(conteo.lstrip("\n"))
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - smoke manual
    print(json.dumps(verify_latest(), indent=2, default=str)[:3000])
