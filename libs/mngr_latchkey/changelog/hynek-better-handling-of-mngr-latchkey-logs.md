`mngr latchkey forward` now has a structured, rotated, timestamped log, reusing the standard mngr/minds JSONL logging rather than the previous unrotated, untimestamped files.

- The supervisor now writes its structured log to `<latchkey_directory>/mngr_latchkey/forward_logs/events.jsonl` (one flat JSON object per line with a nanosecond timestamp, size-rotated with rotated copies pruned). Read this when you need to observe timing.

- The shared `latchkey gateway` subprocess's output is now routed through loguru (each line at DEBUG, prefixed with `[latchkey gateway]`) into that same structured log, so it is timestamped and rotated like the rest of the logs instead of accumulating in the separate, unrotated `latchkey_gateway.log`. That separate file is no longer written.

- The raw `latchkey_forward.log` capture is left as an unrotated catch-all for console output and pre-logging tracebacks (its file descriptor is handed straight to the detached process, so it cannot be rotated mid-write).
