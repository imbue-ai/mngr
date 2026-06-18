The Azure Blob state bucket is now the sole offline store for Azure hosts; the VM tag mirror has been removed. A deallocated VM's offline host/agent records (so `mngr list` / `mngr start` / `mngr event` work while it is stopped) now always live in the Blob state account created by `mngr azure prepare`.

- The Blob state account is required: when it has not been provisioned, mngr raises an actionable error pointing at `mngr azure prepare` -- not just on an offline read (listing/resuming a deallocated host) but also on the create/label write path, since there is no longer a degraded fallback. A transient Blob error on a mirror read or write propagates too (rather than silently dropping state).

- Per-agent `mngr-agent-*` VM tags are no longer written or read.

- `mngr azure prepare` treats the storage account + container as its primary job: a missing storage permission or any bucket-create failure now fails the command (it no longer warns and continues with a network-only prepare).

`AzureProvider` now extends `OfflineCapableVpsDockerProvider` directly and selects a `BucketHostStateStore` when the bucket exists (raising an actionable error otherwise); it keeps a cheap `_offline_discovered_host_from_instance` that labels a deallocated VM from its `mngr-host-id` / `mngr-host-name` tags.
