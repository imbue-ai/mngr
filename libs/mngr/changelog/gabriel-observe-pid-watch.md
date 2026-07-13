The full `mngr observe` observer now detects a local agent's main process dying on its own (OOM, crash, or normal exit) within seconds instead of waiting up to 5 minutes for the next full snapshot.

- It watches each local agent's main process PID (via psutil's event-driven `wait`) and treats the process exit as an activity signal, re-probing and emitting the new lifecycle state (STOPPED/DONE) promptly. Remote agents are unchanged (per-host activity streams plus the periodic snapshot).

- New `mngr observe --stream-events` mode: the full observer echoes each agents-stream event (`AGENT_STATE`, `AGENTS_FULL_STATE`, and the new `AGENT_REMOVED`) to stdout as compact JSONL, in addition to writing the usual event files, so a parent process can consume live agent lifecycle state by reading the observer's stdout. Cannot be combined with `--discovery-only`.

- New `AGENT_REMOVED` event on the agents stream, emitted when a previously-known agent is destroyed, so consumers learn of removals promptly rather than at the next full snapshot.

- `AgentDetails` gains an optional `main_pid` field, populated for running agents on any provider (the PID of their main process, e.g. `claude`, in the host's PID namespace); it is a filterable/sortable listing field. The observer only PID-watches local agents, gated on the new `HostDetails.is_local` field (also filterable/sortable, e.g. `host.is_local`).

- The `libs/mngr` psutil floor is raised from `>=5.9` to `>=7.2` to guarantee the event-driven `wait()` path (os.pidfd_open on Linux, kqueue on macOS).
