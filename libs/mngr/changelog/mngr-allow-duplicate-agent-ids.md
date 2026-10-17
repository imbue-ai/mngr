Agent ids are now unique per host rather than globally: the same agent id may exist on multiple hosts at once (the building block for migrating an agent between hosts while it exists on both).

Address resolution now honors a host id in the `@HOST` component: `AGENT@host-...` addresses match by host id (previously only host names matched, so an id-qualified address never resolved). This applies to all address-taking commands, including `mngr stop --stop-host`.

Added the `AgentInstanceKey` primitive (`<agent_id>@<host_id>`, also a valid CLI address) and re-keyed all cross-host aggregation by it: the discovery aggregator, the discovery event-stream resolution replay, the `mngr observe` observer (state tracking, PID watchers), and shell-completion membership. Agent-destroyed handling is host-scoped everywhere, so destroying an agent on one host never evicts a same-id agent on another host.

`mngr create` now rejects reusing an agent id on the same host with the new `DuplicateAgentIdOnHostError` (per-host uniqueness is load-bearing); reusing an id on a different host is allowed and documented as "this is the same agent" (use `mngr clone` for an independent copy).

Single-target commands given a bare agent id that matches instances on multiple hosts now fail with an error listing each instance and suggesting `ID@HOST` disambiguation; `mngr stop --stop-host` raises the same disambiguation error instead of silently picking a host. Plural commands (`destroy`, `stop`, `start`, `message`, ...) keep their operate-on-all-matches semantics, and `mngr destroy` warns with the full instance list when a bare id matches multiple instances.

`AgentRemovedEvent` on the observe stream gains an additive `host_id` field (readers tolerate its absence in old lines).
