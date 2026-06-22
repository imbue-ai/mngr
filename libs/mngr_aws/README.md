# mngr AWS Provider [experimental]

AWS provider backend plugin for mngr. Runs agents in Docker containers on Amazon EC2 instances.

> This plugin is **experimental**. The shared `mngr_vps` machinery underneath it is well-tested, but AWS-specific defaults may change. Treat the security defaults (see "AWS-specific configuration") as a starting point: review the security group, AMI choice, and `auto_shutdown_seconds` before pointing this at production resources.

See `mngr_vps` for the base architecture and shared infrastructure.

## Setup

Credentials are resolved via boto3's default chain; they are not configurable in `mngr.toml`. Any of the following works:

- Environment: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (and optional `AWS_SESSION_TOKEN`)
- Named profile: `AWS_PROFILE=my-profile`
- `~/.aws/credentials` / `~/.aws/config`
- IAM instance profile (when running on EC2)

```toml
[providers.aws]
backend = "aws"

default_region = "us-east-1"
default_instance_type = "t3.small"  # EC2 instance type
# default_ami_id = "ami-..."        # optional override; defaults to the pinned per-region AMI

# Optional networking. security_group defaults to auto-create with name 'mngr-aws'.
# To override:
# [providers.aws.security_group]
# kind = "existing"
# id = "sg-..."
# subnet_id = "subnet-..."          # default-VPC subnet if unset
# Inbound CIDRs for tcp/22 and the container SSH port. Default ['0.0.0.0/0'];
# tighten for production.
allowed_ssh_cidrs = ["203.0.113.4/32"]

# Optional EBS sizing
root_volume_size_gb = 30
root_volume_type = "gp3"
```

### Multiple regions

Each provider instance is bound to a single region. To work across regions, configure one instance per region and pick the right one at create time:

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

Stopped agents stay listed and resumable with `mngr start`; a paused agent costs only EBS storage. An idle agent stops its own instance to save cost.

## AWS-specific configuration

These fields extend the base `VpsProviderConfig` (see `mngr_vps`):

<!-- BEGIN GENERATED CONFIG TABLE (scripts/make_cli_docs.py) -->
| Field | Default | Description |
|---|---|---|
| `backend` | `aws` | Provider backend (always 'aws' for this type) |
| `default_region` | `us-east-1` | Default AWS region. |
| `default_instance_type` | `t3.small` | EC2 instance type. Surfaced as the `--aws-instance-type=` build arg. |
| `default_ami_id` | `None` (pinned Debian 12 amd64 per region) | Default AMI ID. When None, the pinned per-region default (DEFAULT_AMI_BY_REGION) is consulted for the chosen region. |
| `security_group` | `AutoCreateSecurityGroup(name="mngr-aws")` | Either {'kind': 'existing', 'id': 'sg-...'} to attach an existing security group, or {'kind': 'auto_create', 'name': '...'} to auto-create one by name. The auto-create path consults allowed_ssh_cidrs. |
| `subnet_id` | `None` | Subnet ID. When None, EC2 picks the default-VPC subnet for the AZ. |
| `vpc_id` | `None` | VPC ID. Only used to scope auto-created security group lookups. |
| `root_volume_size_gb` | `30` | Size of the root EBS volume in GB. |
| `root_volume_type` | `gp3` | EBS volume type for the root volume. |
| `iam_instance_profile` | `None` | Optional IAM instance profile name attached to launched instances. |
| `state_bucket_name` | `None` (auto-derived) | S3 bucket where mngr stores a stopped instance's state so it is readable without starting the instance. When None, named 'mngr-state-<account_id>-<region>'. The bucket is required infrastructure (run `mngr aws prepare`); there is no tag fallback. |
| `is_offline_host_dir_enabled` | `true` | When on (default), a stopped instance's host_dir is readable without starting it, so `mngr event` / `mngr transcript` / `mngr file` work against it. `mngr aws prepare` sets up the access it needs. Set False to turn it off. |
| `terminate_on_shutdown` | `false` | EC2 shutdown behavior (InstanceInitiatedShutdownBehavior) on an OS shutdown. False keeps the instance stoppable and resumable via `mngr start` (EBS preserved); True terminates it (ephemeral / self-cleaning). |
| `allowed_ssh_cidrs` | `("0.0.0.0/0",)` | Inbound CIDR blocks allowed on tcp/22 and the container SSH port in the security group / NSG / firewall rule the provider's `prepare` command creates. Default ('0.0.0.0/0',) allows any IP; use e.g. ('203.0.113.4/32',) to restrict to your own, or () for no ingress (no rule is created, so the instance is unreachable from outside its network). A warning is logged when the effective range is 0.0.0.0/0 or empty. Replaced, not merged, across config layers. |
| `associate_public_ip` | `true` | Assign a public IPv4 address to the instance. Required for the current mngr-from-developer-laptop SSH access model. For a more secure deployment, set to False and run mngr from a bastion inside the network. |
| `auto_shutdown_seconds` | `None` | When set, the host OS halts itself after about this many seconds (rounded up to whole minutes, the granularity `shutdown` accepts) -- a hard max-lifetime cap, distinct from the activity-based default_idle_timeout. Whether the halt stops, terminates, or deletes the instance is provider-specific (see the provider's README). |
<!-- END GENERATED CONFIG TABLE -->

## One-time setup: `mngr aws prepare`

Run this once per region, with credentials that can create security groups, before any developer runs `mngr create --provider aws`:

```bash
mngr aws prepare --region us-east-1
# Or with explicit ingress restriction:
mngr aws prepare --region us-east-1 --allowed-ssh-cidr 203.0.113.4/32
```

`prepare` creates (or reuses) the `mngr-aws` security group in the region and authorizes the configured CIDRs on tcp/22 and the container SSH port. It also idempotently creates a private, encrypted S3 **state bucket** (`mngr-state-<account_id>-<region>` by default; override with `state_bucket_name`) that holds mngr's control-plane state so stopped instances stay listable and resumable offline. The bucket is **required** infrastructure and is `prepare`'s primary job: if the key lacks the S3 / `sts:GetCallerIdentity` permissions, `prepare` fails rather than doing security-group-only setup. The security-group step is read-only when the group already exists with the required ingress.

## Teardown: `mngr aws cleanup`

`mngr aws cleanup` deletes the `mngr-aws` security group and the S3 state bucket, returning the region to its pre-`prepare` state.

```bash
mngr aws cleanup --region us-east-1
```

It refuses (deletes nothing) if any mngr-managed instance still exists in the region, so it can never strand a running agent. Destroy those first with `mngr destroy <agent>`, then re-run. It also refuses to delete the state bucket while it still holds offline host state (orphaned records from hosts that no longer exist as instances); pass `--force` to delete it anyway. It is idempotent.

## Required IAM permissions

For `mngr create --provider aws` (per-host path):

```
ec2:RunInstances, ec2:TerminateInstances, ec2:DescribeInstances,
ec2:StopInstances, ec2:StartInstances,
ec2:CreateTags, ec2:DeleteTags,
ec2:DescribeKeyPairs, ec2:ImportKeyPair, ec2:DeleteKeyPair,
ec2:DescribeSecurityGroups,
ec2:DescribeImages,
s3:PutObject, s3:GetObject, s3:DeleteObject, s3:ListBucket
```

The S3 actions mirror state into the bucket `prepare` created (the bucket must already exist). No `iam:*` actions are required by default; `iam:PassRole` is needed only if you set `iam_instance_profile`. Offline `host_dir` capture needs no extra IAM -- it is uploaded operator-side at `mngr stop` with your own credentials.

For `mngr aws prepare` (one-time admin setup), additionally:

```
ec2:CreateSecurityGroup, ec2:AuthorizeSecurityGroupIngress,
sts:GetCallerIdentity,
s3:CreateBucket, s3:PutBucketPublicAccessBlock,
s3:PutEncryptionConfiguration, s3:PutBucketTagging, s3:ListBucket
```

For `mngr aws cleanup` (teardown), additionally:

```
ec2:DeleteSecurityGroup,
s3:ListBucket, s3:DeleteObject, s3:DeleteBucket
```

Deleting the S3 state bucket additionally uses `s3:ListBucket`, `s3:DeleteObject`, and `s3:DeleteBucket`.

Instance and volume tags are set at launch via `RunInstances` `TagSpecifications`. Only the cheap index tags (`mngr-host-id`, `Name`, `mngr-created-at`) are stamped on the instance, to identify a stopped host during discovery; per-agent metadata lives in the S3 state bucket, not in tags (see the offline-discovery note below). `ec2:StopInstances`/`ec2:StartInstances` back `mngr stop --stop-host` / `mngr start`, so a paused agent costs only EBS storage. `DescribeImages` is needed by the AMI-staleness release test (`test_default_amis_describe_successfully`).

## Implementation details

- Uses boto3 for EC2 API access (no hand-rolled SigV4 signing).
- EC2 instances are tagged with `mngr-provider=<name>`, `mngr-host-id=<id>`, and `mngr-created-at=<iso8601>` for discovery and cleanup-tracking.
- SSH key auth: each host gets a per-host EC2 KeyPair via `ImportKeyPair`, deleted on `destroy_host`.
- Discovery: `DescribeInstances` filtered by `tag:mngr-provider`, then SSH to each VPS to read host records from the state volume.
- Instance shutdown behavior (`InstanceInitiatedShutdownBehavior`) is set from the `terminate_on_shutdown` config field: `stop` by default (an OS shutdown — the idle watcher or the `auto_shutdown_seconds` time cap — stops the instance, leaving it resumable with its EBS volume intact) or `terminate` when `terminate_on_shutdown = true` (an OS shutdown terminates the instance, so a self-halted host is garbage-collected automatically). This is independent of `mngr stop --stop-host`, which uses the `StopInstances` API (not an OS halt) and always leaves the instance recoverable.
- **Stop/resume** (`mngr stop --stop-host` / `mngr start`): the provider overrides `stop_host`/`start_host` to stop and start the EC2 instance via the API, preserving the root EBS volume across the stop. A stopped instance loses its public IPv4, so `start_host` reads the fresh IP, rewrites `vps_ip` in the host record, and re-points known_hosts before restarting the container. For a stable address across stops, see the Elastic IP item under "Future improvements".
- **Self-stopping idle watcher**: an idle agent stops its own EC2 instance (so a paused agent costs only EBS), reusing the in-container activity watcher. A container cannot power off its host, so on idle the in-container `shutdown.sh` *signals* by touching a sentinel file (`stop-instance-requested`) on the shared host volume rather than killing the container. At host finalization the provider installs (on the outer host) a systemd path unit (`mngr-aws-idle-watcher.path`) that watches the outer-filesystem location of that sentinel; when it appears, a paired oneshot service powers the host off with `shutdown -P now`. EC2 then applies the instance's `InstanceInitiatedShutdownBehavior` (`stop`, the default — resumable — or `terminate`; see `terminate_on_shutdown`) to decide whether the poweroff stops or terminates the instance. **No IAM role or awscli is involved** — the watcher never calls the EC2 API. The install is best-effort: if the unit setup fails, finalization logs a warning and proceeds with no auto-stop (manual `mngr stop --stop-host` still works). `mngr start` resumes a self-stopped host exactly as it resumes a `mngr stop --stop-host` host (the self-stop service removes the sentinel before powering off so a resumed instance isn't immediately re-stopped).
- **Offline discovery of stopped hosts**: a stopped instance has no public IP, so it falls out of the SSH-based discovery the base provider uses. To keep paused hosts visible in `mngr list` and resolvable for `mngr start`, mngr mirrors state to the S3 state bucket, and `AwsProvider` reconstructs stopped hosts + their agents from it in `discover_hosts_and_agents` / `to_offline_host`. The full `VpsHostRecord` and each per-agent record are written to the bucket by the mngr host machine (via `persist_agent_data` / `_persist_host_record_externally`), and a stopped host's full record + agents are read back from it (there is no size limit, so an oversized `labels` blob survives a stop). The cheap `mngr-host-id` / `Name` EC2 tags stamped at create are still used to *identify* a stopped instance during discovery, but no per-agent `mngr-agent-*` tags are written. The bucket is **required**, with no tag-mirror fallback: when it has not been provisioned (`prepare` never run), mngr raises an actionable error pointing at `mngr aws prepare` -- on the `mngr create` / `mngr label` write path as well as on offline reads. A transient S3 error on a mirror read or write propagates rather than being swallowed.
- **Offline `host_dir`** (on by default via `is_offline_host_dir_enabled`): **operator-driven**, so it needs no instance IAM identity. At `mngr stop` the operator (mngr, already SSH-connected and holding the bucket credentials) reads the host's `host_dir` off the box and uploads it to `s3://<bucket>/hosts/<host_id>/host_dir/` with **your own** credentials -- the same ones that write the state records. Offline reads (`get_volume_for_host` / `get_volume_reference_for_host`) serve it back from the bucket, upgrading a stopped host to an `OfflineHostWithVolume` so `mngr event` / `mngr transcript` / `mngr file` work while the instance is stopped. An empty `host_dir` prefix (nothing captured yet) reads as no volume. **Limitation:** capture happens only at `mngr stop` -- a host that idle-self-poweroffs (or crashes) is **not** captured, since no operator is involved at that moment and (by design) the box holds no bucket credentials; its offline `host_dir` then reflects its last `mngr stop` (or is empty if never stopped that way). The state *records* are unaffected (always operator-written). Set `is_offline_host_dir_enabled = false` to skip the capture entirely.
- The security group (`mngr-aws` by default) is provisioned out-of-band via `mngr aws prepare` (one-time admin setup) and reused across hosts. `create_host` looks it up read-only and raises a clear "run `mngr aws prepare`" error if missing. It is not deleted on `destroy_host`; run `mngr aws cleanup` to delete it when retiring a provider (it refuses while any mngr-managed instance still exists).
- **No snapshot workflow**: unlike `mngr_modal`, where every sandbox is snapshotted at create time so a hard-killed host can be rehydrated, this provider has no host snapshot workflow today. The AWS client exposes no disk-snapshot surface, so a hard-killed host cannot be rehydrated.
- **Spot capacity via `--aws-spot`**: opt-in (presence-only build arg). When set, the instance launches with `InstanceMarketOptions={"MarketType": "spot"}` and is billed at the spot rate. AWS may reclaim the instance with ~2 minutes' notice; mngr does not currently surface the spot-interruption signal, so the host is terminated cold from mngr's perspective (cloud-init's auto-shutdown safety net still fires correctly). Use for cheap experimental agents, not for long-running production-shaped workloads.

## Release tests and cost

Release tests provision real EC2 instances and cost money. They are double-gated:

```bash
AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \
  MNGR_AWS_RELEASE_TESTS=1 \
  just test libs/mngr_aws/imbue/mngr_aws/test_release_aws.py
```

## Limitations

- No host snapshot workflow: restore from a fresh `mngr create` rather than rehydrating a killed host.
