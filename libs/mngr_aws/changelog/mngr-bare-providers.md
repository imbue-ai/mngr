Renamed the `_AGENT_TAG_FIELDS` constant imported from `mngr_vps` to the
public `AGENT_TAG_FIELDS` (matching its sibling `AGENT_TAG_PREFIX`), so the
AWS tag-mirror code no longer imports a private name across modules. No
behavior change.


Updated imports for the `mngr_vps_docker` -> `mngr_vps` package rename and the
accompanying class renames (`VpsDockerProvider` -> `VpsProvider`,
`VpsDockerProviderConfig` -> `VpsProviderConfig`, `VpsDockerHostRecord` ->
`VpsHostRecord`, `VpsDockerError` -> `VpsError`, etc.). Import-only; no behavior
change.


Enabled bare placement (`isolation=NONE`): the idle agent runs `shutdown -P now`
as the VM's root, which stops the EC2 instance via InstanceInitiatedShutdownBehavior,
so the container-only sentinel + host-side systemd watcher is skipped for bare.

Added bare-placement (`isolation=NONE`) release tests, and fixed a resume bug they
caught: `start_host` read the host record via the Docker volume, which a bare host
does not have, so it now resolves the store through the realizer.

``stop_host`` / ``start_host`` moved to the shared base ``OfflineCapableVpsProvider``; AWS now supplies only the EC2 ``_pause_cloud_instance`` / ``_resume_cloud_instance`` hooks (and the final host_dir-to-bucket sync before pause). Behavior-preserving.

Updated the host_dir sync to call the realizer's `host_dir_path_on_outer`
directly after the redundant `_host_dir_path_on_outer` forwarder was removed
from the shared VPS provider. No behavior change.

The idle-watcher install, the host_dir-to-bucket sync daemon install/before-pause, and the best-effort `_on_host_finalized` step runner all moved to the shared `OfflineCapableVpsProvider`. AWS now supplies only small hooks: the `EC2 instance` display name, the `is_host_dir_synced_to_bucket`-plus-bucket sync gate, and the awscli install / `aws s3 sync` `.service` body / s3 target URI. The host-side systemd unit names changed from `mngr-aws-idle-watcher` / `mngr-aws-host-dir-sync` to the shared `mngr-idle-watcher` / `mngr-host-dir-sync`. Behavior-preserving otherwise.

Updated the VPS build-arg parsing imports to point at the new `imbue.mngr_vps.build_args` module (moved out of `imbue.mngr_vps.instance`). Import-only change; no behavior difference.

Updated imports for `TagMirrorVpsProvider`, `AGENT_TAG_PREFIX`, `AGENT_TAG_FIELDS`, and the host_dir-sync unit symbols to the new `imbue.mngr_vps.instance_offline` module (split out of `imbue.mngr_vps.instance`). Import-only change; no behavior difference.

The shared offline read-side reconstruction moved up into the new `KeyValueMirrorVpsProvider` base that `TagMirrorVpsProvider` now extends, so the AWS provider's host-name hook was renamed `_host_name_tag_key` -> `_host_name_key` and its tag-mirror agent-record write call now invokes the renamed `_agent_field_items` (formerly `_agent_field_tags`). The EC2 256-char tag-value cap is still applied (the base reads it from the new `_max_value_len` hook). Internal refactor; no user-visible behavior change.

The host_dir-sync daemon now runs its `aws s3 sync` command from an installed `/usr/local/sbin/mngr-host-dir-sync.sh` script (referenced directly by the oneshot `.service`'s `ExecStart`) instead of an inline `ExecStart=/bin/sh -c '...'`, removing a layer of systemd + shell quoting around the host_dir path and S3 URI. The `.service` unit is now rendered via the shared `render_systemd_unit` helper. No behavior change.

`S3Volume` is now a thin subclass of the shared `BaseObjectStoreVolume` (in
`mngr_vps.state_bucket_base`), supplying only the boto3 S3 primitives and an
error seam (`_translate_errors` / `_is_not_found` / `_bucket_error_type`); the
listing / existence / read / write / delete logic it duplicated with the Azure
`BlobVolume` now lives on the base. `S3StateBucket`'s `_get_object` /
`_delete_object` / `_prefix_has_objects` likewise moved to `BaseStateBucket`,
leaving the bucket with just its raw S3 primitives and the seam. The 1000-key
`DeleteObjects` batching stays S3-specific. No user-visible behavior change.

`mngr aws prepare` / `cleanup` now resolve their `[providers.<name>]` block and refuse-on-existing-instances via the shared `mngr_vps.cli_helpers`, and `AwsProviderConfig` lifts `allowed_ssh_cidrs` / `associate_public_ip` into shared config bases instead of carrying AWS-local copies. The cleanup refusal when instances still exist now raises the unified `ManagedResourcesExistError` (a `MngrError`) so the message matches the other clouds. The `allowed_ssh_cidrs` type is unchanged for AWS (already `ScalarStrTuple`, now unified across all three clouds); no config key changed.

Further internal dedup against the shared offline layer (no user-visible behavior change): the AWS EC2-tag `HostStateStore` (`_Ec2TagHostStateStore`) is gone in favor of the shared `TagHostStateStore`, with AWS supplying only a `_remove_instance_tags` hook for the EC2 tag-removal call; the `_state_store` selection now comes from the base via new `_bucket_error_type` / `_bucket_label` hooks (`_state_bucket` is unchanged); offline `host_dir` is captured operator-side at `mngr stop` by the shared, cloud-agnostic `BucketHostDirBackend` (no AWS host_dir subclass, no IAM instance profile, no on-box sync daemon); `_list_provider_vps_hostnames` is inherited from the shared base; and `_create_vps_instance` uses the shared `_require_parsed` helper.

Integrated the `mngr/volumes` offline-store simplification (commit `f8bb5c0a5`): the per-agent instance-tag mirror is removed in favor of a single uniform external `HostStateStore` per provider -- AWS/Azure use their object-storage state bucket as the sole offline store (a stopped host's offline metadata now requires the bucket; the provider's `_state_store` raises an actionable `missing_state_bucket_error` pointing at `mngr <cloud> prepare` when the bucket is absent), and GCP uses a lossless instance-metadata-backed store (full host record + one JSON value per agent). AWS/Azure/GCP now extend `OfflineCapableVpsProvider` directly. This supersedes the earlier-on-this-branch tag-mirror dedup (the lifted `TagHostStateStore` / `KeyValueMirrorVpsProvider` / `TagMirrorVpsProvider` are gone); the realizer architecture, the systemd-unit hardening, and the cli/config/state-bucket dedup are retained. No behavior change for container hosts beyond the offline-metadata-requires-bucket consequence noted above.

Bugfix: a running bare (`isolation=NONE`) host is now discoverable and reachable
with the default provider config -- `mngr conn`/`list`/`stop`/`start`/`destroy`
no longer need `-S providers.<name>.isolation=NONE` at connect time. Instances
now carry a `mngr-isolation` tag stamped at create (alongside `mngr-host-id` /
`mngr-provider`), so discovery reads the host's placement from the cloud API
without SSH and probes it with the matching realizer. Pre-existing hosts have no
tag and default to container, preserving prior behavior.

Behavior-preserving dedup against the shared offline layer. The AWS `_state_store` / `_host_dir_backend` cached properties are now thin wrappers over the shared `OfflineCapableVpsProvider._select_bucket_store` / `_select_bucket_host_dir_backend` (supplying only the resolved S3 bucket, its label, and `mngr aws prepare`). The near-identical `_offline_discovered_host_from_instance` is dropped in favor of the shared default; AWS now sets only the `Name` host-name tag key via the new `_host_name_tag_key()` hook. No user-visible behavior change.


Bugfix: `mngr rename` now re-stamps the EC2 `Name` identity tag (read by offline discovery) so a renamed-then-stopped host lists under its new name in `mngr list`. Previously the `Name` tag was stamped only at create, so a host renamed while running still surfaced under its old name once stopped. Implemented via a new `AwsVpsClient.set_instance_tags` (an EC2 `create_tags` upsert) called from the AWS provider's `_remirror_host_name` hook.

Doc: removed a stale README note about speculative EBS `create_snapshot` /
`list_snapshots` / `delete_snapshot` client methods that no longer exist.
