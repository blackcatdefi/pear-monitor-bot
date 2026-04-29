"""Git deployment helper — pushes the current working tree to origin/master.

Clean replacement for the deprecated ``auto/github_push.py`` (which was
hardcoded to push from a stale ``deploy-round3/`` directory and overwrote
production on 2026-04-28).

Design goals:
    * Use the *real* git binary against the *real* working tree — no path
      hardcoding, no GitHub REST overlays.
    * Fail loudly with the underlying git stderr instead of silently
      succeeding.
    * Be importable from other helpers and runnable as ``python -m
      auto.git_deploy '<msg>'`` for one-off deploys.

R19 — 2026-04-29
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional, Tuple


def _run(
    args: list[str],
    cwd: str,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Thin wrapper that always returns text-mode CompletedProcess."""
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=capture,
        text=True,
    )


def deploy_to_master(
    commit_message: str,
    working_dir: Optional[str] = None,
    remote: str = "origin",
    branch: str = "master",
) -> Tuple[bool, str]:
    """Stage every change in the working tree, commit, and push to master.

    Args:
        commit_message: Git commit message (must be non-empty).
        working_dir: Directory inside the target repo. Defaults to CWD.
        remote: Git remote name. Defaults to ``origin``.
        branch: Target branch. Defaults to ``master``.

    Returns:
        ``(True, sha)``        — push succeeded.
        ``(True, "no_changes")`` — nothing was staged.
        ``(False, error_msg)`` — anything else (with stderr).
    """
    if not commit_message or not commit_message.strip():
        return (False, "commit_message is empty")

    cwd = working_dir or os.getcwd()

    toplevel = _run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    if toplevel.returncode != 0:
        return (False, f"Not a git repository: {cwd} ({toplevel.stderr.strip()})")
    repo_root = toplevel.stdout.strip()

    status = _run(["git", "status", "--porcelain"], cwd=repo_root)
    if status.returncode != 0:
        return (False, f"git status failed: {status.stderr.strip()}")
    if not status.stdout.strip():
        return (True, "no_changes")

    add = _run(["git", "add", "-A"], cwd=repo_root)
    if add.returncode != 0:
        return (False, f"git add failed: {add.stderr.strip()}")

    commit = _run(["git", "commit", "-m", commit_message], cwd=repo_root)
    if commit.returncode != 0:
        return (False, f"git commit failed: {commit.stderr.strip() or commit.stdout.strip()}")

    sha_proc = _run(["git", "rev-parse", "HEAD"], cwd=repo_root)
    if sha_proc.returncode != 0:
        return (False, f"git rev-parse failed: {sha_proc.stderr.strip()}")
    sha = sha_proc.stdout.strip()

    push = _run(["git", "push", remote, branch], cwd=repo_root)
    if push.returncode != 0:
        return (False, f"git push failed: {push.stderr.strip() or push.stdout.strip()}")

    return (True, sha)


def _cli() -> int:
    if len(sys.argv) < 2:
        print("Usage: python -m auto.git_deploy '<commit message>'")
        return 2
    msg = sys.argv[1]
    success, result = deploy_to_master(msg)
    if success:
        if result == "no_changes":
            print("[git_deploy] working tree clean — nothing to push")
        else:
            print(f"[git_deploy] deployed: {result}")
        return 0
    print(f"[git_deploy] FAILED: {result}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
