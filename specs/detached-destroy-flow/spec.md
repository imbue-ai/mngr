# Detached destroy flow

## Overview

Today, "destroy project" hangs the project-settings page until the underlying `mngr destroy` returns. The destroy runs in a Python daemon thread inside the minds backend, so it dies if minds restarts; while it runs, the settings page shows only a generic "Destroying..." spinner with no visibility into stdout/stderr; on failure the user gets an `alert()` with a single-line error and no way to inspect the actual logs. We need each of:

1. The destroy work moves into a **detached subprocess** (`subprocess.Popen` with `start_new_session=True`, mirroring `apps/minds/imbue/minds/desktop_client/latchkey/_spawn.py`). The process outlives the minds backend the same way the latchkey gateway does.
2. While the detached process is running, the **landing page (`/`)** shows a "Destroying..." marker on the corresponding workspace row.
3. The settings-page destroy button **redirects immediately to `/`** after firing the POST, so the user lands on the page where the marker is already visible (no spinner-on-settings stage).
4. Each destroy run keeps its **stdout/stderr in a per-destroy log file**. The user can drill into a "destroy detail" page that tails the log live and surfaces the failure reason when the process exits non-zero or is killed.

## Expected Behavior

### POST `/api/destroy-agent/<agent_id>`

- Authenticated; otherwise `403`.
- **Synchronously**, before spawning anything:
  - Disassociates the workspace from the session store (existing behavior).
  - Looks up the agent's `host.id` via a fast `mngr list --include 'id == "<id>"' --format json` call, so the spawned subprocess can do host-mates fanout without a second `mngr list`. If the lookup fails (agent not found, mngr error), the subprocess will fall back to single-agent destroy.
- **Spawns a detached subprocess** that performs the destroy. Returns immediately:
  - `202 Accepted`
  - body: `{"agent_id": "<id>", "status": "running", "redirect_url": "/"}`
- **Idempotent**: if a destroy is already running for the same `agent_id` (i.e. `<destroying_dir>/<agent_id>/pid` exists and the pid is alive), the endpoint returns `200 OK` with `{"status": "running", "redirect_url": "/"}` and **does not start a second process**.

### Detached destroy subprocess

- **Command**: a single `bash -c '<chained mngr commands>'` invocation. No new Python subcommand; minds backend formats the shell string from the host_id it just looked up:
  - With host_id: `mngr list --include 'host.id == "<host_id>"' --ids | mngr destroy -f -` (host-mates fanout — every agent on the same Docker host goes down together, matching today's semantics).
  - Without host_id (lookup failed): `mngr destroy <agent_id> -f` (single-agent fallback).
- **No imbue_cloud lease release.** Lease release belongs in `mngr_imbue_cloud.instance.delete_host`, which mngr's GC calls after the destroyed-host grace period. Eagerly calling `mngr imbue_cloud hosts release` here was duplicating that responsibility in two places; we drop the eager call so `delete_host` is the single source of truth for lease lifecycle.
- **Detached spawn**: `subprocess.Popen([...], start_new_session=True, stdin=DEVNULL, stdout=log_file, stderr=log_file, ...)`. Inherits the parent's `MNGR_HOST_DIR` / `MNGR_PREFIX` so the subprocess hits the right minds host dir. The Popen handle is intentionally allowed to go out of scope — same pattern as `spawn_detached_latchkey_gateway`.
- **Output log** at `<paths.data_dir>/destroying/<agent_id>/output.log` (combined stdout+stderr, written via Popen redirection — no Python wrapper writes to it).
- **Pid file** at `<paths.data_dir>/destroying/<agent_id>/pid` (single-line text). Written by the minds backend immediately after `Popen(...)` returns and **before** the API response. The file's existence + the pid's liveness are the only state we track on disk. No `state.json`.
- The subprocess terminates when the chained mngr commands exit; output log + pid file persist for inspection.

### Status derivation (no state file)

For a given `agent_id`, status is computed from three signals:

| Directory present? | `pid` alive? | Agent in `list_known_workspace_ids()`? | Status |
|---|---|---|---|
| no | — | — | not in flight |
| yes | yes | — | **running** |
| yes | no | yes | **failed** (the destroy ran but the agent is still there) |
| yes | no | no | **done** (auto-deleted on next render) |

There is one ~1-second race window: if the subprocess just exited cleanly but the destroy event hasn't propagated through `mngr observe` → plugin → minds resolver yet, the agent will still appear in `list_known_workspace_ids()` and status will momentarily read "failed". The detail page poll picks up the correct status on the next tick (the discovery tail polls at 1 Hz; see `_discovery_stream_tail_events_file` in `libs/mngr/imbue/mngr/api/discovery_events.py:716`). Acceptable jitter.

### Landing-page marker

- Server reads `<paths.data_dir>/destroying/` on each `/` render; for each subdirectory, computes status per the table above using the resolver's current `list_known_workspace_ids()`.
- For **running**: render an inline "Destroying…" marker (small spinner + text) on that workspace's row. The marker is wrapped in an `<a href="/destroying/<agent_id>">` so it doubles as a shortcut to the detail page. The row's main click target (currently `window.location='<plugin>/goto/<id>/'`) is **disabled** while destroying.
- For **failed**: render a "Destroy failed" badge (red), also linking to `/destroying/<agent_id>`. The agent row still shows because the agent wasn't actually destroyed.
- For **done**: the renderer **deletes** `<paths.data_dir>/destroying/<agent_id>/` and renders nothing for that row. By construction, the destroyed agent is already absent from `list_known_workspace_ids()`, so the row vanishes naturally.

### Settings-page destroy button

- The confirmation dialog on `/workspace/<agent_id>/settings` stays.
- On confirm:
  - Fire `POST /api/destroy-agent/<id>`.
  - As soon as the response arrives with `2xx`, `window.location.href = '/'`. No more spinner on the settings page; no more polling on the settings page.
  - On `4xx`/`5xx`, surface an inline error inside the existing dialog and don't redirect.

### Destroy detail page `/destroying/<agent_id>`

- Authenticated; otherwise `403`.
- 404 if no record exists at `<paths.data_dir>/destroying/<agent_id>/`.
- Renders:
  - Workspace name (from minds' agent_names cache or the discovered agent — fall back to the bare id).
  - Status badge: "Running…" / "Done" / "Failed" (computed via the table above).
  - PID + started-at (started-at = directory mtime).
  - **Live log tail**: shows the contents of `output.log`. While status is `running`, polls `GET /api/destroying/<agent_id>/log?after=<bytes>` every 1 s and appends new bytes. Once status flips to `done` or `failed`, polls one final time then stops.
  - On `done`, auto-redirects to `/` after a short delay so the user lands where the cleanup ran.
  - On `failed`, shows two buttons:
    - **Retry**: re-fires `POST /api/destroy-agent/<id>` (which spawns a fresh detached subprocess; the existing dir gets overwritten — `pid` rewritten, `output.log` truncated).
    - **Dismiss**: `POST /api/destroying/<agent_id>/dismiss` removes the directory.

### GET `/api/destroying/<agent_id>/status`

- Returns `{"agent_id", "pid", "pid_alive", "agent_in_resolver", "status"}` where `status` is computed via the table above.
- 404 if no record exists.
- Used by the detail page's polling loop.

### GET `/api/destroying/<agent_id>/log?after=<bytes>`

- Reads `output.log` from byte offset `<after>` (default 0) to current EOF.
- Returns `{"bytes_read": N, "next_offset": M, "content": "<utf8 chunk>"}`.
- 404 if no record exists.

### POST `/api/destroying/<agent_id>/dismiss`

- Removes `<paths.data_dir>/destroying/<agent_id>/` (idempotent — 200 if removed, 200 if already absent).
- Used by the detail page's "Dismiss" button.

### Adoption on minds restart

- When minds backend starts, it does NOT actively reconcile in-flight destroying records — the polling endpoints already return live state from disk + `kill -0`. The user sees "Destroying…" on the landing page if the subprocess from the previous session is still running, and "Failed" (with the log) if it died in between (pid not alive AND agent still in resolver).

## Out of Scope

- TTL-based auto-cleanup of old destroying records. `done` records auto-delete; `failed` records persist until the user dismisses them.
- Cancelling an in-flight destroy from the UI. The detached process owns its lifecycle; if it hangs, the user kills the pid manually (the detail page surfaces it).
- Destroy-progress streaming via WebSocket / SSE. Polling `/api/destroying/<id>/log?after=<bytes>` is sufficient for a destroy that typically completes in <60 s.

## Implementation Plan

1. New module `apps/minds/imbue/minds/desktop_client/destroying.py`:
   - `DestroyingStatus` enum: `RUNNING` / `DONE` / `FAILED`.
   - `DestroyingRecord` model: `agent_id`, `pid`, `started_at`, `pid_alive`, `agent_in_resolver`, `status`, `log_path`, `dir`.
   - `start_destroy(agent_id, paths, host_id, env)` → builds the bash command, opens `output.log`, calls `Popen(...)` detached, writes the `pid` file, returns the new record.
   - `read_destroying(agent_id, paths, agent_in_resolver)` → reads `pid`, computes status, returns the record (or None if no dir).
   - `list_destroying(paths, agent_ids_in_resolver)` → iterates `<paths.data_dir>/destroying/` and yields records.
   - `delete_destroying(agent_id, paths)` → removes the directory (idempotent).
   - `read_log_chunk(agent_id, paths, offset)` → seek + read tail.

2. `apps/minds/imbue/minds/desktop_client/agent_creator.py`:
   - Delete `start_destruction`, `_destroy_agent_background`, `_get_host_id_for_agent`, `_destroy_all_agents_on_host`, `_destroy_single_agent`, `release_imbue_cloud_host`, `get_destruction_info`, the `_destroy_statuses` / `_destroy_errors` private attrs, and the `AgentDestructionStatus` / `AgentDestructionInfo` types. The `imbue_cloud_cli` field becomes optional purely for create — destroy goes elsewhere now.
   - Add a tiny `lookup_host_id(agent_id, mngr_ctx) -> str | None` helper next to where the destroy logic used to live, or move it into `destroying.py`.

3. `apps/minds/imbue/minds/desktop_client/app.py`:
   - `_handle_destroy_agent_api`:
     - Auth check (unchanged).
     - Disassociate workspace from session store (unchanged).
     - Look up host_id via the new helper.
     - Call `destroying.start_destroy(agent_id, paths, host_id, env)`.
     - Return 202 + `{"agent_id", "status": "running", "redirect_url": "/"}`. If a destroy is already running, return 200 + same body.
   - Add `_handle_destroying_status_api` (GET `/api/destroying/<agent_id>/status`).
   - Add `_handle_destroying_log_api` (GET `/api/destroying/<agent_id>/log?after=...`).
   - Add `_handle_destroying_dismiss_api` (POST `/api/destroying/<agent_id>/dismiss`).
   - Add `_handle_destroying_page` (GET `/destroying/<agent_id>`).
   - Delete `_handle_destroy_agent_status_api` (replaced by `/api/destroying/<id>/status`).
   - `_handle_landing_page` → call `list_destroying(...)` and pass `destroying_records: dict[str, DestroyingRecord]` into the landing template.

4. `apps/minds/imbue/minds/desktop_client/templates/landing.html`:
   - For each agent_id, if `destroying_records.get(agent_id)` is set, render the marker (running spinner + link, or failed badge + link). Disable the row's main onclick handler.

5. `apps/minds/imbue/minds/desktop_client/templates/destroying.html` (new):
   - Status, pid, started, log container with `data-agent-id`. Loads `static/destroying.js`.

6. `apps/minds/imbue/minds/desktop_client/static/destroying.js` (new):
   - Polls `/api/destroying/<id>/log?after=<bytes>` every 1 s, appends new content. Polls `/api/destroying/<id>/status` on the same tick; when status flips to `done`, redirects to `/` after ~1 s; when `failed`, stops polling and reveals the Retry / Dismiss buttons.

7. `apps/minds/imbue/minds/desktop_client/templates/workspace_settings.html` + inline JS:
   - On confirm, after `fetch('/api/destroy-agent/<id>', POST)` resolves with 2xx, immediately `window.location.href = '/'`. Drop the `pollDestroyStatus()` and `destroy-spinner` element entirely (server-rendered marker on `/` replaces it).

8. Tests:
   - `destroying_test.py`: round-trip `start_destroy` against a fake destroy command (a tiny bash script that prints to stdout, exits 0 or 1), assert `pid` file is written, `output.log` captures both streams, status reads correctly off pid + agent-presence.
   - `app_test.py` patches: assert `/api/destroying/<id>/status` and `/log` shapes; assert the landing page renders the marker when a destroying dir exists.
