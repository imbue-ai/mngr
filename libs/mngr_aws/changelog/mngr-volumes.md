Added an optional S3 state bucket that holds mngr's control-plane state (the full host record and per-agent records) so a stopped instance's state is readable without SSH and without the 256-char EC2 tag limit.

- `mngr aws prepare` now also creates (idempotently) a private, encrypted S3 state bucket, named `mngr-state-<account_id>-<region>` by default or overridable via the new `state_bucket_name` provider config field. Bucket setup is best-effort: a missing S3/STS permission degrades to a warning so the security-group prepare still succeeds.

- `mngr aws cleanup` now also deletes the state bucket, but refuses (deleting nothing) while the bucket still holds any host state, mirroring the existing refuse-while-instances-exist safety.

- When a state bucket is configured, the per-agent `mngr-agent-<id>-*` EC2 tags are no longer written; agent records and the full offline host record live in the bucket instead. This removes both the silent 256-char `labels` drop and the `TagLimitExceeded` failure at EC2's 50-tag ceiling. Without a configured bucket, the legacy EC2 tag mirror is retained unchanged as a graceful fallback.

- A stopped host's full `VpsDockerHostRecord` is now reconstructed from the bucket (instead of the lossy tag subset) for `mngr list` / `mngr start` when a bucket is present.
