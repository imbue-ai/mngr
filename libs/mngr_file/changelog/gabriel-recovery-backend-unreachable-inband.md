# Follow mngr's discovery return type

Updated for mngr's `discover_hosts_and_agents`, which now returns a `DiscoveryOutcome` (hosts and agents, the providers that answered, and the providers that could not be reached) rather than a two-tuple. No behavior change here: target resolution reads the same discovered agents off the outcome.
