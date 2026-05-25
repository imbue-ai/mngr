# Changelog - mngr_forward

A concise, human-friendly summary of changes for the `mngr_forward` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: `mngr forward`'s `--port` is now optional. When omitted, the server tries its default (8421) and falls back to an OS-assigned port; when supplied explicitly, it still binds exactly that port and fails fast. The `listening` envelope always reports the actually-bound port.
- Changed: Renamed workspace-server envelope contract to system-interface: `WorkspaceBackendFailure*` → `SystemInterfaceBackendFailure*`; envelope type `workspace_backend_failure` → `system_interface_backend_failure`; 503 loader page reads "System interface starting".
- Changed: Picks up the new `providers` / `error_by_provider_name` fields on `FullDiscoverySnapshotEvent` transparently; older builds raise `DiscoverySchemaChangedError` against new snapshots.
- Changed: 503 fallback page is now a styled card with a loading spinner (was a blank "Backend not yet available. Retrying..."); the spinner's animation duration matches the page's 1-second auto-refresh interval.

### Added

- Added: Plugin emits `workspace_backend_failure` (now `system_interface_backend_failure`) envelopes on connection errors, mid-SSE EOF, or 5xx responses from the workspace backend; consumers can track these as a per-agent health state machine.

## [v0.2.7] - 2026-05-11

### Added

- Added: New `mngr_forward` plugin (`libs/mngr_forward/`) that serves `<agent-id>.localhost:8421/*` subdomain forwarding, signed login cookies, optional reverse SSH tunnels (`--reverse`), and CEL agent/event filters; SIGHUP bounces just the `mngr observe` child.

### Fixed

- Fixed: `mngr forward` subprocess no longer pegs ~130% CPU after agents disconnect — the bidirectional relay (lifted to `mngr_forward/relay.py`) now terminates when the paramiko channel has received EOF.
