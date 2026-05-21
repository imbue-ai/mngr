# Changelog - mngr_lima

A concise, human-friendly summary of changes for the `mngr_lima` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: Dropped ssh-keyscan from host creation — each Lima VM now gets a pre-generated ed25519 host keypair injected into the guest via the `provision[mode=system]` script; host machine writes the matching `known_hosts` entry atomically (no scan, no TOFU). Per-host keys live under `<provider-dir>/keys/hosts/<host_id>/` and are cleaned up by `delete_host`.
- Changed: `merge_lima_yaml` now extends `provision` and `mounts` instead of replacing them so mngr's load-bearing entries (host-key injection, `/mngr` mount) survive user `--file` overrides.
- Changed: Serial-log tailer switched to `tail -F` (portable across GNU and BSD); the previous `tail --follow=name --retry` silently lost diagnostics on macOS.
- Changed: Project now participates in the per-project changelog layout (per-project `changelog/`, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md`).

### Fixed

- Fixed: Lima provider now actually disables guest → host port forwarding — the previous empty `portForwards: []` did not suppress Lima's auto-appended fallback rule. Provider now emits two ignore rules (for `guestIP: 0.0.0.0` with `guestIPMustBeZero: true`, and `guestIP: 127.0.0.1`); `merge_lima_yaml` locks `portForwards` against user overrides.
