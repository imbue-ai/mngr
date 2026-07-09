# Plan: event-driven agent liveness via `mngr observe` PID-watch

## Refined prompt

Give the forever-claude-template "system interface" a real, event-driven, process-liveness lifecycle state (RUNNING / WAITING / STOPPED / DONE / ...) per agent, so it can drive its liveness dot and gate its "Thinking..." activity indicator — including catching a `claude` process that dies on its own (OOM, crash, normal exit). This spans two repos, landed and merged separately: a change to `mngr observe` (this repo) and a change to the `system_interface` app in the forever-claude-template repo. Do NOT touch any lifecycle-indicator / liveness-dot / activity-indicator UI logic — this is only about *where the lifecycle state comes from*.

**Part 1 — `mngr observe` (this repo).** The full observer already emits real probed lifecycle state (`AgentDetails.state`) on activity events and on its periodic 5-minute snapshot, but a *spontaneous* process death produces no activity event, so it is only caught at the next full snapshot. Close that gap by watching each local agent's main `claude` PID and treating its exit as an activity signal.

* Watch mechanism is **psutil `Process(pid).wait(timeout)`** — a single cross-platform call that psutil implements event-driven via `os.pidfd_open`+`poll` on Linux and `select.kqueue`+`EVFILT_PROC`/`NOTE_EXIT` on macOS/BSD, with automatic graceful fallback to busy-loop polling. No hand-rolled pidfd/kqueue/poll code and no macOS special-case (empirically verified on macOS: non-child death detected in ~5 ms via kqueue, PID-reuse-safe via create-time identity).
* Run **one watcher thread per local agent** (the watched set is local-only and small). On PID exit, enqueue the agent's `host_id` onto the observer's existing `_activity_queue`, so the current re-probe-and-emit path runs unchanged.
* Only **local-provider** agents are watched. Remote agents (over SSH) stay on the existing per-host activity streams + periodic snapshot.
* Surface the `claude` PID onto a new optional `AgentDetails.main_pid`, populated only on the local-provider probe path (the probe already collects `pane_pid` + the full `ps` tree; the descendant PID is currently discarded). The observer watches exactly those agents that carry a `main_pid`.
* Reconcile watchers from probed `AgentDetails` (open on first sighting, replace on PID change, close on destroy / when the agent stops); never leak fds.
* Bump `libs/mngr`'s psutil floor from `>=5.9` to `>=7.2` (lockfile already resolves 7.2.2) to guarantee the event-driven path.

**Part 2 — `system_interface` (forever-claude-template).** Switch the system interface from `mngr observe --discovery-only` to the full observer as its source of lifecycle state, so it gets real, event-driven state including prompt-death detection. Only the *data source* changes.

* The full observer writes its state events to files today; `--discovery-only` is the only stdout mode. Add a **stdout-streaming mode to the full observer** (`mngr observe --stream-events`) that echoes its `agents` stream as JSONL, so the consumer keeps its existing line-based stdout consumption with a new event schema (recommended over having the consumer tail event files: it preserves the consumer's active-spawner model and avoids on-disk-path coupling; the file design is untouched because the echo is additive, exactly as `--discovery-only` already writes its file *and* echoes stdout).
* Dropping `--discovery-only` removes the consumer's prompt agent create/destroy signal, so the observer additionally emits promptly on agent-level discovery deltas (added → re-probe + emit; removed → new `AGENT_REMOVED` event), preserving today's near-instant membership latency.
* There is **no ~5s `mngr list` poll** in the system interface to remove — that premise from the original prompt was incorrect. The real defect is that the discovery-only stream carries no lifecycle state, so the consumer hardcodes `state="RUNNING"`, pinning every agent to RUNNING and never seeing death. The fix is to feed real `AgentDetails.state` into the consumer's per-agent state.

**Out of scope / decided:** do NOT re-add `DiscoveredAgent.state` (imbue-ai/mngr#2331 closed — the full observer already carries real state via `AgentDetails.state`); do NOT change the dot / dot-shape / activity-indicator UI logic, only the source of the lifecycle state feeding it.

## Overview

* **Root cause, restated.** The system interface consumes `mngr observe --discovery-only`, a lightweight *metadata* stream that carries no lifecycle state — so it hardcodes `state="RUNNING"` for every agent (`agent_manager.py:927`) and can never observe a `claude` process dying on its own. The full observer *does* carry real state but only re-probes on activity or every 5 minutes, so a spontaneous death is invisible for minutes.
* **Two coordinated fixes.** (1) Make the full observer death-aware by watching each local agent's `claude` PID and treating its exit as an activity signal; (2) point the system interface at the full observer's real-state stream instead of the state-less discovery stream.
* **Reuse over hand-rolling.** The PID-watch is a thin wrapper over psutil's event-driven `wait()`, which already encapsulates the Linux `pidfd` and macOS `kqueue` paths and PID-reuse safety — so this repo ships no platform-specific watch code. The death signal reuses the observer's existing `_activity_queue` → re-probe → emit path verbatim.
* **Additive, not architectural.** The observer's file-based event bus (history replay, single-instance lock, multi-consumer `mngr event` tailing) is unchanged; `--stream-events` only adds an opt-in stdout echo of the `agents` stream, mirroring how `--discovery-only` already writes a file and echoes stdout.
* **Behavior-preserving for the consumer's seam.** The system interface's contract is unchanged: `AgentStateItem.state` still feeds `derive_activity_state(is_agent_running = state in RUNNING_LIFECYCLE_STATES)`; it is now populated with real state instead of a hardcoded literal. No dot/indicator UI logic changes.
* **Cross-repo coupling.** The two changes land separately, but Part 2 depends on the Part 1 mngr changes (`--stream-events`, `AGENT_REMOVED`, the observe-event parser, `AgentDetails.main_pid`) being present in the mngr vendored into forever-claude-template.

## Expected behavior

* When a local agent's `claude` process dies on its own (OOM kill, crash, normal exit), the full observer detects it within ~seconds (psutil wakes in ~ms → existing 2s activity debounce → re-probe) and emits a state event transitioning the agent to `STOPPED` / `DONE`, instead of waiting up to 5 minutes for the next full snapshot.
* Remote agents behave exactly as today — detected via their per-host activity streams and the periodic snapshot; they are never PID-watched.
* `mngr observe` (full) is unchanged for existing consumers: it still writes the same JSONL event files, still takes the single-instance lock, still emits the same `agents` and `agent_states` streams. PID-watching is always-on for local agents and adds no new files.
* `mngr observe --stream-events` behaves like `mngr observe` but additionally echoes each `agents`-stream event (`AGENT_STATE`, `AGENTS_FULL_STATE`, `AGENT_REMOVED`) as one JSON line to stdout, for a consumer that spawns it and reads its stdout. The `agent_states` change stream is not echoed.
* Agent create/destroy still propagate promptly to consumers of the `agents` stream: a newly discovered agent triggers a re-probe + `AGENT_STATE` emit; a destroyed agent triggers an `AGENT_REMOVED` emit — matching the near-instant membership latency the discovery stream provides today.
* In the system interface, each agent's liveness state is now real and live: an agent whose prompt dies shows as not-running, which (via the unchanged `is_agent_running` gate) drops its "Thinking..." indicator to idle; the dot/indicator UI itself is unchanged.
* The system interface's first paint is still fast: the existing one-shot startup discovery seeds real state immediately, and the observer's stream then takes over — but the stream no longer clobbers that state to `"RUNNING"`.
* On macOS (dev/tests only) the behavior is identical to Linux, because psutil uses `kqueue` there; there is no separate macOS poll path to maintain.

## Changes

### `libs/mngr` (Part 1)

* **`AgentDetails.main_pid`** (`interfaces/data_types.py`): new optional field carrying the agent's main `claude` process PID, populated only on the local-provider probe path; `None` for remote providers and for stopped agents (no live process). Wire-compatible (optional with a `None` default).
* **Surface the PID from the existing probe** (`hosts/common.py`, `agents/base_agent.py`): extend the lifecycle-state computation to also return the descendant PID whose command matches the expected process name (the same `pane_pid` + `ps` tree already collected; today only the descendant *names* are kept). Thread it into the local provider's `AgentDetails` construction. No new tmux/ps calls.
* **PID-watch manager in `AgentObserver`** (`api/observe.py`): a small watcher registry keyed by `agent_id`, each entry holding the watched PID and a per-agent watcher thread (via the existing `ConcurrencyGroup`). Each thread waits on `psutil.Process(pid).wait(timeout=T)` in a stop-checked loop; on exit it enqueues the agent's `host_id` onto `_activity_queue`. Reconcile after each probe (from the `AgentDetails` the observer already receives in `_emit_agent_state` / `_process_snapshot_agents`): open a watcher for any agent that now carries a `main_pid`, replace it when the PID changes (PID-reuse-safe via psutil's create-time identity), and close it when the agent is destroyed or no longer carries a `main_pid`. Watchers are torn down on observer stop; no fds leak.
* **Prompt membership emits** (`api/observe.py`, `_on_discovery_stream_output`): react to the agent-level `AggregatorDelta` (currently only host-level deltas are used) — for each added agent enqueue its `host_id` onto `_activity_queue` (reusing the existing re-probe/emit path); for each removed agent emit a new removal event.
* **New `AGENT_REMOVED` event** (`api/observe.py`): a new `ObserveEventType` member + event model (carrying `agent_id` / `agent_name`) + constructor + emit helper, written to the `agents` stream (`mngr/agents`) alongside `AGENT_STATE` / `AGENTS_FULL_STATE`.
* **`--stream-events` flag** (`cli/observe.py`, `ObserveCliOptions`; emit path in `api/observe.py`): when set, the full observer echoes each appended `agents`-stream event to stdout as compact JSONL (in addition to the file write), mirroring `--discovery-only`'s file-plus-stdout emit. Human logging stays on stderr / is suppressed via `--quiet`. Update the command help metadata.
* **An observe-event line parser** (`api/observe.py`): a public parse function for the `agents` stream (mirroring `parse_discovery_event_line`) so the consumer can decode `AGENT_STATE` / `AGENTS_FULL_STATE` / `AGENT_REMOVED` lines from stdout.
* **psutil floor bump** (`libs/mngr/pyproject.toml`): `psutil>=5.9` → `psutil>=7.2`; refresh `uv.lock`.
* **Docs**: regenerate the CLI docs (`uv run python scripts/make_cli_docs.py`) for the new `--stream-events` flag; update `libs/mngr/docs/commands/secondary/observe.md` narrative if needed.

### `apps/system_interface` (Part 2 — forever-claude-template worktree)

* Check out forever-claude-template as a worktree at `.external_worktrees/forever-claude-template` (gitignored), on branch `gabriel/observe-pid-watch`, and make these changes there, committing inside the worktree.
* **Swap the observe command** (`agent_manager.py`, `_build_observe_command_argv`): `mngr observe --stream-events` instead of `mngr observe --discovery-only`.
* **Consume the `agents` stream** (`agent_manager.py`, `_handle_observe_output_line` and the fold that today calls `_handle_discovery_event`): parse observe-event lines (via the new mngr parser) instead of discovery-event lines, and fold them into `_agents` — `AGENTS_FULL_STATE` rebuilds the set, `AGENT_STATE` upserts one agent, `AGENT_REMOVED` drops one. Populate `AgentStateItem.state` from the real `AgentDetails.state` (deleting the hardcoded `"RUNNING"` at `agent_manager.py:927`), and `name` / `labels` / `work_dir` from `AgentDetails`.
* **Preserve membership side-effects** (`agent_manager.py`): compute added/removed agent-id sets from the folded events and keep firing the existing per-agent lifecycle side-effects (start/stop app + activity watchers, assist auto-open, `broadcast_agents_updated`) that the discovery `AggregatorDelta` used to drive.
* **Messaging match identity** (`agent_manager.py`, `_build_agent_match`): source `host_id` and `provider_name` from `AgentDetails.host.{id,provider_name}` (both present on `AgentDetails`), replacing the `DiscoveredAgent`-sourced values.
* **Retire discovery-specific plumbing** for the observe path (the `DiscoveryStateAggregator` usage and discovery-event handling), replaced by the observe-event fold above.
* **Keep the startup seed** (`agent_manager.py`, `_initial_discover`): retained as a fast on-connect real-state seed; it is no longer clobbered by the stream. (Safe to drop if minimalism is preferred — correctness does not depend on it.)
* **No UI changes**: the dot / dot-shape / `ActivityIndicator` / `derive_activity_state` gate are untouched; only `AgentStateItem.state`'s source changes.

### Tests

* **Part 1 (mngr).** Acceptance/integration test: with a local agent running, kill its `claude` PID and assert an `AGENT_STATE` (and `AGENT_STATE_CHANGE`) to `STOPPED`/`DONE` is emitted within a few seconds; assert a newly created agent yields a prompt `AGENT_STATE` and a destroyed agent yields `AGENT_REMOVED`. Unit tests: `main_pid` is populated on the local probe and `None` for remote; the watcher registry opens/replaces/closes correctly on PID change and destroy without leaking watchers; `--stream-events` echoes the `agents` stream (and not `agent_states`) to stdout as parseable JSONL. (psutil's non-child, event-driven, PID-reuse-safe `wait()` is already empirically verified, so no macOS poll path is added or tested.)
* **Part 2 (system_interface).** Test that feeding an `AGENT_STATE` (STOPPED) line through the observe stdout path flips the `agents_updated` WebSocket payload's `state` and, via the unchanged `is_agent_running` gate, the derived activity state; that `AGENTS_FULL_STATE` rebuilds the agent set; and that `AGENT_REMOVED` drops an agent and fires the removal side-effects.

### Changelog

* `libs/mngr/changelog/gabriel-observe-pid-watch.md`: PID-watch for local agents making spontaneous `claude` death event-driven, the new `AGENT_REMOVED` event, the `--stream-events` stdout mode, `AgentDetails.main_pid`, and the psutil floor bump.
* forever-claude-template: a changelog entry for the `system_interface` per that repo's convention, describing the switch to the full observer's real, event-driven lifecycle state.

## Cross-repo landing note

* Part 1 (mngr) must land/merge first (or at least be vendored into forever-claude-template) before Part 2 can work, since Part 2 depends on `--stream-events`, `AGENT_REMOVED`, the observe-event parser, and `AgentDetails.main_pid`. The user lands and merges the two separately.
