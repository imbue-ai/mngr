Hardened suspicious edge-case handling in the `file` command internals:

- When probing host availability, `mngr file` now only treats a genuine "host not found" as a reason to fall back to offline volume access, and logs that fallback at warning level. Previously any library error (auth, config, provider bugs) was silently swallowed at trace level and the command continued against a possibly-stale volume copy.
- `mngr file list` now fails loudly if asked to render a display field it has no formatter for, instead of emitting a silent blank column.
- Corrupt list output (malformed lines, unparseable sizes) is now logged at warning level so dropped or unknown entries are visible, rather than being silently discarded.
- For offline file targets, the resolver no longer fabricates a fake host-relative base path (previously `/mngr-host-dir`/`/unknown`); the base path is now absent when the host is offline, so any accidental use surfaces an explicit error instead of a plausible-but-wrong path. This is an internal change with no effect on the documented online/offline behavior.
- Added a clarifying comment about the conventional skipping of un-`lstat`-able directory entries (which silently omits permission-denied entries).
