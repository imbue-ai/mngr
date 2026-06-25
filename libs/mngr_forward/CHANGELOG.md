# Changelog - mngr_forward

A concise, human-friendly summary of changes for the `mngr_forward` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `mngr forward --on-error {abort,continue}` flag (default `abort`). Under `continue`, the `--no-observe` startup snapshot tolerates an unauthenticated/unreachable provider (runs `mngr list --on-error continue` and forwards the agents the healthy providers reported instead of failing to start).

### Fixed

- Fixed: The websocket forward path now emits a `system_interface_backend_failure` envelope when the backend connection fails (unresolved target, SSH-tunnel setup failure, refused host-loopback dial, or a connect-time backend-websocket failure), matching the HTTP/SSE paths. Previously a consumer like minds could go blind to a dead system interface whose only live channel was a websocket — the user was stranded on a frozen workspace because the recovery redirect never fired.

## [v0.1.6] - 2026-06-18

## [v0.1.5] - 2026-06-16

## [v0.1.4] - 2026-06-16

## [v0.1.3] - 2026-06-15

## [v0.1.2] - 2026-06-13

### Fixed

- Fixed: Reverse SSH tunnel teardown no longer leaves the forwarded port allocated on the remote sshd (which had caused the next run's port-forward request to be denied). `SSHTunnelManager.cleanup()` now attempts to cancel each reverse port forward unconditionally, instead of skipping the cancel when the connection looks inactive. Every tunnel SSH connection also sends periodic keepalives so an idle reverse tunnel does not silently die and the health check can repair a dead connection promptly.

## [v0.1.1] - 2026-06-08

### Added

- Added: Auto-discovered as a publishable package by the release tooling (it is a standalone `mngr forward` plugin, usable outside the minds bundle); will be offered for first publication to PyPI on the next release.

## [v0.1.0] - 2026-06-05

### Added

- Added: `mngr_forward` emits `system_interface_backend_failure` envelopes (renamed from `workspace_backend_failure`) on connection errors, mid-SSE EOF, or 5xx responses, so consumers like minds can drive a recovery UI; the plugin's 503 fallback page is now a styled card with a loading spinner.
- Added: `ReverseTunnelInfo.agent_id` field and matching `agent_id` parameter on `setup_reverse_tunnel`; new `remove_reverse_tunnels_for_agent(agent_id)` method that tears down every reverse tunnel tagged with a given agent (careful not to close an SSH client shared with live forward tunnels).
- Added: New `resolver_snapshot` envelope — the plugin emits the full per-agent service map on every mutation (both `update_services` and the destruction paths) so a consumer's mirror does not retain stale entries for destroyed agents. The full map is sent on every change (no per-agent diff) so a late-attaching consumer only needs the most recent envelope to be in sync.
- Added: `mngr forward --observe-via-file` makes the forward server consume discovery by tailing the shared discovery events file in-process instead of spawning its own `mngr observe --discovery-only` subprocess. Per-agent `mngr event` streams are still spawned for discovered agents. The flag is mutually exclusive with `--no-observe` and works with either `--service` or `--forward-port`. In this mode SIGHUP is a no-op (there is no observe child to bounce; the file's writer re-emits snapshots the tailer picks up automatically).

### Changed

- Changed: `SSHTunnelManager` (in `mngr_forward/ssh_tunnel.py`) is now the single SSH tunneling implementation in the monorepo — absorbed the latchkey package's parallel copy. The reverse-tunnel repair loop now uses per-tunnel exponential backoff (1s, 2s, 4s, ..., capped at 5min) instead of a flat 30s cadence; failures clear on successful repair.
- Changed: `mngr forward` no longer crashes when its bind port is already in use — `--port` is now optional and falls back to an OS-assigned port if the default (8421) is taken; the server binds its listen socket up front and hands it to uvicorn so the `listening` envelope always reports the bound port.
- Changed: Renamed the workspace-server envelope contract to system-interface in lockstep with the mngr-side rename (`WorkspaceBackendFailure*` → `SystemInterfaceBackendFailure*`, envelope type `workspace_backend_failure` → `system_interface_backend_failure`); the 503 loader page now reads "System interface starting".
- Changed: Picks up the `FullDiscoverySnapshotEvent` schema bump (`providers`, `error_by_provider_name`); older builds raise `DiscoverySchemaChangedError` against new snapshots.
- Changed: `mngr forward` no longer drops a live agent when its provider's discovery merely errored on a poll — the stream manager retains agents whose provider is in the snapshot's `error_by_provider_name`, keeping their service mapping and per-agent event stream alive (logged at debug). A retained agent is only torn down on an explicit destroy or a later successful snapshot that omits it.
- Changed: Stopped following the per-agent `refresh` event source in `ForwardStreamManager`; default event sources are now just `services` and `requests`. The refresh-via-desktop-client mechanism has been superseded by an `open_tab` WebSocket broadcast from the workspace server.
- Changed: Unreachable backends (SSH-tunnel setup failure and refused host-loopback dial) are now treated as backend failures — the plugin emits a `CONNECT_ERROR` `system_interface_backend_failure` envelope and serves the styled "Loading workspace" loader to HTML callers. The "Loading workspace" loader no longer shows the explanatory "This page will reload automatically…" line.
- Changed: HTTP error handling simplified — the plugin no longer special-cases which status codes matter (previously tagged only 502/503/504 as `FIVEXX_RESPONSE` and 404-on-`GET` as `NOT_FOUND_RESPONSE`). It now forwards every response unchanged and emits a single `ERROR_RESPONSE` reason (carrying the `status_code`) for any non-2xx response, leaving the policy decision to the consumer.
- Changed: Added to the release tooling's publish graph (`scripts/utils.py`); will be offered for first publication to PyPI on the next release. Previously-unpinned internal deps (`imbue-mngr`, `imbue-common`, `concurrency-group`) are now pinned with `==` to their current workspace versions, as a published wheel requires. No runtime change.

### Fixed

- Fixed: "Workspace server starting" loader spinner animation duration now matches the page's 1-second auto-refresh interval so the spinner is at the cycle boundary when the reload fires.

## [v0.2.7] - 2026-05-11

### Added

- Added: New `mngr_forward` plugin (`libs/mngr_forward/`) that serves `<agent-id>.localhost:8421/*` subdomain forwarding, signed login cookies, optional reverse SSH tunnels (`--reverse`), and CEL agent/event filters; SIGHUP bounces just the `mngr observe` child.

### Fixed

- Fixed: `mngr forward` subprocess no longer pegs ~130% CPU after agents disconnect — the bidirectional relay (lifted to `mngr_forward/relay.py`) now terminates when the paramiko channel has received EOF.
