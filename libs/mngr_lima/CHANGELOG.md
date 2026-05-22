# Changelog - mngr_lima

A concise, human-friendly summary of changes for the `mngr_lima` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: Dropped `ssh-keyscan` from the host-creation flow — each Lima VM gets a pre-generated ed25519 host keypair injected via the Lima provision script (mirroring `mngr_vps_docker`'s cloud-init pattern); per-host keys and `known_hosts` live under `<provider-dir>/keys/hosts/<host_id>/`. No more TOFU and no shared-`known_hosts` collisions across restarts.
- Changed: Switched the serial-log tailer to `tail -F` for portability across GNU and BSD `tail` (macOS); the previous `tail --follow=name --retry` was GNU-only and silently dropped serial-log diagnostics on macOS.
- Changed: `merge_lima_yaml` now extends `provision` and `mounts` instead of replacing them, so a user-supplied entry is appended after mngr's load-bearing host-key swap and `/mngr` mount.
- Changed: `mngr create --provider lima` help text shows `--memory=N` / `--disk=N` (plain integers, no `GiB` suffix).
- Changed: Adopted per-project changelog layout (`changelog/` dir, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md` at the project root).

### Fixed

- Fixed: Lima provider now actually disables guest → host port forwarding — the provider emits two ignore rules (one for `guestIP: 0.0.0.0` with `guestIPMustBeZero: true`, one for `127.0.0.1`) because user-supplied rules match literally; `merge_lima_yaml` locks `portForwards` against user `--file` overrides.
