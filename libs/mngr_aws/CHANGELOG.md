# Changelog - mngr_aws

A concise, human-friendly summary of changes for the `mngr_aws` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New `aws` provider backend (`imbue-mngr-aws`) running mngr agents in Docker containers on EC2. Credentials resolve via boto3's default chain (no credential fields in `[providers.aws]`, matching Modal). Per-region security group is auto-created with ingress configurable via `allowed_ssh_cidrs`. Build args use the `--aws-` prefix (`--aws-region`, `--aws-instance-type`, `--aws-ami`, and the presence-only `--aws-spot` for spot capacity). Root EBS volumes are always encrypted, IMDSv2 is enforced, per-host EC2 KeyPairs are deleted on `destroy_host`, and `InstanceInitiatedShutdownBehavior=terminate` means a self-halted instance is GC'd automatically.
- Added: `mngr aws prepare` and `mngr aws cleanup` CLI commands. `prepare` does the privileged security-group setup as a one-time admin step, so the `mngr create` hot path needs only `ec2:DescribeSecurityGroups`. `cleanup` is the safe inverse (refuses while any mngr-managed instance still exists in the region, so it cannot strand a running agent). Both read defaults from `[providers.aws]` and accept overrides via `--region` / `--sg-name` / `--vpc-id` / `--allowed-ssh-cidr`.
- Added: `auto_shutdown_seconds` on the shared VPS-Docker config (see `mngr_vps_docker`); combined with AWS's always-on `InstanceInitiatedShutdownBehavior=terminate`, EC2 instances auto-terminate from inside after the configured window.
