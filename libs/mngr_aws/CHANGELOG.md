# Changelog - mngr_aws

A concise, human-friendly summary of changes for the `mngr_aws` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: Bare placement (`isolation=NONE`) — the agent runs directly on the EC2 instance with no Docker container, reached at `vps_ip:22` as root. Idle agent runs `shutdown -P now`, which stops the EC2 instance via `InstanceInitiatedShutdownBehavior`. Bare release tests added.
- Added: SSH host keys are unique per host — each AWS host gets its own VPS/VM-root and container sshd host keypair at create time (previously shared across every host a provider instance created). `mngr create --format json` surfaces them.
- Added: Required, private, encrypted S3 **state bucket** as the offline store for AWS hosts (replaces the EC2 tag mirror). A stopped instance's full `VpsHostRecord` (config, IP, host keys) plus per-agent records live in the bucket. `mngr aws prepare` creates it (default name `mngr-state-<account_id>-<region>`, overridable via `state_bucket_name`); `mngr aws cleanup` deletes it (refuses non-empty unless `--force`). A missing bucket fails fast with an actionable error pointing at `mngr aws prepare`.
- Added: Offline `host_dir` on AWS, on by default (new `is_offline_host_dir_enabled` provider config field). A stopped instance's `host_dir` is now readable without SSH, so `mngr event` / `mngr transcript` / `mngr file` work against a paused host. Capture is operator-driven at `mngr stop` (uploads to `s3://<bucket>/hosts/<host_id>/host_dir/` with the operator's own credentials — no instance IAM identity needed).
- Added: A running bare AWS host is discoverable with the default provider config — a `mngr-isolation` tag stamped at create lets discovery resolve placement from the cloud API without SSH, so operations no longer need `-S providers.<name>.isolation=NONE` at connect time.

### Changed

- Changed: Unauthenticated AWS now raises the shared `ProviderNotAuthorizedError` (still a `ProviderUnavailableError`). In `mngr list` this surfaces as one consistent error line and a non-zero exit, instead of a one-off message.
- Changed: Collapsed the AWS AMI config knobs — `default_ami_by_region` removed; `default_ami_id` now defaults to `None` and uses the pinned per-region default (`DEFAULT_AMI_BY_REGION`, Debian 12 amd64) when unset. Resolution behavior is unchanged.
- Changed: AWS missing-credential help text now points at `aws configure` and the rest of the boto3 credential chain instead of generic "start Docker" guidance.
- Changed: AWS cleanup refusal when instances still exist now raises the unified `ManagedResourcesExistError` so the message matches the other clouds.
- Changed: Host-side idle-watcher systemd unit renamed from `mngr-aws-idle-watcher` to the shared `mngr-idle-watcher` as the idle-watcher install lifted into the shared `OfflineCapableVpsProvider`.

### Fixed

- Fixed: `mngr destroy` of a stopped AWS host no longer leaks its EC2 instance. The offline destroy path resolves the stopped instance by its `mngr-host-id` tag and terminates it via `TerminateInstances`, removing the state-bucket records — failing loudly if termination could not complete.
- Fixed: `mngr rename` now re-stamps the EC2 `Name` identity tag (read by offline discovery), so a renamed-then-stopped host lists under its new name.
- Fixed: A partial S3 `DeleteObjects` failure (HTTP 200 with per-key failures in the response `Errors` array) now raises instead of being silently dropped, so a failed state/`host_dir` removal can't leave orphaned objects behind.
- Fixed: `mngr aws prepare` is now idempotent under a concurrent prepare race — a `BucketAlreadyOwnedByYou` is treated as a no-op (mngr still applies the bucket's idempotent hardening config).

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
