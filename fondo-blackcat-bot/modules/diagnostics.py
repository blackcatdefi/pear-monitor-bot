"""R-BOT-DEFINITIVE Fase 2 — autodiagnostico completo, una sola fuente de verdad.

QUE PROBLEMA RESUELVE
=====================
Hasta ahora, para saber si el bot estaba sano habia que mirar cinco lugares
distintos y ninguno decia lo mismo: /health traia un puñado de campos, el
reporte traia banners sueltos, los logs de Railway traian el resto y el estado
de varios subsistemas no estaba en ningun lado. Por eso un componente podia
correr degradado durante semanas: no habia UN lugar donde eso apareciera.

Este modulo arma ese lugar. ``full_diagnosis()`` es la funcion que responde
"como esta el bot" y las tres salidas la usan sin recalcular nada:

    /health        -> JSON, para Railway y para revisar desde afuera
    /diagnostico   -> texto en Telegram, a pedido, con TODO el detalle
    self-test      -> corre solo, y SOLO habla cuando algo esta realmente mal

REGLA DE ORO DEL SELF-TEST
==========================
Silencioso cuando esta sano, ruidoso cuando no. Si el bot funciona, BCD no
recibe ni un mensaje extra por esta fase. Un chequeo automatico que manda un
"todo ok" diario entrena a ignorarlo, y el dia que mande "algo falla" tambien
lo van a ignorar. Por eso toda alerta pasa por ``alert_dedup``: un problema
que dura tres dias avisa una vez, no setenta y dos.

NADA DE ESTE MODULO LEVANTA. Cada bloque va envuelto: un diagnostico que
tumba al proceso que diagnostica no sirve. Pero cuando un bloque falla, el
diagnostico lo DICE en vez de omitirlo — un campo ausente se leeria como
"no aplica" y volveriamos al problema del principio.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger(__name__)

# Umbral de espacio en el volumen a partir del cual se avisa UNA vez.
VOLUME_ALERT_PCT = float(os.getenv("VOLUME_ALERT_PCT", "85") or 85)
# Cuantos runs seguidos puede fallar un feed antes de que se avise UNA vez.
FEED_DEAD_RUNS = int(os.getenv("FEED_DEAD_RUNS", "10") or 10)
# Horas sin exito a partir de las cuales un subsistema se considera rancio.
SUBSYSTEM_STALE_HOURS = float(os.getenv("SUBSYSTEM_STALE_HOURS", "36") or 36)


def _safe(fn: Callable[[], Any], nombre: str) -> Any:
    """Corre un bloque del diagnostico sin dejar que tumbe al resto.

    Cuando falla NO devuelve {} ni None: devuelve un objeto que dice que
    fallo. Es la doctrina de toda la ronda aplicada al propio diagnostico —
    la ausencia de un dato no puede parecerse a la ausencia de un problema.
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        log.warning("diagnostics: bloque %s fallo: %s", nombre, exc)
        return {"_error": f"{type(exc).__name__}: {exc}"[:200]}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hours_since_iso(iso: Any) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return round((_now() - dt).total_seconds() / 3600.0, 2)
    except Exception:  # noqa: BLE001
        return None


# ─── bloques ────────────────────────────────────────────────────────────────

def _b_subsistemas() -> dict[str, Any]:
    from modules import health_registry
    return health_registry.all_status()


def _b_ledger() -> dict[str, Any]:
    from modules.trade_ledger import ledger_diagnostics, sync_health
    out = ledger_diagnostics()
    # Por wallet: cursores, horizonte de la API y ultimo sync. Es el detalle
    # que faltaba para saber si un NET corto es "no opero" o "no leyo".
    salud = {}
    for w, h in (sync_health() or {}).items():
        salud[w[:10]] = {
            "ok": bool(h.get("ok")),
            "fills_ok": bool(h.get("fills_ok")),
            "funding_ok": bool(h.get("funding_ok")),
            "detail": str(h.get("detail") or "")[:160],
            "updated_at": h.get("updated_at"),
            "horas_desde_sync": _hours_since_iso(h.get("updated_at")),
        }
    out["sync_por_wallet"] = salud
    return out


def _b_invariantes() -> dict[str, Any]:
    from modules.ledger_invariants import run_all
    return run_all()


def _b_x() -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        from modules.x_provider import backend_name
        out["backend"] = backend_name()
    except Exception as exc:  # noqa: BLE001
        out["backend"] = f"(no disponible: {type(exc).__name__})"
    try:
        from modules.x_intel import X_LIVE_ENABLED, get_api_stats, get_cache_state
        st = get_api_stats() or {}
        cs = get_cache_state() or {}
        out["live"] = bool(X_LIVE_ENABLED)
        out["llamadas_hoy"] = st.get("count", 0)
        out["ultimo_exito"] = cs.get("last_success_at")
        out["horas_desde_exito"] = _hours_since_iso(cs.get("last_success_at"))
    except Exception as exc:  # noqa: BLE001
        out["_error_stats"] = f"{type(exc).__name__}: {exc}"[:160]
    try:
        from modules.x_costs import credits_remaining
        out["creditos_restantes"] = credits_remaining()
    except Exception:  # noqa: BLE001
        # No existe el modulo de creditos en todos los backends: no es un
        # fallo, es que ese backend no cobra por credito.
        out["creditos_restantes"] = None
    return out


def _b_gmail() -> dict[str, Any]:
    from modules import health_registry
    st = health_registry.status("gmail")
    out = {
        "ok": st.get("ok"),
        "detalle": str(st.get("detail") or "")[:200],
        "ultimo_ok": st.get("last_ok_utc"),
        "horas_desde_ok": _hours_since_iso(st.get("last_ok_utc")),
    }
    try:
        from modules.gmail_intel import last_run_stats
        out.update(last_run_stats() or {})
    except Exception:  # noqa: BLE001
        pass
    return out


def _b_feeds() -> dict[str, Any]:
    """Estado por feed: vivo, rancio o muerto, con cuando fue el ultimo exito.

    Fase 4.2. La base ya existia (``source_state`` de _intel_base) pero no la
    leia nadie, asi que ASXN y HypurrScan pudieron imprimir "fuente no
    disponible" durante semanas sin que eso apareciera en ningun lado.
    """
    from modules.intel30._intel_base import SOURCE_STATE_DB
    out: dict[str, Any] = {"vivos": [], "rancios": [], "muertos": [],
                           "retirados": [], "detalle": {}}
    try:
        from modules.feed_registry import RETIRED
    except Exception:  # noqa: BLE001
        RETIRED = {}
    if not os.path.exists(str(SOURCE_STATE_DB)):
        out["_error"] = "source_state.db todavia no existe (ningun run)"
        out["retirados"] = sorted(RETIRED)
        return out
    con = sqlite3.connect(str(SOURCE_STATE_DB), timeout=2.0)
    con.row_factory = sqlite3.Row
    try:
        for r in con.execute("SELECT * FROM source_state"):
            nombre = r["source"]
            fails = int(r["consecutive_fails"] or 0)
            e = {"status": r["status"], "fallos_seguidos": fails,
                 "ultimo_cambio": r["last_change_utc"],
                 "horas": _hours_since_iso(r["last_change_utc"])}
            out["detalle"][nombre] = e
            if nombre in RETIRED:
                out["retirados"].append(nombre)
            elif r["status"] == "LIVE":
                out["vivos"].append(nombre)
            elif fails >= FEED_DEAD_RUNS:
                out["muertos"].append(nombre)
            else:
                out["rancios"].append(nombre)
    finally:
        con.close()
    for k in ("vivos", "rancios", "muertos", "retirados"):
        out[k] = sorted(set(out[k]))
    for n in RETIRED:
        if n not in out["retirados"]:
            out["retirados"].append(n)
    out["retirados"] = sorted(set(out["retirados"]))
    # Fase 4.2 — la lista que de verdad dispara alerta sale del registro, no
    # de este conteo: `dead_feeds` exige que la fuente HAYA funcionado antes.
    # Una que nunca anduvo no puede generar una alerta eterna.
    try:
        from modules.feed_registry import dead_feeds, env_vars_faltantes
        out["muertos_accionables"] = dead_feeds()
        out["env_faltantes"] = sorted(env_vars_faltantes())
    except Exception as exc:  # noqa: BLE001
        out["muertos_accionables"] = []
        out["_error_registro"] = f"{type(exc).__name__}: {exc}"[:160]
    return out


def _b_volumen() -> dict[str, Any]:
    """Fase 5.3. Un disco lleno ya mato una sesion que funcionaba."""
    from config import DATA_DIR
    total, usado, libre = shutil.disk_usage(DATA_DIR)
    pct = round(100.0 * usado / total, 1) if total else None
    # Cuanto pesa lo nuestro dentro de eso (el volumen puede compartirse).
    propio = 0
    for raiz, _, archivos in os.walk(DATA_DIR):
        for a in archivos:
            try:
                propio += os.path.getsize(os.path.join(raiz, a))
            except OSError:
                pass
    return {
        "path": str(DATA_DIR),
        "total_mb": round(total / 1e6, 1),
        "usado_mb": round(usado / 1e6, 1),
        "libre_mb": round(libre / 1e6, 1),
        "usado_pct": pct,
        "datos_propios_mb": round(propio / 1e6, 1),
        "umbral_alerta_pct": VOLUME_ALERT_PCT,
        "sobre_umbral": bool(pct is not None and pct >= VOLUME_ALERT_PCT),
    }


def _b_backup() -> dict[str, Any]:
    from modules.backup_volume import (get_last_backup_status,
                                       hours_since_last_backup)
    st = get_last_backup_status() or {}
    out = {
        "ultimo": st.get("iso", ""),
        "ok": bool(st.get("ok")),
        "tarball": st.get("tarball", ""),
        "horas": hours_since_last_backup(),
    }
    # Fase 5.1 — que el backup EXISTA y que el backup SIRVA son dos preguntas
    # distintas, y hasta esta ronda solo se contestaba la primera. Se guardan
    # separadas a proposito: 'nunca se verifico' no puede leerse como 'ok'.
    try:
        from modules.backup_verify import (cobertura, horas_desde_verificacion,
                                           last_verification)
        ver = last_verification()
        out["verificacion"] = ver
        out["verificacion_horas"] = horas_desde_verificacion()
        out["verificado_alguna_vez"] = ver is not None
        out["cobertura"] = cobertura()
    except Exception as exc:  # noqa: BLE001
        out["verificacion"] = None
        out["verificado_alguna_vez"] = None    # None = no se pudo saber
        out["_error_verificacion"] = f"{type(exc).__name__}: {exc}"[:160]
    return out


def _b_dependencias() -> dict[str, Any]:
    """Versiones instaladas de las dependencias que tienen fallback silencioso.

    Es exactamente el bug de rapidfuzz: faltaba en requirements, el import
    fallaba, el dedup caia a difflib y puntuaba mas bajo, y nadie lo supo
    hasta que alguien fue a mirar. Si esta en /health, se ve.
    """
    out: dict[str, Any] = {}
    for mod in ("rapidfuzz", "httpx", "aiohttp", "telegram", "anthropic"):
        try:
            m = __import__(mod)
            out[mod] = getattr(m, "__version__", "instalado")
        except Exception:  # noqa: BLE001
            out[mod] = None      # None = NO instalado, y eso importa
    faltan = [k for k, v in out.items() if v is None]
    return {"versiones": out, "faltan": faltan, "ok": not faltan}


def _b_ppc() -> dict[str, Any]:
    """Fase 3.3. El estado del PPC, con el porque en vez de solo el n/d."""
    from modules.hype_acquisition import compute_hype_acquisition, get_ppc_override
    ov = get_ppc_override()
    acq = compute_hype_acquisition()
    return {
        "manual": ({"ppc_usd": ov["ppc_usd"], "set_date": ov["set_date"]}
                   if ov else None),
        "auto_calculable": bool(acq.known),
        "razon": acq.reason,
        "fills_desde": acq.fills_from_utc,
        "fills_hasta": acq.fills_to_utc,
        "cubierto_hype": round(acq.covered_qty, 2),
        "sin_cubrir_hype": round(acq.uncovered_qty, 2),
        "cobertura_pct": acq.coverage_pct,
        "saldo_onchain": acq.onchain_balance,
        "historial_truncado": acq.truncated,
    }


def _b_dedup() -> dict[str, Any]:
    """Estado del almacen de dedup de alertas.

    Si esta tabla se pierde o se corrompe, todas las alertas dedupeadas se
    reenvian de golpe. Vale saber que existe y cuanto tiene adentro.
    """
    from modules.alert_dedup import DB_PATH
    if not os.path.exists(DB_PATH):
        return {"path": DB_PATH, "existe": False, "filas": 0}
    con = sqlite3.connect(DB_PATH, timeout=2.0)
    try:
        tabs = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        filas = {}
        for t in tabs:
            filas[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return {"path": DB_PATH, "existe": True, "tablas": filas,
                "filas": sum(filas.values()),
                "tamano_kb": round(os.path.getsize(DB_PATH) / 1024, 1)}
    finally:
        con.close()


def _b_autoactualizacion() -> dict[str, Any]:
    """Fase 0.3 — ¿puede el bot actualizarse solo?

    La ronda pasada se freno esperando que BCD abriera una pagina para
    autorizar un push. Que eso se pueda saber desde afuera, por nombre de
    variable y sin exponer ningun valor, es lo que hace que la proxima ronda
    no necesite nada de el.
    """
    def _tiene(*nombres: str) -> str | None:
        for n in nombres:
            if os.getenv(n, "").strip():
                return n
        return None

    token = _tiene("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT", "BOT_GITHUB_PAT")
    railway = _tiene("RAILWAY_TOKEN", "RAILWAY_API_TOKEN")
    repo = os.getenv("GITHUB_REPO", "").strip() or None
    return {
        # Solo el NOMBRE de la variable encontrada. Nunca el valor.
        "github_token_var": token,
        "railway_token_var": railway,
        "repo": repo,
        "puede_pushear": bool(token and repo),
        "puede_redeployar": bool(railway),
        "requeridas_para_autonomia": [
            "GITHUB_TOKEN  (PAT con scope 'repo' sobre pear-monitor-bot)",
            "GITHUB_REPO   (blackcatdefi/pear-monitor-bot)",
            "RAILWAY_TOKEN (opcional: fuerza redeploy sin esperar el webhook)",
        ],
    }


BLOQUES: dict[str, Callable[[], Any]] = {
    "subsistemas": _b_subsistemas,
    "ledger": _b_ledger,
    "invariantes": _b_invariantes,
    "x": _b_x,
    "gmail": _b_gmail,
    "feeds": _b_feeds,
    "volumen": _b_volumen,
    "backup": _b_backup,
    "dependencias": _b_dependencias,
    "ppc": _b_ppc,
    "dedup_alertas": _b_dedup,
    "autoactualizacion": _b_autoactualizacion,
}


def full_diagnosis(*, incluir: list[str] | None = None) -> dict[str, Any]:
    """El diagnostico completo. Nunca levanta; los bloques que fallan lo dicen."""
    from modules.version_info import (DEPLOY_ID, GIT_COMMIT_SHA, SERVICE_NAME,
                                      START_TIME_UTC, uptime_seconds)
    nombres = incluir or list(BLOQUES)
    out: dict[str, Any] = {
        "generado_utc": _now().isoformat(),
        "commit": GIT_COMMIT_SHA[:7],
        "deploy_id": DEPLOY_ID,
        "servicio": SERVICE_NAME,
        "arrancado_utc": START_TIME_UTC,
        "uptime_segundos": uptime_seconds(),
    }
    try:
        from commands_registry import COMMANDS
        out["comandos"] = len(COMMANDS)
    except Exception:  # noqa: BLE001
        out["comandos"] = None
    for n in nombres:
        out[n] = _safe(BLOQUES[n], n)
    out["problemas"] = detectar_problemas(out)
    out["ok"] = not out["problemas"]
    return out


# ─── que cuenta como "algo esta mal" ────────────────────────────────────────

def detectar_problemas(d: dict[str, Any]) -> list[str]:
    """Traduce el diagnostico a una lista de problemas REALES.

    Esta funcion es la que decide si BCD recibe un mensaje. El criterio es
    estrecho a proposito: solo entra lo que se puede accionar. Un feed rancio
    de un run no entra; uno muerto hace diez, si. Un subsistema sin datos no
    entra; uno que declaro degradacion, si.
    """
    p: list[str] = []

    sub = d.get("subsistemas") or {}
    for nombre in sub.get("degraded", []) or []:
        e = (sub.get("subsystems") or {}).get(nombre, {})
        marca = "MONEY " if e.get("money") else ""
        p.append(f"{marca}subsistema degradado: {e.get('label', nombre)} — "
                 f"{str(e.get('detail') or '')[:120]}")
    if (sub.get("registry_self_errors") or 0) > 0:
        p.append("el registro de salud fallo internamente: no puede "
                 "garantizar lo que reporta")

    inv = d.get("invariantes") or {}
    for v in (inv.get("invariantes") or [])[:5]:
        p.append(f"invariante roto: {v}")
    for v in (inv.get("recomputo") or [])[:5]:
        p.append(f"recomputo no coincide: {v}")

    feeds = d.get("feeds") or {}
    # Fase 4.2. Se usa la lista accionable (fuentes que ANTES funcionaban);
    # si el registro no se pudo consultar se cae a la cruda, porque quedarse
    # sin ninguna es exactamente el silencio que esta ronda viene a eliminar.
    muertos = feeds.get("muertos_accionables")
    if muertos:
        p.append(f"feeds caidos hace {FEED_DEAD_RUNS}+ runs: "
                 f"{'; '.join(muertos)}")
    elif muertos is None and feeds.get("muertos"):
        p.append(f"feeds muertos hace {FEED_DEAD_RUNS}+ runs: "
                 f"{', '.join(feeds['muertos'])}")

    vol = d.get("volumen") or {}
    if vol.get("sobre_umbral"):
        p.append(f"volumen al {vol.get('usado_pct')}% "
                 f"(umbral {vol.get('umbral_alerta_pct')}%)")

    dep = d.get("dependencias") or {}
    if dep.get("faltan"):
        p.append(f"dependencias con fallback silencioso NO instaladas: "
                 f"{', '.join(dep['faltan'])}")

    bk = d.get("backup") or {}
    horas = bk.get("horas")
    if horas is not None and horas > 48:
        p.append(f"el ultimo backup fue hace {horas:.0f}h")
    elif horas is None and (d.get("uptime_segundos") or 0) > 48 * 3600:
        # El agujero mas facil de dejar abierto: si el job de backup NUNCA
        # corrio, `horas` es None para siempre y la condicion de arriba no se
        # cumple nunca. "Nunca hubo backup" tiene que gritar mas fuerte que
        # "el backup es viejo", no menos.
        p.append("el bot lleva mas de 48h arriba y NUNCA corrio un backup")
    ver = bk.get("verificacion")
    if isinstance(ver, dict) and ver.get("ok") is False:
        motivos = "; ".join((ver.get("problemas") or [])[:3]) or "sin detalle"
        p.append(f"el ultimo backup NO es restaurable: {motivos[:200]}")
    elif bk.get("ok") and bk.get("verificado_alguna_vez") is False:
        # Hay backups y nadie probo nunca si sirven. Es exactamente el estado
        # en el que estuvo el fondo hasta esta ronda, y no puede ser silencioso.
        p.append("hay backups pero nunca se verifico que se puedan restaurar")
    vh = bk.get("verificacion_horas")
    if isinstance(vh, (int, float)) and vh > 72:
        p.append(f"la ultima verificacion de restauracion fue hace {vh:.0f}h")
    sin_clasificar = ((bk.get("cobertura") or {}).get("sin_clasificar")) or []
    if sin_clasificar:
        p.append(f"DBs sin clasificar como criticas o descartables: "
                 f"{', '.join(sin_clasificar[:5])}")

    led = d.get("ledger") or {}
    for w, h in (led.get("sync_por_wallet") or {}).items():
        if not h.get("ok"):
            p.append(f"ledger incompleto en {w}: "
                     f"{str(h.get('detail') or '')[:100]}")
    return p


# ─── render para Telegram ───────────────────────────────────────────────────

def _tick(v: Any) -> str:
    if v is True:
        return "\u2705"
    if v is False:
        return "\u274c"
    return "\u2754"          # None = sin datos, que NO es lo mismo que sano


def format_diagnosis(d: dict[str, Any]) -> str:
    L: list[str] = []
    cab = "\u2705 TODO OK" if d.get("ok") else \
        f"\u274c {len(d.get('problemas') or [])} PROBLEMA(S)"
    L.append(f"\U0001f9ea *DIAGNOSTICO* — {cab}")
    L.append(f"commit `{d.get('commit')}` · deploy `{d.get('deploy_id')}` · "
             f"up {d.get('uptime_segundos', 0) // 3600}h · "
             f"{d.get('comandos')} comandos")

    if d.get("problemas"):
        L.append("\n*Problemas*")
        for x in d["problemas"]:
            L.append(f"  \u2022 {x}")

    sub = d.get("subsistemas") or {}
    L.append("\n*Subsistemas*")
    for nombre, e in sorted((sub.get("subsystems") or {}).items()):
        h = e.get("stale_hours")
        cuando = f"hace {h:.0f}h" if isinstance(h, (int, float)) else "nunca"
        money = "\U0001f4b0" if e.get("money") else "  "
        L.append(f"  {_tick(e.get('ok'))}{money} {e.get('label', nombre)} "
                 f"— ultimo ok {cuando}")

    inv = d.get("invariantes") or {}
    L.append("\n*Invariantes del money path*")
    if inv.get("_error"):
        L.append(f"  \u274c no se pudieron correr: {inv['_error']}")
    else:
        L.append(f"  {_tick(inv.get('ok'))} {inv.get('total', 0)} violacion(es)"
                 f" sobre las ultimas {inv.get('limite_filas')} filas")
        for x in (inv.get("invariantes") or [])[:6]:
            L.append(f"     \u2022 {x[:180]}")
        for x in (inv.get("recomputo") or [])[:6]:
            L.append(f"     \u2022 {x[:180]}")

    led = d.get("ledger") or {}
    if led.get("sync_por_wallet"):
        L.append("\n*Ledger por wallet*")
        for w, h in sorted(led["sync_por_wallet"].items()):
            hh = h.get("horas_desde_sync")
            L.append(f"  {_tick(h.get('ok'))} {w} — fills {_tick(h.get('fills_ok'))}"
                     f" funding {_tick(h.get('funding_ok'))}"
                     + (f" · sync hace {hh:.0f}h" if isinstance(hh, (int, float))
                        else " · sin sync"))

    fe = d.get("feeds") or {}
    if not fe.get("_error"):
        L.append("\n*Feeds*")
        L.append(f"  vivos {len(fe.get('vivos') or [])} · "
                 f"rancios {len(fe.get('rancios') or [])} · "
                 f"muertos {len(fe.get('muertos') or [])} · "
                 f"retirados {len(fe.get('retirados') or [])}")
        if fe.get("muertos"):
            L.append(f"  \u274c muertos: {', '.join(fe['muertos'])}")
        if fe.get("retirados"):
            L.append(f"  \U0001f5c3 retirados (no se consultan): "
                     f"{', '.join(fe['retirados'])}")
    else:
        L.append(f"\n*Feeds*\n  \u2754 {fe['_error']}")

    ppc = d.get("ppc") or {}
    if not ppc.get("_error"):
        L.append("\n*PPC de HYPE*")
        if ppc.get("manual"):
            L.append(f"  en uso: manual ${ppc['manual']['ppc_usd']:,.2f} "
                     f"(set {ppc['manual']['set_date']})")
        _auto = "calculable" if ppc.get("auto_calculable") else "no calculable"
        L.append(f"  auto: {_auto}")
        if ppc.get("razon"):
            L.append(f"  motivo: {ppc['razon']}")

    vol = d.get("volumen") or {}
    if not vol.get("_error"):
        L.append("\n*Volumen y backup*")
        L.append(f"  disco {vol.get('usado_pct')}% usado · "
                 f"{vol.get('libre_mb')} MB libres · "
                 f"datos propios {vol.get('datos_propios_mb')} MB")
    bk = d.get("backup") or {}
    if not bk.get("_error"):
        h = bk.get("horas")
        L.append(f"  {_tick(bk.get('ok'))} ultimo backup "
                 + (f"hace {h:.0f}h" if isinstance(h, (int, float)) else "nunca"))
        ver = bk.get("verificacion")
        if isinstance(ver, dict):
            vh = bk.get("verificacion_horas")
            cuando = f"hace {vh:.0f}h" if isinstance(vh, (int, float)) else "?"
            L.append(f"  {_tick(ver.get('ok'))} restauracion probada {cuando} "
                     f"\u2014 {ver.get('dbs_verificadas', 0)} DBs restauradas")
            for x in (ver.get("problemas") or [])[:4]:
                L.append(f"     \u2022 {x[:160]}")
        else:
            L.append("  \u2754 restauracion NUNCA verificada "
                     "(que el tarball exista no prueba que sirva)")
        cob = bk.get("cobertura") or {}
        if cob:
            L.append(f"  cobertura: {len(cob.get('criticas') or [])} criticas / "
                     f"{len(cob.get('descartables') or [])} descartables"
                     + (f" \u2014 \u26a0\ufe0f sin clasificar: "
                        f"{', '.join(cob['sin_clasificar'])}"
                        if cob.get("sin_clasificar") else ""))

    dep = d.get("dependencias") or {}
    if dep.get("faltan"):
        L.append(f"\n\u274c dependencias faltantes: {', '.join(dep['faltan'])}")

    au = d.get("autoactualizacion") or {}
    if not au.get("_error"):
        L.append("\n*Autoactualizacion*")
        L.append(f"  {_tick(au.get('puede_pushear'))} push a GitHub"
                 + (f" (via {au['github_token_var']})" if au.get("github_token_var")
                    else " — falta GITHUB_TOKEN y/o GITHUB_REPO"))
        L.append(f"  {_tick(au.get('puede_redeployar'))} redeploy en Railway")

    x = d.get("x") or {}
    if not x.get("_error"):
        L.append("\n*X / costos*")
        L.append(f"  backend {x.get('backend')} · live "
                 f"{'ON' if x.get('live') else 'OFF'} · "
                 f"{x.get('llamadas_hoy', 0)} llamadas hoy"
                 + (f" · creditos {x['creditos_restantes']}"
                    if x.get("creditos_restantes") is not None else ""))

    return "\n".join(L)


# ─── el self-test que corre solo ────────────────────────────────────────────

SELFTEST_ALERT_TYPE = "selftest_diagnostico"
# Horas minimas entre dos avisos del MISMO conjunto de problemas.
SELFTEST_COOLDOWN_H = float(os.getenv("AUTODIAG_COOLDOWN_H", "24") or 24)


def run_selftest(*, notificar: Callable[[str], Any] | None = None) -> dict[str, Any]:
    """Corre el diagnostico y avisa UNA vez si hay algo realmente mal.

    Devuelve {"ok": bool, "problemas": [...], "alerto": bool}.

    El dedup es por CONTENIDO del problema, no por "hubo un problema": si
    aparece uno nuevo mientras el viejo sigue, avisa por el nuevo. Si es el
    mismo de ayer, se calla. Un problema que dura una semana manda un mensaje,
    no siete.
    """
    d = full_diagnosis()
    problemas = d.get("problemas") or []
    if not problemas:
        # Sano: cero mensajes. Ademas se limpia el dedup para que, si el
        # problema vuelve manana, vuelva a avisar (y no quede tapado por la
        # entrada vieja, que es como una alerta desaparece para siempre).
        try:
            from modules.alert_dedup import clear
            clear(SELFTEST_ALERT_TYPE, "global")
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "problemas": [], "alerto": False}

    # El "estado" para el dedup es un hash del CONJUNTO de problemas. Dos
    # corridas con los mismos problemas dan el mismo estado y no vuelven a
    # avisar; en cuanto aparece o desaparece uno, el estado cambia y avisa.
    firma = " | ".join(sorted(problemas))
    estado = hashlib.sha1(firma.encode("utf-8")).hexdigest()[:16]
    emitir = True
    try:
        from modules.alert_dedup import should_emit
        emitir = bool(should_emit(
            SELFTEST_ALERT_TYPE, "global", estado,
            cooldown_hours=SELFTEST_COOLDOWN_H,
            material={"n": len(problemas)},
        ))
    except Exception as exc:  # noqa: BLE001
        # OJO: fail-open. Si el dedup no funciona se avisa igual, porque
        # perder una alerta real es peor que repetirla. Pero queda en el log
        # para que "el dedup esta roto" no se convierta en el proximo
        # componente degradado en silencio — que seria ironico y ademas
        # convertiria este job en un spammer cada SELFTEST_COOLDOWN_H horas.
        log.warning("selftest: dedup no disponible (%s); se emite igual", exc)

    if emitir and notificar is not None:
        cuerpo = "\n".join(f"  \u2022 {x}" for x in problemas[:12])
        extra = ("\n  \u2026 y mas, ver /diagnostico"
                 if len(problemas) > 12 else "")
        try:
            notificar(
                f"\u26a0\ufe0f *Autodiagnostico* — {len(problemas)} problema(s)\n"
                f"{cuerpo}{extra}\n\nDetalle completo: /diagnostico")
        except Exception:  # noqa: BLE001
            log.exception("selftest: no se pudo notificar")
    return {"ok": False, "problemas": problemas, "alerto": bool(emitir)}
