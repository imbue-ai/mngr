# Minds Dev Iteration Loop

## Overview

* When developing across the minds stack (desktop client, minds_workspace_server, mngr core, forever-claude-template), the current workflow is slow and error-prone: changes require updating git subtrees, rebuilding Docker images, and manually restarting multiple components.
* This spec defines a `propagate_changes` shell script and documented workflow that enables sub-10-second iteration cycles by rsyncing code directly into running containers and restarting only what's needed.
* The key insight is to sidestep the git subtree problem entirely during development: rsync the local mngr repo into the template's `vendor/mngr/` on the host, then rsync the full template (now with updated mngr) into the container. The subtree is only updated for releases.
* This approach works for both Docker containers (via SSH) and local/non-container agents (via local rsync), keeping a single script for both cases.
* The script also restarts the Electron desktop client in parallel, ensuring both the container-side and host-side components are updated atomically.

## Expected Behavior

* Running `propagate_changes` from any mngr worktree syncs code changes into a running minds agent and restarts all affected components within seconds.
* The script expects `forever-claude-template` to exist at `.external_worktrees/forever-claude-template` relative to the mngr worktree root. It fails with a clear error if this directory is missing.
* The agent name is determined by `MINDS_WORKSPACE_NAME` env var if set, otherwise defaults to `"mindtest"`.
* On every invocation, the script:
  1. Rsyncs the local mngr repo into the template's `vendor/mngr/` (on the host). This ensures any subsequent `mngr create` also uses the latest code.
  2. In parallel:
     - **Container/agent side:** stops the agent (`mngr stop`), rsyncs the full template (with updated `vendor/mngr/`) into the target, rebuilds the minds_workspace_server frontend (`npm run build`), starts the agent (`mngr start`).
     - **Desktop client side:** gracefully kills the Electron app (SIGTERM + wait), sources `.env` from the mngr repo root, sets `MINDS_WORKSPACE_GIT_URL` to the template path and `MINDS_WORKSPACE_NAME`, then starts the Electron app (`npm start` from `apps/minds/`).
* `mngr start` automatically restarts the bootstrap service manager, which reads `services.toml` and starts all background services (web server, cloudflared, app-watcher). No manual service restart is needed.
* For Docker/remote agents, the rsync target is `<user>@<host>:/code/` over SSH. For local agents, the rsync target is a local filesystem path. The script detects which mode based on whether `--host` is provided.
* Python code changes are picked up immediately because the Dockerfile uses `uv tool install -e` (editable installs). Frontend changes require the `npm run build` step.
* Rsync uses `--delete` to ensure clean syncs (removed files don't linger). Additional exclusions protect runtime state in the container.

## Implementation Plan

### `apps/minds/scripts/propagate_changes` (new file)

A bash script that orchestrates the full iteration cycle.

**Arguments (named flags):**

* `--user <ssh_user>` -- SSH user for remote rsync (e.g., `root`). Required when `--host` is given.
* `--host <ssh_host>` -- SSH host for remote rsync (e.g., `127.0.0.1`). When omitted, `--target` is required for local mode.
* `--port <ssh_port>` -- SSH port (e.g., `32768`). Required when `--host` is given.
* `--key <ssh_key_path>` -- Path to SSH private key. Required when `--host` is given.
* `--target <local_path>` -- Local filesystem path for the agent's work_dir. Required when `--host` is not given (local/non-container mode).

**Inferred paths:**

* `MNGR_REPO_ROOT`: inferred from the script's own location: `$(cd "$(dirname "$0")/../../.." && pwd)`.
* `TEMPLATE_DIR`: `${MNGR_REPO_ROOT}/.external_worktrees/forever-claude-template`. The script exits with an error if this directory does not exist.
* `AGENT_NAME`: from `MINDS_WORKSPACE_NAME` env var, defaulting to `"mindtest"`.

**Rsync exclusions (shared across both syncs):**

* `.git`, `__pycache__`, `.venv`, `node_modules`, `.test_output`, `.mypy_cache`, `.ruff_cache`, `.pytest_cache`, `uv.lock`

**Step 1: Sync mngr into template's vendor/mngr/ (local rsync)**

```
rsync -a --delete \
  --exclude='.git' --exclude='__pycache__' --exclude='.venv' \
  --exclude='node_modules' --exclude='.test_output' \
  --exclude='.mypy_cache' --exclude='.ruff_cache' \
  --exclude='.pytest_cache' --exclude='uv.lock' \
  "${MNGR_REPO_ROOT}/" "${TEMPLATE_DIR}/vendor/mngr/"
```

This ensures that `vendor/mngr/` in the template on disk always reflects the current mngr checkout, so any future `mngr create` (Docker build) picks up the latest code.

**Step 2 (parallel track A): Container/agent update**

1. `uv run mngr stop "${AGENT_NAME}"` -- stops the agent and all its services (bootstrap, web, cloudflared, app-watcher).
2. Rsync the full template into the target:
   - Remote mode: `rsync -avz --delete --exclude=... -e "ssh -p ${PORT} -i ${KEY} -o StrictHostKeyChecking=no" "${TEMPLATE_DIR}/" "${USER}@${HOST}:/code/"`
   - Local mode: `rsync -a --delete --exclude=... "${TEMPLATE_DIR}/" "${TARGET}/"`
   - Additional `--delete` exclusions for container state: `runtime/`, `.mngr/`
3. `uv run mngr start "${AGENT_NAME}"` -- starts the agent; bootstrap auto-starts all services.
4. Rebuild frontend: `uv run mngr exec "${AGENT_NAME}" "cd /code/vendor/mngr/apps/minds_workspace_server/frontend && npm run build"`
   - For local mode, run the npm build directly at the appropriate path.
   - The agent must be running before `mngr exec` is called, since `mngr exec` requires an active agent connection.

**Step 2 (parallel track B): Desktop client restart**

1. `pkill -TERM -f "electron.*minds"` -- gracefully kill the Electron app.
2. Wait up to 5 seconds for the process to exit. (If it doesn't exit, that's a bug to fix separately -- the script should log a warning, not force-kill.)
3. Source `.env` from the mngr repo root if it exists: `. "${MNGR_REPO_ROOT}/.env"`.
4. Export env vars:
   - `MINDS_WORKSPACE_GIT_URL="${TEMPLATE_DIR}"`
   - `MINDS_WORKSPACE_NAME="${AGENT_NAME}"`
5. Start the Electron app in the background: `cd "${MNGR_REPO_ROOT}/apps/minds" && npm start &`

**Parallel execution:** Tracks A and B run concurrently. The script waits for both to complete before exiting.

### Rsync details

**Mngr -> vendor/mngr/ (Step 1):**

* Source: `${MNGR_REPO_ROOT}/` (trailing slash = copy contents)
* Destination: `${TEMPLATE_DIR}/vendor/mngr/`
* Flags: `-a --delete`
* Exclusions: `.git`, `__pycache__`, `.venv`, `node_modules`, `.test_output`, `.mypy_cache`, `.ruff_cache`, `.pytest_cache`, `uv.lock`

**Template -> container (Step 2A):**

* Source: `${TEMPLATE_DIR}/` (trailing slash = copy contents)
* Destination: `/code/` (remote) or `${TARGET}/` (local)
* Flags: `-a --delete` (add `-vz` for remote)
* Exclusions: same as above, plus `--filter='protect runtime/'` and `--filter='protect .mngr/'` to prevent `--delete` from removing container runtime state

## Implementation Phases

### Phase 1: Core script with remote (Docker) support

* Create `apps/minds/scripts/propagate_changes` with the full argument parsing, path inference, and remote-mode workflow.
* Implement Step 1 (mngr -> vendor/mngr/ rsync).
* Implement Step 2A (stop agent, rsync to container, rebuild frontend, start agent) for remote mode only.
* Implement Step 2B (kill and restart Electron app) running in parallel with 2A.
* Test manually with a Docker-based minds agent.

### Phase 2: Local mode support

* Add `--target` flag handling for local/non-container agents.
* When `--host` is not provided, require `--target` and use local rsync instead of SSH-based rsync.
* For local mode, run the frontend build directly instead of via `mngr exec`.
* Test manually with a DEV-mode minds agent.

### Phase 3: Documentation

* Add a "Development Iteration" section to `apps/minds/docs/` explaining:
  * Prerequisites (running agent, SSH details for Docker, `.external_worktrees/forever-claude-template` setup).
  * Example invocations for Docker and local modes.
  * How the git subtree sidestep works and when to update the subtree for releases.
  * Troubleshooting (stale processes, port conflicts, SSH key issues).

## Testing Strategy

* **Manual verification (primary):** Run the script against a Docker-based minds agent and verify:
  * Python code changes in mngr are reflected immediately (e.g., add a log line to minds_workspace_server, verify it appears in the service logs after restart).
  * Frontend changes are reflected after the build (e.g., modify a template string, verify it appears in the web UI).
  * Template changes (e.g., modify `services.toml`) are picked up by bootstrap.
  * The Electron app restarts and shows the correct template URL in the creation form.
  * The `--delete` behavior removes files that were deleted from the source (create a temp file, sync, delete it, sync again, verify it's gone from the container).
  * Runtime state (`runtime/`, `.mngr/`) survives the sync.
* **Manual verification (local mode):** Same checks but with a DEV-mode agent and `--target` flag.
* **Edge cases to verify manually:**
  * Script fails clearly when `.external_worktrees/forever-claude-template` is missing.
  * Script fails clearly when required SSH flags are missing in remote mode.
  * Script fails clearly when neither `--host` nor `--target` is provided.
  * Running the script twice in quick succession doesn't leave orphaned processes.
  * Creating a new agent after `propagate_changes` picks up the latest mngr code (because `vendor/mngr/` was updated on disk).

## Open Questions

* Should the script also handle the case where the agent doesn't exist yet (i.e., run `mngr create` as part of the first invocation)? Currently, the script assumes the agent already exists and was created via the desktop client or `mngr create`.
* The `pkill -TERM -f "electron.*minds"` pattern could match unrelated Electron apps if any happen to have "minds" in their command line. A more precise matching pattern may be needed.
* When `mngr stop` is called, any in-progress Claude conversation is lost. Should the script warn the user or require a `--force` flag? (Current decision: no, since we're testing infrastructure, not conversations.)
* The frontend build via `mngr exec` requires the agent to be running. The implemented sequencing is: stop -> rsync -> start -> exec (npm build). An alternative would be to run the build directly via SSH between rsync and start (while the agent is still stopped), but this would require duplicating SSH connection logic.
