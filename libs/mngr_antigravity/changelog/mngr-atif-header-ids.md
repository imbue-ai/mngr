The ATIF stream header's `event_id` is now unique per agent stream: `header-<sha256(agent_id:emitter)[:32]>` instead of the fixed `"header"`. A fixed id repeats identically for every agent on every host, so analytics' fleet-wide event-id dedupe collapsed all header rows to one per account, destroying the per-agent (emitter, `schema_version`) mix.

The agent id is the agent state directory's basename (a UUID4-based value), so distinct agents never collide and a restart reuses the same id.
