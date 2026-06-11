# Scoping: `waiting_reason` for `mngr_codex`

Status: scoping (not yet implemented). This document scopes porting the
Claude-style `waiting_reason` listing field to the codex plugin. It is a
follow-up to the gap recorded in `spec.md` section P and in `README.md`
("Not yet implemented").

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

## Open risk to resolve before building

`codex-investigation.md:38-43` verified `SessionStart -> UserPromptSubmit ->
Stop` firing **live** in the TUI, but `PermissionRequest` is only confirmed to
*exist* in the documented hook list (`:39`), not verified to fire live
(`:174-176` calls it "feasible", not verified). This is the one real unknown.

Required verification (do first): launch a supervised codex agent
(`auto_allow_permissions=false`, a sandbox/approval policy that prompts), drive
it to a tool call that triggers an approval dialog, and confirm a
`PermissionRequest` hook fires with the event JSON on stdin. Capture the payload
shape (does it carry `session_id`? a tool name? a call id?) -- this determines
whether a session guard or refcounting is warranted. If `PermissionRequest`
does **not** fire live, fall back to shipping `END_OF_TURN`-only (still strictly
better than today) and document `PERMISSIONS` as blocked on the CLI.

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
   add `PermissionRequest -> set_permissions_waiting.sh`,
   `PostToolUse -> clear_permissions_waiting.sh`,
   `PostToolUseFailure -> clear_permissions_waiting.sh`. Also clear the marker
   in `clear_active_marker.sh` (Stop) as a safety net, mirroring claude, so an
   unresolved dialog can't strand the marker across turn end.

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
  `build_codex_hooks_config` now emitting the three new hook entries. Mirror
  claude's three `_waiting_reason` cases (PERMISSIONS / END_OF_TURN / running)
  in the codex plugin test.
- Acceptance/release: a release test that drives a real codex agent to a
  permission dialog and asserts `mngr list` reports
  `codex.waiting_reason == PERMISSIONS`, then resolves it and asserts the field
  clears -- gated on the live-firing verification above. Model it on the
  existing claude `waiting_reason` release coverage and codex's marker tests.

## Effort estimate

Small-to-medium. The pattern is established (claude) and codex's hook/marker
infrastructure already exists; the work is ~2 short scripts, 3 hook entries, a
provisioning line, the enum + field generator + hookimpl, and tests. The only
schedule risk is the live `PermissionRequest` verification, which could reduce
scope to `END_OF_TURN`-only if codex doesn't fire it.
