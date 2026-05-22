# Changelog - mngr_forward

A concise, human-friendly summary of changes for the `mngr_forward` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `mngr_forward` emits `workspace_backend_failure` envelopes (now renamed `system_interface_backend_failure`) on connection errors, mid-SSE EOF, or 5xx responses from the workspace backend, so consumers can track per-agent health as a state machine and drive a recovery UI.

### Changed

- Changed: Renamed the workspace-server envelope contract to system-interface in lockstep with the mngr-side rename — `WorkspaceBackendFailure*` → `SystemInterfaceBackendFailure*`, envelope type literal `workspace_backend_failure` → `system_interface_backend_failure`, and the plugin's 503 loader page reads "System interface starting".
- Changed: The 503 fallback page is now a styled card with a loading spinner instead of the blank "Backend not yet available. Retrying..." page; the spinner's animation duration matches the page's 1-second auto-refresh interval.
- Changed: Adopted per-project changelog layout (`changelog/` dir, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md` at the project root).

## [v0.2.7] - 2026-05-11

### Added

- Added: New `mngr_forward` plugin (`libs/mngr_forward/`) that serves `<agent-id>.localhost:8421/*` subdomain forwarding, signed login cookies, optional reverse SSH tunnels (`--reverse`), and CEL agent/event filters; SIGHUP bounces just the `mngr observe` child.

### Fixed

- Fixed: `mngr forward` subprocess no longer pegs ~130% CPU after agents disconnect — the bidirectional relay (lifted to `mngr_forward/relay.py`) now terminates when the paramiko channel has received EOF.
