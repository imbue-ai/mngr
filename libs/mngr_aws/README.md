# mngr AWS Provider [experimental]

AWS provider backend plugin for mngr. Runs agents in Docker containers on Amazon EC2 instances.

> This plugin is **experimental** — it has not been exercised in a production setting at the same scale as `mngr_modal` or `mngr_vultr`. The shared `mngr_vps_docker` machinery underneath it is well-tested, but AWS-specific defaults and the IAM permission set may change. Treat the security defaults (see "AWS-specific configuration" below) as a starting point: review the security group, AMI choice, IAM profile, and `auto_shutdown_minutes` before pointing this at production resources.

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
| `iam_instance_profile` | `None` | IAM instance profile name. |
| `auto_shutdown_minutes` | `None` | When set, cloud-init schedules `shutdown -P +N` so the OS halts itself after N minutes. Combined with `InstanceInitiatedShutdownBehavior=terminate` (always on), this auto-terminates the EC2 instance. Leave `None` for normal long-lived behavior; useful for ephemeral test / scratch hosts. |

## One-time setup: `mngr aws prepare`

Run this once, with credentials that can create security groups, before any developer attempts `mngr create --provider aws`:

```bash
mngr aws prepare --region us-east-1
# Or with explicit ingress restriction:
mngr aws prepare --region us-east-1 --allowed-ssh-cidr 203.0.113.4/32
```

`prepare` creates (or reuses) the `mngr-aws` security group in the given region and authorizes the configured CIDRs on tcp/22 and the container SSH port. It needs:

- `ec2:DescribeSecurityGroups`
- `ec2:CreateSecurityGroup`
- `ec2:AuthorizeSecurityGroupIngress`

After `prepare` succeeds, the per-host `mngr create` path only needs the regular RunInstances-style permissions (see the next section); no SG-mutating permissions. This split lets you give devs restricted creds while keeping the privileged setup behind an admin one-shot.

## Teardown: `mngr aws cleanup`

`mngr aws cleanup` is the inverse of `prepare`: it deletes the `mngr-aws` security group so the region returns to its pre-`prepare` state (useful when retiring a provider or testing the first-run experience).

```bash
mngr aws cleanup --region us-east-1
```

It is **safe by design**: it refuses (non-zero exit, deletes nothing) if any mngr-managed instance still exists in the region, so it can never strand a running agent. Destroy those first with `mngr destroy <agent>`, then re-run. It is idempotent -- a no-op when the security group is already gone. It needs `ec2:DescribeInstances`, `ec2:DescribeSecurityGroups`, and `ec2:DeleteSecurityGroup`. It does **not** delete per-host keypairs: those are created and removed by the `mngr create` / `mngr destroy` lifecycle, not by `prepare`.

## Required IAM permissions

For `mngr create --provider aws` (per-host path):

```
ec2:RunInstances, ec2:TerminateInstances, ec2:DescribeInstances,
ec2:StopInstances, ec2:StartInstances,
ec2:DescribeKeyPairs, ec2:ImportKeyPair, ec2:DeleteKeyPair,
ec2:DescribeSecurityGroups,
ec2:DescribeSnapshots, ec2:CreateSnapshot, ec2:DeleteSnapshot,
ec2:DescribeImages
```

For `mngr aws prepare` (one-time admin setup; in addition to the above for convenience):

```
ec2:CreateSecurityGroup, ec2:AuthorizeSecurityGroupIngress
```

For `mngr aws cleanup` (teardown; in addition to the per-host path's `DescribeInstances` / `DescribeSecurityGroups`):

```
ec2:DeleteSecurityGroup
```

Tags are set in the `RunInstances` call via `TagSpecifications`, not via a separate `CreateTags` call. EBS volumes are tagged the same way (no extra permission needed). `mngr stop --stop-host` stops the EC2 **instance** itself (`ec2:StopInstances`) after stopping the inner container, so a paused agent costs only EBS storage; `mngr start` resumes it (`ec2:StartInstances`), preserving the root EBS volume and all on-disk state. `DescribeImages` is needed by the AMI-staleness release test (`test_default_amis_describe_successfully`).

## Implementation details

- Uses boto3 for EC2 API access (no hand-rolled SigV4 signing).
- EC2 instances are tagged with `mngr-provider=<name>`, `mngr-host-id=<id>`, and `mngr-created-at=<iso8601>` for discovery and cleanup-tracking.
- SSH key auth: each host gets a per-host EC2 KeyPair via `ImportKeyPair`, deleted on `destroy_host`.
- Discovery: `DescribeInstances` filtered by `tag:mngr-provider`, then SSH to each VPS to read host records from the state volume.
- Instance shutdown behavior is set to `terminate` so a self-halted instance (e.g. via `auto_shutdown_minutes`) is garbage-collected automatically. This is independent of `mngr stop --stop-host`, which uses the `StopInstances` API (not an OS halt) and so leaves the instance recoverable.
- **Stop/resume** (`mngr stop --stop-host` / `mngr start`): the provider overrides `stop_host`/`start_host` to stop and start the EC2 instance via the API, preserving the root EBS volume across the stop. A stopped instance loses its public IPv4, so `start_host` reads the fresh IP, rewrites `vps_ip` in the host record, and re-points known_hosts before restarting the container. For a stable address across stops, see the Elastic IP item under "Future improvements".
- The security group (`mngr-aws` by default) is provisioned out-of-band via `mngr aws prepare` (one-time admin setup) and reused across hosts. `create_host` looks it up read-only and raises a clear "run `mngr aws prepare`" error if missing. It is not deleted on `destroy_host`; run `mngr aws cleanup` to delete it when retiring a provider (it refuses while any mngr-managed instance still exists).
- **No automatic snapshot-on-create**: unlike `mngr_modal`, where every sandbox is snapshotted at create time so a hard-killed host can be rehydrated, this provider does not snapshot EC2 instances automatically. `AwsVpsClient.create_snapshot` / `list_snapshots` / `delete_snapshot` are implemented; you can call them manually via `mngr snapshot`, or write a plugin that hooks `on_host_created` to do it for you.
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
3. Release tests point `mngr` at a tmp-path settings.toml (via `MNGR_PROJECT_CONFIG_DIR`) that sets `[providers.aws] auto_shutdown_minutes = 60`. This propagates to cloud-init as `shutdown -P +60` on every test instance; combined with `InstanceInitiatedShutdownBehavior=terminate`, the instance auto-terminates 60 minutes after boot even if pytest is killed before any cleanup runs.

Production code enforces this: `AwsProvider._validate_provider_args_for_create` refuses to launch an EC2 instance when `PYTEST_CURRENT_TEST` is set unless `auto_shutdown_minutes` is configured (positive). Mirrors the pattern used by `mngr_modal.backend._create_environment`. Independently, `AwsVpsClient.create_instance` tags every pytest-launched instance with `mngr-pytest-launched=true` (constant `AWS_PYTEST_LAUNCHED_TAG`); the conftest session-end scanner filters on that tag, so leaked test instances are found regardless of the agent / host name shape.

## Future improvements

Tagged `[future]` items are deferred but tracked so the user-facing surface in this README is honest about what does not yet exist:

- `[future]` `mngr aws ami` subcommand that builds and registers a Debian + Docker + deps-baked AMI. Bypasses the ~60-90s cloud-init bootstrap on every create.
- `[future]` mngr-published public AMIs (so users skip the build step entirely). Requires us to commit to a publishing cadence.
- `[future]` GPU AMI automation: the Debian 12 AMIs in `DEFAULT_AMI_BY_REGION` have no CUDA / NVIDIA drivers / nvidia-container-toolkit. Pairs naturally with `mngr aws ami` above.
- `[future]` Optional EIP allocation for stable public addressing across stops/starts. ~$3.60/month per idle EIP.
- `[future]` Auto SSM Parameter Store lookup for current Debian AMIs per region (so the pinned map in `config.py` doesn't drift).
- `[future]` Multi-container per EC2 instance packing.
- `[future]` Auto-cleanup of the `mngr-aws` security group on the final `destroy` of a region.
