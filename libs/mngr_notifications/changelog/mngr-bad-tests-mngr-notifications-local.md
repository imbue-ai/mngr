Improved the notifications plugin test suite quality (no user-facing behavior change beyond an internal refactor):

- `get_notifier` now accepts an optional `system` argument (defaulting to the current platform) so platform selection is injectable instead of patched in tests.
- Strengthened tests to verify real behavior: the Linux verification path now asserts a notification is actually sent; the iTerm connect-command is pinned with a full snapshot; terminal-app lookups assert the resolved class; and `_ensure_observe` asserts a real process is launched and cleaned up.
- Removed a redundant end-to-end watcher test and moved its coverage into the integration-test file; corrected a mis-applied `acceptance` marker.
