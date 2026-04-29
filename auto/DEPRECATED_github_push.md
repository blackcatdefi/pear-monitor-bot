# DEPRECATED: `auto/github_push.py`

**Status**: Deprecated as of R19 (2026-04-29).

**Reason**: The legacy script was hardcoded to push from a `deploy-round3/`
sub-directory regardless of which files actually changed. On 2026-04-28 it
overwrote production code with stale state (commit `6ee50eb`), forcing a
recovery via overlay commit `f013b57`.

**Replacement**: use `auto/git_deploy.py`.

```python
from auto.git_deploy import deploy_to_master

ok, sha = deploy_to_master("R19 — your message here")
if ok and sha != "no_changes":
    print(f"Deployed: {sha}")
```

Or one-shot from the shell:

```bash
python -m auto.git_deploy "R19 — your message here"
```

The new helper:

* Runs `git` against the *real* working tree — no path hardcoding.
* Returns `(False, stderr)` instead of silently succeeding.
* Treats an empty working tree as `(True, "no_changes")`.

Do **not** invoke `auto/github_push.py` (the legacy file lives only in the
local workspace mirror, never in this repo). It remains for forensic
reference and will be removed in a future cleanup round.

---

R19 — operational hardening pass after R18 + R(v2). Single source of truth
for pushes is now `git` itself, wrapped by `auto.git_deploy.deploy_to_master`.
