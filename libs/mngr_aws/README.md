# mngr AWS Provider [experimental]

AWS provider backend plugin for mngr. Runs agents in Docker containers on Amazon EC2 instances.

> This plugin is **experimental** — it has not been exercised in a production setting at the same scale as `mngr_modal` or `mngr_vultr`. The shared `mngr_vps_docker` machinery underneath it is well-tested, but AWS-specific defaults may change. Treat the security defaults (see "AWS-specific configuration" below) as a starting point: review the security group, AMI choice, and `auto_shutdown_seconds` before pointing this at production resources.

See `mngr_vps_docker` for the base architecture and shared infrastructure.

## Setup

Credentials are resolved exclusively via boto3's default chain — they are
deliberately not configurable in `mngr.toml` (matching the Modal provider
convention). Any of the following works:

- Environment: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (and optional `AWS_SESSION_TOKEN`)
- Named profile: `AWS_PROFILE=my-profile`
- `~/.aws/credentials` / `~/.aws/config`
- IAM instance profile (when running on EC2)

```toml
[providers.aws]
backend = "aws"

default_region = "us-east-1"
default_instance_type = "t3.small"  # EC2 instance type
default_ami_id = ""                # leave empty to use default_ami_by_region

# Optional networking
# security_group defaults to auto-create with name 'mngr-aws'. To override:
# [providers.aws.security_group]
# kind = "existing"
# id = "sg-..."
# subnet_id = "subnet-..."          # default-VPC subnet if unset
# Inbound CIDRs for tcp/22 and the container SSH port on the auto-created
# security group. Default ['0.0.0.0/0'] matches Vultr/OVH defaults in this
# monorepo (no managed firewall); tighten for production.
allowed_ssh_cidrs = ["203.0.113.4/32"]

# Optional EBS sizing
root_volume_size_gb = 30
root_volume_type = "gp3"
```

### Multiple regions

Each provider instance is bound to a single region (the underlying
`AwsVpsClient` is built with a single boto3 client at construction time).
To work across regions, configure one instance per region and pick the
right one at create time:

```toml
[providers.aws-east]
backend = "aws"
default_region = "us-east-1"
allowed_ssh_cidrs = ["203.0.113.4/32"]

[providers.aws-west]
backend = "aws"
default_region = "us-west-2"
allowed_ssh_cidrs = ["203.0.113.4/32"]
```

```bash
mngr create my-east-agent --provider aws-east
mngr create my-west-agent --provider aws-west
```

## Usage

```bash
mngr create my-agent --provider aws
mngr create my-agent --provider aws -b --aws-instance-type=t3.medium -b --aws-region=us-west-2
mngr create my-agent --provider aws -b --aws-ami=ami-0123abcd456    # per-host AMI override
mngr create my-agent --provider aws -b --aws-spot                    # run on EC2 spot capacity
mngr list
mngr exec my-agent "echo hello"
mngr stop my-agent
mngr start my-agent
mngr destroy my-agent
```

## AWS-specific configuration

These fields extend the base `VpsDockerProviderConfig` (see `mngr_vps_docker`):

| Field | Default | Description |
|-------|---------|-------------|
| `default_region` | `us-east-1` | AWS region for new instances. |
| `default_instance_type` | `t3.small` | EC2 instance type. Surfaced to users as `--aws-instance-type=` build arg (not `--aws-plan=`) to match AWS's native terminology. |
| `default_ami_id` | `""` | Explicit AMI override; takes precedence over the per-region map. |
| `default_ami_by_region` | (pinned Debian 12 amd64 per region) | Per-region default AMIs. |
| `security_group` | `AutoCreateSecurityGroup(name="mngr-aws")` | Tagged union: `{kind = "existing", id = "sg-..."}` to attach an existing SG, or `{kind = "auto_create", name = "..."}` to look up / create one. |
| `subnet_id` | `None` | Optional explicit subnet. |
| `vpc_id` | `None` | Scopes auto-SG lookup. |
| `allowed_ssh_cidrs` | `("0.0.0.0/0",)` | Tuple of inbound CIDRs for tcp/22 and tcp/`container_ssh_port`. Default matches Vultr/OVH default reachability in this repo (no provider-managed firewall). A warning is logged at provision time when the effective range includes `0.0.0.0/0`; tighten for production (e.g. `("203.0.113.4/32",)`). Empty tuple means "add no ingress" — the SG is unreachable from outside its VPC, also warned. |
| `associate_public_ip` | `True` | Assign a public IPv4 to instances. |
| `root_volume_size_gb` | `30` | Root EBS volume size. |
| `root_volume_type` | `gp3` | Root EBS volume type. |
| `iam_instance_profile` | `None` | Optional IAM instance profile name attached to launched instances. |
| `state_bucket_name` | `None` | Name of the S3 bucket holding mngr control-plane state (host record + agent records) for offline reads. When `None`, derived as `mngr-state-<account_id>-<region>`. When a bucket is configured/derivable, the per-agent EC2 tag mirror is dropped in favor of the bucket; without one, mngr falls back to the tag mirror. |
| `is_host_dir_synced_to_bucket` | `True` | Lima-style offline `host_dir` (mirrors Lima's `is_host_data_volume_exposed`). When on (and a state bucket is present), the create path attaches the prepare-provisioned IAM instance profile, an on-box daemon `aws s3 sync`s `host_dir` to `hosts/<host_id>/host_dir/` every ~60s and once on graceful stop, and a stopped host's `host_dir` is read back from the bucket (so `mngr event` / `mngr transcript` work offline). Set `False` to disable the host_dir sync entirely. |
| `terminate_on_shutdown` | `false` | EC2 `InstanceInitiatedShutdownBehavior` on an OS shutdown. `false` → `stop` (resumable via `mngr start`, EBS preserved); `true` → `terminate` (ephemeral / self-cleaning). |
| `auto_shutdown_seconds` | `None` | When set, cloud-init schedules `shutdown -P` so the OS halts itself after about this many seconds (rounded up to whole minutes, the granularity `shutdown` accepts, with a floor of 1 minute). Whether that stops or terminates the instance is governed by `terminate_on_shutdown` (default `stop`, i.e. resumable). A hard max-lifetime cap, distinct from the activity-based `default_idle_timeout`. Leave `None` for normal long-lived behavior; useful for ephemeral test / scratch hosts. |

## One-time setup: `mngr aws prepare`

Run this once, with credentials that can create security groups, before any developer attempts `mngr create --provider aws`:

```bash
mngr aws prepare --region us-east-1
# Or with explicit ingress restriction:
mngr aws prepare --region us-east-1 --allowed-ssh-cidr 203.0.113.4/32
```

`prepare` creates (or reuses) the `mngr-aws` security group in the given region and authorizes the configured CIDRs on tcp/22 and the container SSH port. It additionally creates (idempotently) a private, encrypted S3 **state bucket** that holds mngr's control-plane state -- the full host record and per-agent records -- so a stopped instance's state is readable offline without SSH and without the 256-char EC2 tag-value limit. The bucket is named `mngr-state-<account_id>-<region>` by default (override with `state_bucket_name` on the provider config). Bucket setup is **best-effort**: if the key lacks the S3 / `sts:GetCallerIdentity` permissions, `prepare` logs a warning and continues (the security group is still set up; offline host state then falls back to the EC2 tag mirror).

`prepare` also provisions (idempotently) the **bucket-write IAM identity** -- an IAM role + instance profile assumable by EC2 -- that lets an instance push its `host_dir` to the bucket for the Lima-style offline `host_dir` (on by default; see `is_host_dir_synced_to_bucket`). This is governed by `--use-offline-host-dir {yes,auto,no}` (default `auto`):

```bash
mngr aws prepare --region us-east-1                            # auto: warns + continues if IAM is denied
mngr aws prepare --region us-east-1 --use-offline-host-dir yes  # fail if the identity can't be provisioned
mngr aws prepare --region us-east-1 --use-offline-host-dir no      # bucket only, no IAM identity
```

- `auto` (default): attempt to provision the identity; on a missing-IAM-permission failure, **log a warning and continue** -- the security group + bucket prepare still succeed, and offline `host_dir` is simply unavailable until `prepare` is re-run with sufficient IAM.
- `require`: attempt and **fail the command** (non-zero exit) if the identity cannot be provisioned.
- `skip`: do not attempt the identity at all (bucket-only prepare).

The bucket + identity steps are idempotent, so a later `prepare --use-offline-host-dir yes` after a bucket-only prepare adds just the identity. The role's inline policy is **least-privilege**: only `s3:PutObject` / `s3:GetObject` / `s3:DeleteObject` on the bucket's `hosts/*` prefix and `s3:ListBucket` on the bucket itself -- nothing outside this bucket or outside the `hosts/*` prefix.

It is **read-only-first**: it issues a `DescribeSecurityGroups` call, and when the security group already exists with the required SSH ingress, it returns without any write call. This means a re-run on an already-prepared region succeeds even with a key that only has `ec2:DescribeSecurityGroups` (so callers -- e.g. minds' auto-prepare -- can safely run it before every create regardless of the key's privileges). The write permissions are needed only when something is actually missing:

- `ec2:DescribeSecurityGroups` (always)
- `ec2:CreateSecurityGroup` (only when the group does not exist)
- `ec2:AuthorizeSecurityGroupIngress` (only when a required ingress rule is missing)

After `prepare` succeeds, the per-host `mngr create` path only needs the regular RunInstances-style permissions (see the next section); no SG-mutating permissions. This split lets you give devs restricted creds while keeping the privileged SG setup behind an admin one-shot.

## Teardown: `mngr aws cleanup`

`mngr aws cleanup` is the inverse of `prepare`: it deletes the `mngr-aws` security group so the region returns to its pre-`prepare` state (useful when retiring a provider or testing the first-run experience).

```bash
mngr aws cleanup --region us-east-1
```

It is **safe by design**: it refuses (non-zero exit, deletes nothing) if any mngr-managed instance still exists in the region, so it can never strand a running agent. Destroy those first with `mngr destroy <agent>`, then re-run. It is idempotent -- a no-op when the security group is already gone. It needs only `ec2:DescribeInstances`, `ec2:DescribeSecurityGroups`, and `ec2:DeleteSecurityGroup`. It does **not** delete per-host keypairs: those are created and removed by the `mngr create` / `mngr destroy` lifecycle, not by `prepare`.

`cleanup` also deletes the S3 state bucket. Because the instance check above has already passed, any state left in the bucket is **orphaned** offline state (from hosts that are no longer running instances), so `cleanup` **refuses** to delete a non-empty bucket rather than silently dropping records you may still want -- pass `--force` to delete the bucket and its remaining state. It additionally deletes the bucket-write IAM identity (role + instance profile) provisioned by `prepare`, best-effort and idempotent.

## Required IAM permissions

For `mngr create --provider aws` (per-host path):

```
ec2:RunInstances, ec2:TerminateInstances, ec2:DescribeInstances,
ec2:StopInstances, ec2:StartInstances,
ec2:CreateTags, ec2:DeleteTags,
ec2:DescribeKeyPairs, ec2:ImportKeyPair, ec2:DeleteKeyPair,
ec2:DescribeSecurityGroups,
ec2:DescribeImages
```

`iam:PassRole` is needed at create when a profile is attached: either the optional operator-supplied `iam_instance_profile`, or the bucket-write host identity that `mngr aws prepare` provisions for the offline `host_dir` feature (`is_host_dir_synced_to_bucket`, on by default). With the feature off and no `iam_instance_profile`, no `iam:*` action is required at create.

For `mngr aws prepare` (one-time admin setup; in addition to the above for convenience):

```
ec2:CreateSecurityGroup, ec2:AuthorizeSecurityGroupIngress
```

The S3 state bucket setup additionally uses `sts:GetCallerIdentity` (to derive the bucket name) and `s3:CreateBucket`, `s3:PutBucketPublicAccessBlock`, `s3:PutEncryptionConfiguration`, `s3:PutBucketTagging`, `s3:ListBucket`. These are **best-effort**: missing them downgrades to a warning and a bucket-less prepare (offline state falls back to EC2 tags). The per-host `mngr create` path uses `s3:PutObject` / `s3:GetObject` / `s3:DeleteObject` / `s3:ListBucket` to mirror state when a bucket is present.

Provisioning the bucket-write IAM host identity (for the offline `host_dir` feature) additionally uses `iam:GetInstanceProfile`, `iam:CreateRole`, `iam:PutRolePolicy`, `iam:CreateInstanceProfile`, and `iam:AddRoleToInstanceProfile`. With `--use-offline-host-dir auto` (default), missing these downgrades to a warning (bucket-only prepare; offline `host_dir` unavailable); `--use-offline-host-dir yes` fails the command instead, and `--use-offline-host-dir no` does not touch IAM at all. The per-host `mngr create` path then needs `iam:PassRole` for that instance profile.

For `mngr aws cleanup` (teardown; in addition to the per-host path's `DescribeInstances` / `DescribeSecurityGroups`):

```
ec2:DeleteSecurityGroup
```

Deleting the S3 state bucket additionally uses `s3:ListBucket`, `s3:DeleteObject`, and `s3:DeleteBucket`. Deleting the bucket-write IAM host identity additionally uses `iam:GetInstanceProfile`, `iam:RemoveRoleFromInstanceProfile`, `iam:DeleteInstanceProfile`, `iam:DeleteRolePolicy`, and `iam:DeleteRole` (best-effort).

Instance and volume tags are set at launch via `RunInstances` `TagSpecifications`. After launch, `ec2:CreateTags`/`ec2:DeleteTags` mirror per-agent metadata onto the instance (tags keyed `mngr-agent-<id>`) so a stopped host still lists its agents and resolves by name (see the offline-discovery note below). `ec2:StopInstances`/`ec2:StartInstances` back `mngr stop --stop-host` / `mngr start`, so a paused agent costs only EBS storage. `DescribeImages` is needed by the AMI-staleness release test (`test_default_amis_describe_successfully`).

## Implementation details

- Uses boto3 for EC2 API access (no hand-rolled SigV4 signing).
- EC2 instances are tagged with `mngr-provider=<name>`, `mngr-host-id=<id>`, and `mngr-created-at=<iso8601>` for discovery and cleanup-tracking.
- SSH key auth: each host gets a per-host EC2 KeyPair via `ImportKeyPair`, deleted on `destroy_host`.
- Discovery: `DescribeInstances` filtered by `tag:mngr-provider`, then SSH to each VPS to read host records from the state volume.
- Instance shutdown behavior (`InstanceInitiatedShutdownBehavior`) is set from the `terminate_on_shutdown` config field: `stop` by default (an OS shutdown — the idle watcher or the `auto_shutdown_seconds` time cap — stops the instance, leaving it resumable with its EBS volume intact) or `terminate` when `terminate_on_shutdown = true` (an OS shutdown terminates the instance, so a self-halted host is garbage-collected automatically). This is independent of `mngr stop --stop-host`, which uses the `StopInstances` API (not an OS halt) and always leaves the instance recoverable.
- **Stop/resume** (`mngr stop --stop-host` / `mngr start`): the provider overrides `stop_host`/`start_host` to stop and start the EC2 instance via the API, preserving the root EBS volume across the stop. A stopped instance loses its public IPv4, so `start_host` reads the fresh IP, rewrites `vps_ip` in the host record, and re-points known_hosts before restarting the container. For a stable address across stops, see the Elastic IP item under "Future improvements".
- **Self-stopping idle watcher**: an idle agent stops its own EC2 instance (so a paused agent costs only EBS), reusing the in-container activity watcher. A container cannot power off its host, so on idle the in-container `shutdown.sh` *signals* by touching a sentinel file (`stop-instance-requested`) on the shared host volume rather than killing the container. At host finalization the provider installs (on the outer host) a systemd path unit (`mngr-aws-idle-watcher.path`) that watches the outer-filesystem location of that sentinel; when it appears, a paired oneshot service powers the host off with `shutdown -P now`. EC2 then applies the instance's `InstanceInitiatedShutdownBehavior` (`stop`, the default — resumable — or `terminate`; see `terminate_on_shutdown`) to decide whether the poweroff stops or terminates the instance. **No IAM role or awscli is involved** — the watcher never calls the EC2 API. The install is best-effort: if the unit setup fails, finalization logs a warning and proceeds with no auto-stop (manual `mngr stop --stop-host` still works). `mngr start` resumes a self-stopped host exactly as it resumes a `mngr stop --stop-host` host (the self-stop service removes the sentinel before powering off so a resumed instance isn't immediately re-stopped).
- **Offline discovery of stopped hosts**: a stopped instance has no public IP, so it falls out of the SSH-based discovery the base provider uses. To keep paused hosts visible in `mngr list` and resolvable for `mngr start`, mngr mirrors state to an external store, and `AwsProvider` reconstructs stopped hosts + their agents from it in `discover_hosts_and_agents` / `to_offline_host`.
  - **With an S3 state bucket** (after `mngr aws prepare`): the full `VpsDockerHostRecord` and each per-agent record are written to the bucket by the mngr host machine (via `persist_agent_data` / `_persist_host_record_externally`), and a stopped host's full record + agents are read back from it. There is no size limit, so an oversized `labels` blob survives a stop, and the per-agent EC2 tags are **not written at all** -- removing both the 256-char `labels` drop and the 50-tag-per-instance ceiling.
  - **Without a bucket** (graceful fallback): agent records are mirrored into EC2 tags. Each agent is stored as up to three per-field tags keyed `mngr-agent-<id>-name` / `-type` / `-labels` (the id lives in the key; `name`/`type` raw, `labels` as compact JSON), so `labels` gets the full 256-char value budget and an offline `mngr label` on a stopped host round-trips. A field whose value still overflows 256 chars (realistically only `labels`) is dropped with a warning rather than a silent no-op. EC2 caps a resource at 50 tags; when a host has so many agents that mirroring another would exceed that, `persist_agent_data` raises a `NotImplementedError` (which the CLI turns into an "open an issue" prompt) -- run `mngr aws prepare` to create the bucket (no such limit) as the fix.
- **Offline `host_dir`** (Lima-style, on by default via `is_host_dir_synced_to_bucket`): when a state bucket is present, the create path attaches the `prepare`-provisioned IAM instance profile, then installs (on the outer host, over SSH, best-effort -- never fails create) a systemd oneshot `mngr-aws-host-dir-sync.service` + paired `.timer`. The timer fires every ~60s; the oneshot runs `aws s3 sync <btrfs_mount>/<host_id>/host_dir/ s3://<bucket>/hosts/<host_id>/host_dir/ --delete` using the instance profile's IMDSv2 credentials (no long-lived keys on the box). `awscli` is installed on the outer (apt, guarded so a re-run / baked AMI is a no-op). `stop_host` triggers the same oneshot once before powering the instance off so the offline copy is current. `host_dir` is synced **directly** (not via a btrfs snapshot) -- the container is already stopped at that point, so the tree is quiesced. Offline reads (`get_volume_for_host` / `get_volume_reference_for_host`) serve from the bucket using the **operator's** credentials (no instance identity needed to read), upgrading a stopped host to an `OfflineHostWithVolume` so `mngr event` / `mngr transcript` / `mngr file` work while the instance is stopped. If a host's instance has no attached IAM profile (so it never pushed its `host_dir`), the offline read logs a warning pointing at `mngr aws prepare --use-offline-host-dir yes` rather than silently returning an empty volume.
- The security group (`mngr-aws` by default) is provisioned out-of-band via `mngr aws prepare` (one-time admin setup) and reused across hosts. `create_host` looks it up read-only and raises a clear "run `mngr aws prepare`" error if missing. It is not deleted on `destroy_host`; run `mngr aws cleanup` to delete it when retiring a provider (it refuses while any mngr-managed instance still exists).
- **No snapshot workflow**: unlike `mngr_modal`, where every sandbox is snapshotted at create time so a hard-killed host can be rehydrated, this provider has no host snapshot workflow today. `AwsVpsClient.create_snapshot` / `list_snapshots` / `delete_snapshot` are intentionally unwired -- they raise `VpsDockerError` with an actionable "EBS snapshot support is not implemented in mngr_aws" message rather than running real EBS API calls that nothing else consumes. Restore from a fresh `mngr create` instead.
- **Spot capacity via `--aws-spot`**: opt-in (presence-only build arg). When set, the instance launches with `InstanceMarketOptions={"MarketType": "spot"}` and is billed at the spot rate. AWS may reclaim the instance with ~2 minutes' notice; mngr does not currently surface the spot-interruption signal, so the host is terminated cold from mngr's perspective (cloud-init's auto-shutdown safety net still fires correctly). Use for cheap experimental agents, not for long-running production-shaped workloads.

## Release tests and cost

Release tests provision real EC2 instances and cost money. They are double-gated:

```bash
AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \
  MNGR_AWS_RELEASE_TESTS=1 \
  just test libs/mngr_aws/imbue/mngr_aws/test_release_aws.py
```

Three layers of damage control limit leaks from killed-mid-run tests:

1. Every test's `finally` calls `mngr destroy --force`.
2. A `pytest_sessionfinish` hook in `imbue/mngr_aws/conftest.py` scans for any test-tagged EC2 instance older than 1 hour at session end, force-terminates leaks, and fails the session.
3. Release tests point `mngr` at a tmp-path settings.toml (via `MNGR_PROJECT_CONFIG_DIR`) that sets `[providers.aws] auto_shutdown_seconds = 3600` and `terminate_on_shutdown = true`. This propagates to cloud-init as `shutdown -P +60` on every test instance; combined with `InstanceInitiatedShutdownBehavior=terminate` (from `terminate_on_shutdown = true`), the instance auto-terminates 60 minutes after boot even if pytest is killed before any cleanup runs. (The one resumable-idle-stop test overrides `terminate_on_shutdown = false` so its idle poweroff stops, not terminates, the instance — it relies on the session-end leak scanner to reap any leak.)

Production code enforces this: `AwsProvider._validate_provider_args_for_create` refuses to launch an EC2 instance when `PYTEST_CURRENT_TEST` is set unless `auto_shutdown_seconds` is configured (positive). Mirrors the pattern used by `mngr_modal.backend._create_environment`. Independently, `AwsVpsClient.create_instance` tags every pytest-launched instance with `mngr-pytest-launched=true` (constant `AWS_PYTEST_LAUNCHED_TAG`); the conftest session-end scanner filters on that tag, so leaked test instances are found regardless of the agent / host name shape.

## Future improvements

Tagged `[future]` items are deferred but tracked so the user-facing surface in this README is honest about what does not yet exist:

- `[future]` `mngr aws ami` subcommand that builds and registers a Debian + Docker + deps-baked AMI. Bypasses the ~60-90s cloud-init bootstrap on every create.
- `[future]` mngr-published public AMIs (so users skip the build step entirely). Requires us to commit to a publishing cadence.
- `[future]` GPU AMI automation: the Debian 12 AMIs in `DEFAULT_AMI_BY_REGION` have no CUDA / NVIDIA drivers / nvidia-container-toolkit. Pairs naturally with `mngr aws ami` above.
- `[future]` Optional EIP allocation for stable public addressing across stops/starts. ~$3.60/month per idle EIP.
- `[future]` Auto SSM Parameter Store lookup for current Debian AMIs per region (so the pinned map in `config.py` doesn't drift).
- `[future]` Multi-container per EC2 instance packing.
- `[future]` Auto-cleanup of the `mngr-aws` security group on the final `destroy` of a region.
