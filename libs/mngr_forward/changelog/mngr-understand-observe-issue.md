## Discovery schema bump

- `mngr_forward` parses `FullDiscoverySnapshotEvent` lines from its inner `mngr observe --discovery-only` subprocess. The event grew two additional fields (`providers` and `error_by_provider_name`) in `libs/mngr`. This build picks them up transparently -- older `mngr_forward` builds running against new snapshots will raise `DiscoverySchemaChangedError` and must be rebuilt.
