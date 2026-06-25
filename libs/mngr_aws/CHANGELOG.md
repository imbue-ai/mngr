# Changelog - mngr_aws

A concise, human-friendly summary of changes for the `mngr_aws` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: Bare placement (`isolation=NONE`): the agent runs directly on the EC2 VM (no Docker container), reached as root over the same VPS keypair. The idle agent runs `shutdown -P now` to stop the instance via InstanceInitiatedShutdownBehavior. The `mngr-isolation` instance tag stamped at create lets discovery resolve a stopped bare host's placement from the cloud API without SSH.

### Changed

- Changed: Replaced the EC2 tag mirror with a required, private, encrypted S3 **state bucket** as the offline store. A stopped instance's full `VpsHostRecord` (config, IP, host keys) and per-agent records live in the bucket instead of `mngr-agent-<id>-*` EC2 tags -- removing the silent 256-char `labels` drop and the EC2 50-tag ceiling. The bucket is required: mngr raises an actionable error pointing at `mngr aws prepare` when it is absent. `mngr aws prepare` creates the bucket (default name `mngr-state-<account_id>-<region>`, overridable via `state_bucket_name`); `mngr aws cleanup` deletes it and refuses to delete a non-empty bucket unless `--force`.

- Changed: Added an offline `host_dir` on by default (`is_offline_host_dir_enabled`). A stopped instance's `host_dir` is now readable without SSH so `mngr event` / `mngr transcript` / `mngr file` work against a paused host. Capture is operator-driven at `mngr stop` and uses the operator's own credentials (no instance IAM profile, no `iam:PassRole`). Limitation: a host that idle-self-poweroffs or crashes is not captured.

- Changed: Collapsed the AWS provider's two AMI config knobs. `default_ami_by_region` is gone; `default_ami_id` now defaults to `None` and falls back to a pinned per-region default (`DEFAULT_AMI_BY_REGION`, Debian 12 amd64) when unset.

- Changed: `mngr aws prepare` is now idempotent under a concurrent prepare race (a `BucketAlreadyOwnedByYou` is treated as a no-op).

- Changed: Renamed the package to `mngr_vps` (was `mngr_vps_docker`); the AWS provider follows shared base classes whose names dropped "Docker" (`VpsProvider`, `VpsHostRecord`, etc.). Import-only.

### Removed

- Removed: The orphaned `AwsVpsClient.add_tags` / `AwsVpsClient.remove_tags` client methods that only ever existed to push per-agent records into EC2 instance tags for the old tag mirror.

### Fixed

- Fixed: `mngr destroy` of a stopped AWS host no longer leaks its EC2 instance. Destroying a host that had been stopped (`mngr stop --stop-host` or idle self-stop) previously failed to terminate the still-billing instance while reporting success. Destroy now falls back to the offline path, resolving the instance by its `mngr-host-id` tag and terminating via `TerminateInstances`, then removes the state-bucket records, failing loudly if termination could not be carried out.

- Fixed: `mngr rename` now re-stamps the EC2 `Name` identity tag that offline discovery reads, so a host renamed while running lists under its new name once stopped.

## [v0.1.4] - 2026-06-18

### Changed

- Changed: AWS's stopped-host offline discovery/resolution and its stop/start lifecycle, known_hosts rebinding, and idle-watcher install now come from the shared `OfflineCapableVpsDockerProvider` base; AWS supplies only the EC2-specific hooks (stop/start the instance, poweroff idle action). No behavior change.

## [v0.1.3] - 2026-06-16

### Added

- Added: AWS agents now have a Modal-like idle-paused-but-resumable lifecycle: `mngr stop --stop-host` stops the EC2 instance itself (not just the inner container), so a paused agent costs only EBS storage, and `mngr start` resumes it with the root EBS volume and all on-disk state intact. A stopped host still shows in `mngr list` (with its agents) and resolves by name for `mngr start`. Agent records are mirrored into per-resource EC2 tags as they're created/updated; `AwsProvider` reconstructs stopped hosts and their agents from tags during discovery. New per-host EC2 permissions: `ec2:StopInstances`, `ec2:StartInstances`, `ec2:CreateTags`, `ec2:DeleteTags`.
- Added: Self-stopping idle watcher — an idle AWS agent stops its own EC2 instance (Modal-style idle-pause analog) with no IAM role and no awscli. An in-container `shutdown.sh` touches a sentinel on the shared host volume on idle; an outer-host systemd path unit installed at finalization watches it and powers the host off with `shutdown -P now`. EC2 then applies the instance's `InstanceInitiatedShutdownBehavior` to decide stop vs terminate. New `terminate_on_shutdown` config field controls the choice (default `false` → resumable idle-pause; `true` → instance-autonomously self-terminating); without an IAM role, an instance is one or the other, not both.
- Added: Offline `mngr label` on a stopped AWS host persists — the agent's `labels` are stored in their own `mngr-agent-<id>-labels` tag (full 256-char value budget) and reassembled on discovery; labels too large for a single tag are dropped with a warning rather than silently no-op'ing.

### Changed

- Changed: `mngr aws prepare` and `mngr aws cleanup` are now security-group-only (no IAM provisioning, since idle self-stop needs none). `prepare` needs just `ec2:DescribeSecurityGroups`/`CreateSecurityGroup`/`AuthorizeSecurityGroupIngress`; `cleanup` just `ec2:DescribeInstances`/`DescribeSecurityGroups`/`DeleteSecurityGroup`.
- Changed: `start_host` rebinds `known_hosts` for the instance's new IP from mngr's locally-held host keypairs (injected into the instance at create), not from EC2 tags — account-writable tags must not be a source of SSH host-key trust. Offline discovery tolerates a malformed `mngr-host-id`/`Name` tag (skips that instance with a warning rather than aborting the whole sweep), and resolving an instance by `mngr-host-id` refuses an ambiguous duplicate-tag match.

## [v0.1.2] - 2026-06-16

### Changed

- Changed: `mngr aws prepare` and `mngr aws cleanup` now respect `--format`, emitting a structured `{security_group_id, region, created/deleted}` object in `json` mode and a `prepared`/`cleaned_up` event in `jsonl` mode; the `created`/`deleted` booleans let a caller distinguish a first-run create from an idempotent no-op.
- Changed: Shortened the wide-open-CIDR warning emitted by `mngr aws prepare` with `0.0.0.0/0` ingress (the trailing dev-vs-production advice sentence was dropped).

## [v0.1.1] - 2026-06-15

### Changed

- Changed: `mngr aws prepare` is now read-only-first: when the `mngr-aws` security group already exists with the required SSH ingress, it returns without any write API call. A re-run on an already-prepared region therefore succeeds with a key that only has `ec2:DescribeSecurityGroups`; `ec2:CreateSecurityGroup` / `ec2:AuthorizeSecurityGroupIngress` are only needed when the group or a rule is actually missing. Lets callers safely run `prepare` before every create regardless of the key's privileges.

## [v0.1.0] - 2026-06-13

### Added

- Added: New `aws` provider backend (`imbue-mngr-aws`) running mngr agents in Docker containers on EC2. Credentials resolve via boto3's default chain (no credential fields in `[providers.aws]`, matching Modal). Per-region security group is auto-created with ingress configurable via `allowed_ssh_cidrs`. Build args use the `--aws-` prefix (`--aws-region`, `--aws-instance-type`, `--aws-ami`, and the presence-only `--aws-spot` for spot capacity). Root EBS volumes are always encrypted, IMDSv2 is enforced, per-host EC2 KeyPairs are deleted on `destroy_host`, and `InstanceInitiatedShutdownBehavior=terminate` means a self-halted instance is GC'd automatically.
- Added: `mngr aws prepare` and `mngr aws cleanup` CLI commands. `prepare` does the privileged security-group setup as a one-time admin step, so the `mngr create` hot path needs only `ec2:DescribeSecurityGroups`. `cleanup` is the safe inverse (refuses while any mngr-managed instance still exists in the region, so it cannot strand a running agent). Both read defaults from `[providers.aws]` and accept overrides via `--region` / `--sg-name` / `--vpc-id` / `--allowed-ssh-cidr`.
- Added: `auto_shutdown_seconds` on the shared VPS-Docker config (see `mngr_vps_docker`); combined with AWS's always-on `InstanceInitiatedShutdownBehavior=terminate`, EC2 instances auto-terminate from inside after the configured window.
