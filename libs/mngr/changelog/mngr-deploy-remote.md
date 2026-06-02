`mngr create --format jsonl` (and every other command) now emits a structured
error record when a command fails:

```json
{"event": "error", "error_class": "FastPathUnavailableError", "message": "..."}
```

Previously a failing command only printed a human-formatted `Error: <message>`
line with no machine-readable type, so subprocess callers (e.g. minds) had to
substring-match the error text to detect specific failures -- which silently
broke when the error surfaced cleanly without the class name in a traceback. The
top-level CLI exception handler now calls `emit_error_event(...)` for real
errors (not control-flow exits like Ctrl-C / `--help`) when the resolved output
format is JSONL, attaching the exception's class name. `on_error` likewise
includes `error_class` in its JSONL error event when given the exception.

`mngr create --new-host` now tears down the host it just created if a later step
fails (provisioning, agent start, etc.), so a failed create never leaks the
host. Previously the only cleanup was removing the host lock so idle-shutdown
providers could reclaim the host on their own -- which never helped providers
that disable idle shutdown (e.g. imbue_cloud pool leases), leaving the host (and
its lease) stranded. The teardown is gated by the existing
`MNGR_DEBUG_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE=1`, which now retains the
failed host (not just its lock) for debugging.
