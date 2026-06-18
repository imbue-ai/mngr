The S3 state bucket is now the sole offline store for AWS hosts; the EC2 tag mirror has been removed. A stopped EC2 instance's offline host/agent records (so `mngr list` / `mngr start` / `mngr event` work while it is stopped) now always live in the S3 state bucket created by `mngr aws prepare`.

- The S3 state bucket is required: when it has not been provisioned, mngr raises an actionable error pointing at `mngr aws prepare` -- not just on an offline read (listing/resuming a stopped host) but also on the create/label write path, since there is no longer a degraded fallback. A transient S3 error on a mirror read or write propagates too (rather than silently dropping state).

- Per-agent `mngr-agent-*` EC2 tags are no longer written or read. This removes the EC2 50-tag ceiling concern entirely (the `TagLimitExceededError` that flagged it is gone).

- `mngr aws prepare` treats the bucket as its primary job: a missing S3/STS permission or any bucket-create failure now fails the command (it no longer warns and continues with a security-group-only prepare).

`AwsProvider` now extends `OfflineCapableVpsDockerProvider` directly and selects a `BucketHostStateStore` when the bucket exists (raising an actionable error otherwise); it keeps a cheap `_offline_discovered_host_from_instance` that labels a stopped instance from its `mngr-host-id` / `Name` tags.
