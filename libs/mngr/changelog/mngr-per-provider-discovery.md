Made provider discovery per-provider and resilient to slow/hung providers, so a single stuck provider can no longer block discovery of all the others.

- Each provider is now discovered independently and emits its own `ProviderDiscoverySnapshotEvent`, authoritative only for its own provider. `mngr observe --discovery-only` runs one decoupled poll loop per provider, and `mngr list` writes one per-provider snapshot per provider as a side-effect.

- The legacy global `FullDiscoverySnapshotEvent` / `DISCOVERY_FULL` snapshot is no longer produced and all live usages were removed, but the type is kept (deprecated) so historical on-disk discovery logs still parse.

- Added per-provider discovery cadence and timeout settings to each `[providers.<name>]` block: `discovery_poll_interval_seconds` (default 30), `discovery_warn_seconds` (default 20), `discovery_error_timeout_seconds` (default 120), and per-host / per-agent `host_discovery_timeout_seconds` / `agent_discovery_timeout_seconds` (default 30, validated to stay below the provider error timeout). A slow host whose read exceeds its timeout surfaces as explicitly UNKNOWN (its previously-known agents retained as unknown) instead of holding up its whole provider's snapshot.

- A hung provider is bounded without killing threads: discovery warns after the warn threshold, then emits a per-provider `DiscoveryError` after the error timeout while the abandoned read keeps running; its late result is accepted on a later poll.

- Added a shared, span-aware discovery state aggregator (`DiscoveryStateAggregator`) so a host/agent state change that arrives while a provider is mid-discovery is no longer clobbered by that older in-flight snapshot. All discovery consumers now reconcile through it.
