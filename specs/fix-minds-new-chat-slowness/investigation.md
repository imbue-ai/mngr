# Investigation: minds "+ New Chat" appears to hang for ~6 min

Captured from a live `engman` workspace (host `host-3f74174b139a451e99a8c1d6b7aa3fdc`,
local-host id inside container `host-78bcf7f3d6a54563a65869adb20bcebb`) where the user
clicked "+ → New Chat", named the agent `driver`, and the dockview tab stayed in its
"Creating..." / "No conversation data" state for ~6 minutes before the agent surfaced.
A subsequent attempt by the user to create-and-destroy a new chat on the **same**
workspace-server process (PID 869, no restart between the two tests) worked fine,
which constrains the diagnosis: this is a transient/first-use problem, not a permanent
broken pipeline.

This document is investigation-only. No code is changed.

## Timeline (UTC)

- `18:00:46` engman primary agent created.
- `18:00:55` `runtime-backup` service starts inside the container (60s interval).
- `18:01:04.534` minds-workspace-server starts; `_initial_discover` runs in-process and
  populates `agent_manager._agents` with `engman` only.
- `18:01:04.699` first `DISCOVERY_FULL` snapshot written to
  `/mngr/events/mngr/discovery/events.jsonl` by that in-process `list_agents`.
- `18:01:09.788` `mngr observe --discovery-only` subprocess (PID 1300) starts, spawned
  by `agent_manager._start_observe`.
- `18:10:33.578` `mngr create driver --id ... --transfer none --template chat
  --no-connect` starts (PID 7465 inside the engman container, parent is PID 869 the
  workspace-server).
- `18:10:33.779` `/mngr/agents/agent-36f.../data.json` written.
- `18:10:34.376` `Starting agent driver ...`.
- `18:10:34.391` `Starting agent driver in tmux session minds-driver` — tmux session
  exists from this point.
- `18:10:34.547` `Calling on_agent_created hooks`. Three immediately-following lines
  (`Loading all agents from host...`, `Listing agent dir for host...`, `Listing agent
  files from dir for host...`) fire at the same millisecond — the `host.discover_agents()`
  scan inside `emit_discovery_events_for_host`.
- `18:10:34.549` `AGENT_DISCOVERED` for `agent-36f...` written to
  `/mngr/events/mngr/discovery/events.jsonl` by `mngr create` itself.
- **No further log lines for PID 7465 are recorded in `/mngr/events/logs/mngr/events.jsonl`.**
  `log_span`'s `[done in N sec]` / `[failed after N sec]` exit messages are at TRACE
  level (see `libs/imbue_common/.../logging.py`), and TRACE is not retained in the
  events file, so the absence is not by itself proof that mngr create hung — it may
  have exited cleanly without further DEBUG-or-above logging.
- `18:10:39.112` first `DISCOVERY_FULL` containing both `engman` and `driver`. Every
  subsequent ~10s snapshot also contains both.
- `~18:10:33–~18:16:24` uvicorn access log (system_interface): a flood of `GET
  /api/agents/agent-36f.../screen?scrollback=true` returning `404 Not Found`, plus
  `/events 404`, `/stream 404`. Since `_get_screen_capture` calls `_find_agent` which
  only consults `agent_manager._agents`, this means `_agents` did not contain the new
  chat agent during this entire window.
- `~18:16:24` first `/screen?scrollback=true` returns `200 OK`.
- `18:16:30.359` `session_watcher._discover_sessions` starts ticking for the new
  agent. Frontend has reloaded; `/events` and `/stream` now return 200.
- `18:16:35.598` user sends first message; `send_message_to_agents` calls
  `discover_hosts_and_agents` directly (not via the observe pipeline).

## Where the agent actually lives

`mngr create` from inside the engman container lands the new agent on the in-container
`local` provider (`host-78bcf7f3d6a54563a65869adb20bcebb`). From outside the container,
the same agent shows up under the `docker` host `engman-host`. Both are correct views
of the same tmux session (`minds-driver`).

## Why the UI hung

`apps/system_interface/imbue/minds_workspace_server/server.py:_get_screen_capture` (and
`_get_events`, `_stream_events`) call `_find_agent`, which only consults
`agent_manager._agents`. While `_agents` does not contain the new agent, every chat-tab
API for that agent returns 404. `ChatPanel.fetchScreenCapture` re-fires on every
`m.redraw` while `screenContent === null && !screenLoading`, so a 404 turns into a hot
polling loop — the source of the ~hundreds of `screen?scrollback=true 404` lines in the
access log.

So the question reduces to: **why did `agent_manager._agents` not include the new chat
agent until ~6 minutes after `mngr create` had already started the agent?**

## Two paths feed `_agents`

1. **Direct from `_run_creation`.** `agent_manager._run_creation` runs `mngr create`
   via `run_local_command_modern_version` (blocking), then under `self._lock` does
   `self._agents[agent_id] = AgentStateItem(...)` — but **only when
   `result.returncode == 0`**. After that it broadcasts `proto_agent_completed`.
2. **Indirect from `mngr observe`.** Every ~10s, the observe subprocess writes a fresh
   `DISCOVERY_FULL` to the discovery events file; its tail thread reads new lines and
   pipes them to its stdout; the workspace-server's pipe reader hands each line to
   `_handle_observe_output_line`, which dispatches `FullDiscoverySnapshotEvent` to
   `_handle_full_snapshot` (replaces `self._agents`).

For `_agents` to be missing the new agent for ~6 min, **both paths must have failed to
update it during that window**.

### Why path #2 likely was reaching the workspace-server

Strong evidence the observe pipeline was healthy upstream of `_handle_full_snapshot`:

- `/mngr/events/mngr/discovery/events.jsonl` was being appended every ~10s (we can see
  the timestamps).
- Inside the observe subprocess (PID 1300), the loguru `Discovery tail: consumed N new
  bytes, M lines from events file` debug line in
  `discovery_events.py:_discovery_stream_tail_events_file` fires every ~10s without a
  gap from `18:10:35` through `18:16:27`. That log line lives **immediately before**
  the loop that calls `_discovery_stream_emit_line` for each new line, and emit ends
  with `sys.stdout.write(...)` + `sys.stdout.flush()`. For the next iteration's
  `Discovery tail` log to appear on schedule, every prior `flush()` must have returned
  — which means the OS pipe to the workspace-server **was getting drained**.

So either `_handle_observe_output_line` was being called and silently doing nothing
useful for those events, OR — much more likely — the workspace-server had _already_
populated `_agents` with the new chat agent at `~18:10:39`, but something else was
making the `/screen` lookup fail.

…but `_find_agent` only checks `_agents.get(agent_id)`, full stop. There is no second
condition. So `_agents` really did not have the agent.

### Why path #1 likely was the one blocked

The user-visible symptom that the chat tab showed the build log ("Creating agent...")
during the wait is more compatible with path #1 being stuck than with `_agents` being
silently mis-updated:

- The build log only renders while `isProtoAgent(agentId)` is true, i.e. while the
  frontend has not yet seen `proto_agent_completed`. That broadcast only fires from
  the bottom of `_run_creation`, after `run_local_command_modern_version` returns.
- So the user seeing "Creating agent..." for ~6 min implies `mngr create` (PID 7465)
  did not exit for ~6 min, even though the agent process was up and running by
  `18:10:34`.
- Once the user reloaded and the WS reconnected, the frontend re-receives `agents_updated`
  with the **current** `_agents` snapshot (`server.py:_run_ws_broadcast_loop` sends
  `agents_updated` immediately on accept). If `mngr create` had already returned and
  `_run_creation` had populated `_agents`, the new agent would have appeared on the
  reload. The user reports it did **not** show up after the reload — consistent with
  `_run_creation` still being blocked on the subprocess.

The events file has nothing further from PID 7465 after `18:10:34.547`, but as noted
that is consistent both with "exited cleanly" (no DEBUG output expected after `Listing
agent files from dir for host`) and with "hung" (no log produced because no further
code ran). What disambiguates is the user's report and the WS reload behavior.

## Best guess at the root cause

**The `mngr create` subprocess hung between `emit_discovery_events_for_host`
returning and click-cmd exit, for ~6 minutes, on the first chat-create after the
workspace had just been bootstrapped.** The subsequent same-session test worked.

Plausible candidates inside that window (none individually proven):

- A plugin's `on_agent_created` hook doing first-time work that happened to contend
  with concurrent activity on the workspace (e.g. `runtime-backup` running its 60s
  git cycle, or the engman primary agent's claude finishing its initial session
  setup). After the first run, the cache/state is warm.
- The `mngr_modal` plugin's `on_agent_created` (`libs/mngr_modal/.../backend.py:588`)
  unconditionally raises `MngrError` when the host is not a `Host` (modal). This
  hook fires for **every** create, including local ones, even when modal is disabled
  via `[providers.modal] is_enabled = false`. If pluggy is configured to swallow
  hook exceptions (which it commonly is for non-firstresult hooks), the raise is
  silently logged and execution continues — but the path through pluggy's exception
  bookkeeping under load could explain a stall on the first call. (This deserves
  direct verification: I did not catch the hook firing live.)
- Concurrent FS / git operations: `runtime-backup` runs `git commit` inside
  `/code/runtime/.git` every 60s; the chat agent's `mngr create` does its own
  `Calling provision for agent driver` work that may touch overlapping paths. A
  flock or git index lock contention could stall `mngr create` until the backup
  cycle releases.

Because the bug is transient and self-resolved, definitive root-cause requires
reproducing the cold-start condition (fresh workspace, first chat creation within
the first ~10 min after engman startup).

## Side findings (cosmetic / pre-existing)

- `agent_manager._build_observe_command` passes `--events-dir
  $MNGR_AGENT_STATE_DIR/workspace_server/observe`, but
  `libs/mngr/imbue/mngr/cli/observe.py:64-69` only honours `events_base_dir` in the
  *non-discovery* branch. With `--discovery-only`, `run_discovery_stream` uses
  `get_discovery_events_path(mngr_ctx.config)` (i.e.
  `$MNGR_HOST_DIR/events/mngr/discovery/events.jsonl`). That is why each agent's
  `workspace_server/observe/` directory is empty.
- `_handle_observe_output_line` re-raises `json.JSONDecodeError` /
  `DiscoverySchemaChangedError` / `BaseMngrError` without a `try/except`. This is
  not the cause here (the reader thread was clearly alive), but it is fragile and
  worth hardening.
- `ChatPanel.fetchScreenCapture` re-fires on every redraw while `screenContent ===
  null && !screenLoading`, so any 404 produces a polling storm. Worth backing off.

## Recommended next steps to actually pin this down

1. Reproduce the cold-start condition: spin up a fresh minds workspace and trigger
   the first new-chat within the first few minutes. If the hang reproduces, it is
   first-call-only.
2. Add a structured "mngr create returned (rc=N, elapsed=T)" log line at the end of
   `_run_creation`. The current absence makes it impossible to tell from logs
   whether the subprocess hung or returned.
3. Wrap `_handle_observe_output_line` in a `try/except (json.JSONDecodeError,
   DiscoverySchemaChangedError, BaseMngrError, ValidationError)` that logs at
   warning level and continues. Defensive only.
4. Fix `mngr observe --discovery-only` to honour `--events-dir`, or drop the flag
   from `agent_manager._build_observe_command`.
5. Make `ChatPanel.fetchScreenCapture` back off after a 404 so a stuck agent does
   not produce a polling storm.
