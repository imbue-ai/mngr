## AWS provider

- New `aws` provider backend (`mngr_aws`) that runs agents in Docker containers on EC2.
- Credentials are resolved exclusively via boto3's default chain (`AWS_*` env vars, `~/.aws/credentials`, `~/.aws/config`, EC2 IMDS) — `[providers.aws]` config has no credential fields, matching the Modal provider convention.
- Auto-creates a per-region security group (`mngr-aws` by default) opening tcp/22 and the container SSH port to every CIDR in `allowed_ssh_cidrs`. Default `("0.0.0.0/0",)` matches the de-facto Vultr / OVH norm in this repo (no provider-managed firewall) so behaviour is consistent across providers; tighten for production (e.g. `("203.0.113.4/32",)`) or pre-create the SG. A warning is logged at provision time when the effective CIDR is `0.0.0.0/0`, and when it is empty (in which case the SG ends up with no usable ingress).
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

## Provider backend interface cleanup

- `ProviderBackendInterface.build_provider_instance` no longer carries the Modal-specific `is_for_host_creation` flag. Backends with one-time per-user resources (currently just Modal's environment) override the new `bootstrap_for_host_creation` method; the `mngr create` path calls it before `build_provider_instance`. Other backends (Local, SSH, Docker, AWS, Vultr, OVH, Lima, imbue_cloud) get the default no-op.
- `mngr_aws` adds `boto3-stubs[ec2]` as a dependency so botocore calls are typed instead of `Any`.
- `wait_for_instance_active` lifted onto `VpsClientInterface` as a default method; AWS / Vultr no longer carry the identical polling implementation. A new `slow_provisioning_warning_threshold_seconds` field lets each provider tune the "took longer than usual" warning (90s for AWS, 60s default for Vultr).
- `AwsProvider` raises `ProviderEmptyError` at construction time when credentials or AMIs are unresolvable, matching the Modal pattern (read paths skip the provider instead of constructing a half-working placeholder).
- `AwsVpsClient` no longer carries an `ec2_client` field for test injection; the test-only `_StubbedAwsVpsClient` subclass in `mngr_aws.testing` does that.
- AWS security-group config moved to a tagged union (`security_group: ExistingSecurityGroup | AutoCreateSecurityGroup` keyed on `kind`), replacing the parallel `security_group_id` / `security_group_name` fields.
- `mngr_aws/test_release_aws.py` ships a `test_default_amis_describe_successfully` release test that calls `DescribeImages` on every entry in `DEFAULT_AMI_BY_REGION` so stale AMI IDs surface in CI rather than silently failing host creates.
- After merging `main`, `test_ratchets.py` gains `test_prevent_bare_tmux_targets` and `test_prevent_per_file_host_upload` (the new package was created before `main` added those repo-wide ratchet checks). Test-only.
- `mngr_aws` internal dep pins bumped to match current workspace versions (`imbue-mngr==0.2.12`, `imbue-mngr-vps-docker==0.1.5`). Build metadata only.
