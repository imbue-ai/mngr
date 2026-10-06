The release test covering `mngr observe --discovery-only` now checks for the event type discovery actually emits.

It required a `DISCOVERY_FULL` event in the stream. That event is the historical whole-scan snapshot: per-provider discovery superseded it, each provider now emits its own `DISCOVERY_PROVIDER` snapshot on its own loop, and `DISCOVERY_FULL` is no longer produced at all (the enum keeps it only so historical on-disk logs still parse). The test could therefore never pass. `mngr observe` itself is unchanged; the test and its comments now describe per-provider discovery.
