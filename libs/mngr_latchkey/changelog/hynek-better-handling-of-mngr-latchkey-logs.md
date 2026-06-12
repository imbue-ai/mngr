`mngr latchkey forward` now has a structured, rotated, timestamped log, reusing the standard mngr/minds JSONL logging rather than the previous unrotated, untimestamped files.

- The supervisor now writes its structured log to `<latchkey_directory>/mngr_latchkey/events.jsonl` (one flat JSON object per line with a nanosecond timestamp, size-rotated with rotated copies pruned). Read this when you need to observe timing.

- The shared `latchkey gateway` subprocess's output is now routed through loguru (each line at DEBUG, prefixed with `[latchkey gateway]`) into that same structured log, so it is timestamped and rotated like the rest of the logs instead of accumulating in the separate, unrotated `latchkey_gateway.log`. That separate file is no longer written.

- The detached supervisor is now spawned with `--quiet`, so its raw `latchkey_forward.log` capture file no longer accumulates console output in steady state (everything goes to the structured `events.jsonl`). The raw file stays effectively empty and only ever captures rare startup-failure output (Click errors or a pre-logging traceback), which is exactly when you want it. Its fd is handed straight to the detached process, so it cannot be rotated mid-write -- keeping it near-empty is what bounds it.
