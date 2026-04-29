"""Trade del Ciclo — read/update helpers for the /ciclo and /ciclo_update bot
commands.

The canonical state lives in ``fund_state.py`` as module constants (BCD edits
them by hand when the Blofin position opens / closes). This module exposes:

* ``render_cycle_status()`` — builds the human-readable message shown by
  ``/ciclo``.
* ``apply_cycle_update(status, last_entry=None)`` — mutates ``fund_state.py``
  on disk, commits the change via git, and pushes to origin/master so Railway
  redeploys automatically. Uses GITHUB_TOKEN from env (or .secrets/tokens.env)
  if HTTPS push auth is needed.

Design notes:
 - We write to ``fund_state.py`` with a textual rewrite (regex-replace) so the
   diff is minimal and human-reviewable.
 - Mutation is guarded: only STATUS values in {"OPEN", "CLOSED"} are allowed,
   LAST_ENTRY must parse as float.
 - On Railway (no git repo present / no push token), write succeeds but push
   fails → we report the partial success so BCD can commit via GitHub web UI.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# fund_state.py lives one directory up from this file (modules/)
_REPO_ROOT = Path(__file__).resolve().parent.parent
_FUND_STATE_PATH = _REPO_ROOT / "fund_state.py"

# Parent of fondo-blackcat-bot/ is the git repo root
_GIT_REPO_ROOT = _REPO_ROOT.parent


def _read_fund_state() -> dict[str, Any]:
    """Import fund_state fresh (importlib) and return the trade-del-ciclo fields.

    We import inside the function so tests/reloads see live values after a
    /ciclo_update rewrite (though the running bot only sees the post-redeploy
    values — Python does not hot-reload module constants).
    """
    import importlib

    import fund_state  # type: ignore

    importlib.reload(fund_state)
    return {
        "status": getattr(fund_state, "TRADE_DEL_CICLO_STATUS", "?"),
        "platform": getattr(fund_state, "TRADE_DEL_CICLO_PLATFORM", "?"),
        "leverage": getattr(fund_state, "TRADE_DEL_CICLO_LEVERAGE", 0),
        "last_entry": getattr(fund_state, "TRADE_DEL_CICLO_LAST_ENTRY", 0.0),
        "last_update": getattr(fund_state, "TRADE_DEL_CICLO_LAST_UPDATE", "?"),
        "last_close": getattr(fund_state, "TRADE_DEL_CICLO_LAST_CLOSE", ""),
        "pnl_realized": getattr(fund_state, "TRADE_DEL_CICLO_PNL_REALIZED", 0.0),
        "blofin_balance": getattr(fund_state, "BLOFIN_BALANCE_AVAILABLE", 0.0),
        "basket_v5_plan": getattr(fund_state, "BASKET_V5_PLAN", {}),
    }


def render_cycle_status() -> str:
    """Human-readable /ciclo output."""
    s = _read_fund_state()
    status = (s["status"] or "?").upper()
    status_icon = "\U0001f7e2" if status == "OPEN" else "\U0001f534" if status == "CLOSED" else "\u26aa"
    plan = s["basket_v5_plan"] or {}
    bonus_unlock = plan.get("bonus_blofin_unlock", "?")

    lines = [
        "\U0001f3af TRADE DEL CICLO",
        "\u2501" * 28,
        f"Status: {status_icon} {status}",
    ]
    if status == "OPEN":
        lines.append(f"\u00daltimo entry: ${s['last_entry']:,.2f}")
        lines.append(f"Leverage: {s['leverage']}x")
        lines.append(f"Plataforma: {(s['platform'] or '?').title()}")
        lines.append(f"\u00daltima actualizaci\u00f3n: {s['last_update']}")
        lines.append("")
        lines.append(
            "\u26a0\ufe0f Blofin NO tiene API p\u00fablica. El UPnL real se consulta "
            "manualmente en la app."
        )
    elif status == "CLOSED":
        lines.append(f"PnL realizado: ${s['pnl_realized']:+,.2f}")
        last_close = s["last_close"] or "?"
        lines.append(f"\u00daltimo close: {last_close[:16].replace('T', ' ')} UTC")
        lines.append(f"Plataforma: {(s['platform'] or '?').title()}")
        lines.append(f"\u00daltimo entry (cerrado): ${s['last_entry']:,.2f}")
        lines.append(f"Balance Blofin disponible: ${s['blofin_balance']:,.2f} USDT")
    else:
        lines.append("(estado desconocido — revisar fund_state.py)")

    lines.append("")
    lines.append(f"\U0001f381 Bono Blofin unlock: {bonus_unlock}")
    lines.append("")
    lines.append("Para cambiar estado:")
    lines.append("  /ciclo_update OPEN 77000   \u2192 abre con entry $77,000")
    lines.append("  /ciclo_update CLOSED       \u2192 cierra posici\u00f3n")

    return "\n".join(lines)


# ─── Mutators (/ciclo_update) ──────────────────────────────────────────────


_STATUS_RE = re.compile(
    r'^(TRADE_DEL_CICLO_STATUS\s*=\s*)"[^"]*"(.*)$', re.MULTILINE
)
_LAST_ENTRY_RE = re.compile(
    r"^(TRADE_DEL_CICLO_LAST_ENTRY\s*=\s*)[\d\.]+(.*)$", re.MULTILINE
)
_LAST_UPDATE_RE = re.compile(
    r'^(TRADE_DEL_CICLO_LAST_UPDATE\s*=\s*)"[^"]*"(.*)$', re.MULTILINE
)
_LAST_CLOSE_RE = re.compile(
    r'^(TRADE_DEL_CICLO_LAST_CLOSE\s*=\s*)"[^"]*"(.*)$', re.MULTILINE
)


def apply_cycle_update(new_status: str, last_entry: float | None = None) -> dict[str, Any]:
    """Rewrite fund_state.py, commit and push.

    Returns a dict: ``{"ok": bool, "wrote": bool, "pushed": bool, "message": str}``.
    """
    new_status = new_status.strip().upper()
    if new_status not in {"OPEN", "CLOSED"}:
        return {
            "ok": False,
            "wrote": False,
            "pushed": False,
            "message": f"STATUS inv\u00e1lido: {new_status}. Valores v\u00e1lidos: OPEN | CLOSED",
        }

    if not _FUND_STATE_PATH.is_file():
        return {
            "ok": False,
            "wrote": False,
            "pushed": False,
            "message": f"No encuentro {_FUND_STATE_PATH}",
        }

    text = _FUND_STATE_PATH.read_text(encoding="utf-8")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1. status
    text, n_status = _STATUS_RE.subn(
        rf'\1"{new_status}"\2', text, count=1
    )
    # 2. last_update (bumped always)
    text, n_update = _LAST_UPDATE_RE.subn(
        rf'\1"{now_iso}"\2', text, count=1
    )
    # 3. last_entry (only when OPEN and provided)
    n_entry = 0
    if new_status == "OPEN" and last_entry is not None:
        text, n_entry = _LAST_ENTRY_RE.subn(
            rf"\g<1>{last_entry:.2f}\g<2>", text, count=1
        )
    # 4. last_close (bumped on CLOSED)
    n_close = 0
    if new_status == "CLOSED":
        text, n_close = _LAST_CLOSE_RE.subn(
            rf'\1"{now_iso}"\2', text, count=1
        )

    if n_status == 0 or n_update == 0:
        return {
            "ok": False,
            "wrote": False,
            "pushed": False,
            "message": "No pude localizar las constantes TRADE_DEL_CICLO_* en fund_state.py",
        }

    _FUND_STATE_PATH.write_text(text, encoding="utf-8")
    log.info(
        "fund_state.py rewritten: STATUS=%s last_entry_updated=%s last_close_updated=%s",
        new_status,
        bool(n_entry),
        bool(n_close),
    )

    # Commit + push. If no git/tokens we still return ok=True for the write.
    push_result = _git_commit_and_push(new_status, last_entry)
    return {
        "ok": True,
        "wrote": True,
        "pushed": push_result["pushed"],
        "message": push_result["message"],
    }


def _git_commit_and_push(new_status: str, last_entry: float | None) -> dict[str, Any]:
    """Commit fund_state.py and push to origin. Best-effort; no exceptions escape."""
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        # Try tokens.env fallback (local dev). Railway runtime will not have it.
        tokens_env = _GIT_REPO_ROOT.parent / ".secrets" / "tokens.env"
        if tokens_env.is_file():
            for line in tokens_env.read_text(encoding="utf-8").splitlines():
                if line.startswith("GITHUB_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                    break

    entry_note = f" entry=${last_entry:,.2f}" if last_entry is not None else ""
    msg = f"chore(fund_state): /ciclo_update STATUS={new_status}{entry_note}"

    def _run(args: list[str], *, env_extra: dict[str, str] | None = None) -> tuple[int, str]:
        env = os.environ.copy()
        if env_extra:
            env.update(env_extra)
        proc = subprocess.run(
            args,
            cwd=str(_GIT_REPO_ROOT),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()

    # Ensure we are inside a git repo
    rc, out = _run(["git", "rev-parse", "--is-inside-work-tree"])
    if rc != 0:
        return {"pushed": False, "message": f"No es repo git — write OK pero sin push. ({out})"}

    # stage + commit
    rel_path = str(_FUND_STATE_PATH.relative_to(_GIT_REPO_ROOT))
    rc, out = _run(["git", "add", rel_path])
    if rc != 0:
        return {"pushed": False, "message": f"git add fall\u00f3: {out}"}

    rc, out = _run([
        "git",
        "-c", "user.email=bot@blackcatdefi.local",
        "-c", "user.name=fondo-blackcat-bot",
        "commit",
        "-m", msg,
    ])
    if rc != 0:
        low = out.lower()
        if "nothing to commit" in low or "no changes added" in low:
            return {"pushed": False, "message": "Nada para commitear (sin cambios)."}
        return {"pushed": False, "message": f"git commit fall\u00f3: {out}"}

    if not token:
        return {
            "pushed": False,
            "message": "Commit local OK, pero sin GITHUB_TOKEN → no push. Editar manual en GitHub.",
        }

    # Configure push URL with token (one-shot via `-c`)
    remote_url = f"https://x-access-token:{token}@github.com/blackcatdefi/pear-monitor-bot.git"
    rc, out = _run([
        "git", "push", remote_url, "HEAD:master",
    ])
    if rc != 0:
        return {"pushed": False, "message": f"git push fall\u00f3: {out[:200]}"}

    return {"pushed": True, "message": "Commit + push OK. Railway redeployea autom\u00e1ticamente."}


def parse_cycle_update_args(args: list[str]) -> tuple[str, float | None]:
    """Parse /ciclo_update CLI-style args.

    Examples:
        ["OPEN", "77000"]      → ("OPEN", 77000.0)
        ["open", "75,298.70"]  → ("OPEN", 75298.70)
        ["CLOSED"]             → ("CLOSED", None)
    Raises ValueError on malformed input.
    """
    if not args:
        raise ValueError("Uso: /ciclo_update <STATUS> [LAST_ENTRY]\n  STATUS = OPEN | CLOSED")
    status = args[0].strip().upper()
    if status not in {"OPEN", "CLOSED"}:
        raise ValueError(f"STATUS inv\u00e1lido: {status}. V\u00e1lidos: OPEN | CLOSED")
    if status == "CLOSED":
        return status, None
    # OPEN requires last_entry
    if len(args) < 2:
        raise ValueError("OPEN requiere LAST_ENTRY. Uso: /ciclo_update OPEN 77000")
    raw = args[1].replace(",", "").replace("$", "").strip()
    try:
        entry = float(raw)
    except ValueError as exc:
        raise ValueError(f"LAST_ENTRY inv\u00e1lido: {args[1]}") from exc
    if entry <= 0:
        raise ValueError("LAST_ENTRY debe ser > 0")
    return status, entry


__all__ = [
    "apply_cycle_update",
    "parse_cycle_update_args",
    "render_cycle_status",
]
