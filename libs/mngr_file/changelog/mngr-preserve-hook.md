`mngr file get`, `list`, and `put` now operate through the unified host file interfaces
instead of branching internally between an online host and a separately-fetched volume.

- Target resolution returns a single readable host (an online host, or a volume-backed stopped
  host) addressed by absolute paths under the host's `host_dir`; the previous
  `(online_host, volume)` pair and the per-command "volume path" computation are gone.
- `get` reads via `host.read_file`; `list` lists via `host.list_directory`; `put` writes via the
  host's write interface (`HostFileWriteInterface`) for both online and stopped hosts.
- `mngr file list`'s duplicate cross-platform listing script was removed in favor of the shared
  `host.list_directory`. As a result, online listings no longer populate the opt-in `permissions`
  field (it is now always `-`, matching what offline listings already showed); the default
  listing (name, type, size, modified) is unchanged.
- Writing to a stopped host (offline `put`) still works, now through the volume-backed host's
  write interface; `--mode` continues to be ignored when the host is offline.
- Behavior for offline access (which `--relative-to` modes are reachable, the "provider does not
  support volume access" error) is unchanged.
