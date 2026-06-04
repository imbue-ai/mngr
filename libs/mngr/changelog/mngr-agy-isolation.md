Two small shared core helpers were added/extracted to support the antigravity per-agent isolation work (reusable by other plugins):

- `imbue.mngr.utils.git_utils.find_git_source_path` -- the per-agent source-repo trust resolution now delegates to this, eliminating logic duplicated byte-for-byte between the `antigravity` and `claude` plugins (no behavior change).
- `imbue.mngr.hosts.common.symlink_or_copy_on_host(host, source, dest, *, symlink, ensure_source_parent=...)` -- a one-round-trip helper that symlinks (always, even to a not-yet-existing source -- a write-through symlink) or copies (only if the source exists) a path on the host, centralizing the symlink-vs-copy credential/cache pattern.
