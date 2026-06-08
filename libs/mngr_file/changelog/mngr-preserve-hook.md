`mngr file get`, `list`, and `put` now operate through the unified host file interfaces
instead of branching internally between an online host and a separately-fetched volume.

- Target resolution returns a single readable host (an online host, or a volume-backed stopped
  host) addressed by absolute paths under the host's `host_dir`; the previous
  `(online_host, volume)` pair and the per-command "volume path" computation are gone.
- `get` reads via `host.read_file`; `list` lists via `host.list_directory`; `put` writes via the
  host's write interface (`HostFileWriteInterface`) for both online and stopped hosts.
- `mngr file list`'s duplicate cross-platform listing script was removed in favor of the shared
  `host.list_directory`. The shared listing now carries the full file type and a permissions
  string when the source can report them: a host (online, or the local machine) classifies the
  real `stat` mode -- so symlinks, pipes, sockets, and device files are reported as their own
  types and the opt-in `permissions` field shows the mode string -- while a bare volume-backed
  stopped host only distinguishes file vs. directory and leaves `permissions` as `-`. The default
  listing (name, type, size, modified) is unchanged.
- Writing to a stopped host (offline `put`) still works, now through the volume-backed host's
  write interface; `--mode` continues to be ignored when the host is offline.
- Behavior for offline access (which `--relative-to` modes are reachable, the "provider does not
  support volume access" error) is unchanged.
