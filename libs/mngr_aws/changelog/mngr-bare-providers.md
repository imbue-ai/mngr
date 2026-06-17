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

Further internal dedup against the shared offline layer (no user-visible behavior change): the AWS EC2-tag `HostStateStore` (`_Ec2TagHostStateStore`) is gone in favor of the shared `TagHostStateStore`, with AWS supplying only a `_remove_instance_tags` hook for the EC2 tag-removal call; the `_state_store` selection now comes from the base via new `_bucket_error_type` / `_bucket_label` hooks (`_state_bucket` is unchanged); the host_dir-sync daemon install now returns a `HostDirSyncInstallPlan` consumed by the shared `BucketHostDirBackend.install_sync` skeleton; `_list_provider_vps_hostnames` is inherited from the shared base; and `_create_vps_instance` uses the shared `_require_parsed` helper.
