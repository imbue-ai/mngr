Added a shared shell library `mngr_common_transcript_lib.sh`, provisioned to every
agent's `commands/` dir alongside `mngr_log.sh` and `mngr_transcript_lib.sh` (via
`Host._ensure_shared_shell_libs`). It centralizes the common-transcript converter
primitives shared across agent plugins:

- the convert lock (a coarse mkdir-based mutex serializing the converter's
read-modify-write so the background daemon and an on-demand `--single-pass` flush
can't append duplicate events), and

- the turn-end flush (one synchronous `--single-pass` of the raw streamer + common
converter, in pipeline order), used by agent turn-end hooks so a WAITING-signal
consumer can't outrun the converter.
