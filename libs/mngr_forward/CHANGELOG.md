# Changelog - mngr_forward

A concise, human-friendly summary of changes for the `mngr_forward` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `mngr_forward` emits `system_interface_backend_failure` envelopes (renamed from `workspace_backend_failure`) on connection errors, mid-SSE EOF, or 5xx responses, so consumers like minds can drive a recovery UI; the plugin's 503 fallback page is now a styled card with a loading spinner.

### Changed

- Changed: `mngr forward` no longer crashes when its bind port is already in use — `--port` is now optional and falls back to an OS-assigned port if the default (8421) is taken; the server binds its listen socket up front and hands it to uvicorn so the `listening` envelope always reports the bound port.
- Changed: Renamed the workspace-server envelope contract to system-interface in lockstep with the mngr-side rename (`WorkspaceBackendFailure*` → `SystemInterfaceBackendFailure*`, envelope type `workspace_backend_failure` → `system_interface_backend_failure`); the 503 loader page now reads "System interface starting".
- Changed: Picks up the `FullDiscoverySnapshotEvent` schema bump (`providers`, `error_by_provider_name`); older builds raise `DiscoverySchemaChangedError` against new snapshots.
- Changed: Adopted the new per-project changelog layout.

### Fixed

- Fixed: "Workspace server starting" loader spinner animation duration now matches the page's 1-second auto-refresh interval so the spinner is at the cycle boundary when the reload fires.
- Fixed: `UNABRIDGED_CHANGELOG.md` intro now references the correct entries directory.

## [v0.2.7] - 2026-05-11

### Added

- Added: New `mngr_forward` plugin (`libs/mngr_forward/`) that serves `<agent-id>.localhost:8421/*` subdomain forwarding, signed login cookies, optional reverse SSH tunnels (`--reverse`), and CEL agent/event filters; SIGHUP bounces just the `mngr observe` child.

### Fixed

- Fixed: `mngr forward` subprocess no longer pegs ~130% CPU after agents disconnect — the bidirectional relay (lifted to `mngr_forward/relay.py`) now terminates when the paramiko channel has received EOF.
