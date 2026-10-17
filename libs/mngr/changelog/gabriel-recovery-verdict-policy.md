Diagnostics for docker discovery reporting existing hosts/agents as absent (observed live: `mngr start <agent>` failed with "Agent not found" during a minds app launch, against a stopped host whose records another process read successfully one second later):

- `DockerVolume.listdir` now distinguishes a genuinely-missing directory (still `FileNotFoundError`, the normal fresh-env case) from any other `ls` failure inside the state container, which now raises `VolumeListingError` (an `MngrError` that is also an `OSError`) carrying the exit code and output.

- The docker host store logs a warning (instead of silently returning an empty list) when listing host records or a host's persisted agent data fails for any reason other than the directory not existing -- an empty result there makes hosts/agents invisible to discovery.

- An agent lookup that is about to fail with "No agent(s) found matching" first logs a warning summarizing what discovery did return (per host: provider, name, id, state, and agent count), so a not-found caused by a discovery gap is distinguishable from a truly-absent agent after the fact.

Agent-scoped commands (e.g. `mngr start <agent>`) no longer fall back to a full all-provider discovery scan after a provider outage, which could stall them for a minute or more on an unrelated unreachable provider (observed live: a minds cold-boot restart of a stopped docker workspace spent ~60s of its 73s inside `mngr start`, waiting on SSH to a dead aws-us-west-1 host):

- The event-stream replay that resolves an agent identifier to its provider no longer forgets a provider's agents when replaying errored snapshots (e.g. the "docker state container is stopped" backlog written while the minds app is closed). Per the snapshot contract, absence from an errored snapshot means the read failed, not that the agents are gone -- matching what `DiscoveryStateAggregator` already did.

- The snapshot replay window now reaches back to each provider's latest non-errored snapshot (instead of merely its latest), so a provider's last-known membership stays reconstructable for as long as its outage lasts. This also lets `mngr forward --observe-via-file` consumers (the minds resolver) see the last-known agents of an errored provider immediately on attach.
