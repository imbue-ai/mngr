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

The template contains a `vendor/mngr/` directory (git subtree) with a copy of the mngr repo. During development, we sidestep the subtree by rsyncing the local mngr repo directly into `vendor/mngr/`.

### How changes propagate

```
local mngr repo  -->  template's vendor/mngr/  -->  Docker container's /code/
                      (on the host)                 (via rsync over SSH)
```

The desktop client runs on the host (via Electron). The workspace server + mngr run inside the container. The `propagate_changes` script syncs everything.

## Setup (one-time per worktree)

### 1. Create the template worktree

The forever-claude-template must exist at `.external_worktrees/forever-claude-template` relative to the mngr worktree root:

```bash
cd ~/project/forever-claude-template
git worktree add /path/to/mngr/worktree/.external_worktrees/forever-claude-template -b <branch-name> main
```

### 2. Sync current mngr code into the template

**CRITICAL**: The template's `vendor/mngr/` starts with whatever was committed on `main`. You MUST rsync the current mngr repo into it before creating any agents, otherwise the container will run stale code:

```bash
rsync -a --delete \
    --exclude='.git' --exclude='__pycache__' --exclude='.venv' \
    --exclude='node_modules' --exclude='.test_output' --exclude='.mypy_cache' \
    --exclude='.ruff_cache' --exclude='.pytest_cache' --exclude='uv.lock' \
    --exclude='.external_worktrees' \
    ./ .external_worktrees/forever-claude-template/vendor/mngr/
```

This is the same rsync that `propagate_changes` does as step 1, but it must happen once before the first agent creation.

### 3. Install Electron dependencies

```bash
cd apps/minds && npm install && cd ../..
```

### 4. Find your Docker SSH key

Minds agents register their hosts under `~/.minds/mngr/`, not the default `~/.mngr/`, because the minds desktop client overrides `MNGR_HOST_DIR` (see `propagate_changes` lines ~43-46). The SSH key for a minds Docker agent lives at:
```
~/.minds/mngr/profiles/<profile_id>/providers/docker/docker/keys/docker_ssh_key
```

Find yours with:
```bash
find ~/.minds/mngr/profiles -path "*/docker/*/keys/docker_ssh_key"
```

Do NOT use a key from `~/.mngr/profiles/...` -- that belongs to non-minds mngr agents and will silently fail with "Permission denied (publickey)".

### 5. Start the Electron app

Source `.env` from the mngr repo root and set these env vars:

- `MINDS_WORKSPACE_GIT_URL` -- path to the template worktree (e.g., `/path/to/mngr/worktree/.external_worktrees/forever-claude-template`)
- `MINDS_WORKSPACE_NAME` -- agent name (default: `mindtest`)
- `MINDS_WORKSPACE_BRANCH` -- branch to use (set to the template worktree's branch if not `main`)

**IMPORTANT**: `MINDS_WORKSPACE_BRANCH` MUST match the branch the template worktree is on. Get it with `cd .external_worktrees/forever-claude-template && git branch --show-current`. If this is wrong, agent creation will fail with `git checkout failed for branch`.

```bash
TEMPLATE_BRANCH=$(cd .external_worktrees/forever-claude-template && git branch --show-current)
(
  set -a
  source .env
  [ -f .test_env ] && source .test_env
  set +a
  export MINDS_WORKSPACE_GIT_URL="$(pwd)/.external_worktrees/forever-claude-template"
  export MINDS_WORKSPACE_NAME="mindtest"
  export MINDS_WORKSPACE_BRANCH="$TEMPLATE_BRANCH"
  python3 -c "import subprocess; subprocess.Popen(['bash','-c','cd apps/minds && npm start'], start_new_session=True, stdout=open('/tmp/minds-electron.log','a'), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)"
)
```

### 6. Create the agent

Use the Electron app's creation form to create a Docker agent. The form will default to the template path and agent name from the env vars.

### 7. Find the Docker container's SSH port

```bash
docker ps --format '{{.Names}} {{.Ports}}' | grep mindtest
# Output: mngr-mindtest-host 0.0.0.0:32772->22/tcp
```

## Iterating

After making changes to any component, run:

```bash
apps/minds/scripts/propagate_changes \
  --user root --host 127.0.0.1 --port <SSH_PORT> \
  --key <SSH_KEY_PATH>
```

This does:
1. Rsyncs the mngr repo into the template's `vendor/mngr/` (so future `mngr create` picks up changes too)
2. Stops the agent (`mngr stop`)
3. Rsyncs the full template (with updated vendor/mngr/) into `/code/` in the container
4. Rebuilds the workspace server frontend (`npm run build` via SSH)
5. Starts the agent (`mngr start`)
6. Stops and restarts the Electron desktop client (clean SIGTERM shutdown)

The whole cycle takes about 5-10 seconds.

### For local (non-container) agents

```bash
apps/minds/scripts/propagate_changes --target /path/to/agent/workdir
```

## Key details

### Clean shutdown

The Electron app shuts down cleanly via this chain:
- Electron window close -> `before-quit` handler -> `backend.js shutdown()` -> SIGTERM to `uv run`
- `uv run` forwards SIGTERM to Python
- Uvicorn catches SIGTERM, does 1-second graceful shutdown (`timeout_graceful_shutdown=1`)
- ASGI lifespan shutdown hook runs `stream_manager.stop()` (terminates mngr observe/events subprocesses)
- Uvicorn re-raises SIGTERM, process exits with code 143

If this chain breaks (orphaned `mngr observe`/`mngr event` processes appear), something is wrong -- investigate, don't just kill the orphans.

### Env vars

| Variable | Purpose | Default |
|----------|---------|---------|
| `MINDS_WORKSPACE_GIT_URL` | Template repo path/URL for creation form | `https://github.com/imbue-ai/forever-claude-template.git` |
| `MINDS_WORKSPACE_NAME` | Default agent name in creation form | `selene` |
| `MINDS_WORKSPACE_BRANCH` | Default git branch for template | `main` |

### Rsync exclusions

Both syncs (mngr->vendor/mngr and template->container) exclude:
`.git`, `__pycache__`, `.venv`, `node_modules`, `.test_output`, `.mypy_cache`, `.ruff_cache`, `.pytest_cache`, `uv.lock`, `.external_worktrees`

The template->container sync also protects `runtime/` and `.mngr/` from deletion.

### Editable installs

The Dockerfile uses `uv tool install -e` for mngr and minds_workspace_server, so Python code changes in `vendor/mngr/` are picked up immediately after rsync. Frontend changes require the `npm run build` step (done automatically by the script).

### Template settings

The template's `.mngr/settings.toml` controls agent types, create templates, env vars, and extra_window entries. Key settings:
- `disable_plugin = ["recursive", "ttyd"]` -- disables plugins that would conflict with template-managed services
- `extra_window` entries for bootstrap, telegram, terminal, reviewer_settings
- `env` entries for `IS_SANDBOX`, `IS_AUTONOMOUS`, and reviewer toggles

### Logs

- Electron app: `/tmp/minds-electron.log`
- Minds backend: `~/.minds/logs/minds.log` and `~/.minds/logs/minds-events.jsonl`
