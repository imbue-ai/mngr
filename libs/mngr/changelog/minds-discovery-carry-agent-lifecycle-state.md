`DiscoveredAgent` now always carries the agent's lifecycle state, and the discovery event stream always reports a real one.

- Full-listing snapshots carry the state the listing already computed (previously it was dropped during conversion, so `mngr observe` consumers could not tell a stopped agent from a running one without re-listing).

- Incremental `agent_discovered` events (create/start/stop/archive/cleanup/rename) now probe each agent's liveness before emitting, instead of reporting no state. Consumers such as the minds system interface no longer need to guess "running" for a just-surfaced agent -- which also fixes the window where a just-stopped agent read as running until the next snapshot.

- The `state` field is non-optional: references built without a probe read `UNKNOWN` (offline hosts read `STOPPED`, since a down host cannot have a running agent process). Discovery-event lines written by older versions (missing or null `state`) still parse, reading as `UNKNOWN`.
