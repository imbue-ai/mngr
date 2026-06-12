`mngr latchkey forward` logs are now rotated and have observable timestamps, matching the core minds logs.

- The supervisor now writes a structured, co-located log at `<latchkey_directory>/mngr_latchkey/latchkey_forward_events.jsonl` (one flat JSON object per line with a nanosecond timestamp), rotated by size. This is the log to read when you need to observe timing.

- The raw `latchkey_forward.log` capture is now rotated at (re)spawn time so it can no longer grow without bound across supervisor restarts.

- `latchkey_gateway.log` (the shared `latchkey gateway` subprocess's output, which we do control) is now size-rotated, and each captured line is prefixed with a UTC receipt timestamp so the timing of the gateway's otherwise-unstructured output is observable.

- Documented the three log files in the README.
