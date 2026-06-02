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
- `OuterHost` gained a `list_directory()` implementation (local filesystem walk, or a small
  cross-platform script over SSH for remote hosts).
