# Scoping: `waiting_reason` for `mngr_codex`

Status: scoping (not yet implemented). This document scopes porting the
Claude-style `waiting_reason` listing field to the codex plugin. It is a
follow-up to the gap recorded in `spec.md` section P and in `README.md`
("Not yet implemented").

The central open question -- does codex's `PermissionRequest` hook fire live --
has now been **verified** against real codex 0.139.0 (see "Live verification"
below). Both reasons are confirmed implementable.

## Goal

Surface, in `mngr list`, *why* a codex agent is WAITING -- mirroring claude's
`claude.waiting_reason` field, which reports:

- `PERMISSIONS` -- blocked on a tool-approval dialog
- `END_OF_TURN` -- idle, turn complete, awaiting user input
- `None` -- actively running

Core has no first-class "WAITING reason"; this is a plugin-namespaced
embellishment exposed via the `agent_field_generators` hook.

## Reference implementation (claude)

- Enum + logic + hook: `libs/mngr_claude/imbue/mngr_claude/plugin.py:2498-2535`.
  `_waiting_reason` reads two marker files under the agent state dir:
  `permissions_waiting` present -> `PERMISSIONS`; else `active` absent ->
  `END_OF_TURN`; else `None`.
- Marker maintenance (claude hooks): `libs/mngr_claude/imbue/mngr_claude/claude_config.py`:
  - `PermissionRequest` -> `touch permissions_waiting` (`:665-673`)
  - `PostToolUse` / `PostToolUseFailure` -> `rm -f permissions_waiting` (`:675-694`)
  - `UserPromptSubmit` / `Stop` / idle `Notification` also clear it as a safety
    net (`:546-556`, `:655`). claude uses a simple touch/remove (no refcount),
    plus a `SESSION_GUARD` so a nested claude sharing the home can't interfere.

## Current codex state (what already exists)

- Lifecycle marker is fully maintained. `active` exists IFF
  (`codex_root_active` present OR `codex_subagents/` non-empty), recomputed
  under a mkdir lock. Constants at
  `libs/mngr_codex/imbue/mngr_codex/codex_config.py:132-189`; invariant and
  rationale in the module docstring (`:28-62`).
- Four hooks wired in `build_codex_hooks_config`
  (`codex_config.py:484-525`): `UserPromptSubmit`, `Stop`, `SubagentStart`,
  `SubagentStop`. Each runs a script provisioned into
  `$MNGR_AGENT_STATE_DIR/commands/` (provisioning at `plugin.py:379-399`).
- Marker scripts: `set_active_marker.sh`, `clear_active_marker.sh`,
  `subagent_started.sh`, `subagent_stopped.sh`, shared helper
  `codex_marker_state.sh` (lock + `codex_marker_recompute`).
- Hooks already run because launch passes `--dangerously-bypass-hook-trust`
  (`plugin.py:138`, consent-gated). A new hook entry will therefore fire
  without extra trust plumbing.
- `get_lifecycle_state` is inherited from the interactive-TUI base agent and
  reads only the `active` marker; it does **not** special-case permissions.
  This matches claude -- `waiting_reason` is computed independently in the
  field generator, not in `get_lifecycle_state`.
- No `agent_field_generators` hookimpl exists in the codex plugin yet.

## Feasibility

Both reasons are feasible.

- `END_OF_TURN`: trivial -- read absence of `active` (already maintained).
- `PERMISSIONS`: feasible -- codex has a full Claude-style hooks system
  (stable) that includes `PermissionRequest`, `PostToolUse`, and
  `PostToolUseFailure` (`codex-investigation.md:38-43`). The same
  marker pattern ports over.

### Mode dependency (important)

`PERMISSIONS` only ever applies in *supervised* mode. `approval_policy` is left
unset (codex's prompting default) unless `auto_allow_permissions=true`, which
forces `approval_policy="never"` and suppresses all dialogs
(`plugin.py:413`, `codex_config.py:326`). So:

- `auto_allow_permissions=false` (default) -> codex prompts -> `PermissionRequest`
  fires -> `PERMISSIONS` is meaningful.
- `auto_allow_permissions=true` -> no dialogs -> `permissions_waiting` is never
  created, which is correct (the agent never waits on permission).

No special handling needed; the marker simply never appears in auto-allow mode.

## Live verification (codex 0.139.0)

The one prior unknown -- `codex-investigation.md:38-43` had verified
`SessionStart -> UserPromptSubmit -> Stop` firing live but only confirmed
`PermissionRequest` *exists* in the hook list, not that it fires -- is now
**resolved**. Verified against real codex 0.139.0 (ChatGPT auth), driven in
tmux in supervised mode (`approval_policy="untrusted"`, the bundled-sandbox
escalation path), with a throwaway `CODEX_HOME` wiring every event to a logging
hook and `--dangerously-bypass-hook-trust`. Prompted a shell command; observed
this exact ordering from the hook log:

| # | Event | Marker effect | Notable payload fields |
|---|-------|---------------|------------------------|
| 1 | `SessionStart` | -- | `session_id`, `transcript_path`, `source` |
| 2 | `UserPromptSubmit` | -- | `turn_id`, `prompt` |
| 3 | `PreToolUse` | -- | `tool_name`, `tool_input`, **`tool_use_id`** |
| 4 | `PermissionRequest` | touch `permissions_waiting` | `tool_name`, `tool_input`, `turn_id` -- **no `tool_use_id`** |
| 5 | `PostToolUse` (after approval) | rm `permissions_waiting` | `tool_input`, `tool_response`, `tool_use_id` |
| 6 | `Stop` | rm `permissions_waiting` (safety net) | `last_assistant_message` |

Findings that shape the design:

- **`PermissionRequest` fires live and blocks.** The marker was present for the
  entire duration the approval dialog was open and cleared on `PostToolUse`.
  The exact claude pattern works unmodified.
- **`PostToolUseFailure` does not exist in codex** (0 occurrences in the
  binary; claude has it, codex does not). Cleanup must use `PostToolUse` plus
  `Stop` as the safety net -- do **not** wire `PostToolUseFailure`.
- **`PermissionRequest` carries no `tool_use_id`** (only `PreToolUse`/
  `PostToolUse` do). It does carry `session_id` and `turn_id`. So a refcount
  keyed on call id is not possible from `PermissionRequest` alone; a session/
  turn guard is, if ever needed. The simple flag (below) needs neither.
- Every event payload includes `session_id` and `cwd`, matching the existing
  marker scripts' assumptions.

Repro lives at `/tmp/codex_hooktest/` (throwaway `CODEX_HOME` + `hook_events.log`).

## Async-subagent concurrency analysis

codex subagents are asynchronous: the root's `Stop` can fire while subagents
keep running, with no ordering guarantee on later `SubagentStop` hooks. This is
why the `active` marker needs refcounting (one file per subagent). Does
`permissions_waiting` need the same?

- The `active` marker tracks *ongoing activity* across N concurrent actors, so
  it must refcount.
- `permissions_waiting` tracks *a blocking dialog*. In the common case there is
  at most one outstanding approval dialog at a time (the user can only answer
  one prompt). A simple touch-on-request / remove-on-resolve matches claude and
  is correct for that case.
- Edge case: root blocked on a dialog while an async subagent also requests
  approval, or a subagent's `PostToolUse` clears the marker while the root's
  dialog is still open. With a single flag file, the first resolve clears the
  marker prematurely. This is a transient mis-report in a list column (self-
  corrects on the next request cycle), not a correctness bug in lifecycle state.

Recommendation: ship the simple flag first (matches claude, low risk). Only add
refcounting (e.g. one file per pending `(session, call_id)` under a
`codex_permissions/` dir, recomputed under the existing lock) if live testing
shows overlapping root+subagent dialogs are common and the mis-report is
annoying. Defer that complexity until evidence justifies it.

## Implementation plan

Mirror claude, adapted to codex's provisioned-script + commands/ dir pattern.

1. **Constants** (`codex_config.py`, near `:178-189`): add
   `PERMISSIONS_WAITING_FILENAME = "permissions_waiting"` and two script-name
   constants `SET_PERMISSIONS_WAITING_SCRIPT_NAME` /
   `CLEAR_PERMISSIONS_WAITING_SCRIPT_NAME`.

2. **Scripts** (`resources/`): two small scripts following the existing style
   (read nothing complex from stdin; the simple version needs no lock):
   - `set_permissions_waiting.sh`: `touch "$MNGR_AGENT_STATE_DIR/permissions_waiting"`
   - `clear_permissions_waiting.sh`: `rm -f "$MNGR_AGENT_STATE_DIR/permissions_waiting"`
   Each existing marker script has a `_test.py`; add matching unit tests.

3. **Hook wiring** (`build_codex_hooks_config`, `codex_config.py:518-525`):
   add `PermissionRequest -> set_permissions_waiting.sh` and
   `PostToolUse -> clear_permissions_waiting.sh`. (Codex has **no**
   `PostToolUseFailure` event -- verified above -- so unlike claude there is no
   third clear hook; `PostToolUse` fires after the approved tool runs.) Also
   clear the marker in `clear_active_marker.sh` (Stop) as a safety net, mirroring
   claude, so an unresolved/cancelled dialog can't strand the marker across turn
   end. The live trace confirmed `Stop` fires reliably at turn end.

4. **Provisioning** (`plugin.py:379-399`): add the two new scripts to the
   `provision_scripts_to_commands_dir` map.

5. **Field generator** (`plugin.py`): add a `WaitingReason` enum
   (`PERMISSIONS`, `END_OF_TURN`), a `_waiting_reason(agent, host)` that reads
   the two markers (copy claude's `plugin.py:2514-2529`, swapping the agent dir
   resolution to codex's `host.host_dir / "agents" / str(agent.id)`), and an
   `agent_field_generators` hookimpl returning `("codex", {"waiting_reason":
   _waiting_reason})`. Reuse a host-file-exists helper rather than SSH/tmux.

6. **Docs**: update `libs/mngr_codex/README.md` (remove `waiting_reason` from
   "Not yet implemented") and `spec.md` section P (codex moves from "feasible"
   to "implemented"). Add a `dev`/`mngr_codex` changelog entry per the
   changelog policy.

## Testing plan

- Unit (`*_test.py`): the two new marker scripts (touch/remove), and
  `build_codex_hooks_config` now emitting the two new hook entries
  (`PermissionRequest`, `PostToolUse`). Mirror claude's three `_waiting_reason`
  cases (PERMISSIONS / END_OF_TURN / running) in the codex plugin test.
- Acceptance/release: a release test that drives a real codex agent to a
  permission dialog and asserts `mngr list` reports
  `codex.waiting_reason == PERMISSIONS`, then resolves it and asserts the field
  clears. The live trace above is the manual version of exactly this arc, so the
  test is known-achievable. Model it on the existing claude `waiting_reason`
  coverage and codex's marker tests.

## Effort estimate

Small-to-medium, and now de-risked. The pattern is established (claude), codex's
hook/marker infrastructure already exists, and the one unknown (live
`PermissionRequest` firing) is verified. The work is ~2 short scripts, 2 hook
entries, a one-line `Stop`-script addition, a provisioning line, the enum +
field generator + hookimpl, and tests. No remaining blockers.
