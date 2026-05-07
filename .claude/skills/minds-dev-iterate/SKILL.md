---
name: minds-dev-iterate
description: Set up and iterate on the minds app stack (desktop client, workspace server, mngr, forever-claude-template) with a running Docker agent
---

# Minds Dev Iteration Loop

This skill sets up and manages the development iteration loop for testing the minds app stack end-to-end with a running Docker agent.

## Architecture Overview

The minds stack has four components that need to stay in sync:

1. **minds desktop client** (`apps/minds/`) -- Electron app + FastAPI backend that runs locally, proxies to agent web servers
2. **minds_workspace_server** (`apps/minds_workspace_server/`) -- FastAPI + web UI that runs INSIDE the agent's Docker container as a background service
3. **mngr core** (`libs/mngr/`) -- the agent management CLI
4. **forever-claude-template** -- the template repo that defines the Docker container (Dockerfile, services.toml, skills, scripts)

The template contains a `vendor/mngr/` directory (a snapshot of the mngr repo). During development, we sidestep that snapshot by rsyncing the local mngr working tree directly into a parallel-named branch of an FCT worktree under `.external_worktrees/forever-claude-template/`.

### How changes propagate

```
local mngr repo  -->  FCT worktree's vendor/mngr/  -->  Docker container's /code/
                      (under .external_worktrees/)     (via rsync over SSH)
```

The desktop client runs on the host (via Electron). The workspace server + mngr run inside the container. `just create-pool-hosts-dev` does the first sync; `apps/minds/scripts/propagate_changes` does subsequent ones to a running agent.

## Quick start

The just recipes handle the boring setup -- use them instead of the manual steps unless something is broken:

```bash
# 1. Install electron deps once.
cd apps/minds && pnpm install && cd ../..

# 2. Stand up an FCT worktree at .external_worktrees/forever-claude-template/
#    on a branch parallel-named to your current mngr branch, with this repo's
#    working tree rsynced into vendor/mngr/, then provision a Vultr pool host.
#    Optional -- skip this if you don't need a pool host yet; minds-start will
#    fall back to ~/project/forever-claude-template if the worktree is missing.
just create-pool-hosts-dev 1 production

# 3. Start the desktop client. Auto-sets MINDS_WORKSPACE_{GIT_URL,NAME,BRANCH}
#    so the create-form auto-fills "repository", "name", "branch". Sources
#    .env so ANTHROPIC_API_KEY etc. are visible.
just minds-start

# Override defaults if needed:
just minds-start agent_name=my-test-agent branch=some-other-branch
```

After the create-form is filled in and you've created an agent, see [Iterating on a running agent](#iterating-on-a-running-agent) for the inner loop.

## Iterating on a running agent

After making changes to any component (mngr, minds_workspace_server, the template, etc.), sync them into a running agent's container:

```bash
apps/minds/scripts/propagate_changes \
  --user root --host 127.0.0.1 --port <SSH_PORT> \
  --key <SSH_KEY_PATH>
```

This:

1. Rsyncs the mngr repo into the FCT worktree's `vendor/mngr/`
2. Stops the agent (`mngr stop`)
3. Rsyncs the full template (with updated vendor/mngr/) into `/code/` in the container
4. Rebuilds the workspace server frontend (`npm run build` via SSH)
5. Starts the agent (`mngr start`)
6. Stops and restarts the Electron desktop client (clean SIGTERM shutdown)

The whole cycle takes about 5-10 seconds.

For local (non-container) agents:

```bash
apps/minds/scripts/propagate_changes --target /path/to/agent/workdir
```

### Find the Docker container's SSH port and key

The port is randomly assigned by Docker per agent:

```bash
docker ps --format '{{.Names}} {{.Ports}}' | grep mindtest
# e.g.  mngr-mindtest-host 0.0.0.0:32772->22/tcp
```

The SSH key for a minds Docker agent lives under `MNGR_HOST_DIR`, which the minds desktop client overrides to `~/.minds/mngr/` (production) or `~/.devminds/mngr/` (dev) instead of the default `~/.mngr/`:

```bash
find ~/.minds/mngr/profiles -path "*/docker/*/keys/docker_ssh_key"
# or for dev:
find ~/.devminds/mngr/profiles -path "*/docker/*/keys/docker_ssh_key"
```

Do NOT use a key from `~/.mngr/profiles/...` -- that belongs to non-minds mngr agents and will silently fail with "Permission denied (publickey)".

## Reference

### Just recipes that touch this stack

| Recipe | Purpose |
|---|---|
| `just minds-start` | Launch the desktop client with `MINDS_WORKSPACE_*` env vars set so the create-form auto-fills. Sources `.env`. |
| `just minds-build` | Build the desktop client distributable via `todesktop` (slow, only for releases). |
| `just create-pool-hosts-dev <count>` | Persistent FCT worktree at `.external_worktrees/forever-claude-template/` on a parallel branch, with this repo's working tree rsynced into `vendor/mngr/`, then provision N Vultr pool hosts. Resets `vendor/mngr/` after success so the worktree's tracked state stays clean. |
| `just create-pool-hosts <count>` | Provision N pool hosts from a tagged FCT release; never touches `vendor/mngr/`. Use for reproducible release-style provisioning, not iteration. |
| `just deploy-connector [env]` | Deploy `remote-service-connector` to Modal. |
| `just deploy-litellm [env]` | Deploy `modal_litellm` proxy to Modal. |
| `just deploy-all [env]` | Push secrets + deploy connector + deploy litellm. |
| `just push-secrets [env]` | Upsert per-env Modal secrets from `.minds/<env>/*.sh`. |

### Env vars `just minds-start` sets

| Variable | Purpose | Default in dev |
|----------|---------|----------------|
| `MINDS_WORKSPACE_GIT_URL` | Template repo path/URL for the create-form | `<repo>/.external_worktrees/forever-claude-template/` if it exists, else `~/project/forever-claude-template` |
| `MINDS_WORKSPACE_NAME` | Default agent name in the create-form | `mindtest` (override with `agent_name=...`) |
| `MINDS_WORKSPACE_BRANCH` | Default git branch for the template | The FCT path's current branch (matches your mngr branch when using the worktree from `create-pool-hosts-dev`) |

The desktop client reads these in `apps/minds/imbue/minds/desktop_client/templates.py`.

### Clean shutdown

The Electron app shuts down cleanly via this chain:

- Electron window close -> `before-quit` handler -> `backend.js shutdown()` -> SIGTERM to `uv run`
- `uv run` forwards SIGTERM to Python
- Uvicorn catches SIGTERM, does 1-second graceful shutdown (`timeout_graceful_shutdown=1`)
- ASGI lifespan shutdown hook runs `stream_manager.stop()` (terminates `mngr observe`/`mngr event` subprocesses)
- Uvicorn re-raises SIGTERM, process exits with code 143

If this chain breaks (orphaned `mngr observe`/`mngr event` processes appear), something is wrong -- investigate, do not just kill the orphans.

### Rsync exclusions

Both `create-pool-hosts-dev` (mngr-working-tree -> vendor/mngr) and `propagate_changes` exclude:
`.git`, `__pycache__`, `.venv`, `node_modules`, `.test_output`, `.mypy_cache`, `.ruff_cache`, `.pytest_cache`, `uv.lock`, `.external_worktrees`

`propagate_changes` additionally protects `runtime/`, `.mngr/`, and `.claude/settings.local.json` from deletion.

### Editable installs

The Dockerfile uses `uv tool install -e` for mngr and minds_workspace_server, so Python code changes in `vendor/mngr/` are picked up immediately after rsync. Frontend changes require the `npm run build` step (done automatically by `propagate_changes`).

### Template settings

The template's `.mngr/settings.toml` controls agent types, create templates, env vars, and `extra_window` entries. Notable knobs:

- `disable_plugin = ["recursive", "ttyd"]` -- disables plugins that conflict with template-managed services
- `extra_window` entries for bootstrap, telegram, terminal, reviewer_settings
- `env` entries for `IS_SANDBOX`, `IS_AUTONOMOUS`, and reviewer toggles

### Logs

| Path | Contents |
|---|---|
| `/tmp/claude-*/.../tasks/<id>.output` | Electron app stdout when launched via `just minds-start` (path printed at launch) |
| `~/.minds/logs/minds.log`, `~/.minds/logs/minds-events.jsonl` | Minds backend (production) |
| `~/.devminds/logs/minds.log`, `~/.devminds/logs/minds-events.jsonl` | Minds backend (dev) |

## Manual setup (fallback)

If a recipe is broken or you want to run something the recipes don't cover, here are the underlying steps the recipes wrap.

### Create the FCT worktree by hand

```bash
cd ~/project/forever-claude-template
git worktree add /path/to/mngr/worktree/.external_worktrees/forever-claude-template -b <branch-name> origin/main
```

### Sync mngr code into the FCT worktree's vendor/mngr/ by hand

```bash
rsync -a --delete \
    --exclude='.git' --exclude='__pycache__' --exclude='.venv' \
    --exclude='node_modules' --exclude='.test_output' --exclude='.mypy_cache' \
    --exclude='.ruff_cache' --exclude='.pytest_cache' --exclude='uv.lock' \
    --exclude='.external_worktrees' \
    ./ .external_worktrees/forever-claude-template/vendor/mngr/
```

This is what `create-pool-hosts-dev` does as part of the worktree prep, and the same rsync that `propagate_changes` does as step 1 on each iteration.

### Start electron by hand without the just recipe

```bash
TEMPLATE_BRANCH=$(cd .external_worktrees/forever-claude-template && git branch --show-current)
(
  set -a
  source .env
  set +a
  export MINDS_WORKSPACE_GIT_URL="$(pwd)/.external_worktrees/forever-claude-template"
  export MINDS_WORKSPACE_NAME="mindtest"
  export MINDS_WORKSPACE_BRANCH="$TEMPLATE_BRANCH"
  cd apps/minds && pnpm start
)
```
