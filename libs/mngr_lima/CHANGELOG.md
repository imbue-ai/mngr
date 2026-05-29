# Changelog - mngr_lima

A concise, human-friendly summary of changes for the `mngr_lima` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.2] - 2026-05-28

### Changed

- Changed: Dropped `ssh-keyscan` from the host-creation flow — each Lima VM now gets a pre-generated ed25519 host keypair injected via the Lima `provision[mode=system]` script, eliminating the TOFU and the `Broken pipe` race during VM bring-up. Per-host keys live under `<provider-dir>/keys/hosts/<host_id>/`; `merge_lima_yaml` extends (rather than replaces) `provision` and `mounts` so mngr's load-bearing entries are preserved.
- Changed: `mngr create --provider lima` help text now shows `--memory=N` / `--disk=N` (plain integers, no `GiB` suffix), matching what `limactl start` expects.
- Changed: Serial-log tailer switched from `tail --follow=name --retry` (GNU-only) to `tail -F` for macOS BSD-tail compatibility.

### Fixed

- Fixed: Lima provider now actually disables guest → host port forwarding — emits two ignore rules (`guestIP: 0.0.0.0` with `guestIPMustBeZero: true` and `guestIP: 127.0.0.1`), and `merge_lima_yaml` locks `portForwards` against user `--file` overrides.
