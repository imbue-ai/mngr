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
  `OfflineHost` in this readable form, and every provider's offline-host construction now does
  so -- so a stopped host is readable whether it is reached via `get_host` (the destroy/GC
  path) or `to_offline_host`. The volume is resolved lazily on first read (not at
  construction), so host discovery -- which materializes offline hosts but only reads their
  metadata -- never pays for it; for providers whose volume lookup is a network probe (e.g.
  Modal) this matters. When no volume is available, reads behave as "nothing there". This lets
  callers treat a stopped-but-volume-backed host uniformly with an online one instead of
  branching on online-vs-offline and reaching for the raw `Volume` API.
- New `api/preservation.py` with `PreservedItem`, `preserve_agent_data()`, and
  `get_preserved_agent_dir()`. Callers declare a list of paths (relative to the agent state
  dir) to keep; the same declaration is executed against either an online host (rsync for
  directories) or a volume-backed offline host (file-by-file walk). Preserved files mirror the
  agent-state-dir layout verbatim under `<local_host_dir>/preserved/<agent-name>--<agent-id>/`.
- `OuterHost` gained a `list_directory()` implementation (local filesystem walk, or SFTP
  `listdir_attr` over the same paramiko channel used for remote file reads).
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
