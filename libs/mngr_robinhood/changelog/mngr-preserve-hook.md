Updated an internal test to construct `EventsTarget` with its new single `host` field (replacing
the former `online_host`) after the events API was migrated to read through the unified host
file-read interface. No behavior change to the orchestrator itself.
