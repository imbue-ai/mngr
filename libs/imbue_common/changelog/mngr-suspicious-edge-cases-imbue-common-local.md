Hardened suspicious edge-case handling in `imbue_common`:

- JSONL log formatting now distinguishes a missing exception type/value from a
  falsy one (uses `is not None` instead of truthiness), so a custom exception
  whose `__bool__` is falsy no longer drops its `value` from the structured log.
- `rotation_lock` now catches the precise `BlockingIOError` (lock contended)
  rather than a broad `OSError` when probing a non-blocking `flock`, matching the
  shared pytest lock implementation.
- Added clarifying comments documenting why several intentionally-defensive
  fallbacks are safe (git-blame header parsing, log-sink size stat) and removed a
  redundant `hasattr` guard in the if/elif-chain ratchet helper.
