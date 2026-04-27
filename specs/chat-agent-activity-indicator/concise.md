# Chat Agent Activity Indicator

## Refined prompt

> add indication on the chat page of when an agent is working/thinking/outputting versus stopped, so it's clear what state the agent is in
>
> * Surface four agent states: `tool-running`, `thinking`, `waiting-on-permission`, `idle`
> * Render an animated indicator (pulsing dot or spinner) plus a label, just above the message input on each chat panel — no tab indicator, no top banner
> * Labels: `Running tool…`, `Thinking…`, `Waiting for permission`. The `idle` state hides the indicator entirely (slot collapses)
> * Hide the indicator for proto-agents, agents with no Claude session, and destroyed agents
> * Hide the indicator on subagent panels — derivation cost not worth the visual
> * Treat non-Claude agent types as out of scope; the indicator is Claude-specific
> * State derivation rules (in priority order): `permissions_waiting` marker present → `waiting-on-permission`; else `active` marker absent → `idle`; else last assistant_message in session has at least one `tool_use` whose `tool_call_id` has no matching `tool_result` yet → `tool-running`; else → `thinking`
> * No debounce — accept brief flicker between states (e.g. between consecutive tool calls)
> * Accept that a hard agent crash leaves the `active` marker stale (indicator stuck on `Thinking…`); no freshness heuristic
> * Plumbing for state changes is an open question — choices: per-agent watchdog on `active` + `permissions_waiting` marker files; subscribing to the existing `events/mngr/activity/events.jsonl` stream; or dropping `--discovery-only` from the workspace_server's `mngr observe` to consume `AgentStateChangeEvent`s
> * Workspace_server today hard-codes `state="RUNNING"` when broadcasting agents; needs to start carrying real per-agent activity state, or a parallel activity channel needs to be added
> * Tests: unit-test the state derivation function and programmatically verify the watcher → broadcast pipeline (e.g. that a marker file change reaches a connected WS client). Manual testing handles visuals.

## Overview

- Today the chat panel gives no indication of whether an agent is mid-turn, blocked on a permission dialog, or idle. The desired UX is a visible "this agent is working" affordance per chat, with `idle` collapsed away.
- The hooks system already records the necessary per-agent lifecycle data via marker files in `$MNGR_AGENT_STATE_DIR` (`active`, `permissions_waiting`) and emits activity events to `events/mngr/activity/events.jsonl`. The workspace_server is not currently consuming any of it — it hard-codes `state="RUNNING"` for every agent it broadcasts.
- Decision: surface four states (`thinking`, `tool-running`, `waiting-on-permission`, `idle`) by combining the marker files with the session JSONL events the workspace_server already tails. `tool-running` is the only state that requires session-event derivation; everything else is a marker-file read.
- Decision: render the indicator as an animated dot/spinner plus a label, immediately above the message input on each chat panel. No tab-bar indicator, no top banner, no subagent indicator. `idle` collapses the indicator entirely.
- Decision: scope to Claude agents only. Proto-agents, no-conversation chats, destroyed agents, and non-Claude agent types do not get an indicator.

## Expected Behavior

- A user opens a chat panel for a Claude agent that is mid-turn. The indicator is rendered above the message input on first paint and reflects the current state (`Thinking…`, `Running tool…`, or `Waiting for permission`).
- The user sends a message. Within ≤ 1s of the `UserPromptSubmit` hook firing, the indicator transitions from collapsed to `Thinking…`.
- The agent emits an assistant message containing tool calls. The indicator transitions from `Thinking…` to `Running tool…` as soon as the new event lands in the session JSONL stream.
- Tool calls complete and are matched by `tool_result` events. The indicator returns to `Thinking…`.
- Claude pops a permission dialog (`PermissionRequest` hook fires). The indicator transitions to `Waiting for permission`.
- Permission resolves (`PostToolUse` or `PostToolUseFailure`). The indicator returns to `Thinking…` (or `Running tool…` if a tool is now running).
- Claude finishes its turn (`Stop` hook fires, `active` marker removed). The indicator collapses to nothing — the slot above the message input is empty.
- The user opens a chat panel for an agent with no Claude session yet (proto-agent in build-log mode, or a `No conversation data` view). No indicator is shown.
- The user opens a subagent view. No indicator is shown.
- Multiple chat panels can be open simultaneously, each rendering its own indicator independently.
- Brief flicker between `Thinking…` and `Running tool…` is acceptable when consecutive tool calls run with little gap; no debouncing is applied.
- If an agent crashes hard without firing the `Stop` hook, the indicator is stuck on `Thinking…` (or whichever state the agent was in when it died) until the chat tab is closed. This is accepted; the panel itself will eventually disappear when the agent is reaped.
- Latency target: state transitions are visible within ≤ 1s end-to-end on a local agent, dominated by filesystem-watch latency.

## Changes

### Workspace server (Python, `apps/minds_workspace_server/`)

- Add a per-agent activity-state tracker that watches each agent's `active` and `permissions_waiting` marker files under `$MNGR_HOST_DIR/agents/<id>/`. Plumbed in via the existing `AgentManager` lifecycle (start/stop alongside the application watchers).
- Combine marker-file state with the existing `AgentSessionWatcher` events to compute one of `tool-running`, `thinking`, `waiting-on-permission`, `idle` per agent. The `tool-running` derivation walks the latest events to find an unmatched `tool_use` `tool_call_id`.
- Cache the current activity state per agent so the `_ws_endpoint` initial snapshot can include it without waiting for the next event.
- Extend the `AgentStateItem` model and the `agents_updated` WebSocket payload with a new `activity_state` field. Re-broadcast `agents_updated` whenever any agent's activity state changes. (Open question: whether to use a separate event type for high-frequency activity changes — see Open Questions.)
- Skip the new tracker entirely for proto-agents, agents with no Claude session, and non-Claude agent types — those agents broadcast with `activity_state = null`.
- Leave the existing top-level lifecycle `state` field ("RUNNING") unchanged; the new `activity_state` is additive.

### Frontend (TypeScript, `apps/minds_workspace_server/frontend/src/`)

- Extend the `AgentState` interface in `models/AgentManager.ts` with an optional `activity_state` field.
- Add a small presentational component (e.g. an `AgentActivityIndicator`) that renders an animated dot/spinner plus a label for the three visible states and renders nothing for `idle` / `null`.
- Mount the indicator inside `views/ChatPanel.ts`, in the footer section above `MessageInput`. Suppress it when the chat is in proto-agent mode or in the `No conversation data` branch (those branches already short-circuit the message input). Do not mount it inside `views/SubagentView.ts`.
- Add styling for the dot/spinner animation in `style.css` (or co-located CSS) and the labels copy. The exact visual treatment (color per state vs. single accent) is left to the implementer; flag as an open question.

### Hooks / mngr (no changes expected)

- The `UserPromptSubmit`, `PermissionRequest`, `PostToolUse`, `PostToolUseFailure`, `Notification`, and `Stop` hooks already maintain the marker files and write activity events. No changes here.
- The `mngr/activity` event stream already exists; whether the workspace_server consumes it directly or just re-watches the marker files is captured under Open Questions.

### Tests

- Unit-test the activity-state derivation function: given combinations of `active`/`permissions_waiting` marker presence and a list of session events, assert the returned state. Cover all four states and the boundary cases (unmatched tool_use, matched tool_use, multiple tool_calls in a single message).
- Integration-test the marker watcher pipeline: create the marker files in a temporary state dir, assert that the watcher fires and the broadcaster emits an `agents_updated` (or equivalent) message containing the updated `activity_state`. Use the existing `WebSocketBroadcaster` test seam.
- Frontend rendering and end-to-end UI tests are out of scope; manual testing covers them.

### Open questions

- WebSocket transport shape: extend `agents_updated` with an `activity_state` field (simpler, but re-broadcasts the full agent list on every state flip — potentially noisy with many agents) versus introducing a dedicated `agent_activity_changed` event carrying just `{agent_id, state}`. Default to extending `agents_updated`; revisit if churn becomes a problem.
- Source of truth for marker-state changes: per-agent watchdog on the marker files, subscribing to `events/mngr/activity/events.jsonl`, or dropping `--discovery-only` from the workspace_server's `mngr observe` to consume `AgentStateChangeEvent`s. Default to the per-agent watchdog (lowest blast radius, no extra subprocess, fits the existing `AgentManager` patterns).
- Visual treatment of the indicator: single accent color with state-only labels, vs. per-state colors (e.g. blue thinking / amber tool-running / red waiting-on-permission). Default left to the implementer.
- Subagent activity inheritance: should a subagent panel later show the parent agent's state, or a derived per-subagent-session state? Currently scoped out; revisit if the visual is missed.
- Stale-marker handling: a hard crash leaves `active` set; should we add a freshness heuristic (e.g. clear the indicator after N seconds with no session activity) in a follow-up?
