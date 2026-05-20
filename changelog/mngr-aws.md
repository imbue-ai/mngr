## AWS provider

- New `aws` provider backend (`mngr_aws`) that runs agents in Docker containers on EC2.
- Credentials are resolved exclusively via boto3's default chain (`AWS_*` env vars, `~/.aws/credentials`, `~/.aws/config`, EC2 IMDS) — `[providers.aws]` config has no credential fields, matching the Modal provider convention.
- Auto-creates a per-region security group (`mngr-aws` by default) opening tcp/22 and the container SSH port to every CIDR in `allowed_ssh_cidrs`. Default is empty (fail-closed): users must explicitly opt in (e.g. `['203.0.113.4/32']` for their own IP) or pre-create the SG.
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
- `AwsProvider` refuses to launch an EC2 instance under pytest if `auto_shutdown_minutes` is unset or non-positive, and `AwsVpsClient.create_instance` refuses if the EC2 `Name` tag does not start with `mngr-test-aws-`. Both mirror the Modal-style guard in `mngr_modal.backend._create_environment` and prevent a test that forgets to override these from silently leaking instances.
