# Investigation: minds "+ New Chat" appears to hang for ~6 min

Captured from a live `engman` workspace (host `host-3f74174b139a451e99a8c1d6b7aa3fdc`,
local-host id inside container `host-78bcf7f3d6a54563a65869adb20bcebb`) where the user
clicked "+ → New Chat", named the agent `driver`, and the dockview tab stayed in its
"Creating..." / "No conversation data" state for ~6 minutes before the agent surfaced.

This document is investigation-only. No code is changed.

## Timeline (UTC)

- `18:10:33.578` `mngr create driver --id ... --transfer none --template chat --no-connect` starts (PID 7465 in the engman container).
- `18:10:33.779` `/mngr/agents/agent-36f.../data.json` written.
- `18:10:34.376` `Starting agent driver ...`.
- `18:10:34.391` `Starting agent driver in tmux session minds-driver`.
- `18:10:34.547` `Calling on_agent_created hooks` — `mngr create` returns successfully.
- `18:10:34.549` `AGENT_DISCOVERED` for `agent-36f...` written to `/mngr/events/mngr/discovery/events.jsonl`.
- `18:10:39.112` first `DISCOVERY_FULL` snapshot containing both `engman` and `driver`.
- ~every 10s thereafter: another `DISCOVERY_FULL` (2254 bytes, 1 line) — the workspace-server's `mngr observe --discovery-only` subprocess (PID 1300) logs `Discovery tail: consumed 2254 new bytes, 1 lines from events file` for each, uninterrupted, through the entire window.
- `~18:10:33`–`~18:16:24` uvicorn access log on the system-interface service: a flood of `GET /api/agents/agent-36f.../screen?scrollback=true` returning `404 Not Found`, plus `/events 404`, `/stream 404`.
- `~18:16:24` first `/screen?scrollback=true` returns `200 OK`.
- `18:16:30.359` `session_watcher._discover_sessions` starts ticking for the new agent (frontend has reloaded; `/events`, `/stream` now return 200).
- `18:16:35.598` user sends first message; `send_message_to_agents` calls `discover_hosts_and_agents` directly.

## Where the agent actually lives

`mngr create` from inside the engman container lands the new agent on the in-container
`local` provider (`host-78bcf7f3d6a54563a65869adb20bcebb`). From outside the container,
the same agent shows up under the `docker` host `engman-host`. Both are correct views of
the same tmux session (`minds-driver`).

## Why the UI hung

`apps/system_interface/imbue/minds_workspace_server/server.py:_get_screen_capture` (and
`_get_events`, `_stream_events`) call `_find_agent`, which only consults
`agent_manager._agents`. While `_agents` does not contain the new agent, every chat-tab
API for that agent returns 404.

`ChatPanel.fetchScreenCapture` re-fires on every `m.redraw` while
`screenContent === null && !screenLoading`, so a 404 turns into a hot polling loop. That
is the source of the ~hundreds of `screen?scrollback=true 404` lines in the access log.

## Why `_agents` did not update

`agent_manager._handle_full_snapshot` is what populates `_agents` from observe events.
The `mngr observe --discovery-only` pipeline upstream of that handler was healthy
throughout the gap:

- `/mngr/events/mngr/discovery/events.jsonl` was being appended to every ~10s.
- The observe subprocess's own debug log (`/mngr/events/logs/mngr/events.jsonl`, written
  by loguru from PID 1300) shows `Discovery tail: consumed 2254 new bytes, 1 lines from
  events file` every ~10s without a gap. That log line lives in
  `discovery_events.py:_discovery_stream_tail_events_file`, immediately before the loop
  that calls `_discovery_stream_emit_line` for each new line. `_discovery_stream_emit_line`
  ends with `sys.stdout.write(...)` + `sys.stdout.flush()`, so for that debug line to keep
  firing on schedule, the OS pipe to the workspace-server must have been getting drained.

So events were reaching the workspace-server's stdout reader. The bug is downstream of
that — between the reader and `_agents`. Most likely culprit:

- `agent_manager._handle_observe_output_line` calls `parse_discovery_event_line(stripped)`
  and re-raises (`json.JSONDecodeError` from a malformed/non-JSON line, or
  `DiscoverySchemaChangedError` from an unexpected event-type, or an explicit
  `BaseMngrError` if `parse_discovery_event_line` returns `None`). It has no `try/except`.
  An exception there propagates out of `PartialOutputContainer.write` and out of
  `gather_output`, killing the read thread for that subprocess wrapper. `_watch_observe_process`
  only fires on subprocess exit, not on a dead reader thread, so the bug would be
  invisible.

`_agents` likely got repaired around `~18:16:24` when something else (most plausibly the
`send_message_to_agents` path that runs `discover_hosts_and_agents` in-process at
`18:16:35`, or an analogous earlier trigger) refreshed state directly, bypassing the
broken observe pipeline.

## Side findings (not the cause but worth fixing later)

- `agent_manager._build_observe_command` passes `--events-dir
  $MNGR_AGENT_STATE_DIR/workspace_server/observe`, but
  `libs/mngr/imbue/mngr/cli/observe.py:64-69` only honours `events_base_dir` in the
  *non-discovery* branch. With `--discovery-only`, `run_discovery_stream` uses
  `get_discovery_events_path(mngr_ctx.config)` (i.e. `$MNGR_HOST_DIR/events/mngr/discovery/events.jsonl`).
  That is why each agent's `workspace_server/observe/` directory is empty.
- `_handle_observe_output_line` is the most fragile point in the pipeline because it
  has no exception guard and re-raises on any unparseable line.
- The chat agent inherits `type = "claude"`, which has `sync_claude_credentials = true`
  (only `agent_types.main` overrides to `false`). On a local host this resolves to
  `_sync_user_resources` (symlink), which is fast — not a contributor here.

## Recommended next steps (out of scope for this PR)

1. Wrap `_handle_observe_output_line` in a `try/except (json.JSONDecodeError,
   DiscoverySchemaChangedError, BaseMngrError, ValidationError)` that logs at warning
   level and continues, so a single bad line cannot decapitate observe consumption.
2. Add a watchdog on the *reader* thread itself (not just the subprocess), or restart
   the observe subprocess when the reader dies.
3. Fix `mngr observe --discovery-only` to honour `--events-dir`, or drop the flag from
   `agent_manager._build_observe_command` so callers do not assume a per-agent file is
   being written.
4. Make `ChatPanel.fetchScreenCapture` back off (or only fire once per agent until the
   agent is known) so a transient 404 does not produce a polling storm.
