Hardened the turn-end signal so consumers that read the common transcript on the WAITING
transition (e.g. an orchestrator harvesting the agent's final message) can no longer
outrun the converter. `statusline.sh` now, on the busy->idle edge, flushes the transcript
pipeline (a synchronous `--single-pass` of the raw streamer and common-transcript
converter, in pipeline order) before clearing the `active` marker -- so by the time the
agent reports WAITING the common transcript already reflects the final assistant message.
The flush is gated to the busy->idle edge so it costs at most one conversion pass per turn.

The flush and the converter's convert lock now come from the shared
`mngr_common_transcript_lib.sh` (see the `mngr` changelog) rather than being duplicated
per agent. The convert lock keeps the on-demand flush from racing the background 5s
daemon into duplicate events; its timeout is tunable via `MNGR_CONVERT_LOCK_TIMEOUT`.
