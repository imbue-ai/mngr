GCP's offline host/agent store is now exposed through the same `HostStateStore` interface as the AWS/Azure object-storage buckets, and now stores the *full* host record (not a lossy subset).

- The full `VpsDockerHostRecord` JSON is mirrored to the `mngr-host-state` instance-metadata value, and each agent record to a single `mngr-agent-<id>` metadata value (full JSON). A stopped GCE instance's `mngr list` / `mngr start` now reconstructs the complete record (config, IP, host keys), matching the AWS/Azure bucket behavior, instead of the previous minimal label-only reconstruction.

- This replaces the per-field `mngr-agent-<id>-<name|type|labels>` metadata layout and the `mngr-created-at`-label reconstruction. GCE instance metadata is large and permissive enough (256 KB per value, 512 KB per instance) to hold these records, so GCP needs no separate object-storage bucket.

Internal: `GcpProvider` now implements the shared `_state_store` (a new `_GceMetadataHostStateStore`) and the cheap identity-only `_offline_discovered_host_from_instance`, dropping the bespoke metadata reconstruction helpers; the offline read/write paths are inherited unchanged from `OfflineCapableVpsDockerProvider`.
