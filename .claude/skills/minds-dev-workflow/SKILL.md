---
name: minds-dev-workflow
description: End-to-end dev workflow for the minds app stack -- first-time bring-up, every-startup vendor/mngr sync, and the iteration loop against a running Docker agent. Use this when starting or restarting the dev Electron app, or after changing any minds component (mngr, the system interface, the FCT template).
---

# Minds Dev Workflow

This skill covers the full minds dev cycle: standing up an FCT worktree, syncing the live mngr code into that worktree's `vendor/mngr/`, activating a per-developer dev env, starting the dev Electron app, and iterating against a running Docker agent. Use it whenever you're about to start the dev app (the vendor/mngr sync needs to happen *every* startup) or after editing any component (mngr, the system_interface, the FCT template).

## Architecture Overview

The minds stack has four components that need to stay in sync:

1. **minds desktop client** (`apps/minds/`) -- Electron app + FastAPI backend that runs locally, proxies to agent web servers
2. **system_interface** (lives in `forever-claude-template/apps/system_interface/`, distributed as the `system-interface` CLI) -- FastAPI + web UI that runs INSIDE the agent's Docker container as a background service
3. **mngr core** (`libs/mngr/`) -- the agent management CLI
4. **forever-claude-template** -- the template repo that defines the Docker container (Dockerfile, services.toml, skills, scripts)

The template contains a `vendor/mngr/` directory (a snapshot of the mngr repo). During development, we sidestep that snapshot by rsyncing the local mngr working tree directly into a parallel-named branch of an FCT worktree under `.external_worktrees/forever-claude-template/`.

### How changes propagate

```
local mngr repo  -->  FCT worktree's vendor/mngr/  -->  Docker container's /code/
                      (under .external_worktrees/)     (via rsync over SSH)
```

The desktop client runs on the host (via Electron). The system interface + mngr run inside the container. The vendor/mngr/ sync is what makes the dev loop work end-to-end.

### Critical: the vendor/mngr/ sync must happen BEFORE every Create

When you click "Create" in the desktop client with a LOCAL-Docker provider, the desktop client (`apps/minds/.../agent_creator.py`) takes whatever's currently in the FCT worktree (including `vendor/mngr/`), shallow-clones it to a temp dir, rsyncs the worktree's working dir over the clone (so uncommitted FCT-side changes propagate), and ships the result into `/code/` in the Docker container. mngr inside the container is `uv tool install -e`'d from `/code/vendor/mngr/`.

**The desktop client does NOT auto-sync live mngr code into the worktree.** If `vendor/mngr/` is stale relative to your live mngr working tree, the Docker container's mngr will be stale too -- and (depending on what you've changed) `mngr create` inside the container may reject your `.mngr/settings.toml` with errors like `Unknown fields in agent_types.claude: [...]`. The bootstrap's chat-agent-create step then fails, and you'll see an empty workspace with "No conversation data" in the chat panel.

`just minds-start` (the all-in-one recipe described below) does this sync for you on every invocation. Use it rather than running `pnpm start` directly when you're testing local mngr changes.

## Quick start (first time and every time)

```bash
# 1. (Once) Install electron deps.
cd apps/minds && pnpm install && cd ../..

# 2. (Once) Stand up an FCT worktree at .external_worktrees/forever-claude-template/
#    on a branch named after your current mngr branch (so template-side
#    edits stay parallel-named). Required by `just minds-start`.
git -C ~/project/forever-claude-template worktree add \
    -b "$(git rev-parse --abbrev-ref HEAD)" \
    "$PWD/.external_worktrees/forever-claude-template" \
    origin/main   # base branch/tag; origin/main is the safe default

# 3. (Once) Bootstrap your personal dev env. Pick a name like
#    "dev-<your-user>" (convention; the DevEnvName validator requires the
#    tier prefix FIRST -- "dev-" or "ci-" -- so "dev-josh" is valid but
#    "josh-dev" is not). --create idempotently mkdirs the env root
#    ~/.minds-dev-<your-user>/ if it doesn't exist.
eval "$(uv run minds env activate --create dev-<your-user>)"
uv run minds env deploy

# 4. (Every time you start the app, in a fresh shell) Activate the env
#    and run `just minds-start`. The recipe re-syncs live mngr ->
#    vendor/mngr/ and launches Electron.
eval "$(uv run minds env activate dev-<your-user>)"
just minds-start
```

That's it. After the create-form is filled in and you've created an agent, see [Iterating on a running agent](#iterating-on-a-running-agent) for the inner loop.

If you want to run against prod / staging instead of a personal dev env, use `eval "$(uv run minds env activate production)"` (or `... activate staging`) and then `just minds-start`. **Do not** run `minds env deploy` against production / staging without coordinating with the rest of the team -- that pushes Vault secrets to Modal and re-deploys the live tier; the unified deploy CLI requires `--yes-i-mean-production` / `--yes-i-mean-staging` as a safety bar.

### What `just minds-start` does

1. Verifies a minds env is activated in the shell (refuses with a helpful error if not).
2. Verifies the FCT worktree exists at `.external_worktrees/forever-claude-template/` and bails with a helpful error if not.
3. Rsyncs the live mngr working tree into the FCT worktree's `vendor/mngr/` using the same exclusions as the pool-bake's `--mngr-source` path (`.git`, `__pycache__`, `.venv`, `node_modules`, etc.). Uncommitted changes are included; nothing is committed in the FCT worktree.
4. Launches Electron with the right `MINDS_WORKSPACE_*` env vars so the create-form auto-fills "repository", "name", and "branch".

## Iterating on a running agent

After making changes to any component (mngr, the template's system_interface, the template, etc.), sync them into a running agent's container:

```bash
eval "$(uv run minds env activate dev-<your-user>)"
apps/minds/scripts/propagate_changes \
  --user root --host 127.0.0.1 --port <SSH_PORT> \
  --key <SSH_KEY_PATH>
```

This:

1. Verifies a minds env is activated in the shell (refuses without it).
2. Rsyncs the mngr repo into the FCT worktree's `vendor/mngr/` (same step `just minds-start` does, idempotent)
3. Stops the agent (`mngr stop`)
4. Rsyncs the full template (with updated vendor/mngr/) into `/code/` in the container
5. Rebuilds the system interface frontend (`npm run build` via SSH)
6. Starts the agent (`mngr start`)
7. Stops and restarts the Electron desktop client (clean SIGTERM shutdown)

The whole cycle takes about 5-10 seconds.

For local (non-container) agents:

```bash
eval "$(uv run minds env activate dev-<your-user>)"
apps/minds/scripts/propagate_changes --target /path/to/agent/workdir
```

### Find the Docker container's SSH port and key

The port is randomly assigned by Docker per agent. The container name is `<MNGR_PREFIX><agent-name>-host` (set by your activated env's `MNGR_PREFIX`):

```bash
eval "$(uv run minds env activate dev-<your-user>)"   # so we know MNGR_PREFIX
docker ps --format '{{.Names}} {{.Ports}}' | grep "${MNGR_PREFIX}mind-"
# e.g.  minds-dev-<your-user>-mind-1-host 0.0.0.0:32772->22/tcp
```

The SSH key for a minds Docker agent lives under the activated env's `MNGR_HOST_DIR`:

```bash
eval "$(uv run minds env activate dev-<your-user>)"   # exports MNGR_HOST_DIR
find "${MNGR_HOST_DIR}/profiles" -path "*/docker/*/keys/docker_ssh_key"
```

Do NOT use a key from `~/.mngr/profiles/...` -- that belongs to non-minds mngr agents and will silently fail with "Permission denied (publickey)". Likewise do NOT use a key from a different activated env (each env has its own profile dir).

## Reference

### Just recipes that touch this stack

| Recipe | Purpose |
|---|---|
| `just minds-start` | **Preferred dev entry point.** Sync live mngr -> FCT vendor/mngr, then launch the Electron app. Requires an activated minds env in the shell. |
| `just minds-stop` | Kill the desktop client started in this worktree by `just minds-start`. |
| `just minds-build` | Build the desktop client distributable via `todesktop` (slow, only for releases). |
| `apps/minds/scripts/propagate_changes ...` | Sync changes into a running container without restarting the Electron app from scratch. See "Iterating on a running agent". Requires an activated env. |
| `mngr imbue_cloud admin pool create --mngr-source <monorepo-root> ...` | Bake an OVH pool host (the imbue_cloud pool's VPS provider). `--mngr-source` rsyncs the monorepo into the FCT vendor/mngr/ for the duration of the bake. (For pool hosts only -- has no effect on Docker mode.) Requires an activated env. Typically driven via the `minds pool create` wrapper, which injects OVH + pool-ssh credentials from Vault. |
| `just deploy [--yes-i-mean-<tier>]` | Run `minds env deploy` on the activated env. For dev envs: provisions Modal env / Neon / SuperTokens + deploys both Modal apps + writes `~/.minds-<env>/{client.toml,secrets.toml}`. For tier deploys: pushes Vault secrets to Modal + deploys both Modal apps, no local state written. |
| `just sync-vendor-mngr <fct-path>` | One-shot: snapshot mngr HEAD into FCT's vendor/mngr/ via `git archive` and commit in FCT. Use for "release" syncs, not dev iteration (it commits and only carries committed mngr content). |

### Vault (for pool / slice bakes)

Slice bakes (`minds pool create`, `just bake-slice-{dev,prod}`) read secrets from Vault (the tier's `POOL_SSH_PRIVATE_KEY`, the host-pool DSN, etc.). (Baking new OVH classic VPS pool hosts is deprecated and no longer supported.) Two things to know:

- **Login is interactive.** Run `vault login -method=oidc` once per session (browser OIDC); the token lands at `~/.vault-token`.
- **`VAULT_ADDR` / `VAULT_NAMESPACE` are usually NOT set in a non-interactive shell.** The minds wrappers (`minds pool ...` and the `bake-*` recipes) apply the imbue HCP defaults automatically via `apps/minds/imbue/minds/envs/vault_reader.py`, so they "just work" with only the token -- **prefer them**. If you run a **raw** `vault` or `mngr imbue_cloud admin ...` command, a bare `vault` defaults to `https://127.0.0.1:8200` and fails with "connection refused" -- that is a missing address, **NOT** "logged out" (don't ask the operator to re-login, and don't ask them for `VAULT_ADDR`). Export the defaults first:

  ```bash
  export VAULT_ADDR=https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200
  export VAULT_NAMESPACE=admin
  ```

  Single source of truth: `_DEFAULT_VAULT_ADDR` / `_DEFAULT_VAULT_NAMESPACE` in `vault_reader.py` -- read them from there in case they drift.

### Env vars `just minds-start` sets

`MINDS_ROOT_NAME` / `MNGR_HOST_DIR` / `MNGR_PREFIX` / `MINDS_CLIENT_CONFIG_PATH` come from `minds env activate <name>` in your shell -- `minds-start` requires them to be set and refuses otherwise. Beyond those:

| Variable | Purpose | Default |
|----------|---------|---------|
| `MINDS_WORKSPACE_GIT_URL` | Template repo path/URL for the create-form | `<repo>/.external_worktrees/forever-claude-template/` if it exists, else `~/project/forever-claude-template` |
| `MINDS_WORKSPACE_NAME` | Pins the workspace name in the create-form (used verbatim; a collision errors at create time) | Unset unless you pass an `agent_name` (`just minds-start my-agent`); when unset the form generates a `mind-N` name |
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

`just minds-start`, `mngr imbue_cloud admin pool create --mngr-source ...`, and `propagate_changes` all rsync into `vendor/mngr/` using one shared form (`rsync -a --delete --filter=':- .gitignore' --exclude=.git --exclude=uv.lock`). The form, the rationale for each exclude, and the source-of-truth constants live in `apps/minds/docs/vendor-mngr-sync.md`.

`propagate_changes` additionally protects `runtime/`, `.mngr/`, and `.claude/settings.local.json` from deletion when rsyncing into `/code/`.

### Editable installs

The FCT Docker build installs mngr (`vendor/mngr/libs/mngr`) and the system_interface (`apps/system_interface/`) editable via `uv tool install -e`, run by `scripts/build_workspace.sh` (which the Dockerfile invokes with `RUN bash`), so Python code changes in either location are picked up immediately after rsync. Frontend changes require the `npm run build` step (done automatically by `propagate_changes`).

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
| `~/.minds-<env-name>/logs/minds.log`, `~/.minds-<env-name>/logs/minds-events.jsonl` | Minds backend (per-env) |

### Cleaning up the legacy `~/.devminds/`

If you used the pre-refactor layout (`~/.devminds/` for all dev iteration plus `~/.devminds/envs/<dev-name>.toml` per-env overrides), that root is now obsolete. No migration script -- just `rm -rf ~/.devminds/` when convenient. A stale `MINDS_ROOT_NAME=devminds` in a parent shell is silently treated as unset (with a warning); the in-shell `minds env activate <name>` always wins.

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

This is what `just minds-start` does internally, what `mngr imbue_cloud admin pool create --mngr-source ...` does for the duration of the bake, and what `propagate_changes` does as step 1 on each iteration.

### Start electron by hand without the just recipe

```bash
eval "$(uv run minds env activate dev-<your-user>)"
TEMPLATE_BRANCH=$(cd .external_worktrees/forever-claude-template && git branch --show-current)
(
  set -a
  source .env
  set +a
  export MINDS_WORKSPACE_GIT_URL="$(pwd)/.external_worktrees/forever-claude-template"
  # Optional: pin the workspace name (used verbatim). Omit to let the form pick a `mind-N` name.
  # export MINDS_WORKSPACE_NAME="my-agent"
  export MINDS_WORKSPACE_BRANCH="$TEMPLATE_BRANCH"
  cd apps/minds && pnpm start
)
```
