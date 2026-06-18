The S3 state bucket is now the sole offline store for AWS hosts; the EC2 tag mirror has been removed. A stopped EC2 instance's offline host/agent records (so `mngr list` / `mngr start` / `mngr event` work while it is stopped) now always live in the S3 state bucket created by `mngr aws prepare`.

- When no state bucket exists, offline host *reads* now raise an actionable error pointing at `mngr aws prepare` (a stopped host can no longer be listed or resumed without the bucket), instead of falling back to the EC2 tag mirror. Creating and operating a *running* host is unaffected: mirror writes are skipped silently when there is no bucket.

- Per-agent `mngr-agent-*` EC2 tags are no longer written or read. This removes the EC2 50-tag ceiling concern entirely (the `TagLimitExceededError` that flagged it is gone).

- `mngr aws prepare` still creates the bucket; its warnings now say offline host state will be unavailable until prepare succeeds, rather than that it falls back to tags.

`AwsProvider` now extends `OfflineCapableVpsDockerProvider` directly and selects a `BucketHostStateStore` (bucket present) or a raise-on-read placeholder (bucket absent); it keeps a cheap `_offline_discovered_host_from_instance` that labels a stopped instance from its `mngr-host-id` / `Name` tags.
