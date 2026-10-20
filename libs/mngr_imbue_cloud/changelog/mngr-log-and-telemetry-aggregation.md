`mngr imbue_cloud admin server prep` gained an optional `--extra-prep-script <file>`: an additional idempotent root bash script appended to the box prep script, running in the same pinned-host-key SSH session as the standard prep steps.

This is the generic hook the observability rollout uses to install the pinned OpenTelemetry Collector on bare-metal boxes (`just prep-server` renders the collector install via the `observability` CLI and passes it here); the prep flow itself stays observability-agnostic.
