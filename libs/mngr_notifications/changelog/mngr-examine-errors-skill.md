Fixed several over-defensive edge-case handlers surfaced by the `identify-suspicious-edge-cases` skill:

- The agent-state watcher no longer crashes or silently drops a WAITING notification when it reads a torn (partially-written) trailing line from the concurrently-appended events file. It now uses `split_complete_lines` plus a `MalformedJsonLineWarner`, advancing its read offset only past complete lines so a partial line is retried.
- Required event fields (`agent_id`, `agent_name`) are now read directly and raise on a malformed record instead of defaulting to `"unknown"` (which collapsed distinct agents together and produced "unknown is waiting" notifications).
- A real plugin-config type mismatch now raises instead of silently discarding the user's configured settings; likewise a foreign override type in `merge_with` now raises instead of being dropped.
- `mngr notify` on an unsupported platform now surfaces a clear error instead of exiting silently.
- File-read and alerter-invocation error handling was narrowed so genuine `OSError`s (permissions, I/O) are no longer mislabeled as "no events" / "alerter not found".

New library error module with `NotificationsError`-based types.