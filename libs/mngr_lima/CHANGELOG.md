# Changelog - mngr_lima

A concise, human-friendly summary of changes for the `mngr_lima` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: Project adopted the per-project changelog layout (`changelog/`, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md` at the project root).
- Changed: `mngr create --provider lima` help text now shows `--memory=N` / `--disk=N` (plain integers, no `GiB` suffix) matching what `limactl start` expects.
- Changed: Lima provider drops `ssh-keyscan` from host-creation — each VM gets a pre-generated ed25519 host keypair injected via the Lima provision script, eliminating TOFU and the `Broken pipe` race during VM bring-up. Per-host keys + matching `known_hosts` live under `<provider-dir>/keys/hosts/<host_id>/`. `merge_lima_yaml` now extends `provision` and `mounts` instead of replacing them.
- Changed: Serial-log tailer switched from `tail --follow=name --retry` (GNU-only) to portable `tail -F` so macOS BSD tail no longer exits immediately and loses serial-log diagnostics during VM boot.

### Fixed

- Fixed: Lima provider now actually disables guest → host port forwarding by emitting two ignore rules (`guestIP: 0.0.0.0` with `guestIPMustBeZero: true`, and `guestIP: 127.0.0.1`); the previous empty `portForwards: []` left Lima's fallback rule in place. `merge_lima_yaml` locks `portForwards` against user `--file` overrides.
