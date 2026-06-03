# Changelog - mngr_forward

A concise, human-friendly summary of changes for the `mngr_forward` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `mngr_forward` emits `system_interface_backend_failure` envelopes (renamed from `workspace_backend_failure`) on connection errors, mid-SSE EOF, or 5xx responses, so consumers like minds can drive a recovery UI; the plugin's 503 fallback page is now a styled card with a loading spinner.
- Added: `ReverseTunnelInfo.agent_id` field and matching `agent_id` parameter on `setup_reverse_tunnel`; new `remove_reverse_tunnels_for_agent(agent_id)` method that tears down every reverse tunnel tagged with a given agent (careful not to close an SSH client shared with live forward tunnels).
- Added: New `resolver_snapshot` envelope — the plugin emits the full per-agent service map on every mutation of that map (`update_services` set/replace and the destruction paths `remove_known_agent` / `update_known_agents` when they drop an agent that had services), so a consumer's mirror does not retain stale entries for destroyed agents. The full map is sent on every change (no per-agent diff) so a late-attaching consumer only needs the most recent envelope to be in sync.

### Changed

- Changed: `SSHTunnelManager` (in `mngr_forward/ssh_tunnel.py`) is now the single SSH tunneling implementation in the monorepo — absorbed the latchkey package's parallel copy. The reverse-tunnel repair loop now uses per-tunnel exponential backoff (1s, 2s, 4s, ..., capped at 5min) instead of a flat 30s cadence; failures clear on successful repair.
- Changed: `mngr forward` no longer crashes when its bind port is already in use — `--port` is now optional and falls back to an OS-assigned port if the default (8421) is taken; the server binds its listen socket up front and hands it to uvicorn so the `listening` envelope always reports the bound port.
- Changed: Renamed the workspace-server envelope contract to system-interface in lockstep with the mngr-side rename (`WorkspaceBackendFailure*` → `SystemInterfaceBackendFailure*`, envelope type `workspace_backend_failure` → `system_interface_backend_failure`); the 503 loader page now reads "System interface starting".
- Changed: Picks up the `FullDiscoverySnapshotEvent` schema bump (`providers`, `error_by_provider_name`); older builds raise `DiscoverySchemaChangedError` against new snapshots.
- Changed: HTTP error handling simplified — the plugin no longer special-cases which status codes matter (it previously tagged only 502/503/504 as `FIVEXX_RESPONSE` and 404-on-`GET` as `NOT_FOUND_RESPONSE`). It now forwards every response unchanged and emits a single `ERROR_RESPONSE` reason carrying the `status_code` for any non-2xx response, leaving the policy decision (which statuses warrant action, and what action) entirely to the consumer.
- Changed: SSH-tunnel setup failure and refused host-loopback dial (no SSH tunnel available) are now both treated as backend failures — the plugin emits a `CONNECT_ERROR` `system_interface_backend_failure` envelope and serves the styled "Loading workspace" loader to HTML callers, the same as other unreachable-backend cases.
- Changed: "Loading workspace" loader no longer shows the explanatory "This page will reload automatically..." line — it just shows the heading, vertically centered against the spinner.

### Fixed

- Fixed: "Workspace server starting" loader spinner animation duration now matches the page's 1-second auto-refresh interval so the spinner is at the cycle boundary when the reload fires.

## [v0.2.7] - 2026-05-11

### Added

- Added: New `mngr_forward` plugin (`libs/mngr_forward/`) that serves `<agent-id>.localhost:8421/*` subdomain forwarding, signed login cookies, optional reverse SSH tunnels (`--reverse`), and CEL agent/event filters; SIGHUP bounces just the `mngr observe` child.

### Fixed

- Fixed: `mngr forward` subprocess no longer pegs ~130% CPU after agents disconnect — the bidirectional relay (lifted to `mngr_forward/relay.py`) now terminates when the paramiko channel has received EOF.
