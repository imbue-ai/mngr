Added an optional S3 state bucket that holds mngr's control-plane state (the full host record and per-agent records) so a stopped instance's state is readable without SSH and without the 256-char EC2 tag limit.

- `mngr aws prepare` now also creates (idempotently) a private, encrypted S3 state bucket, named `mngr-state-<account_id>-<region>` by default or overridable via the new `state_bucket_name` provider config field. Bucket setup is best-effort: a missing S3/STS permission degrades to a warning so the security-group prepare still succeeds.

- `mngr aws cleanup` now also deletes the state bucket. Since it runs after the refuse-while-instances-exist check, any state left in the bucket is orphaned (hosts no longer present as instances), so cleanup refuses to delete a non-empty bucket rather than silently dropping offline records; pass the new `--force` flag to delete the bucket and its remaining state.

- When a state bucket is configured, the per-agent `mngr-agent-<id>-*` EC2 tags are no longer written; agent records and the full offline host record live in the bucket instead. This removes both the silent 256-char `labels` drop and the `TagLimitExceeded` failure at EC2's 50-tag ceiling. Without a configured bucket, the EC2 tag mirror is retained unchanged as a graceful fallback.

- A stopped host's full `VpsDockerHostRecord` is now reconstructed from the bucket (instead of the lossy tag subset) for `mngr list` / `mngr start` when a bucket is present.

Added a Lima-style offline `host_dir`, **on by default** (new `is_host_dir_synced_to_bucket` provider config field, mirroring Lima's `is_host_data_volume_exposed`). A stopped instance's `host_dir` is now readable without SSH, so `mngr event` / `mngr transcript` work against a paused host.

- `mngr aws prepare` gained a `--use-offline-host-dir {yes,auto,no}` option (default `auto`). It provisions a least-privilege IAM role + instance profile that lets an instance push its `host_dir` to the bucket: `auto` warns and continues if the bucket could not be set up or it lacks IAM permissions (the security group + bucket prepare still succeed), `yes` fails the command if the bucket could not be set up or the identity can't be provisioned (the identity's inline policy is scoped to the bucket, so it is meaningless without one), and `no` doesn't attempt it. The inline policy grants only `s3:PutObject` / `s3:GetObject` / `s3:DeleteObject` on the bucket's `hosts/*` prefix and `s3:ListBucket` on the bucket. Re-running `prepare --use-offline-host-dir yes` after a bucket-only prepare adds just the identity.

- `mngr aws cleanup` now also deletes the host-dir IAM identity (role + instance profile), best-effort and idempotent, after the bucket.

- At host create, when the feature is on and the identity exists, the provisioned instance profile is attached so an on-box systemd daemon can `aws s3 sync` `host_dir` to `s3://<bucket>/hosts/<host_id>/host_dir/` every ~60s (and once on graceful stop) via IMDS credentials. The operator-supplied `iam_instance_profile`, if set, takes precedence. Attaching a profile at create requires `iam:PassRole`.

- Offline `host_dir` reads use the operator's credentials (no instance identity needed to read). When the feature is on but a host's instance has no attached IAM profile (so it never pushed its `host_dir`), `get_volume_for_host` logs a clear warning pointing at `mngr aws prepare --use-offline-host-dir yes` rather than silently returning an empty volume.

Internal refactor (no behavior change): `AwsProvider` now extends the shared `TagMirrorVpsDockerProvider` and consumes the shared `state_keys` layout, generic `BucketHostStateStore`, and hoisted instance-lookup / path helpers, rather than carrying AWS-local copies. The duplicate-`mngr-host-id` ambiguous-match error is now phrased provider-neutrally.

Internal refactor (no behavior change): `AwsProvider` no longer overrides `discover_hosts_and_agents` / `list_persisted_agent_data_for_host`; it now implements only the shared `_offline_agent_dicts_for` hook (reading the S3 bucket or EC2 tag mirror via `_state_store`). Validated end-to-end on real EC2 (create / stop / list-while-stopped / start).

Internal refactor (no behavior change): `AwsProvider` no longer overrides `persist_agent_data` / `remove_persisted_agent_data` / `_persist_host_record_externally` / `_delete_host_record_externally`; the shared envelope lives on the base and the `_state_store`-backed steps live on `TagMirrorVpsDockerProvider`. AWS keeps only `_state_store`, `_persist_agent_to_tags` (the EC2-tag write the tag store uses), and provider specifics. Validated on real EC2.
