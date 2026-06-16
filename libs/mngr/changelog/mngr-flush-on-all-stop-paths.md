Changed: `mngr_common_transcript_flush` (shared common-transcript helper) now takes an
optional lock-acquire timeout (seconds), exported as `MNGR_CONVERT_LOCK_TIMEOUT` to each
synchronous converter pass. This lets a latency-sensitive caller (e.g. a SIGTERM/SIGINT
handler) cap how long the flush blocks waiting for the convert lock -- its only
potentially-slow step. Implemented without `timeout(1)` so it stays portable to macOS.
Callers that pass no argument are unchanged (default 30s).
