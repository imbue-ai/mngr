Hardened suspicious edge-case handling in `imbue_common`:

- Ratchet AST scanning no longer *silently* skips files that fail to parse: it now
  logs a warning naming the file and the `SyntaxError` (the skip itself is kept so
  a file using newer-than-runtime syntax can't crash a scan, but it is no longer
  invisible).
- JSONL log formatting now distinguishes a missing exception type/value from a
  falsy one (uses `is not None` instead of truthiness), so a custom exception
  whose `__bool__` is falsy no longer drops its `value` from the structured log.
- `rotation_lock` now catches the precise `BlockingIOError` (lock contended)
  rather than a broad `OSError` when probing a non-blocking `flock`, matching the
  shared pytest lock implementation.
- Added clarifying comments documenting why several intentionally-defensive
  fallbacks are safe (git-blame header parsing, log-sink size stat) and removed a
  redundant `hasattr` guard in the if/elif-chain ratchet helper.
