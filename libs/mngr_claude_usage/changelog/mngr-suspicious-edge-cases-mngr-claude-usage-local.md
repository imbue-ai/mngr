Hardened suspicious edge-case handling in the usage statusline scripts:

- `claude_usage_writer.sh` now fails loudly (exit 64 + stderr) when `jq` is
  missing instead of silently treating every render as a no-op, which would
  have disabled usage tracking host-wide with no diagnostic.
- The statusline shim no longer guards the writer invocation with
  `[ -x "$writer" ]`; a missing/non-executable writer (a broken install) now
  surfaces on stderr rather than being silently skipped, while `|| true` keeps
  the user's statusline working.
- Removed dead `try` wrappers on `.session_id` / `.cost` in the writer's event
  construction (the input is already guaranteed to be a JSON object at that
  point).

No user-facing behavior change in the normal (jq-present, correctly-provisioned)
path.
