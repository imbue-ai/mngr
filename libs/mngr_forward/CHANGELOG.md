# Changelog - mngr_forward

A concise, human-friendly summary of changes for the `mngr_forward` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `mngr_forward` plugin emits `workspace_backend_failure` envelopes (mid-SSE EOF, connection errors, 5xx) so consumers (minds) can track per-agent health and trigger recovery UI.

### Changed

- Changed: `mngr forward` no longer crashes when its bind port is already in use — `--port` is now optional and falls back to an OS-assigned port; the listen socket is bound up front so the `listening` envelope always reports the actual bound port.
- Changed: Renamed envelope contract from workspace-server to system-interface (`WorkspaceBackendFailure*` → `SystemInterfaceBackendFailure*`, `workspace_backend_failure` → `system_interface_backend_failure`); 503 loader page now reads "System interface starting".
- Changed: 503 fallback page is now a styled card with a loading spinner (matching the page's 1-second auto-refresh); previously was a blank "Backend not yet available. Retrying..." page.
- Changed: Picks up new `providers` and `error_by_provider_name` fields on `FullDiscoverySnapshotEvent`; older `mngr_forward` builds running against new snapshots will raise `DiscoverySchemaChangedError` and must be rebuilt.

## [v0.2.7] - 2026-05-11

### Added

- Added: New `mngr_forward` plugin (`libs/mngr_forward/`) that serves `<agent-id>.localhost:8421/*` subdomain forwarding, signed login cookies, optional reverse SSH tunnels (`--reverse`), and CEL agent/event filters; SIGHUP bounces just the `mngr observe` child.

### Fixed

- Fixed: `mngr forward` subprocess no longer pegs ~130% CPU after agents disconnect — the bidirectional relay (lifted to `mngr_forward/relay.py`) now terminates when the paramiko channel has received EOF.
