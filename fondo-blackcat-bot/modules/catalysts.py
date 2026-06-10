"""R-BOT-DEFINITIVE WI-1 — Catalyst engine (macro calendar with REAL sources).

Problem it kills: DESTACADO printed "NEXT CATALYST <72h: ninguno" the night
before US CPI because the old path only merged the (stale) macro_calendar
SQLite roadmap + token unlocks — it had NO macro-release data source.

Three sources, one SQLite table ``catalysts``:

  a) FRED releases calendar API (free, key already in env ``FRED_API_KEY``):
     upcoming release dates for CPI, PPI and Employment Situation. Release ids
     are RESOLVED dynamically via ``/fred/releases`` (name match) with the
     canonical ids (CPI=10, PPI=46, Employment Situation=50) as fallback.
     Refreshed daily by ``refresh_catalysts`` (scheduler job).
  b) Official 2026 FOMC meeting calendar (federalreserve.gov, verified
     2026-06-10): Jan 27-28, Mar 17-18, Apr 28-29, **Jun 16-17**, Jul 28-29,
     Sep 15-16, Oct 27-28, Dec 8-9. Mar/Jun/Sep/Dec carry the dot plot (SEP).
  c) Manual entries via ``/setcatalyst add|del|list`` (bot command).

Consumers:
  * ``next_catalyst_candidates(window_hours)`` feeds the DESTACADO
    "NEXT CATALYST <72h" header line (merged with unlocks by formatters).
  * ``build_llm_catalyst_block(days=7)`` injects the next 7 days of catalysts
    into the FULL ANALYSIS prompt so the catalysts section is DETERMINISTIC —
    never LLM memory.

Robustness: NEVER raises from any public function. No paid APIs.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

try:
    from config import DATA_DIR
except Exception:  # noqa: BLE001 — importable in isolated tests
    DATA_DIR = os.getenv("DATA_DIR", "/tmp")

DB_PATH = os.path.join(DATA_DIR, "catalysts.db")

IMPACTS = ("low", "medium", "high", "critical")
_IMPACT_EMOJI = {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}
_IMPACT_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}

# ── FRED releases we track. Canonical ids (verified against the public FRED
#    catalog) used as fallback when the dynamic /fred/releases lookup fails. ──
FRED_RELEASES = {
    "Consumer Price Index": {"fallback_id": 10, "name": "US CPI", "impact": "critical"},
    "Producer Price Index": {"fallback_id": 46, "name": "US PPI", "impact": "high"},
    "Employment Situation": {"fallback_id": 50, "name": "US Employment Situation (NFP)", "impact": "critical"},
}
# US BLS releases are at 08:30 ET = 12:30/13:30 UTC depending on DST.
_FRED_RELEASE_TIME_UTC = "12:30"

# ── Official 2026 FOMC calendar (federalreserve.gov, verified 2026-06-10).
#    Date = SECOND day (decision + presser). dot=True → SEP/dot-plot meeting. ──
FOMC_2026 = [
    ("2026-01-28", False),
    ("2026-03-18", True),
    ("2026-04-29", False),
    ("2026-06-17", True),
    ("2026-07-29", False),
    ("2026-09-16", True),
    ("2026-10-28", False),
    ("2026-12-09", True),
]
_FOMC_TIME_UTC = "18:00"  # 2:00 pm ET statement


@dataclass
class Catalyst:
    id: int
    date_utc: str            # YYYY-MM-DD
    time_utc: str | None     # HH:MM or None
    name: str
    impact: str
    source: str              # fred | fomc | manual | seed

    @property
    def dt(self) -> datetime:
        """Best-effort UTC datetime (date-only → 12:00 UTC midpoint)."""
        t = self.time_utc or "12:00"
        try:
            return datetime.strptime(f"{self.date_utc} {t}", "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return datetime.strptime(self.date_utc, "%Y-%m-%d").replace(
                hour=12, tzinfo=timezone.utc
            )


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS catalysts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_utc TEXT NOT NULL,
            time_utc TEXT,
            name TEXT NOT NULL,
            impact TEXT NOT NULL DEFAULT 'medium',
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(date_utc, name)
        )
        """
    )
    return conn


def _norm_impact(impact: str | None) -> str:
    s = (impact or "medium").strip().lower()
    return s if s in IMPACTS else "medium"


def add_catalyst(
    date_utc: str,
    name: str,
    time_utc: str | None = None,
    impact: str = "medium",
    source: str = "manual",
) -> int | None:
    """Insert (or update impact/time of) one catalyst. Returns row id, None on error."""
    try:
        datetime.strptime(date_utc, "%Y-%m-%d")
        if time_utc:
            datetime.strptime(time_utc, "%H:%M")
        conn = _conn()
        try:
            conn.execute(
                """
                INSERT INTO catalysts (date_utc, time_utc, name, impact, source)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(date_utc, name) DO UPDATE SET
                  time_utc=COALESCE(excluded.time_utc, time_utc),
                  impact=excluded.impact,
                  source=excluded.source
                """,
                (date_utc, time_utc, name.strip(), _norm_impact(impact), source),
            )
            conn.commit()
            cur = conn.execute(
                "SELECT id FROM catalysts WHERE date_utc=? AND name=?",
                (date_utc, name.strip()),
            )
            row = cur.fetchone()
            return int(row["id"]) if row else None
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("add_catalyst(%s, %s) failed: %s", date_utc, name, exc)
        return None


def delete_catalyst(cat_id: int) -> bool:
    try:
        conn = _conn()
        try:
            cur = conn.execute("DELETE FROM catalysts WHERE id=?", (int(cat_id),))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("delete_catalyst(%s) failed: %s", cat_id, exc)
        return False


def list_catalysts(
    *,
    include_past: bool = False,
    limit: int = 40,
    now: datetime | None = None,
) -> list[Catalyst]:
    """Upcoming catalysts ordered by date asc. NEVER raises (empty on error)."""
    try:
        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        conn = _conn()
        try:
            if include_past:
                cur = conn.execute(
                    "SELECT * FROM catalysts ORDER BY date_utc ASC, time_utc ASC LIMIT ?",
                    (limit,),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM catalysts WHERE date_utc >= ? "
                    "ORDER BY date_utc ASC, time_utc ASC LIMIT ?",
                    (cutoff, limit),
                )
            out = [
                Catalyst(
                    id=int(r["id"]),
                    date_utc=r["date_utc"],
                    time_utc=r["time_utc"],
                    name=r["name"],
                    impact=r["impact"] or "medium",
                    source=r["source"] or "manual",
                )
                for r in cur.fetchall()
            ]
        finally:
            conn.close()
        if not include_past:
            out = [c for c in out if c.dt >= now - timedelta(hours=12)]
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("list_catalysts failed: %s", exc)
        return []


# ─── Source b: FOMC official calendar (hardcoded, verified) ─────────────────


def sync_fomc_calendar() -> int:
    """Upsert every 2026 FOMC meeting. Idempotent. Returns rows touched."""
    n = 0
    for date_str, dot in FOMC_2026:
        name = "FOMC decision + dot plots (SEP)" if dot else "FOMC decision"
        if add_catalyst(date_str, name, time_utc=_FOMC_TIME_UTC,
                        impact="critical", source="fomc") is not None:
            n += 1
    return n


# ─── Source a: FRED releases calendar ───────────────────────────────────────


async def _fred_get(path: str, params: dict[str, Any]) -> Any:
    import httpx
    api_key = os.getenv("FRED_API_KEY", "").strip()
    if not api_key:
        return None
    base = "https://api.stlouisfed.org/fred"
    p = dict(params)
    p.update({"api_key": api_key, "file_type": "json"})
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(f"{base}/{path}", params=p)
        r.raise_for_status()
        return r.json()


async def _resolve_release_ids() -> dict[str, int]:
    """Resolve FRED release ids by NAME via /fred/releases; fallback canonical."""
    ids = {k: v["fallback_id"] for k, v in FRED_RELEASES.items()}
    try:
        data = await _fred_get("releases", {"limit": 1000})
        for rel in (data or {}).get("releases", []) or []:
            rname = str(rel.get("name") or "")
            for key in FRED_RELEASES:
                if rname.strip().lower() == key.lower():
                    try:
                        ids[key] = int(rel["id"])
                    except (KeyError, TypeError, ValueError):
                        pass
    except Exception as exc:  # noqa: BLE001
        log.info("FRED /releases lookup failed (using canonical ids): %s", exc)
    return ids


async def refresh_fred_catalysts(days_ahead: int = 45) -> int:
    """Pull upcoming CPI/PPI/Employment release dates from FRED. Returns count.

    Requires FRED_API_KEY (already in Railway env). Missing key → 0, silent.
    NEVER raises.
    """
    try:
        if not os.getenv("FRED_API_KEY", "").strip():
            return 0
        ids = await _resolve_release_ids()
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(days=days_ahead)
        n = 0
        for key, meta in FRED_RELEASES.items():
            rid = ids.get(key, meta["fallback_id"])
            try:
                data = await _fred_get(
                    f"release/dates",
                    {
                        "release_id": rid,
                        "include_release_dates_with_no_data": "true",
                        "realtime_start": now.strftime("%Y-%m-%d"),
                        "realtime_end": horizon.strftime("%Y-%m-%d"),
                        "sort_order": "asc",
                        "limit": 30,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                log.info("FRED release/dates %s failed: %s", rid, exc)
                continue
            for rd in (data or {}).get("release_dates", []) or []:
                d = str(rd.get("date") or "")
                try:
                    dt = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if not (now - timedelta(days=1) <= dt <= horizon):
                    continue
                if add_catalyst(d, meta["name"], time_utc=_FRED_RELEASE_TIME_UTC,
                                impact=meta["impact"], source="fred") is not None:
                    n += 1
        return n
    except Exception as exc:  # noqa: BLE001
        log.warning("refresh_fred_catalysts failed: %s", exc)
        return 0


# ─── Seed (post-deploy migration, idempotent) ───────────────────────────────

SEED_EVENTS = [
    ("2026-06-10", "12:30", "US CPI", "critical"),
    ("2026-06-11", "12:30", "US PPI", "high"),
    ("2026-06-11", None, "Argentina CPI (INDEC)", "medium"),
    ("2026-06-12", None, "SpaceX IPO (SPCX Nasdaq — liquidity drain)", "high"),
]


def seed_catalysts() -> int:
    """Seed the ticket's known June events + FOMC calendar. Idempotent."""
    n = 0
    try:
        for d, t, name, impact in SEED_EVENTS:
            if add_catalyst(d, name, time_utc=t, impact=impact, source="seed") is not None:
                n += 1
        n += sync_fomc_calendar()
    except Exception as exc:  # noqa: BLE001
        log.warning("seed_catalysts failed: %s", exc)
    return n


async def refresh_catalysts() -> dict[str, int]:
    """Daily scheduler entry point: seed/sync static + pull FRED. NEVER raises."""
    out = {"seed": 0, "fred": 0}
    try:
        out["seed"] = seed_catalysts()
    except Exception:  # noqa: BLE001
        pass
    try:
        out["fred"] = await refresh_fred_catalysts()
    except Exception:  # noqa: BLE001
        pass
    return out


# ─── Consumers ──────────────────────────────────────────────────────────────


def next_catalyst_candidates(
    window_hours: int = 72,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Header candidates inside the window: [{label, dt, emoji, rank}].

    Shaped exactly like the candidates the DESTACADO header merger consumes.
    NEVER raises (empty list on any failure).
    """
    try:
        now = now or datetime.now(timezone.utc)
        window = timedelta(hours=window_hours)
        out: list[dict[str, Any]] = []
        for c in list_catalysts(now=now, limit=60):
            delta = c.dt - now
            if timedelta(0) <= delta <= window:
                out.append({
                    "label": c.name,
                    "dt": c.dt,
                    "emoji": _IMPACT_EMOJI.get(c.impact, "⚪"),
                    "rank": _IMPACT_RANK.get(c.impact, 0),
                })
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("next_catalyst_candidates failed: %s", exc)
        return []


def build_llm_catalyst_block(days: int = 7, now: datetime | None = None) -> str:
    """Deterministic catalysts block for the FULL ANALYSIS prompt context.

    The LLM must consume THESE dates verbatim — the catalysts section is never
    written from model memory. Empty window → explicit "sin catalysts" line so
    the model can't invent any. NEVER raises ("" on hard failure).
    """
    try:
        now = now or datetime.now(timezone.utc)
        horizon = now + timedelta(days=days)
        rows = [c for c in list_catalysts(now=now, limit=60) if c.dt <= horizon]
        lines = [
            "═══════ CATALYSTS PRÓXIMOS 7 DÍAS (TABLA AUTORITATIVA — fuente: "
            "FRED + FOMC oficial + manual) ═══════",
            "La sección de catalysts del reporte se escribe SOLO con estas filas, "
            "VERBATIM. PROHIBIDO inventar, recordar o estimar fechas de CPI/PPI/"
            "FOMC/unlocks por memoria del modelo. Si la tabla está vacía, decí "
            "explícitamente que no hay catalysts registrados en la ventana.",
            "",
        ]
        if not rows:
            lines.append(f"(sin catalysts registrados entre hoy y {horizon.strftime('%Y-%m-%d')})")
        for c in rows:
            t = f" {c.time_utc} UTC" if c.time_utc else ""
            em = _IMPACT_EMOJI.get(c.impact, "⚪")
            lines.append(f"• {c.date_utc}{t} — {em} {c.name} [impact={c.impact} · src={c.source}]")
        lines.append("═══════ FIN CATALYSTS ═══════")
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        log.warning("build_llm_catalyst_block failed: %s", exc)
        return ""


# ─── /setcatalyst command surface ───────────────────────────────────────────


def format_catalyst_list(now: datetime | None = None) -> str:
    rows = list_catalysts(now=now, limit=40)
    if not rows:
        return ("📅 CATALYSTS: tabla vacía.\n"
                "Usá /setcatalyst add YYYY-MM-DD [HH:MM] <nombre> [impact]")
    lines = [f"📅 CATALYSTS PRÓXIMOS ({len(rows)})", "─" * 34]
    for c in rows:
        t = f" {c.time_utc}" if c.time_utc else ""
        em = _IMPACT_EMOJI.get(c.impact, "⚪")
        lines.append(f"{em} #{c.id} {c.date_utc}{t} — {c.name} [{c.impact}/{c.source}]")
    lines.append("")
    lines.append("/setcatalyst add YYYY-MM-DD [HH:MM] <nombre> [impact]")
    lines.append("/setcatalyst del <id> · /setcatalyst list")
    return "\n".join(lines)


def handle_setcatalyst(args: list[str]) -> str:
    """Pure command handler (testable without Telegram). NEVER raises."""
    try:
        if not args:
            return ("Uso: /setcatalyst add YYYY-MM-DD [HH:MM] <nombre> [impact]\n"
                    "     /setcatalyst del <id>\n"
                    "     /setcatalyst list")
        sub = args[0].strip().lower()
        if sub == "list":
            return format_catalyst_list()
        if sub == "del":
            if len(args) < 2 or not args[1].isdigit():
                return "Uso: /setcatalyst del <id> (id numérico de /setcatalyst list)"
            ok = delete_catalyst(int(args[1]))
            return f"🗑 Catalyst #{args[1]} eliminado." if ok else f"⚠️ No existe el id #{args[1]}."
        if sub == "add":
            rest = args[1:]
            if not rest:
                return "Uso: /setcatalyst add YYYY-MM-DD [HH:MM] <nombre> [impact]"
            date_str = rest[0]
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                return f"⚠️ Fecha inválida '{date_str}' (formato YYYY-MM-DD)."
            rest = rest[1:]
            time_str: str | None = None
            if rest:
                try:
                    datetime.strptime(rest[0], "%H:%M")
                    time_str = rest[0]
                    rest = rest[1:]
                except ValueError:
                    time_str = None
            impact = "medium"
            if rest and rest[-1].strip().lower() in IMPACTS:
                impact = rest[-1].strip().lower()
                rest = rest[:-1]
            name = " ".join(rest).strip()
            if not name:
                return "⚠️ Falta el nombre del catalyst."
            cid = add_catalyst(date_str, name, time_utc=time_str, impact=impact, source="manual")
            if cid is None:
                return "⚠️ No se pudo guardar el catalyst (ver logs)."
            t = f" {time_str} UTC" if time_str else ""
            return f"✅ Catalyst #{cid} guardado: {date_str}{t} — {name} [{impact}]"
        return f"Subcomando desconocido '{sub}'. Usá add / del / list."
    except Exception as exc:  # noqa: BLE001
        log.exception("handle_setcatalyst failed")
        return f"⚠️ Error procesando /setcatalyst: {exc}"
