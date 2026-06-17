# Unabridged Changelog - mngr_aws

Full, unedited changelog entries consolidated nightly from individual files in the `changelog/mngr_aws/` directory.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

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
