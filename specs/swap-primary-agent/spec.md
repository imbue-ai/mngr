# Swap primary agent

## Overview

- Today the minds "primary" agent does two unrelated jobs: it runs the bootstrap + services manager *and* is the user's first chat interface (a normal `claude` TUI in window 0 of the tmux session). Destroying or restarting it tears down services along with the chat.
- This spec separates the two. The services agent stays a `claude`-type agent (so all the existing settings/auth machinery keeps working) but its window 0 just sleeps, never invoking `claude`. A real chat agent is spawned on first container boot and is the user's actual interlocutor.
- Mechanism: a new shared-`CLAUDE_CONFIG_DIR` flag (`use_env_config_dir`, already landed) is turned on by default for the `claude` agent type, and off only for the `main` (services) type. The services agent's per-agent config dir becomes the shared dir; every other agent inherits it via the host env file the bootstrap writes.
- The bootstrap script (FCT `libs/bootstrap`) gets three new responsibilities, in order: (1) write `CLAUDE_CONFIG_DIR` to the host env file, (2) create the initial chat agent on first boot (guarded by a git-backed signal file under `runtime/`), (3) then proceed as today.
- The system_interface frontend hides agents with `is_primary=true` from the agent list. The destroy endpoint server-side rejects destroys of `is_primary=true` agents.
- No backwards compatibility for existing pre-change workspaces. Existing workspaces will need to be re-created.

## Expected Behavior

### Fresh container boot

- On first boot of a new minds workspace, the user sees one chat agent in the UI named the same as the host. It opens with `/welcome` already sent.
- The services agent (`system-services`) is present in the underlying agent list but hidden from the UI list; it has `is_primary=true`.
- All services (system_interface, web, cloudflared, app-watcher, runtime-backup) are running in tmux windows of the services agent.

### Subsequent boots of the same container

- Bootstrap re-runs. It re-writes `CLAUDE_CONFIG_DIR` to the host env (only if missing or different — idempotent), sees `runtime/initial_chat_created` already exists, and skips chat-agent creation.
- Whatever chat agents existed at last shutdown continue to show in the UI (their state lives in the shared `CLAUDE_CONFIG_DIR` and per-agent state dir).

### Lifecycle changes

- Destroying the initial chat agent (or any chat agent) leaves services untouched. The signal file is **not** cleared — no replacement is auto-created. The user creates a new chat via the "New Agent" button.
- Restarting the services agent (e.g. via container restart) does not break any chat agents — their tmux sessions are independent.
- Destroying the services agent is rejected by the workspace_server destroy endpoint with a 400 response naming the `is_primary=true` label as the reason. The UI never offers this anyway.
- Creating a new chat agent via the "New Agent" button continues to work exactly as today (`agent_manager.create_chat_agent`); the new agent inherits `CLAUDE_CONFIG_DIR` from the host env file and shares auth/plugins/settings with all other agents.

### Auth, settings, plugins

- The services agent provisions its own per-agent `CLAUDE_CONFIG_DIR` at `$MNGR_AGENT_STATE_DIR/plugin/claude/anthropic/` as before, including the existing copy-from-`~/.claude` flow for credentials/settings/plugins.
- Every other agent (initial chat, worktree, worker, user-created chat) skips per-agent config provisioning and points at the services agent's config dir via the inherited env var. They never touch the user's `~/.claude/` because they're in-container.
- Sessions live in `$CLAUDE_CONFIG_DIR/projects/<encoded-work_dir>/`. Multiple agents that share a `work_dir` share that subdir but get distinct session IDs; per-agent `claude_session_id_history` keeps the workspace_server's per-agent view correct.

### Out-of-scope edge cases

- API-key mismatch dialog: already documented as an open question in the `single-claude-data-dir` spec, not re-litigated here.
- `mngr transcript` / `mngr events`: read per-agent `events/` and `logs/` from the agent state dir, not the shared config dir; no changes needed.
- `mngr_claude._preserve_session_files`: already shared-mode-aware (skips `projects/` copy when `use_env_config_dir=True`).

## Implementation Plan

### `forever-claude-template/.mngr/settings.toml`

- `[agent_types.claude]`: add `use_env_config_dir = true`. All children of `claude` (including `worker`) inherit it.
- `[agent_types.main]`: add `use_env_config_dir = false` (explicit override) and `command = "sleep infinity && claude"` with a short comment explaining that the trailing `&& claude` is unreachable; it keeps the command claude-shaped so `assemble_command` produces something well-formed.
- `[create_templates.main]`: remove `message = "/welcome"`. The welcome message now belongs to the bootstrap-created chat agent. Reviewer env machinery and `reviewer_settings` extra_window stay.
- `[create_templates.worktree]`: change `type = "main"` → `type = "claude"`. Worktree agents thereby get a real claude command and the shared `CLAUDE_CONFIG_DIR`, not sleep-infinity.
- `[agent_types.worker]`: unchanged. Inherits `use_env_config_dir = true` from claude.
- `[create_templates.chat]`: unchanged. Still inherits from claude, still gets shared config dir.

### `forever-claude-template/libs/bootstrap/src/bootstrap/manager.py`

- New constants near the top:
  - `INITIAL_CHAT_SIGNAL = Path("runtime/initial_chat_created")` — git-backed via runtime-backup.
  - `HOST_DIR_ENV_VAR = "MNGR_HOST_DIR"`, `AGENT_ID_ENV_VAR = "MNGR_AGENT_ID"`, `AGENT_STATE_DIR_ENV_VAR = "MNGR_AGENT_STATE_DIR"`.
  - `CLAUDE_CONFIG_DIR_ENV_VAR = "CLAUDE_CONFIG_DIR"`.
- New module-level function `_resolve_services_claude_config_dir() -> Path | None`: returns `Path($MNGR_AGENT_STATE_DIR)/plugin/claude/anthropic` when both env vars are set, else `None` with a warning. This is the services agent's own config dir.
- New function `_ensure_host_claude_config_dir(target: Path) -> None`: parses `$MNGR_HOST_DIR/env` (use the existing `parse_env_file` from `imbue.mngr.utils.env_utils` if available, else a local minimal parser to keep bootstrap dependency-light); only rewrites the file if `CLAUDE_CONFIG_DIR` is missing or differs from `target`. Logs the action.
- New function `_read_host_name() -> str | None`: reads `host_name` from `$MNGR_HOST_DIR/data.json` (same logic as `workspace_server._read_host_name`). Returns `None` on failure.
- New function `_read_main_agent_labels() -> dict[str, str]`: reads `$MNGR_HOST_DIR/agents/$MNGR_AGENT_ID/data.json` and returns `data["labels"]` as a dict[str, str]. Returns `{}` on any read/parse error.
- New function `_create_initial_chat_agent(host_name: str, labels: Mapping[str, str]) -> bool`: runs `mngr create <host_name> --template chat --message /welcome --no-connect` plus `--label workspace=<value>` / `--label project=<value>` for any of those keys present in `labels`. Returns `True` on success, `False` on failure. Logs subprocess stdout/stderr at info/error level.
- New function `_maybe_create_initial_chat() -> None`: if `INITIAL_CHAT_SIGNAL` exists, return. Else read host_name (fallback: workspace label from data.json), read main-agent labels, call `_create_initial_chat_agent`. Touch the signal file **only on success** (matches the `1b` decision from Q&A round 3). Failure is logged and bootstrap proceeds — services come up so the user has a working UI; next boot retries.
- New function `_bootstrap_init_chat_dir() -> None`: convenience wrapper that does (1) `_ensure_host_claude_config_dir(...)` (2) `_maybe_create_initial_chat()`.
- `main()`: reorder to run `_bootstrap_init_chat_dir()` **first**, then `_init_runtime_worktree()`, then enter the services reconcile loop. (The reorder is required: chat-agent create needs the host env to already point at the shared `CLAUDE_CONFIG_DIR`, and the signal file we check first should ideally end up in `runtime/`; but `runtime/` isn't a git worktree yet on the very first run. The chat-create call doesn't depend on `runtime/` being a worktree — only on the signal file path existing or not — so this ordering is safe. The signal file will be inside the `runtime/` directory once `_init_runtime_worktree` runs; on the first boot, we create `runtime/` lazily by `mkdir -p` before touching the signal file.)
- New function `_touch_signal() -> None`: ensures `runtime/` exists (`mkdir -p`) and creates `runtime/initial_chat_created` with current timestamp content.

### `forever-claude-template/libs/bootstrap/src/bootstrap/manager_test.py`

- Add unit tests for the new helpers (see Testing Strategy below).

### `forever-claude-template/apps/system_interface/imbue/minds_workspace_server/server.py`

- `_destroy_agent`: before invoking the `mngr destroy` subprocess, look up the agent via `agent_manager.get_agent_by_id` and check `labels.get("is_primary") == "true"`. If so, return a 400 with an `ErrorResponse` whose detail names the `is_primary` guard. No env-var-based detection — labels are the authoritative signal.

### `forever-claude-template/apps/system_interface/imbue/minds_workspace_server/server_test.py`

- Add `test_destroy_rejects_is_primary_agent`: monkeypatched agent_manager returning a primary, POST to `/api/agents/<id>/destroy` returns 400 and `mngr destroy` is not invoked.

### `forever-claude-template/apps/system_interface/frontend/`

- Filter `is_primary=true` agents at the data layer (matches `2b` from Q&A round 3). Identify the single source-of-truth state slice / hook (likely a `useAgents` hook or a redux/zustand selector) and apply a `.filter(a => a.labels?.is_primary !== "true")` (or equivalent) before any component consumes it.
- Audit any other consumer of the raw agents list — anywhere that reads the websocket `agents_updated` payload or the `/api/agents` response — and route through the same selector so no component bypasses the filter.
- No backend changes to `/api/agents`, the websocket `agents_updated` broadcast, or `create_chat_agent`'s primary-agent lookup — those still need to see the services agent.

### `apps/minds/imbue/minds/desktop_client/agent_creator.py`

- Audit only — the local desktop-side creator (in this mngr repo, not in FCT) currently passes `--label is_primary=true` (line 454) for the main agent. That stays. No change.

### Documentation

- `apps/minds/README.md` and `apps/minds/docs/design.md`: add a short paragraph in the "How it works" section noting the split between the services agent and chat agents and pointing at this spec.
- `forever-claude-template/CLAUDE.md` (or wherever the FCT-side primer lives): note that the services agent runs `sleep infinity && claude` and exists only to host the bootstrap and services tmux windows.

### Changelog

- `changelog/mngr-swap-primary-agent.md`: replace placeholder with a user-facing paragraph describing the split, the destroy guard, and the new `initial_chat_created` signal file under `runtime/`.

## Implementation Phases

Each phase ends in a working state. Phase boundaries are commit-friendly.

### Phase 1 — Settings.toml flips

- Edit `forever-claude-template/.mngr/settings.toml`:
  - `[agent_types.claude]`: add `use_env_config_dir = true`.
  - `[agent_types.main]`: add `use_env_config_dir = false`, set `command = "sleep infinity && claude"` with comment.
  - `[create_templates.main]`: drop `message = "/welcome"`.
  - `[create_templates.worktree]`: switch `type` from `main` to `claude`.
- Manually verify the values by inspecting `mngr config print` or equivalent. Don't yet expect end-to-end correctness — the chat agent hasn't been created yet, and the bootstrap hasn't been taught to write the host env.

### Phase 2 — Bootstrap teaches itself to write CLAUDE_CONFIG_DIR

- Add `_ensure_host_claude_config_dir`, `_resolve_services_claude_config_dir` to `bootstrap/manager.py`.
- Wire into `main()` before `_init_runtime_worktree()`.
- Unit test the env-file write (read/parse/idempotent rewrite).
- After this phase, manually launching a new chat agent via the "New Agent" button should pick up the shared config dir.

### Phase 3 — Bootstrap creates initial chat agent

- Add `_read_host_name`, `_read_main_agent_labels`, `_create_initial_chat_agent`, `_maybe_create_initial_chat`, `_touch_signal`.
- Wire into `main()` after the env-write step.
- Unit test the gating: signal-file present → skip; signal-file absent + create succeeds → signal-file written; create fails → signal-file untouched, exception not raised.

### Phase 4 — Hide services agent in UI; server-side destroy guard

- Add the `is_primary` filter in the frontend data layer.
- Add the destroy-endpoint guard in `server.py:_destroy_agent`.
- Server-test for the destroy guard.

### Phase 5 — Docs + changelog

- Replace the placeholder changelog with user-facing copy.
- Update `apps/minds/README.md` and FCT docs.

## Testing Strategy

### Bootstrap unit tests (`manager_test.py`)

- `test_ensure_host_claude_config_dir_writes_when_missing`: empty `MNGR_HOST_DIR/env` → file gains `CLAUDE_CONFIG_DIR=...`.
- `test_ensure_host_claude_config_dir_no_rewrite_when_match`: file already has the right value → no fs write (use mtime check).
- `test_ensure_host_claude_config_dir_overwrites_drifted_value`: file has a different value → rewrite.
- `test_read_host_name_from_data_json` / `test_read_host_name_returns_none_on_missing_file`.
- `test_read_main_agent_labels_returns_dict` / `test_read_main_agent_labels_returns_empty_on_missing_workspace_label`.
- `test_create_initial_chat_agent_command_shape`: mock the subprocess and assert the constructed argv matches `mngr create <host_name> --template chat --message /welcome --no-connect --label workspace=... [--label project=...]`.
- `test_maybe_create_initial_chat_skips_when_signal_present`.
- `test_maybe_create_initial_chat_touches_signal_only_on_success`: subprocess returns rc=0 → signal file exists; subprocess returns rc=1 → signal file does not exist.

### workspace_server tests (`server_test.py`)

- `test_destroy_rejects_is_primary_agent`: agent_manager returning an agent with `labels={"is_primary": "true"}` → POST returns 400, `mngr destroy` is not invoked. Existing destroy success test still passes.

### Integration / acceptance (`test_*.py`, offload)

- `test_workspace_first_boot_creates_initial_chat_agent`: spin up a minds container from scratch (or simulate via an FCT-shaped test fixture), wait for bootstrap → assert one chat agent exists with name == workspace name, has `/welcome` in its session.
- `test_workspace_second_boot_does_not_recreate_chat_agent`: same fixture, second bootstrap run → still one chat agent (no duplicate).
- `test_destroy_chat_agent_leaves_services_running`: destroy the initial chat, then poll for running services → all still up.
- `test_destroy_services_agent_rejected`: HTTP POST /api/agents/<services-id>/destroy → 400; services still running.
- `test_multiple_chat_agents_share_config_dir_but_distinct_sessions`: create two chat agents, confirm their `claude_session_id_history` files differ, confirm session JSONLs coexist under the same `projects/<encoded>/` dir without collision.

### Edge cases to verify

- Bootstrap runs on second boot of an existing pre-change workspace (no compat shim): one of two things happens — either the user re-creates the workspace (expected), or the bootstrap fails predictably with a clear error. Verify the failure mode is loud.
- `mngr create` for the initial chat agent fails (e.g. naming collision because someone manually created an agent named the same as the host before bootstrap got there): bootstrap logs the failure, services come up, user can resolve manually. Signal file remains absent; next boot retries.
- `runtime/` is read-only on first bootstrap run (transient git issue): `_touch_signal` should fail gracefully and log; do not crash the bootstrap. The chat agent was still created. Next boot retries — this can produce a duplicate. Acceptable for v1; documented in Open Questions.

### Ratchets

- None expected. The frontend filter adds one TS file change; the destroy guard adds a few lines to existing server code. No new anti-patterns to track.

## Open Questions

- **Duplicate-chat-on-touch-failure**: if `_create_initial_chat_agent` succeeds but `_touch_signal` then fails (very narrow: `runtime/` write error), the next boot will create a second chat agent named the same as the host. `mngr create` will most likely fail on name collision, so the practical impact is "one duplicate-attempt log line per boot until the signal file can be written". Spec accepts this as v1. Alternative: write the signal file *before* the chat-create call (matches the original prompt's "exactly-once ever" phrasing more literally) — at the cost of swallowing transient mngr-create failures. The user explicitly chose touch-on-success in Q&A round 3, so the spec keeps that.
- **Bootstrap ordering vs. `_init_runtime_worktree`**: `runtime/initial_chat_created` lives inside `runtime/`, which becomes a git worktree of `mindsbackup/$MNGR_AGENT_ID` via `_init_runtime_worktree`. Doing the chat-create *before* the runtime-worktree init means the signal file is written to a plain `runtime/` directory; `_init_runtime_worktree` later does `_stage_preexisting_aside()` → `git worktree add` → `_restore_preexisting_into_worktree()`, so the signal file ends up inside the worktree (and thus git-backed) on the same boot. Verify this by inspection during Phase 3 testing. If it turns out to break, an alternative is to swap the order — but then the very-first-boot fast-path waits on the runtime fetch before the user sees their chat agent.
- **Frontend filter location**: spec says "data layer" but the concrete location depends on whether the frontend uses a hook (`useAgents`), a redux/zustand store, or component-level state. The implementer should pick the single point of truth that funnels the `/api/agents` REST response *and* the `agents_updated` WS payload through a common selector. Both paths must apply the same filter.
- **`mngr create` reachability inside the bootstrap subprocess**: bootstrap runs inside the services agent's tmux session, where `mngr` is on PATH (installed via `uv tool install`). Verify in Phase 3 that the subprocess inherits PATH correctly; if not, qualify with `$HOME/.local/bin/mngr`.
- **`CLAUDE_CONFIG_DIR` value drift across plugin path rewrites**: `mngr_claude._resolve_plugins_dir_sentinel` rewrites plugin paths in the per-agent config dir at provision time. The services agent (non-shared) still gets that pass. Other agents (shared) skip it per the existing `use_env_config_dir` spec. No interaction expected, but worth a smoke test in Phase 4 acceptance.
