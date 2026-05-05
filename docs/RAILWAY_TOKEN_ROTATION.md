# Railway PAT Rotation Playbook

Permanent procedure for rotating the Railway Personal Access Token used by Cowork
for autonomous deploys to **gentle-luck** (and any sibling Railway projects in
the `blackcatdefi's Projects` workspace).

When to run: token revoked, suspected leak, expired, scope changed, or annual hygiene.

---

## Last rotation
- 2026-05-05 — `cowork-autonomous-deploy-20260505` (replacing previous `cowork`)
- Smoke deploy gentle-luck/pear-monitor-bot/production: deploy `3cff38a3-1339-4d72-be83-4361e3e8208c` SUCCESS, commit `740bd0ec`.

## Canonical IDs (workspace `blackcatdefi's Projects`)

| Project | Project ID | Service | Service ID | Env (`production`) ID |
|---|---|---|---|---|
| gentle-luck | `a8c939aa-338f-4a76-b2a6-f7551702c854` | pear-monitor-bot | `88dca3af-2e3b-48b9-950f-0fd50e74e887` | `9848b702-e186-40bf-85b7-811ef1342f12` |
| amusing-acceptance | `be38a440-37ee-455d-b9bf-0672a30659bb` | (Python bot) | look up via `services` query | look up via `environments` query |

If IDs ever drift, rediscover with:

```graphql
{ projects { edges { node { id name } } } }
{ project(id: "<project-id>") { services { edges { node { id name } } } environments { edges { node { id name } } } } }
```

---

## Step-by-step

### 1. Generate new PAT (Chrome MCP, no manual hand-off)

1. Cowork opens `https://railway.com/account/tokens` in the BCD-authenticated Brave/Chrome session.
2. Fill the **New Token** form:
   - **Name:** `cowork-autonomous-deploy-YYYYMMDD` (today's UTC date)
   - **Workspace:** `blackcatdefi's Projects`  ← MUST be set; default is `No workspace` and that scope cannot reach project mutations.
3. Click **Create**. The token is shown ONCE — capture it immediately.
4. Click **Got it** to dismiss the reveal.

### 2. Validate the PAT

```bash
RW_TOKEN="<new-token-uuid>"

# Workspace-scoped tokens reject `me` (expected). The authoritative probe is `projects`.
curl -s -H "Authorization: Bearer ${RW_TOKEN}" \
     -H "Content-Type: application/json" \
     -X POST https://backboard.railway.com/graphql/v2 \
     -d '{"query":"{ projects { edges { node { id name } } } }"}'
```

A 200 with all three projects (`trustworthy-flow`, `amusing-acceptance`, `gentle-luck`) confirms full workspace scope.

> Note: `{ me { name } }` returns `Not Authorized` for workspace-scoped PATs. That is **not** a token failure — it is by design. Do not treat it as a failed test. Use `projects` instead.

### 3. Persist in Cowork secrets store

Single source of truth: `<workspace>/.secrets/tokens.env`, key `RAILWAY_TOKEN`.

```bash
# Replace, do not append. Idempotent edit:
python3 - <<'PY'
import re, pathlib, os
p = pathlib.Path(os.path.expandvars("$WORKSPACE/.secrets/tokens.env"))
text = p.read_text()
new = re.sub(r"^RAILWAY_TOKEN=.*$",
             f"RAILWAY_TOKEN={os.environ['RW_TOKEN']}", text, flags=re.M)
p.write_text(new)
PY
```

Never commit `tokens.env` to git. The repo's `.gitignore` already excludes `.secrets/`.

### 4. Smoke deploy via API (no Chrome MCP)

```bash
RW_TOKEN=$(grep '^RAILWAY_TOKEN=' "$WORKSPACE/.secrets/tokens.env" | cut -d= -f2-)
SERVICE_ID="88dca3af-2e3b-48b9-950f-0fd50e74e887"     # gentle-luck/pear-monitor-bot
ENV_ID="9848b702-e186-40bf-85b7-811ef1342f12"         # production

DEPLOY_ID=$(curl -s \
  -H "Authorization: Bearer ${RW_TOKEN}" \
  -H "Content-Type: application/json" \
  -X POST https://backboard.railway.com/graphql/v2 \
  -d "{\"query\":\"mutation { serviceInstanceDeployV2(serviceId: \\\"${SERVICE_ID}\\\", environmentId: \\\"${ENV_ID}\\\") }\"}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['serviceInstanceDeployV2'])")

echo "deploy=$DEPLOY_ID"
```

Poll until terminal:

```bash
for i in $(seq 1 40); do
  s=$(curl -s -H "Authorization: Bearer ${RW_TOKEN}" -H "Content-Type: application/json" \
      -X POST https://backboard.railway.com/graphql/v2 \
      -d "{\"query\":\"{ deployment(id: \\\"${DEPLOY_ID}\\\") { status } }\"}" \
      | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['deployment']['status'])")
  echo "$s"; [[ "$s" =~ ^(SUCCESS|FAILED|CRASHED|REMOVED|SKIPPED)$ ]] && break
  sleep 8
done
```

Acceptance: terminal status `SUCCESS`. If `FAILED` / `CRASHED`, inspect logs:

```bash
curl -s -H "Authorization: Bearer ${RW_TOKEN}" -H "Content-Type: application/json" \
  -X POST https://backboard.railway.com/graphql/v2 \
  -d "{\"query\":\"{ deploymentLogs(deploymentId: \\\"${DEPLOY_ID}\\\", limit: 200) { message severity } }\"}"
```

### 5. Hygiene — delete the revoked old token entry

After the new token is verified, return to `https://railway.com/account/tokens` and click the trash icon on the previous `cowork` (or whichever name) row. This avoids dead entries accumulating.

> Keep `cowork-deploy` and `BCD` rows untouched unless explicitly rotating those.

### 6. Update auto-memory

Update the `project_railway_*_token*` memory entry with:
- New token name + creation date
- Confirmation that autonomous deploys are restored
- Pointer back to this playbook

---

## Known gotchas

- **Workspace = `No workspace`** is the dropdown default. Tokens created with that scope can list `projects` but **cannot** trigger `serviceInstanceDeployV2` — they 403 with `Not Authorized`. Always pick `blackcatdefi's Projects`.
- **`me` query fails on workspace tokens.** Don't use `me` as the validation probe — use `projects`.
- **`gentle-luck` has `ignoreWatchPatterns: true`** by design. Git pushes do **not** auto-deploy. Manual `serviceInstanceDeployV2` after every commit is intentional, not a bug.
- **Token leak in logs:** the bot's `auto/logging_config.py` already redacts. Never `echo $RAILWAY_TOKEN` in scripts that pipe to logs.
- **Token revocation propagation:** Railway invalidates tokens within seconds. After deletion, retry attempts return `Not Authorized` immediately.
