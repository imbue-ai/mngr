# Changelog - mngr_forward

A concise, human-friendly summary of changes for the `mngr_forward` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.2.7] - 2026-05-11

### Added

- Added: New `mngr_forward` plugin (`libs/mngr_forward/`) that serves `<agent-id>.localhost:8421/*` subdomain forwarding, signed login cookies, optional reverse SSH tunnels (`--reverse`), and CEL agent/event filters; SIGHUP bounces just the `mngr observe` child.

### Fixed

- Fixed: `mngr forward` subprocess no longer pegs ~130% CPU after agents disconnect — the bidirectional relay (lifted to `mngr_forward/relay.py`) now terminates when the paramiko channel has received EOF.
