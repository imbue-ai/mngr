The forward resolver, stream manager, service-map cache, and `resolver_snapshot` envelope are now keyed by the agent instance (`<agent_id>@<host_id>`) instead of the bare agent id: agent ids are unique per host, not globally, so the same id on two hosts (e.g. mid-migration) keeps independent services, SSH info, and routing.

Per-agent `mngr event` subprocesses address `ID@HOST`, so their streams stay unambiguous when an id exists on multiple hosts.

Old service-map cache entries keyed by bare agent ids are dropped at seed time (a benign one-time startup slowdown that self-corrects once discovery and the event streams repopulate the map).
