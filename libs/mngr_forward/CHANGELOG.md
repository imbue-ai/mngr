# Changelog - mngr_forward

A concise, human-friendly summary of changes for the `mngr_forward` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: Plugin now emits `system_interface_backend_failure` envelopes on connection errors / mid-SSE EOF / 5xx responses from the workspace backend, so consumers (minds) can drive a per-agent health state machine.

### Changed

- Changed: Renamed the workspace-server envelope contract to system-interface in lockstep with the mngr-side rename: `WorkspaceBackendFailure*` → `SystemInterfaceBackendFailure*`, envelope type `workspace_backend_failure` → `system_interface_backend_failure`; plugin 503 page now reads "System interface starting".
- Changed: 503 fallback page is a styled card with a loading spinner (replacing the blank "Backend not yet available. Retrying..." page); spinner animation duration matches the 1-second auto-refresh interval.
- Changed: Project now participates in the per-project changelog layout (per-project `changelog/`, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md`).

## [v0.2.7] - 2026-05-11

### Added

- Added: New `mngr_forward` plugin (`libs/mngr_forward/`) that serves `<agent-id>.localhost:8421/*` subdomain forwarding, signed login cookies, optional reverse SSH tunnels (`--reverse`), and CEL agent/event filters; SIGHUP bounces just the `mngr observe` child.

### Fixed

- Fixed: `mngr forward` subprocess no longer pegs ~130% CPU after agents disconnect — the bidirectional relay (lifted to `mngr_forward/relay.py`) now terminates when the paramiko channel has received EOF.
