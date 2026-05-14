---
name: minds-dev-workflow
description: End-to-end dev workflow for the minds app stack -- first-time bring-up, every-startup vendor/mngr sync, and the iteration loop against a running Docker agent. Use this when starting or restarting the dev Electron app, or after changing any minds component (mngr, the workspace server, the FCT template).
---

# Minds Dev Workflow

This skill covers the full minds dev cycle: standing up an FCT worktree, syncing the live mngr code into that worktree's `vendor/mngr/`, starting the dev Electron app the first time, and iterating against a running Docker agent. Use it whenever you're about to start the dev app (the vendor/mngr sync needs to happen *every* startup) or after editing any component (mngr, the system_interface workspace server, the FCT template).

## Architecture Overview

The minds stack has four components that need to stay in sync:

1. **minds desktop client** (`apps/minds/`) -- Electron app + FastAPI backend that runs locally, proxies to agent web servers
2. **system_interface workspace server** (lives in `forever-claude-template/apps/system_interface/`, distributed as the `minds-workspace-server` CLI) -- FastAPI + web UI that runs INSIDE the agent's Docker container as a background service
3. **mngr core** (`libs/mngr/`) -- the agent management CLI
4. **forever-claude-template** -- the template repo that defines the Docker container (Dockerfile, services.toml, skills, scripts)

The template contains a `vendor/mngr/` directory (a snapshot of the mngr repo). During development, we sidestep that snapshot by rsyncing the local mngr working tree directly into a parallel-named branch of an FCT worktree under `.external_worktrees/forever-claude-template/`.

### How changes propagate

```
local mngr repo  -->  FCT worktree's vendor/mngr/  -->  Docker container's /code/
                      (under .external_worktrees/)     (via rsync over SSH)
```

The desktop client runs on the host (via Electron). The workspace server + mngr run inside the container. The vendor/mngr/ sync is what makes the dev loop work end-to-end.

### Critical: the vendor/mngr/ sync must happen BEFORE every Create

When you click "Create" in the desktop client with a LOCAL-Docker provider, the desktop client (`apps/minds/.../agent_creator.py`) takes whatever's currently in the FCT worktree (including `vendor/mngr/`), shallow-clones it to a temp dir, rsyncs the worktree's working dir over the clone (so uncommitted FCT-side changes propagate), and ships the result into `/code/` in the Docker container. mngr inside the container is `uv tool install -e`'d from `/code/vendor/mngr/`.

**The desktop client does NOT auto-sync live mngr code into the worktree.** If `vendor/mngr/` is stale relative to your live mngr working tree, the Docker container's mngr will be stale too -- and (depending on what you've changed) `mngr create` inside the container may reject your `.mngr/settings.toml` with errors like `Unknown fields in agent_types.claude: [...]`. The bootstrap's chat-agent-create step then fails, and you'll see an empty workspace with "No conversation data" in the chat panel.

`just devminds-start` (the all-in-one recipe described below) does this sync for you on every invocation. Use it rather than running `just minds-start` directly when you're testing local mngr changes.

## Quick start (first time and every time)

```bash
# 1. (Once) Install electron deps.
cd apps/minds && pnpm install && cd ../..

# 2. (Once) Stand up an FCT worktree at .external_worktrees/forever-claude-template/
#    on a branch named after your current mngr branch (so template-side
#    edits stay parallel-named). Required by `just minds-start` / `just devminds-start`.
git -C ~/project/forever-claude-template worktree add \
    -b "$(git rev-parse --abbrev-ref HEAD)" \
    "$PWD/.external_worktrees/forever-claude-template" \
    josh/start-minds   # or another base branch / tag

# 3. (Every time you start the app) Sync the live mngr working tree into
#    the FCT worktree's vendor/mngr/ AND launch the devminds Electron
#    app. This single recipe wraps both steps so the Create-form's first
#    Create works without a follow-up propagate_changes.
#
#    Override agent_name / branch via positional args:
#       just devminds-start agent_name=foo branch=some-other-branch
just devminds-start
```

That's it. After the create-form is filled in and you've created an agent, see [Iterating on a running agent](#iterating-on-a-running-agent) for the inner loop.

If you want the prod (`MINDS_ROOT_NAME=minds`) profile rather than the dev profile, do the vendor/mngr sync manually with `just sync-vendor-mngr .external_worktrees/forever-claude-template` (which commits to FCT) or rsync by hand, then `MINDS_ROOT_NAME=minds just minds-start`. `devminds-start` is dev-only.

### What `just devminds-start` does

1. Verifies the FCT worktree exists at `.external_worktrees/forever-claude-template/` and bails with a helpful error if not.
2. Rsyncs the live mngr working tree into the FCT worktree's `vendor/mngr/` using the same exclusions as the pool-bake's `--mngr-source` path (`.git`, `__pycache__`, `.venv`, `node_modules`, etc.). Uncommitted changes are included; nothing is committed in the FCT worktree.
3. Sets `MINDS_ROOT_NAME=devminds` (so the app uses `~/.devminds/` for state, separate from the prod `~/.minds/`).
4. Delegates to `just minds-start` to launch Electron with the right `MINDS_WORKSPACE_*` env vars and PID-file gating.

## Iterating on a running agent

After making changes to any component (mngr, the template's system_interface workspace server, the template, etc.), sync them into a running agent's container:

```bash
apps/minds/scripts/propagate_changes \
  --user root --host 127.0.0.1 --port <SSH_PORT> \
  --key <SSH_KEY_PATH>
```

This:

1. Rsyncs the mngr repo into the FCT worktree's `vendor/mngr/` (same step `devminds-start` does, idempotent)
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
# e.g.  devminds-mindtest 0.0.0.0:32772->22/tcp
```

The SSH key for a minds Docker agent lives under `MNGR_HOST_DIR`, which the minds desktop client overrides to `~/.minds/mngr/` (production) or `~/.devminds/mngr/` (dev) instead of the default `~/.mngr/`:

```bash
find ~/.devminds/mngr/profiles -path "*/docker/*/keys/docker_ssh_key"
# or for prod:
find ~/.minds/mngr/profiles -path "*/docker/*/keys/docker_ssh_key"
```

Do NOT use a key from `~/.mngr/profiles/...` -- that belongs to non-minds mngr agents and will silently fail with "Permission denied (publickey)".

## Reference

### Just recipes that touch this stack

| Recipe | Purpose |
|---|---|
| `just devminds-start` | **Preferred dev entry point.** Sync live mngr -> FCT vendor/mngr, then launch the Electron app with `MINDS_ROOT_NAME=devminds`. Wraps the next two recipes. |
| `just minds-start` | Launch the desktop client with `MINDS_WORKSPACE_*` env vars set so the create-form auto-fills. Sources `.env`. Does NOT sync vendor/mngr -- use only if you've already synced (or are deliberately testing the stale-vendor-mngr state). |
| `just sync-vendor-mngr <fct-path>` | One-shot: snapshot mngr HEAD into FCT's vendor/mngr/ via `git archive` and commit in FCT. Use for "release" syncs, not dev iteration (it commits and only carries committed mngr content). |
| `just minds-stop` | Kill the desktop client started in this worktree by `just minds-start` / `just devminds-start`. |
| `just minds-build` | Build the desktop client distributable via `todesktop` (slow, only for releases). |
| `apps/minds/scripts/propagate_changes ...` | Sync changes into a running container without restarting the Electron app from scratch. See "Iterating on a running agent". |
| `mngr imbue_cloud admin pool create --mngr-source <monorepo-root> ...` | Bake a Vultr pool host. `--mngr-source` rsyncs the monorepo into the FCT vendor/mngr/ for the duration of the bake. (For pool hosts only -- has no effect on Docker mode.) |
| `just deploy-connector [env]` | Deploy `remote-service-connector` to Modal. |
| `just deploy-litellm [env]` | Deploy `modal_litellm` proxy to Modal. |
| `just deploy-all [env]` | Push secrets + deploy connector + deploy litellm. |
| `just push-secrets [env]` | Upsert per-env Modal secrets from `.minds/<env>/*.sh`. |

### Env vars `minds-start` / `devminds-start` set

| Variable | Purpose | Default in dev |
|----------|---------|----------------|
| `MINDS_ROOT_NAME` | Selects the data root: `devminds` -> `~/.devminds/`, `minds` -> `~/.minds/` | `devminds` (set by `devminds-start`) |
| `MINDS_WORKSPACE_GIT_URL` | Template repo path/URL for the create-form | `<repo>/.external_worktrees/forever-claude-template/` if it exists, else `~/project/forever-claude-template` |
| `MINDS_WORKSPACE_NAME` | Default agent name in the create-form | `mindtest` (override with `agent_name=...`) |
| `MINDS_WORKSPACE_BRANCH` | Default git branch for the template | The FCT path's current branch (matches your mngr branch when you set up the worktree on a parallel-named branch) |

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

`devminds-start`, `mngr imbue_cloud admin pool create --mngr-source ...`, and `propagate_changes` all share one form when rsyncing into `vendor/mngr/`:

```
rsync -a --delete --filter=':- .gitignore' --exclude=.git --exclude=uv.lock ...
```

`--filter=':- .gitignore'` is rsync's dir-merge filter: it reads `.gitignore` at each directory level under the source and applies its `-` (exclude) rules. That covers `__pycache__`, `.venv`, `node_modules`, `.test_output`, `.mypy_cache`, `.ruff_cache`, `.pytest_cache`, `.external_worktrees`, and anything else listed in the source repo's gitignore.

The two manual excludes are for things gitignore deliberately doesn't list:

- `.git` -- gitignore never lists it (git's internal dir).
- `uv.lock` -- intentionally committed at the mngr root, but each install context should regenerate its own.

`propagate_changes` additionally protects `runtime/`, `.mngr/`, and `.claude/settings.local.json` from deletion when rsyncing into `/code/`.

### Editable installs

The Dockerfile uses `uv tool install -e` for mngr (vendored under `vendor/mngr/`) and for the system_interface workspace server (at `apps/system_interface/`), so Python code changes in either location are picked up immediately after rsync. Frontend changes require the `npm run build` step (done automatically by `propagate_changes`).

### Template settings

The template's `.mngr/settings.toml` controls agent types, create templates, env vars, and `extra_window` entries. Notable knobs:

- `disable_plugin = ["recursive", "ttyd"]` -- disables plugins that conflict with template-managed services
- `extra_window` entries for bootstrap, telegram, terminal, reviewer_settings
- `env` entries for `IS_SANDBOX`, `IS_AUTONOMOUS`, and reviewer toggles

### Logs

| Path | Contents |
|---|---|
| `/tmp/claude-*/.../tasks/<id>.output` | Electron app stdout when launched via `just minds-start` / `just devminds-start` (path printed at launch) |
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
    --filter=':- .gitignore' \
    --exclude=.git --exclude=uv.lock \
    ./ .external_worktrees/forever-claude-template/vendor/mngr/
```

This is what `just devminds-start` does internally, what `mngr imbue_cloud admin pool create --mngr-source ...` does for the duration of the bake, and what `propagate_changes` does as step 1 on each iteration.

### Start electron by hand without the just recipe

```bash
TEMPLATE_BRANCH=$(cd .external_worktrees/forever-claude-template && git branch --show-current)
(
  set -a
  source .env
  set +a
  export MINDS_ROOT_NAME=devminds
  export MINDS_WORKSPACE_GIT_URL="$(pwd)/.external_worktrees/forever-claude-template"
  export MINDS_WORKSPACE_NAME="mindtest"
  export MINDS_WORKSPACE_BRANCH="$TEMPLATE_BRANCH"
  cd apps/minds && pnpm start
)
```
