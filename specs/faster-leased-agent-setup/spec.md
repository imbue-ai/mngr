# Faster leased-agent setup

## Overview

- Creating a new minds project in LEASED mode currently takes ~67s end-to-end: rename 25s, start 20s, tunnel create 5s, tunnel inject 17s.
- Cause: each step spawns a fresh `mngr` subprocess that re-runs provider discovery (scanning every provider, resolving names to IDs), and tunnel creation/injection blocks `DONE`.
- Fix A — skip the discovery tax: invoke every `mngr` command with an explicit `agent-id@host-id.provider-name` address so discovery only loads the SSH provider and does no ID lookup.
- Fix B — parallelize the independent work: reorder setup to `rename → (label ∥ MINDS_API_KEY inject) → start`, sharing a single `ConcurrencyGroup`.
- Fix C — remove tunnel from the critical path: `_OnCreatedCallbackFactory` schedules tunnel creation+injection as a fire-and-forget background task on a new root `ConcurrencyGroup` and returns immediately (applies to both LEASED and non-LEASED modes).
- Failures in the detached tunnel task are surfaced to the user via the existing `NotificationDispatcher` (distinct copy, no rate limit).
- Scope excludes the destroy path's code and non-LEASED modes' explicit-address conversion; those CGs are reparented under the root for hierarchy coherence but otherwise untouched.

## Expected Behavior

- LEASED agent creation reaches `DONE` noticeably faster than the current 67s baseline; rename / label / start / apikey-inject phase runs in visibly less wall-clock time than today's sequential trio.
- Tunnel setup is no longer on the critical path; the user is redirected to the working agent as soon as `mngr start` completes, and the Cloudflare tunnel appears shortly afterward.
- When the background tunnel task fails, the user sees an OS notification titled "Tunnel setup failed" naming the affected agent and the underlying error; they can retry via the Share UI (unchanged).
- When the background tunnel task succeeds, nothing user-visible happens (same as today).
- If `rename` fails, the flow aborts and cleanup runs (same as today).
- If either the `label` or apikey-inject subprocess fails, the shared `ConcurrencyGroup` propagates shutdown to its sibling and aggregates the failures; the flow aborts and cleanup runs.
- If `mngr start` fails, the flow aborts and cleanup runs (same as today).
- On desktop client shutdown, the root `ConcurrencyGroup` waits the default `shutdown_timeout_seconds` for any in-flight tunnel tasks to complete before forcing exit (default CG behavior).
- Concrete timing numbers will be measured manually post-implementation; no automated benchmark is added.

## Implementation Plan

Packaged as a single PR.

### New: root `ConcurrencyGroup`

- `apps/minds/imbue/minds/desktop_client/runner.py`
  - In `run_desktop_client` (or equivalent lifecycle entry point), construct a root `ConcurrencyGroup(name="desktop-client")` and enter it with a `with` block that brackets the FastAPI lifespan.
  - Pass the root CG as a new parameter to `AgentCreator` at construction.

### `AgentCreator` (`apps/minds/imbue/minds/desktop_client/agent_creator.py`)

- Add two new `Field`s on `AgentCreator`:
  - `root_concurrency_group: ConcurrencyGroup` — the top-level CG owned by `runner.py`.
  - `notification_dispatcher: NotificationDispatcher | None` — for surfacing tunnel failures from background tasks; default `None` so tests that don't care can skip it.
- Every existing `ConcurrencyGroup(name=...)` constructed inside this module (`git-clone`, `git-checkout`, `rsync-worktree`, `ssh-keygen`, `git-ls-remote-tags`, `mngr-create`, `mngr-list-host`, `mngr-destroy-host`, `mngr-destroy`, and the new leased-setup group) becomes a child group created via the parent-CG `child_group()`/`parent=` mechanism.
  - Helper-level functions that currently create their own CG (`clone_git_repo`, `checkout_branch`, `_rsync_worktree_over_clone`, `_load_or_create_leased_host_keypair`, `resolve_template_version`, `run_mngr_create`) gain a new required `parent_cg: ConcurrencyGroup` parameter and create their child group from it. Callers thread the root CG through.
- New helper `_leased_agent_address(agent_id: AgentId) -> str` returns `f"{agent_id}@leased-{agent_id}.ssh"`. Centralizes the format so all mngr calls in the leased flow share one definition.

### Leased-setup flow (`_setup_and_start_leased_agent`)

- Replace the three independent `ConcurrencyGroup(name="mngr-label"/"mngr-rename"/"mngr-start"/"mngr-exec-apikey")` groups with a single shared child `ConcurrencyGroup(name="mngr-leased-setup")` whose parent is `self.root_concurrency_group`.
- New sequence inside the single `with cg:` block:
  1. **rename** (sequential) — `cg.run_process_to_completion(command=[MNGR_BINARY, "rename", _leased_agent_address(agent_id), str(parsed_name)], ...)`, raise `MngrCommandError` on non-zero.
  2. **Emit** both log lines (`log_queue.put("[minds] Applying labels and injecting MINDS_API_KEY in parallel...")` — single consolidated line).
  3. **label + apikey inject** (parallel):
     - `label_proc = cg.run_process_in_background(command=[MNGR_BINARY, "label", _leased_agent_address(agent_id), "-l", f"workspace={parsed_name}", "-l", "user_created=true", "-l", "is_primary=true"], is_checked_by_group=True, on_output=emit_log)`
     - `apikey_proc = cg.run_process_in_background(command=[MNGR_BINARY, "exec", _leased_agent_address(agent_id), sed_inject_command], is_checked_by_group=True, on_output=emit_log)`
     - `label_proc.wait(); apikey_proc.wait()` — failures propagate through the group's `is_checked_by_group` aggregation.
  4. **start** (sequential) — `cg.run_process_to_completion(command=[MNGR_BINARY, "start", _leased_agent_address(agent_id)], ...)`.
- Generate + hash + save the API key *before* the parallel step (same function calls as today — `generate_api_key`, `hash_api_key`, `save_api_key_hash`) so the sed command already has the key to inject.
- Keep the `sed -i '/^MINDS_API_KEY=/d' && echo 'MINDS_API_KEY=...' >> /mngr/agents/<id>/env` command unchanged; just move it to run in parallel with label (still via `mngr exec`).
- After `start` succeeds: immediately set `self._statuses[aid] = AgentCreationStatus.DONE` and set `redirect_url` — before invoking any `on_created` callback.
- Call `on_created(agent_id)` last (after DONE). `on_created` now returns quickly (spawns a background task); the existing try/except around the callback is **removed** per the "callback never raises synchronously" contract.

### `_OnCreatedCallbackFactory` (`apps/minds/imbue/minds/desktop_client/app.py`)

- New field `root_concurrency_group: ConcurrencyGroup` (frozen).
- New field `notification_dispatcher: NotificationDispatcher | None` (frozen).
- `__call__(agent_id)` no longer does the Cloudflare work inline. It:
  1. Looks up the account / access token exactly as today.
  2. If no account or token: return (same as today).
  3. Otherwise: schedule a background task on `self.root_concurrency_group` that performs the current body (`enriched_client.create_tunnel` → `_save_tunnel_token` → `inject_tunnel_token_into_agent`) and returns immediately.
- The background task uses `root_concurrency_group.start_thread(...)` with a small wrapper function `_run_tunnel_setup(agent_id, ...)` defined in `app.py`. The wrapper catches exceptions from the tunnel work and dispatches notifications/logs (see below).
- `_build_on_created_callback` gains `root_concurrency_group` and `notification_dispatcher` arguments, plumbed from `request.app.state` / the runner.

### Tunnel background task (`_run_tunnel_setup`)

- Defined in `app.py`; takes `(agent_id, enriched_client, paths, notification_dispatcher, agent_display_name)`.
- Calls `create_tunnel` unconditionally (it's idempotent on the Cloudflare side — no `load_tunnel_token` short-circuit).
- On `create_tunnel` success: `_save_tunnel_token`, then `inject_tunnel_token_into_agent`. On injection failure (non-zero exit): `logger.warning(...)` AND dispatch a `NotificationRequest(title="Tunnel setup failed", message=f"Couldn't set up the Cloudflare tunnel for '{agent_display_name}'. Sharing may be unavailable. Error: {msg}", urgency=NotificationUrgency.NORMAL)` via `self.notification_dispatcher.dispatch(...)`.
- On `create_tunnel` failure: same notification + loguru error.
- No rate limiting — every failure dispatches one notification.
- `inject_tunnel_token_into_agent` continues to use `mngr exec` with a bare agent id (non-LEASED paths also call this; out-of-scope for explicit-address conversion this PR).

### Non-LEASED path (`_create_agent_background`)

- Keep the body unchanged except for the `on_created` call site: still invokes the callback at the same point, but the callback now returns quickly and the real work happens in the background thread.
- The `try/except (ValueError, OSError)` around `on_created(agent_id)` is **removed** per the tightened contract.

### `AgentCreator` construction in `runner.py`

- Thread `root_concurrency_group` and `notification_dispatcher` into `AgentCreator(...)`.
- `_OnCreatedCallbackFactory` construction (inside `_build_on_created_callback`) gets both wired in from `request.app.state`.

### API endpoint parity

- The `_handle_cloudflare_enable` endpoint in `api_v1.py` keeps its existing synchronous `create_tunnel` + `inject_tunnel_token_into_agent` call — it's a user-initiated retry path where blocking is acceptable. No change.

## Implementation Phases

1. **Root `ConcurrencyGroup` plumbing.** Add the root CG in `runner.py`; pass it into `AgentCreator` and `_build_on_created_callback`. Reparent every existing CG in `agent_creator.py` under it. No behavior change yet; tests should still pass.
2. **Leased-setup reorder + shared CG.** Consolidate the four per-operation CGs into one shared child group; switch to `rename → (label ∥ apikey) → start`; convert every mngr call in the leased flow to use `_leased_agent_address()`. Update `agent_creator_test.py` assertions for the new sequence. Tests green.
3. **Tunnel out-of-band.** Move `_OnCreatedCallbackFactory.__call__` body into a background task scheduled on the root CG; set `DONE` in `_setup_and_start_leased_agent` before invoking `on_created`; remove the try/except around `on_created` in both caller sites; wire `notification_dispatcher` into `AgentCreator` and the factory; implement `_run_tunnel_setup` with failure notifications. Update tests.
4. **Manual end-to-end measurement.** Run a fresh LEASED create flow against a staging pool and record timings for rename / label+apikey / start / time-to-DONE; include a commit-message summary in the PR description.

Each phase leaves the system in a working state and is individually verifiable via `just test-quick apps/minds`.

## Testing Strategy

- **`agent_creator_test.py` updates.** Existing assertions that expect sequential `mngr label` → `mngr rename` → `mngr start` → `mngr exec` subprocess calls are revised to expect:
  - first subprocess: `mngr rename {agent_id}@leased-{agent_id}.ssh {name}`
  - next two subprocesses: `mngr label ...` and `mngr exec ... sed ...` started in either order (assert both were started before `mngr start`)
  - final subprocess: `mngr start {agent_id}@leased-{agent_id}.ssh`
  - `DONE` status observable before the tunnel task's work is observable
- **`app.py` / `_OnCreatedCallbackFactory` test update.** Assert that invoking the callback returns immediately (does not block on `create_tunnel`) and that a fake `CloudflareClient`'s `create_tunnel` is invoked asynchronously; a fake `NotificationDispatcher` receives a dispatch when `create_tunnel` raises.
- **Failure-path tests.** Simulate label / apikey-inject failure and assert the flow aborts with an aggregated `ConcurrencyExceptionGroup` (or equivalent from `ConcurrencyGroup`); simulate rename failure and assert cleanup runs (same as today).
- **No new overlap-timing test.** Per agreed scope; parallelism is asserted structurally (both subprocesses started before `start`), not by measuring wall-clock.
- **Ratchets.** If any regex-based ratchet in `apps/minds/imbue/minds/test_ratchets.py` flags the new patterns (e.g. multiple `run_process_in_background` calls in sequence), fix in the spirit of the ratchet or bump the count with explanation.
- **CI.** Full `just test-offload` run post-implementation; manual tmux verification of an end-to-end LEASED create against a real pool.

## Open Questions

- Other `ConcurrencyGroup` call sites outside `agent_creator.py` (`api_v1.inject_tunnel_token_into_agent`, `backend_resolver.StreamManager._cg`, `notification.py`, `cli/pool.py`) are not reparented by this PR — is that worth a follow-up sweep? The spec leaves them as-is.
- The background tunnel task uses `mngr exec` with a bare agent id (not the explicit `id@host.ssh` address). Converting it would require knowing whether the agent is LEASED vs not at tunnel-inject time; out of scope.
- `shutdown_timeout_seconds` for the root CG uses `ConcurrencyGroup` defaults; no custom tuning for "possibly-long Cloudflare API call" cases. Acceptable risk — worst case the shutdown forcibly aborts a tunnel task that would have succeeded, and the user retries via Share UI on next launch.
- Qualitative measurement only — no automated regression guard for the timing improvement. If the wins later regress, we'll notice via manual timing, not a test.
