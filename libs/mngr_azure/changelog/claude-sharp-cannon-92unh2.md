The Azure Blob state bucket is now the sole offline store for Azure hosts; the VM tag mirror has been removed. A deallocated VM's offline host/agent records (so `mngr list` / `mngr start` / `mngr event` work while it is stopped) now always live in the Blob state account created by `mngr azure prepare`.

- When no state bucket exists, offline host *reads* now raise an actionable error pointing at `mngr azure prepare` (a deallocated host can no longer be listed or resumed without the bucket), instead of falling back to the VM tag mirror. Creating and operating a *running* host is unaffected: mirror writes are skipped silently when there is no bucket.

- Per-agent `mngr-agent-*` VM tags are no longer written or read.

- `mngr azure prepare` still creates the storage account + container; its warnings now say offline host state will be unavailable until prepare succeeds, rather than that it falls back to tags.

`AzureProvider` now extends `OfflineCapableVpsDockerProvider` directly and selects a `BucketHostStateStore` (bucket present) or a raise-on-read placeholder (bucket absent); it keeps a cheap `_offline_discovered_host_from_instance` that labels a deallocated VM from its `mngr-host-id` / `mngr-host-name` tags.
