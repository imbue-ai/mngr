# Changelog - mngr_aws

A concise, human-friendly summary of changes for the `mngr_aws` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New `aws` provider backend (`imbue-mngr-aws`) running mngr agents in Docker containers on EC2. Credentials resolve via boto3's default chain (no credential fields in `[providers.aws]`, matching Modal). Per-region security group is auto-created with ingress configurable via `allowed_ssh_cidrs`. Build args use the `--aws-` prefix (`--aws-region`, `--aws-instance-type`, `--aws-ami`, and the presence-only `--aws-spot` for spot capacity). Root EBS volumes are always encrypted, IMDSv2 is enforced, per-host EC2 KeyPairs are deleted on `destroy_host`, and `InstanceInitiatedShutdownBehavior=terminate` means a self-halted instance is GC'd automatically.
- Added: `mngr aws prepare` and `mngr aws cleanup` CLI commands. `prepare` does the privileged security-group setup as a one-time admin step, so the `mngr create` hot path needs only `ec2:DescribeSecurityGroups`. `cleanup` is the safe inverse (refuses while any mngr-managed instance still exists in the region, so it cannot strand a running agent). Both read defaults from `[providers.aws]` and accept overrides via `--region` / `--sg-name` / `--vpc-id` / `--allowed-ssh-cidr`.
- Added: `allowed_ssh_cidrs` is a replace-by-default `ScalarStrTuple`, so a developer's `settings.local.toml` can tighten the default `("0.0.0.0/0",)` to their own IP without tripping the settings-narrowing guard. A warning logs at provision time when the effective CIDR is `0.0.0.0/0` or empty.
- Added: `auto_shutdown_seconds` on the shared VPS-Docker config (see `mngr_vps_docker`); combined with AWS's always-on `InstanceInitiatedShutdownBehavior=terminate`, EC2 instances auto-terminate from inside after the configured window. AWS pytest-launched instances are tagged `mngr-pytest-launched=true` and a session-end orphan scanner force-terminates any test-tagged instance older than 1h, so a leaked test instance cannot keep billing.

### Changed

- Changed: AWS-provider shared-layer refactor — `is_for_host_creation` removed from `ProviderBackendInterface` and replaced with the default-no-op `bootstrap_for_host_creation` hook (Modal-only override). `AwsProviderBackend.build_provider_instance` raises `ProviderUnavailableError` (not `ProviderEmptyError`) when credentials are unresolvable so a transient auth blip doesn't falsely claim "reached and definitively empty" and hide real hosts from `mngr list` / `connect` / `gc`. AMI resolution moves to create-time (`AwsProvider._create_vps_instance`) so a misconfigured AMI no longer hides already-running instances from read paths.
- Changed: AWS security-group config moved to a tagged union (`security_group: ExistingSecurityGroup | AutoCreateSecurityGroup` keyed on `kind`), replacing the parallel `security_group_id` / `security_group_name` fields.
- Changed: EBS snapshot support is intentionally unwired — `AwsVpsClient.create_snapshot` / `delete_snapshot` / `list_snapshots` now raise `VpsDockerError` with an actionable "EBS snapshot support is not implemented" message (matching `ExternallyManagedVpsClient`). The previous implementation made real `CreateSnapshot` / `DeleteSnapshot` / `DescribeSnapshots` calls but no production code path consumed them; keeping the wiring around invited footguns.
- Changed: `config.get_session()` / `get_ami_id_for_region()` now raise `AwsConfigError(MngrError, ValueError)` instead of a bare `ValueError`, so they render as a clean CLI error and satisfy the no-bare-builtins ratchet (still a `ValueError`, so the wrapping `except` chain is unchanged).
- Changed: A new `test_default_amis_describe_successfully` release test calls `DescribeImages` on every entry in `DEFAULT_AMI_BY_REGION`, so stale AMI IDs surface in CI rather than silently failing host creates.
