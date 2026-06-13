Hardened the turn-end signal so consumers that read the common transcript on the WAITING
transition (e.g. an orchestrator harvesting the agent's final message) can no longer
outrun the converter. `statusline.sh` now, on the busy->idle edge, runs a synchronous
`--single-pass` of the raw streamer and common-transcript converter, in pipeline order,
before clearing the `active` marker -- so by the time the agent reports WAITING the common
transcript already reflects the final assistant message. The flush is gated to the
busy->idle edge so it costs at most one conversion pass per turn.

The common-transcript converter now takes a coarse mkdir-based lock around its
read-modify-write so the background 5s daemon and the on-demand flush can't both append
the same events into duplicates. A lock left by a crashed pass is broken once it is older
than a minute; the timeout is tunable via `MNGR_CONVERT_LOCK_TIMEOUT`.
