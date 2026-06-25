# Unabridged Changelog - mngr_aws

Full, unedited changelog entries consolidated nightly from individual files in the `changelog/mngr_aws/` directory.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-23

SSH host keys are now unique per host (inherited from the shared VPS provider): each host gets its own VPS/VM-root and container sshd host keypair at create time rather than sharing one keypair across every host the provider instance created. Pause/resume of hosts created before this change still works via a fallback to the legacy provider-global key.

## 2026-06-22

Report an unauthenticated AWS provider consistently with the other cloud providers.

A missing/unresolvable AWS session now raises the shared `ProviderNotAuthorizedError` (still a `ProviderUnavailableError`, so read paths treat it as unavailable). In `mngr list` this surfaces as one consistent error line and a non-zero exit, instead of a one-off message.

## 2026-06-19

Collapsed the AWS provider's two AMI config knobs into one. The `default_ami_by_region` field is gone; `default_ami_id` now defaults to `None`, and when unset the pinned per-region default (`DEFAULT_AMI_BY_REGION`, Debian 12 amd64) for the chosen region is used. Behavior is unchanged -- only the configuration surface is simpler.

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

No production behavior change. The `allowed_ssh_cidrs` narrowing-exemption test now exercises the unified overlay narrowing path (`merge_models_via_overlay`) instead of the removed model-walking `detect_settings_narrowing`. The behavior it locks in is unchanged: a `ScalarStrTuple`-validated `allowed_ssh_cidrs` override replaces the committed default without tripping the cross-scope narrowing guard (the overlay pipeline re-marks the `ScalarTuple` stripped by `model_dump`), while an unmarked plain-tuple override of the same shape still narrows.

Trimmed the README to user-relevant content (removed internal implementation details, release-test instructions, and roadmap notes) and tightened it for concision.

Aligned the `AwsProviderConfig` field descriptions (surfaced via `mngr config` / help) with the README configuration table so the two are consistent.

Fact-checked the README against the collapsed `default_ami_id` (a single nullable field that falls back to the pinned per-region default) and documented the required offline-state S3 bucket that `mngr aws prepare` creates.

Added `test_provider_release_trip1` to the AWS release suite: a single-boot full-lifecycle trip (create, exec, stop, real `--stop-host`, start, persistence, snapshot, out-of-band kill, gc, backend-clean) parametrized over container and bare isolation, built on the shared provider release harness. Also added `test_provider_release_trip3` (snapshot survives destroy); on AWS the docker-commit snapshot is not portable, so the trip asserts that documented divergence (the snapshot is gone after destroy).

Retired the old per-step AWS lifecycle release tests now that the trips supersede them: the container and bare variants of create/exec/destroy, stop/start, `--stop-host`, and the idle-watcher auto-stop tests. The bare-shape check the bare tests owned (the agent shell is the VM's own root -- `/var/lib/mngr-host` present, no `/.dockerenv`) now runs inside Trip 1 for the NONE-isolation parametrization.

Also added `test_provider_release_trip4` (error classification): a no-boot CLI trip asserting `mngr create` with unresolvable AWS credentials surfaces the contract `ProviderUnavailableError`, and that a `--vps-*` build arg is rejected with the migration hint. This PR also fixes the AWS missing-credential help text to point at `aws configure` (and the rest of the boto3 credential chain) instead of the generic "start Docker" guidance; the trip asserts that curated help.

Also added `test_provider_release_trip2` (idle auto-shutdown contract), parametrized over container and bare isolation: it creates an idle host with the `terminate_on_shutdown = false` settings variant so the idle poweroff STOPS (not terminates) the EC2 instance, polls until it is HALTED (billing stops), then resumes via `mngr start` and asserts a pre-shutdown marker survived.

AWS also opts into Trip 1's offline-host_dir read (`supports_offline_host_dir`): with `MNGR_RELEASE_TEST_OFFLINE_HOST_DIR=1`, the trip asserts a stopped host's host_dir marker is served from the S3 state bucket via `mngr file get --relative-to host`.

## 2026-06-18

Replaced the EC2 tag mirror with a required, private, encrypted S3 **state bucket** as the offline store for AWS hosts. A stopped instance's offline host/agent records (so `mngr list` / `mngr start` / `mngr event` work while it is stopped) now live in the bucket instead of `mngr-agent-<id>-*` EC2 tags. This removes the tag mirror's limits -- the silent 256-char `labels` drop and the EC2 50-tag ceiling -- and lets a stopped host's *full* `VpsDockerHostRecord` (config, IP, host keys) be reconstructed rather than a lossy tag subset. The bucket is **required**, with no tag-mirror fallback: mngr raises an actionable error pointing at `mngr aws prepare` when the bucket is absent -- on the `mngr create` / `mngr label` write path as well as on offline reads -- and a transient S3 error on a mirror read/write propagates rather than being swallowed.

- `mngr aws prepare` creates (idempotently) the S3 state bucket, named `mngr-state-<account_id>-<region>` by default or overridable via the new `state_bucket_name` provider config field. The bucket is prepare's primary job: a missing S3/STS permission, an unresolvable bucket name, or any create failure fails the command.

- `mngr aws cleanup` deletes the state bucket. Since it runs after the refuse-while-instances-exist check, any remaining state is orphaned (hosts no longer present as instances), so cleanup refuses to delete a non-empty bucket rather than silently dropping records; pass the new `--force` flag to delete the bucket and its remaining state.

Added an offline `host_dir`, **on by default** (new `is_offline_host_dir_enabled` provider config field). A stopped instance's `host_dir` is now readable without SSH, so `mngr event` / `mngr transcript` / `mngr file` work against a paused host.

- Capture is **operator-driven** -- it needs no instance IAM identity. At `mngr stop`, mngr (already SSH-connected and holding the bucket credentials) reads the host's `host_dir` off the box and uploads it to `s3://<bucket>/hosts/<host_id>/host_dir/` with the operator's own credentials (the same ones that write the state records). So `mngr aws prepare` provisions no host-dir IAM identity, `mngr create` attaches no profile for it, `cleanup` deletes none, and `iam:PassRole` is needed only for an operator-supplied `iam_instance_profile`. Offline reads serve `host_dir` back from the bucket.

- Limitation: capture happens only at `mngr stop`. A host that idle-self-poweroffs (or crashes) is **not** captured -- its offline `host_dir` then reflects its last `mngr stop` (or is empty if never stopped that way); the state *records* are unaffected (always operator-written). An empty `host_dir` prefix reads as no volume. Set `is_offline_host_dir_enabled = false` to disable the capture entirely.

Fixed `mngr destroy` of a stopped AWS host leaking its EC2 instance. Destroying a host that had been stopped (`mngr stop --stop-host`, or idle self-stop) previously failed to terminate the still-billing EC2 instance and left its S3 state behind while appearing to succeed. Destroy now falls back to the offline path -- resolving the stopped instance by its `mngr-host-id` tag and terminating it via `TerminateInstances` -- and removes the state-bucket records, failing loudly if the instance could not be terminated.

A partial S3 `DeleteObjects` failure (the API returns HTTP 200 with per-key failures only in the response `Errors` array) now raises instead of being silently dropped, so a failed state/`host_dir` removal can't leave orphaned objects behind unnoticed.

Follow-up cleanup: removed the now-orphaned `AwsVpsClient.add_tags` / `AwsVpsClient.remove_tags` client methods (and their unit tests). They only ever existed to push per-agent records into EC2 instance tags for the old tag mirror, which the state bucket replaces; nothing reachable called them.

`mngr aws prepare` is now idempotent under a concurrent `prepare` race: a `BucketAlreadyOwnedByYou` from the bucket create (two prepares racing, or the existence check racing the create) is treated as a no-op -- mngr still applies the bucket's (idempotent) hardening config and reports it as not-created -- rather than surfacing as an error. Any other create failure still raises.

## 2026-06-17

Internal: AWS's stopped-host offline discovery and resolution (listing stopped / mid-stop hosts, resolving them by id, and falling back to EC2 tags), plus its stop/start lifecycle, known_hosts rebinding, and idle-watcher install, now come from a shared `OfflineCapableVpsDockerProvider` base instead of AWS-specific copies; AWS supplies only the EC2-specific hooks (stop/start the instance, poweroff idle action). No behavior change.

## 2026-06-16

AWS agents now have a Modal-like idle-paused-but-resumable lifecycle: `mngr stop --stop-host` stops the EC2 **instance** itself (not just the inner Docker container), so a paused agent costs only EBS storage, and `mngr start` resumes it with the root EBS volume and all on-disk state intact. A stopped host still shows in `mngr list` (with its agents) and resolves by name for `mngr start`.

Under the hood:

- `AwsVpsClient` gained `stop_instance` (StopInstances, waits for the terminal `stopped` state), `start_instance` (StartInstances, waits for `running`, returns the fresh public IP), and `add_tags`/`remove_tags`.

- `AwsProvider` overrides `stop_host`/`start_host`: stop stops the container then the instance and records `stop_reason=STOPPED`; start locates the stopped instance by its `mngr-host-id` tag (it isn't SSH-reachable), starts it, and rebinds the host record + known_hosts to the instance's new public IP before restarting the container. Resolve-by-name stays stable across the EC2 stop transition: offline discovery reconstructs an instance that is `stopping` as well as fully `stopped` (so a host doesn't briefly vanish from `mngr list` / `mngr start` mid-stop), and `start_instance` waits out a `stopping` instance before issuing `start-instances` (which AWS rejects until the instance is `stopped`).

- Because a stopped instance has no public IP and drops out of SSH-based discovery, agent records are mirrored into EC2 tags as they are created/updated, and `AwsProvider` reconstructs stopped hosts and their agents from tags in discovery / `to_offline_host`. This keeps paused hosts visible and resumable by name. Each agent is stored as up to three per-field tags (`mngr-agent-<id>-name`/`-type`/`-labels`). EC2 caps a resource at 50 tags, so a host with very many agents can run out of mirror space; rather than failing obscurely, `mngr create` then raises a `NotImplementedError` that prompts you to open an issue (an S3-backed store for many-agent hosts is the planned fix).

- New per-host EC2 permissions: `ec2:StopInstances`, `ec2:StartInstances`, `ec2:CreateTags`, `ec2:DeleteTags`.

The self-stopping idle watcher is now live: an idle AWS agent stops its own EC2 instance (so a paused agent costs only EBS storage), the Modal-style idle-pause analog. It needs **no IAM role and no awscli** -- the watcher powers the host off rather than calling the EC2 API. A container cannot power off its host, so on idle the in-container `shutdown.sh` touches a sentinel file (`stop-instance-requested`) on the shared host volume rather than killing the container. At host finalization, `mngr create` installs (on the outer host) a systemd path unit (`mngr-aws-idle-watcher.path`) that watches the outer-filesystem location of that sentinel and, when it appears, runs a oneshot service that powers the host off with `shutdown -P now`. EC2 then applies the instance's `InstanceInitiatedShutdownBehavior` to decide whether that stops or terminates the instance. The install degrades gracefully: if the unit install fails, finalization logs a warning and proceeds with no auto-stop -- `mngr stop --stop-host` still works -- and `mngr start` resumes a self-stopped host the same way it resumes any stopped host.

A new `terminate_on_shutdown` config field controls the stop-vs-terminate behavior (default `false` -> `stop`, i.e. resumable idle-pause; `true` -> `terminate`, ephemeral / self-cleaning); it governs both the idle watcher's poweroff and the `auto_shutdown_seconds` time cap. The tradeoff: without an IAM role the watcher can only power off, so an instance is either resumable-on-idle (`stop`, the default) or instance-autonomously self-terminating (`terminate`), not both. The existing `iam_instance_profile` config field remains as an operator escape hatch for attaching your own role.

`mngr aws prepare` and `mngr aws cleanup` are **security-group-only** -- no IAM provisioning, since idle self-stop needs none. `prepare` needs just `ec2:DescribeSecurityGroups`/`CreateSecurityGroup`/`AuthorizeSecurityGroupIngress`; `cleanup` just `ec2:DescribeInstances`/`DescribeSecurityGroups`/`DeleteSecurityGroup`.

Offline `mngr label` on a stopped AWS host persists: the agent's `labels` are stored in their own `mngr-agent-<id>-labels` tag (so they get the full 256-char tag-value budget rather than sharing it with id/name/type), and reassembled on discovery. Labels too large for a single tag are dropped with a warning rather than silently no-op'ing.

For security, `start_host` rebinds `known_hosts` for the instance's new IP from mngr's locally-held host keypairs (injected into the instance at create), not from EC2 tags -- account-writable tags must not be a source of SSH host-key trust. Offline discovery also tolerates a malformed `mngr-host-id`/`Name` tag (it skips that instance with a warning rather than aborting the whole `mngr list` sweep), and resolving an instance by `mngr-host-id` refuses an ambiguous duplicate-tag match rather than acting on the first one.

## AWS provider

- The AWS release-test settings now also disable the `azure` provider (`[providers.azure] is_enabled = false`), mirroring the existing modal/gcp/vultr/ovh disables. Without it, `mngr list` inside the AWS lifecycle tests would enumerate the newly-added azure provider and exit non-zero when Azure credentials weren't resolvable in that subprocess, failing the AWS tests for a non-AWS reason (the same gap that was already fixed for gcp).

- `mngr aws prepare` / `mngr aws cleanup` group their AWS-specific options under a "Provider" option group, so `--help` and the generated docs list them ahead of the shared common options instead of below them.

Removed the dead VPS client methods `create_snapshot`, `delete_snapshot`, `list_snapshots`, and `list_ssh_keys` (and the now-unused `_snapshots_unavailable` helper) from `AwsVpsClient`. These had no production callers and are being dropped from the shared `VpsClientInterface`. The corresponding unit and release tests were removed as well.


The `mngr_aws` README's snapshot note now states the AWS client exposes no EBS-snapshot surface (rather than naming the removed `create_snapshot` / `list_snapshots` / `delete_snapshot` methods).

## 2026-06-15

- `mngr aws prepare` and `mngr aws cleanup` now respect `--format`. Previously they ignored it: success was logged to stderr and the bare security-group id was echoed to stdout regardless of format. They now emit a single result line in `human` mode, a structured object in `json` mode (`{security_group_id, region, created}` for prepare; `{security_group_id, region, deleted}` for cleanup), and a `prepared` / `cleaned_up` event in `jsonl` mode. The `created` / `deleted` booleans let a caller distinguish a first-run create from an idempotent no-op (`created` is also `False` for a caller-supplied `ExistingSecurityGroup`, which prepare never creates).

- The wide-open-CIDR warning is shorter: `mngr aws prepare` with `0.0.0.0/0` ingress now logs just "auto-created security group '<name>' will permit SSH from the public internet." (the trailing dev-vs-production advice sentence was dropped).

- Removed the unexplained `time.sleep(20)` settle-cushion after `mngr destroy --force` in the two lifecycle release tests (`test_release_aws.py`). The sleep was the last statement of each test and masked no race -- nothing runs after it except the TTL-gated session-end leak scanner. The `time_sleep` ratchet for this project is tightened 2 -> 0 accordingly.

- The AWS release tests now disable the `gcp` provider in their `settings.toml` (alongside the modal/vultr/ovh/imbue_cloud disables that were already there). Without this, once the GCP provider was added in this branch, `mngr list` inside the AWS lifecycle tests enumerated GCP and exited non-zero when GCP credentials were not resolvable in that subprocess, failing the AWS tests for a reason unrelated to AWS. The GCP release tests already disable `aws` symmetrically.

## 2026-06-14

`mngr aws prepare` is now read-only-first: when the `mngr-aws` security group already exists with the required SSH ingress, it returns without issuing any write API call. A re-run on an already-prepared region therefore succeeds with a key that only has `ec2:DescribeSecurityGroups`; the `ec2:CreateSecurityGroup` / `ec2:AuthorizeSecurityGroupIngress` permissions are needed only when the group or a rule is actually missing. This lets callers safely run `prepare` before every create regardless of the key's privileges.

## 2026-06-12

Declared the AWS provider's `allowed_ssh_cidrs` as a replace-by-default field so a
developer's `settings.local.toml` can tighten it to their own IP without tripping the
settings-narrowing guard.

The default is now a non-empty `["0.0.0.0/0"]`, so a higher-precedence config layer that
set `allowed_ssh_cidrs` to a specific CIDR used to be rejected as "narrowing" (silently
dropping `0.0.0.0/0`), which broke every `mngr` command for anyone who tightened the value
-- exactly the security-conscious case. `allowed_ssh_cidrs` is now typed `ScalarStrTuple`,
a tuple that the narrowing guard treats as a single scalar value (combining CIDRs across
config layers is never the intent), so a local override cleanly replaces the default.

## AWS provider

- New `aws` provider backend (`mngr_aws`) that runs agents in Docker containers on EC2.
- Credentials are resolved exclusively via boto3's default chain (`AWS_*` env vars, `~/.aws/credentials`, `~/.aws/config`, EC2 IMDS) — `[providers.aws]` config has no credential fields, matching the Modal provider convention.
- Auto-creates a per-region security group (`mngr-aws` by default) opening tcp/22 and the container SSH port to every CIDR in `allowed_ssh_cidrs`. Default `("0.0.0.0/0",)` matches the de-facto Vultr / OVH norm in this repo (no provider-managed firewall) so behaviour is consistent across providers; tighten for production (e.g. `("203.0.113.4/32",)`) or pre-create the SG. A warning is logged at provision time when the effective CIDR is `0.0.0.0/0`, and when it is empty (in which case the SG ends up with no usable ingress).
- `mngr aws prepare` and `mngr aws cleanup` now read defaults from the user's resolved `[providers.NAME]` block in settings.toml (default_region, vpc_id, security_group.name, allowed_ssh_cidrs), with a new `--provider NAME` option (default `aws`) selecting which block to load. Previously both commands constructed a fresh `AwsProviderConfig()` and read class-level field defaults, so a user with a non-default `default_region` in `[providers.aws]` running `mngr aws prepare` without `--region` would land the SG in `us-east-1` while the runtime `mngr create --provider aws` path looked elsewhere -- the SG and the create path never met. Both commands now run through `setup_command_context` (the standard mngr CLI entry point), so the user's settings.toml is loaded the same way every other command loads it. CLI flags (`--region` / `--sg-name` / `--vpc-id` / `--allowed-ssh-cidr`) still override the resolved config; when the named provider block does not exist (or points at a non-AWS backend), class defaults are used as a graceful fallback for first-run users.

- New `mngr aws prepare` CLI command (registered via `register_cli_commands` hookimpl) does the privileged SG setup as a one-time admin step. The hot path in `AwsVpsClient.create_instance` now uses a lookup-only `resolve_security_group_id()` that needs only `ec2:DescribeSecurityGroups`, so developers can run `mngr create --provider aws` with restricted IAM (no `CreateSecurityGroup` / `AuthorizeSecurityGroupIngress`). Missing-SG errors point users at `mngr aws prepare`. A `[future]`-tagged `mngr aws ami` stub command is also registered so the planned AMI-build command's name is reserved and discoverable.

- New `mngr aws cleanup` CLI command, the safe inverse of `prepare`: it deletes the `mngr-aws` security group so a region returns to its pre-`prepare` state (handy when retiring a provider or testing the first-run experience). It refuses (deletes nothing) while any mngr-managed instance still exists in the region, so it cannot strand a running agent, and is idempotent (a no-op when the SG is already gone). Needs `ec2:DescribeInstances` + `ec2:DescribeSecurityGroups` + `ec2:DeleteSecurityGroup`. Does not touch per-host keypairs (those follow the create/destroy lifecycle, not `prepare`). Backed by new `AwsVpsClient.delete_security_group()` and `list_mngr_managed_instances()`.
- **AWS build args use the `--aws-` prefix**: `--aws-region=`, `--aws-instance-type=` (not `--aws-plan=` -- "instance type" is what AWS docs / console call it), and `--aws-ami=` for per-host AMI override. `AwsProviderConfig.default_plan` renamed to `default_instance_type` to match. The old `--vps-region=` / `--vps-plan=` raise a migration error pointing at the new names.
- **Per-host AMI override**: `--aws-ami=<ami-id>` on `mngr create` flows through `ParsedAwsBuildOptions(ParsedVpsBuildOptions)` and a new `_create_vps_instance` provider hook to `AwsVpsClient.create_instance`'s new optional `ami_id_override` kwarg. The shared `VpsClientInterface.create_instance` contract is unchanged: the override lives on the AWS-specific client signature and is reached via `self.aws_client.create_instance(...)` in `AwsProvider._create_vps_instance`. Other providers ignore the seam entirely. Falls back to `default_ami_id` / `default_ami_by_region` when omitted.
- **Spot capacity opt-in**: presence-only `--aws-spot` build arg. Flows through `ParsedAwsBuildOptions.spot: bool` and the same `_create_vps_instance` hook to `AwsVpsClient.create_instance`'s new `spot: bool = False` kwarg, which conditionally sets `InstanceMarketOptions={"MarketType": "spot"}` on RunInstances. AWS may reclaim the instance with ~2 minutes' notice; reclaim terminates (does not stop) the host, and the cloud-init auto-shutdown safety net still fires. Opt-in only -- safe for ephemeral / experimental agents, risky for long-lived ones. A new `extract_presence_flag` helper joins the composable parser kit so future boolean flags (e.g. `--aws-eip` when the destroy-path lifecycle work lands) follow the same shape.
- Every EC2 instance launched with `Encrypted: True` on the root EBS volume (guaranteed regardless of account default-encryption setting) and IMDSv2 enforced (`HttpTokens: required`, `HttpPutResponseHopLimit: 1`) so the instance metadata service is unreachable from a hostile container.
- Per-host EC2 KeyPair via `ImportKeyPair`, deleted on `destroy_host`.
- EC2 instances tagged with `mngr-provider`, `mngr-host-id`, and `mngr-created-at`; discovery filters `DescribeInstances` by `tag:mngr-provider`.
- `InstanceInitiatedShutdownBehavior=terminate` so a self-halted instance is GC'd automatically.
- Release tests double-gated by `MNGR_AWS_RELEASE_TESTS=1` plus credential presence; Modal-style `pytest_sessionfinish` hook in `libs/mngr_aws/imbue/mngr_aws/conftest.py` scans for any test-tagged EC2 instance older than 1h at session end, force-terminates leaks, and fails the session.

## VPS Docker shared interface cleanup

- Shared discovery logic (parallel SSH-read across tagged VPSes, cache fallback, name/id lookup) lifted from `VultrProvider` into `VpsDockerProvider`. Subclasses implement two small extension points: `_list_provider_vps_hostnames()` and `_credentials_configured()`.
- Dropped `os_id` from `VpsClientInterface.create_instance`, `ParsedVpsBuildOptions`, `VpsHostConfig`, and `VpsDockerProviderConfig`. The shared interface no longer carries a Vultr-specific image-selection field.
- Vultr now stores `os_id` on `VultrVpsClient` itself (from `VultrProviderConfig.default_os_id`). The `--vps-os=` per-host build arg is removed; users set the OS via `default_os_id` on the provider config.

## VPS Docker auto-shutdown TTL

- New optional `auto_shutdown_minutes` field on `VpsDockerProviderConfig`. When set, cloud-init schedules `shutdown -P +N` so the VPS halts itself after the configured number of minutes.
- On AWS, combined with `InstanceInitiatedShutdownBehavior=terminate` (always on), this auto-terminates the EC2 instance — useful as a runaway-cost safety net for ephemeral / test hosts.
- AWS release tests set this to 60 minutes via a tmp-path `settings.toml` pointed at by `MNGR_PROJECT_CONFIG_DIR`, so instances self-terminate even if pytest is killed before any cleanup runs.
- The session-scoped `mngr aws prepare` fixture now isolates `HOME` + `MNGR_HOST_DIR` (and points `MNGR_PROJECT_CONFIG_DIR` at an opted-in `settings.toml`) so the subprocess doesn't load the developer's real mngr profile, which lacks `is_allowed_in_pytest` and the pytest guard rejects. Previously this fixture passed only in CI (no profile) and failed on developer machines. AWS credentials are frozen into the subprocess env before the HOME swap so boto3 still authenticates. The per-test and prepare settings now share a `_write_release_settings` helper. (Backported from the GCP provider's equivalent fix.)
- `AwsProvider` refuses to launch an EC2 instance under pytest if `auto_shutdown_minutes` is unset or non-positive (in `AwsProvider._validate_provider_args_for_create`), mirroring the Modal-style guard in `mngr_modal.backend._create_environment` so a test that forgets to override that field cannot silently leak instances. Independently, `AwsVpsClient.create_instance` tags every EC2 instance launched while `PYTEST_CURRENT_TEST` is set with `mngr-pytest-launched=true` (constant `AWS_PYTEST_LAUNCHED_TAG`); the conftest session-end orphan scanner filters on that tag, so leaked test instances are found regardless of the agent / host name shape.

## Provider backend interface cleanup

- `ProviderBackendInterface.build_provider_instance` no longer carries the Modal-specific `is_for_host_creation` flag. Backends with one-time per-user resources (currently just Modal's environment) override the new `bootstrap_for_host_creation` method; the `mngr create` path calls it before `build_provider_instance`. Other backends (Local, SSH, Docker, AWS, Vultr, OVH, Lima, imbue_cloud) get the default no-op.
- `mngr_aws` adds `boto3-stubs[ec2]` as a dependency so botocore calls are typed instead of `Any`.
- `wait_for_instance_active` lifted onto `VpsClientInterface` as a default method; AWS / Vultr no longer carry the identical polling implementation. A new `slow_provisioning_warning_threshold_seconds` field lets each provider tune the "took longer than usual" warning (90s for AWS, 60s default for Vultr).
- `AwsProviderBackend.build_provider_instance` raises `ProviderUnavailableError` (not `ProviderEmptyError`) when credentials are unresolvable. Per the `ProviderEmptyError` / `ProviderUnavailableError` contract in `mngr.errors`, an auth blip means the backend's state is *unknown* (running instances may still exist), so `Unavailable` is the right shape; `Empty` would falsely claim "reached and definitively empty" and silently hide real hosts from `mngr list` / `connect` / `gc`. The shared discovery loop's generic catch-all already logs `ProviderUnavailableError` at error level, so no backend-side warning is needed and no `bootstrap_for_host_creation` override is needed either -- the create path calls `build_provider_instance` first and inherits the same failure shape. Matches the Azure pattern. AMI selection is a create-only concern: `build_provider_instance` does not touch it, so a misconfigured AMI no longer hides already-running instances from read paths. AMI resolves just-in-time inside `AwsProvider._create_vps_instance` (the only create-path site), preserving the per-host `--aws-ami=` override flow. Missing AMI at create time raises a plain `MngrError` ("No AMI configured for region X. Set default_ami_id or add ..."); the create flow's existing `create_host` except handler cleans up any SSH key uploaded before the raise, so no leak. `AwsVpsClient.ami_id` becomes optional with an empty default; the production path always supplies an override and `create_instance` refuses to run without one.

- **EBS snapshot support is now intentionally unwired.** `AwsVpsClient.create_snapshot` / `delete_snapshot` / `list_snapshots` raise `VpsDockerError` with an actionable "EBS snapshot support is not implemented in mngr_aws" message (matching the shape used by `ExternallyManagedVpsClient`). The previous implementation made real `CreateSnapshot` / `DeleteSnapshot` / `DescribeSnapshots` calls but no production code path consumed them; keeping the wiring around invited footguns (e.g. a future caller silently creating real EBS snapshots they did not mean to). The `_get_root_volume_id` helper goes with them. Tests were rewritten as `pytest.raises(VpsDockerError, ...)` smoke tests; the release-test `test_api_client_list_snapshots_does_not_error` is dropped.
- `AwsVpsClient` no longer carries an `ec2_client` field for test injection; the test-only `_StubbedAwsVpsClient` subclass in `mngr_aws.testing` does that.
- AWS security-group config moved to a tagged union (`security_group: ExistingSecurityGroup | AutoCreateSecurityGroup` keyed on `kind`), replacing the parallel `security_group_id` / `security_group_name` fields.
- `mngr_aws/test_release_aws.py` ships a `test_default_amis_describe_successfully` release test that calls `DescribeImages` on every entry in `DEFAULT_AMI_BY_REGION` so stale AMI IDs surface in CI rather than silently failing host creates.
- After merging `main`, `test_ratchets.py` gains `test_prevent_bare_tmux_targets` and `test_prevent_per_file_host_upload` (the new package was created before `main` added those repo-wide ratchet checks). Test-only.
- `mngr_aws` internal dep pins bumped to match current workspace versions (`imbue-mngr==0.2.12`, `imbue-mngr-vps-docker==0.1.5`). Build metadata only.

- **Review-feedback cleanups.** `config.get_session()` / `get_ami_id_for_region()` now raise a new `AwsConfigError(MngrError, ValueError)` instead of a bare `ValueError` -- it still IS a `ValueError`, so the `except (ValueError, BotoCoreError)` that wraps `get_session()` into `ProviderUnavailableError` is unchanged, but it renders as a clean CLI error and satisfies the no-bare-builtins ratchet. The `allowed_ssh_cidrs` config docstring was rewritten to make clear it controls **inbound** (security-group ingress) on tcp/22 + the container SSH port (egress is untouched). The single-use `_force_terminate_instances` test helper was inlined into its one caller. A FIXME in `_parse_build_args` now enumerates the AWS config knobs that could become `--aws-*` build args but are not yet wired up.
