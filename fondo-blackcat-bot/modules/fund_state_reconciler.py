"""Round 17 — Detect and reconcile fund_state.py vs on-chain reality.

Problema que resuelve: BCD/Claude editaban a mano fund_state.BASKET_V5_STATUS
cuando se abría/cerraba un basket. Esto causaba que el LLM dijera
"basket v5 PENDING_CAPITAL" cuando en realidad ya estaba ACTIVE 4 días.

Comportamiento:
    - reconcile_fund_state() compara las wallets de basket vs declared status
    - render_discrepancies(...) formatea bonito para Telegram
    - apply_basket_v5_status() reescribe el archivo + git commit/push (igual
      que cycle_trade._git_commit_and_push)

Reglas:
    1. Si BASKET_STATUS["active"]=False y existen posiciones SHORT con notional
       > $50 sobre wallets de ALT_SHORT_BLEED_WALLETS → discrepancia
       PHANTOM_BASKET (v5 está corriendo y nadie le avisó al state).
    2. Si BASKET_V5_STATUS == "ACTIVE" pero no hay posiciones → GHOST_BASKET.
    3. Si BASKET_V5_STATUS == "PENDING_CAPITAL" pero hay posiciones → mover a
       ACTIVE.

El scheduler corre cada N minutos. La PRIMERA vez que detecta una
discrepancia nueva, dispara una alerta Telegram (max 1 por día por tipo).
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import DATA_DIR, TELEGRAM_CHAT_ID

log = logging.getLogger(__name__)

_RECONCILE_STATE_FILE = os.path.join(DATA_DIR, "reconcile_state.json")

_FUND_STATE_PATH = Path(__file__).resolve().parent.parent / "fund_state.py"
_GIT_REPO_ROOT = _FUND_STATE_PATH.parents[1]


@dataclass
class Discrepancy:
    type: str  # PHANTOM_BASKET | GHOST_BASKET | STATUS_OUT_OF_SYNC
    wallet: str | None
    suggested_action: str
    detail: str
    detected_at: str
    suggested_basket_v5_status: str | None = None


# ─── State persistence ───────────────────────────────────────────────────────


def _load_state() -> dict[str, Any]:
    if not os.path.isfile(_RECONCILE_STATE_FILE):
        return {}
    try:
        with open(_RECONCILE_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        with open(_RECONCILE_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        log.exception("Could not save reconcile_state.json")


# ─── Detection logic ─────────────────────────────────────────────────────────


def _wallet_matches_basket_label(wallet_addr: str) -> bool:
    """True if `wallet_addr` is in fund_state.ALT_SHORT_BLEED_WALLETS by prefix."""
    try:
        from fund_state import ALT_SHORT_BLEED_WALLETS
    except Exception:
        return False
    addr_low = (wallet_addr or "").lower()
    for prefix in ALT_SHORT_BLEED_WALLETS:
        if addr_low.startswith((prefix or "").lower()):
            return True
    return False


async def reconcile_fund_state() -> list[Discrepancy]:
    """Compare on-chain reality vs declared state. Return list of discrepancies."""
    from modules.portfolio import fetch_all_wallets
    try:
        from fund_state import (
            BASKET_STATUS,
            BASKET_V5_STATUS,
            BASKET_PERP_TOKENS,
        )
    except Exception:
        log.exception("fund_state import failed")
        return []

    out: list[Discrepancy] = []
    wallets = await fetch_all_wallets()
    now_iso = datetime.now(timezone.utc).isoformat()

    # Aggregate basket SHORT positions across the fund's known basket wallets.
    total_basket_notional = 0.0
    basket_tokens_seen: set[str] = set()
    basket_wallets_with_pos: set[str] = set()

    for w in wallets or []:
        if w.get("status") != "ok":
            continue
        d = w.get("data", {})
        addr = (d.get("wallet") or "").lower()
        # Iterate positions and identify basket SHORTs
        for pos in d.get("positions") or []:
            coin = (pos.get("coin") or "").upper()
            if coin not in BASKET_PERP_TOKENS:
                continue
            try:
                ntl = float(pos.get("position_value") or pos.get("ntl_pos") or 0.0)
            except Exception:
                ntl = 0.0
            try:
                szi = float(pos.get("szi") or 0.0)
            except Exception:
                szi = 0.0
            if abs(ntl) < 50:
                continue  # ignore dust
            # SHORT means szi < 0
            if szi >= 0:
                continue
            total_basket_notional += abs(ntl)
            basket_tokens_seen.add(coin)
            basket_wallets_with_pos.add(addr)

    has_active_basket = total_basket_notional > 100  # >$100 notional total
    declared_active = bool(BASKET_STATUS.get("active")) or BASKET_V5_STATUS == "ACTIVE"

    # ── Case 1: PHANTOM_BASKET (real positions, but state says inactive) ────
    if has_active_basket and not declared_active:
        suggested = (
            "Update fund_state.py:\n"
            "  BASKET_STATUS['active'] = True\n"
            "  BASKET_V5_STATUS = 'ACTIVE'"
        )
        detail = (
            f"On-chain: {len(basket_tokens_seen)} tokens SHORT activos "
            f"({', '.join(sorted(basket_tokens_seen))}) "
            f"sobre {len(basket_wallets_with_pos)} wallets, notional ~${total_basket_notional:,.0f}.\n"
            f"Declared: BASKET_V5_STATUS='{BASKET_V5_STATUS}', "
            f"BASKET_STATUS.active={BASKET_STATUS.get('active')}"
        )
        out.append(
            Discrepancy(
                type="PHANTOM_BASKET",
                wallet=None,
                suggested_action=suggested,
                detail=detail,
                detected_at=now_iso,
                suggested_basket_v5_status="ACTIVE",
            )
        )

    # ── Case 2: GHOST_BASKET (state says ACTIVE, but no positions) ──────────
    if declared_active and not has_active_basket:
        suggested = (
            "Update fund_state.py:\n"
            "  BASKET_STATUS['active'] = False\n"
            "  BASKET_V5_STATUS = 'CLOSED'"
        )
        detail = (
            f"On-chain: 0 posiciones SHORT (notional <$100).\n"
            f"Declared: BASKET_V5_STATUS='{BASKET_V5_STATUS}', "
            f"BASKET_STATUS.active={BASKET_STATUS.get('active')}"
        )
        out.append(
            Discrepancy(
                type="GHOST_BASKET",
                wallet=None,
                suggested_action=suggested,
                detail=detail,
                detected_at=now_iso,
                suggested_basket_v5_status="CLOSED",
            )
        )

    # ── Case 3: PENDING_CAPITAL with positions (rename to ACTIVE) ───────────
    if has_active_basket and BASKET_V5_STATUS == "PENDING_CAPITAL":
        suggested = "Update fund_state.py:\n  BASKET_V5_STATUS = 'ACTIVE'"
        detail = (
            f"State dice PENDING_CAPITAL pero hay {len(basket_tokens_seen)} "
            f"tokens SHORT con ${total_basket_notional:,.0f} notional. "
            f"Capital ya deployado."
        )
        # already covered by PHANTOM_BASKET above — only emit if wasn't emitted
        if not any(d.type == "PHANTOM_BASKET" for d in out):
            out.append(
                Discrepancy(
                    type="STATUS_OUT_OF_SYNC",
                    wallet=None,
                    suggested_action=suggested,
                    detail=detail,
                    detected_at=now_iso,
                    suggested_basket_v5_status="ACTIVE",
                )
            )

    return out


def format_reconcile_report(discrepancies: list[Discrepancy]) -> str:
    """Alias used by bot.py — delegates to render_discrepancies."""
    return render_discrepancies(discrepancies)


def render_discrepancies(discrepancies: list[Discrepancy]) -> str:
    if not discrepancies:
        return (
            "✅ /reconcile — fund_state.py coherente con realidad on-chain.\n"
            "Nada que actualizar."
        )

    lines = [
        "🔄 RECONCILE — discrepancias detectadas",
        "─" * 40,
    ]
    for i, d in enumerate(discrepancies, 1):
        lines.append(f"\n[{i}] {d.type}")
        lines.append(f"    Detail: {d.detail}")
        lines.append(f"    Sugerencia:")
        for ln in d.suggested_action.splitlines():
            lines.append(f"      {ln}")
        lines.append(f"    Detected at: {d.detected_at}")

    lines.append("")
    lines.append(
        "Para aplicar automáticamente:\n"
        "  /reconcile apply"
    )
    lines.append("(Ejecuta git commit + push de fund_state.py)")
    return "\n".join(lines)


# ─── Apply (mutate fund_state.py + git push) ────────────────────────────────

# Reuses the same regex patterns as cycle_trade
_BASKET_V5_RE = re.compile(
    r'(BASKET_V5_STATUS\s*=\s*")([^"]+)(")'
)
_BASKET_ACTIVE_RE = re.compile(
    r'("active"\s*:\s*)(True|False)',
    re.IGNORECASE,
)
_BASKET_LAST_BASKET_RE = re.compile(r'("last_basket"\s*:\s*")([^"]+)(")')


def apply_basket_v5_status(new_status: str) -> dict[str, Any]:
    """Rewrite BASKET_V5_STATUS in fund_state.py and git push.

    Also flips BASKET_STATUS["active"] to True/False to keep both in sync.
    """
    if not _FUND_STATE_PATH.is_file():
        return {"ok": False, "wrote": False, "pushed": False, "message": "fund_state.py NOT found"}

    text = _FUND_STATE_PATH.read_text(encoding="utf-8")

    text2, n_v5 = _BASKET_V5_RE.subn(rf'\g<1>{new_status}\g<3>', text, count=1)
    target_active = "True" if new_status == "ACTIVE" else "False"
    text3, n_active = _BASKET_ACTIVE_RE.subn(rf'\g<1>{target_active}', text2, count=1)

    if n_v5 == 0 or n_active == 0:
        return {
            "ok": False,
            "wrote": False,
            "pushed": False,
            "message": (
                f"No pude localizar constantes (n_v5={n_v5}, n_active={n_active})"
            ),
        }

    _FUND_STATE_PATH.write_text(text3, encoding="utf-8")
    log.info(
        "fund_state.py: BASKET_V5_STATUS → %s, active → %s",
        new_status,
        target_active,
    )

    push = _git_commit_and_push(new_status)
    return {
        "ok": True,
        "wrote": True,
        "pushed": push["pushed"],
        "message": push["message"],
    }


def _git_commit_and_push(new_status: str) -> dict[str, Any]:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        tokens_env = _GIT_REPO_ROOT.parent / ".secrets" / "tokens.env"
        if tokens_env.is_file():
            for line in tokens_env.read_text(encoding="utf-8").splitlines():
                if line.startswith("GITHUB_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                    break

    msg = f"chore(fund_state): /reconcile auto BASKET_V5_STATUS={new_status}"

    def _run(args: list[str]) -> tuple[int, str]:
        proc = subprocess.run(
            args,
            cwd=str(_GIT_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()

    rc, out = _run(["git", "rev-parse", "--is-inside-work-tree"])
    if rc != 0:
        return {"pushed": False, "message": f"No es repo git: {out}"}

    rel = str(_FUND_STATE_PATH.relative_to(_GIT_REPO_ROOT))
    rc, out = _run(["git", "add", rel])
    if rc != 0:
        return {"pushed": False, "message": f"git add fail: {out}"}

    rc, out = _run([
        "git",
        "-c", "user.email=bot@blackcatdefi.local",
        "-c", "user.name=fondo-blackcat-bot",
        "commit", "-m", msg,
    ])
    if rc != 0:
        low = out.lower()
        if "nothing to commit" in low or "no changes added" in low:
            return {"pushed": False, "message": "Sin cambios para commitear."}
        return {"pushed": False, "message": f"commit fail: {out}"}

    if not token:
        return {"pushed": False, "message": "Sin GITHUB_TOKEN — commit local OK, push pendiente."}

    # Push via temporary remote URL
    rc, out = _run(["git", "remote", "get-url", "origin"])
    if rc != 0:
        return {"pushed": False, "message": f"sin remote origin: {out}"}
    origin_url = out.splitlines()[-1].strip()

    if "github.com" in origin_url:
        if origin_url.startswith("https://"):
            authed = origin_url.replace(
                "https://", f"https://x-access-token:{token}@", 1
            )
        else:
            authed = origin_url
    else:
        authed = origin_url

    rc, out = _run(["git", "push", authed, "HEAD:master"])
    if rc != 0:
        return {"pushed": False, "message": f"push fail: {out[-200:]}"}

    return {"pushed": True, "message": f"pushed: {msg}"}


# ─── Scheduler integration ───────────────────────────────────────────────────


async def scheduled_reconcile(bot) -> int:
    """Corre cada 15min. Detecta + alerta. Devuelve nº de alertas enviadas.

    Solo alerta cuando aparece una discrepancia que NO estaba ya alertada (rate
    limit 1× por día por tipo).
    """
    if os.getenv("AUTO_RECONCILE_ENABLED", "true").strip().lower() == "false":
        return 0
    if not TELEGRAM_CHAT_ID:
        return 0

    from utils.telegram import send_bot_message

    discrepancies = await reconcile_fund_state()
    state = _load_state()

    today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sent = 0
    types_alerted_today = set(state.get(today_key, []))

    for d in discrepancies:
        if d.type in types_alerted_today:
            continue
        msg = (
            f"🔄 RECONCILE — {d.type}\n"
            f"{d.detail}\n\n"
            f"Sugerencia:\n{d.suggested_action}\n\n"
            f"Ejecutar /reconcile para revisar y aplicar."
        )
        try:
            await send_bot_message(bot, TELEGRAM_CHAT_ID, msg)
            types_alerted_today.add(d.type)
            sent += 1
        except Exception:
            log.exception("scheduled_reconcile alert failed: %s", d.type)

    state[today_key] = list(types_alerted_today)
    # keep only last 7 days
    cutoff = (datetime.now(timezone.utc).date()).toordinal() - 7
    state = {
        k: v for k, v in state.items()
        if not k.startswith("20") or datetime.fromisoformat(k).toordinal() > cutoff
    }
    _save_state(state)

    if sent:
        log.info("scheduled_reconcile dispatched %d alert(s)", sent)
    return sent
