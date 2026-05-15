## AWS provider

- New `aws` provider backend (`mngr_aws`) that runs agents in Docker containers on EC2.
- Credentials resolve via boto3's default chain (env vars, profile, `~/.aws/credentials`, IMDS) with optional explicit fields in `[providers.aws]` config.
- Auto-creates a per-region security group (`mngr-aws` by default) opening tcp/22 and the container SSH port to `allowed_ssh_cidr` (default `0.0.0.0/0` — tighten in prod).
- Per-host EC2 KeyPair via `ImportKeyPair`, deleted on `destroy_host`.
- EC2 instances tagged with `mngr-provider`, `mngr-host-id`, and `mngr-created-at`; discovery filters `DescribeInstances` by `tag:mngr-provider`.
- `InstanceInitiatedShutdownBehavior=terminate` so a self-halted instance is GC'd automatically.
- Release tests double-gated by `MNGR_AWS_RELEASE_TESTS=1` plus credential presence; session-scoped cleanup fixture force-terminates leaked test instances older than 1h.

## VPS Docker shared discovery refactor

- Shared discovery logic (parallel SSH-read across tagged VPSes, cache fallback, name/id lookup) lifted from `VultrProvider` into `VpsDockerProvider`.
- Subclasses now only implement two small extension points: `_get_tagged_vps_ips()` and `_credentials_configured()`. `VultrProvider` and the new `AwsProvider` both consume the shared implementation.

## VPS Docker auto-shutdown TTL

- New optional `auto_shutdown_minutes` field on `VpsDockerProviderConfig`. When set, cloud-init schedules `shutdown -P +N` so the VPS halts itself after the configured number of minutes.
- On AWS, combined with `InstanceInitiatedShutdownBehavior=terminate` (always on), this auto-terminates the EC2 instance — useful as a runaway-cost safety net for ephemeral / test hosts.
- AWS release tests force this to 60 minutes via `MNGR_AWS_AUTO_SHUTDOWN_MINUTES=60` (test-only env-var escape hatch) so instances self-terminate even if pytest is killed before any cleanup runs.
