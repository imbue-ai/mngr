Introduced a standard way for plugins to preserve files from an agent's state directory when
the agent (or its whole host) is destroyed, and made stopped hosts readable through a uniform
interface.

- New `HostFileReadInterface` (in `interfaces/host.py`) captures the read-only file operations
  (`read_file`, `read_text_file`, `path_exists`, `get_file_mtime`, `list_directory`) that work
  even when a host is not online, as long as its persistent storage (volume) is reachable.
  `OuterHostInterface` now extends it, so every online host is a `HostFileReadInterface`.
- New `OfflineHostWithVolume` (in `hosts/offline_host.py`) implements `HostFileReadInterface`
  on top of a stopped host's persisted volume, addressing files by absolute paths under
  `host_dir` exactly as an online host would. `make_readable_offline_host()` wraps a plain
  `OfflineHost` in this readable form when the provider yields a volume for it (else returns the
  plain `OfflineHost`), and every provider's offline-host construction now does so -- so a
  stopped host is readable whether it is reached via `get_host` (the destroy/GC path) or
  `to_offline_host`. The volume *reference* is fetched via a new provider method,
  `get_volume_reference_for_host`, which (unlike `get_volume_for_host`) skips any network
  existence probe -- e.g. Modal's `listdir` -- and returns the lazy reference, so constructing a
  readable offline host (including during host discovery) adds no per-host probe; only providers
  that actually probe (Modal) override the method, and a since-deleted volume surfaces as a
  read/write failure at access time. This lets callers treat a stopped-but-volume-backed host
  uniformly with an online one instead of branching on online-vs-offline and reaching for the
  raw `Volume` API.
- New `api/preservation.py` with `PreservedItem`, `preserve_agent_data()`, and
  `get_preserved_agent_dir()`. Callers declare a list of paths (relative to the agent state
  dir) to keep; the same declaration is executed against either an online host (rsync for
  directories) or a volume-backed offline host (file-by-file walk). Preserved files mirror the
  agent-state-dir layout verbatim under `<local_host_dir>/preserved/<agent-name>--<agent-id>/`.
- `OuterHost` gained a `list_directory()` implementation (local filesystem walk, or SFTP
  `listdir_attr` over the same paramiko channel used for remote file reads).
- The listing-entry type `VolumeFile` is now the shared return type for every
  `HostFileReadInterface.list_directory` (hosts as well as volumes). Its `file_type` uses the
  full `FileType` enum (file, directory, symlink, pipe, socket, block, character, other), moved
  into core `interfaces/data_types.py` from `mngr_file` (which now re-exports it), with a
  canonical `FileType.from_stat_mode` classifier; `VolumeFile` also gained an optional
  `permissions` string. Producers fill these to the fidelity their source allows: a host
  classifies the real `stat`/`lstat` mode and reports a permissions string, while a bare volume
  only distinguishes file vs. directory and leaves `permissions` None. (`VolumeFileType` is gone,
  folded into `FileType`.)
- New `HostFileWriteInterface` (`write_file`, `write_text_file`), the write companion to
  `HostFileReadInterface`. `OuterHostInterface` extends it (so every online host writes), and
  `OfflineHostWithVolume` implements it by writing the stopped host's volume (file modes are not
  settable through a volume write, so `mode` is ignored there). This lets write commands target
  an online or a stopped host through one interface.
- `api/events.py` now reads and discovers event journals through `HostFileReadInterface`
  (an online host, or a readable stopped host whose volume is reachable) addressed by a single
  absolute events path under the host's `host_dir`. This removes the separate code paths that
  shelled out `find`/`cat` over SSH for online hosts and used a separately-fetched
  events-scoped `Volume` for everything else, collapsing the dual `online_host`/`volume`
  representation on `EventsTarget` into one `host` handle. It also drops the trailing-newline
  "sentinel-cat" workaround: the host read path is byte-exact (local reads bytes directly,
  remote uses SFTP), so a file's exact trailing-newline state survives without the sentinel.
