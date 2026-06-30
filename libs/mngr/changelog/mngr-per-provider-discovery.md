Made provider discovery per-provider and resilient to slow/hung providers, so a single stuck provider can no longer block discovery of all the others.

- Each provider is now discovered independently and emits its own `ProviderDiscoverySnapshotEvent` (the global `FullDiscoverySnapshotEvent` / `DISCOVERY_FULL` snapshot is being removed). A snapshot is authoritative only for its own provider.

- Added per-provider discovery cadence and timeout settings to each `[providers.<name>]` block: `discovery_poll_interval_seconds` (default 30), `discovery_warn_seconds` (default 20), `discovery_error_timeout_seconds` (default 120), and per-host / per-agent `host_discovery_timeout_seconds` / `agent_discovery_timeout_seconds` (default 30, validated to stay below the provider error timeout). A slow host/agent surfaces as explicitly UNKNOWN instead of holding up its whole provider.

- Added a shared, span-aware discovery state aggregator so a host/agent state change that arrives while a provider is mid-discovery is no longer clobbered by that older in-flight snapshot.
