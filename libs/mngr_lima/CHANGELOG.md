# Changelog - mngr_lima

A concise, human-friendly summary of changes for the `mngr_lima` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: `mngr create --provider lima` help text now shows `--memory=N` / `--disk=N` (plain integers, no `GiB` suffix), matching what `limactl start` expects.
- Changed: Dropped `ssh-keyscan` from the host-creation flow — each Lima VM gets a pre-generated ed25519 host keypair injected via the provision script, removing TOFU and the `Broken pipe` race during VM bring-up. Per-host keys and `known_hosts` live under `<provider-dir>/keys/hosts/<host_id>/`; `delete_host` cleans up that directory.
- Changed: `merge_lima_yaml` now extends `provision` and `mounts` instead of replacing them, so user-supplied entries are appended after mngr's load-bearing host-key injection and `/mngr` mount.
- Changed: Serial-log tailer switched from `tail --follow=name --retry` (GNU-only) to portable `tail -F` so BSD tail on macOS no longer exits immediately and loses diagnostics.

### Fixed

- Fixed: Lima provider actually disables guest→host port forwarding now — emits two ignore rules (`guestIP: 0.0.0.0` with `guestIPMustBeZero: true`, plus `guestIP: 127.0.0.1`) so guest sockets on any interface no longer leak to host loopback. `merge_lima_yaml` locks `portForwards` against user `--file` overrides.
