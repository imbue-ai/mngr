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
| `default_region` | `us-east-1` | Default AWS region. |
| `default_instance_type` | `t3.small` | EC2 instance type. Surfaced as the `--aws-instance-type=` build arg. |
| `default_ami_id` | `None` (pinned Debian 12 amd64 per region) | Default AMI ID. When None, the pinned per-region default (DEFAULT_AMI_BY_REGION) is consulted for the chosen region. |
| `security_group` | `AutoCreateSecurityGroup(name="mngr-aws")` | Either {'kind': 'existing', 'id': 'sg-...'} to attach an existing security group, or {'kind': 'auto_create', 'name': '...'} to auto-create one by name. The auto-create path consults allowed_ssh_cidrs. |
| `subnet_id` | `None` | Subnet ID. When None, EC2 picks the default-VPC subnet for the AZ. |
| `vpc_id` | `None` | VPC ID. Only used to scope auto-created security group lookups. |
| `allowed_ssh_cidrs` | `("0.0.0.0/0",)` | Inbound (ingress) CIDR blocks allowed on tcp/22 and the container SSH port on the security group / NSG / firewall rule the provider's `prepare` command creates. Default ('0.0.0.0/0',) allows any IP; use e.g. ('203.0.113.4/32',) to restrict to your own IP, or () for no ingress (no rule is created, leaving the instance unreachable from outside its network). A warning is logged when the effective range is 0.0.0.0/0 or empty. Replace-by-default across config layers (combining CIDRs across layers is never the intent). |
| `associate_public_ip` | `True` | Assign a public IPv4 address to the instance. Required for the current mngr-from-developer-laptop SSH access model. For a more secure deployment, set to False and run mngr from a bastion inside the network. |
| `root_volume_size_gb` | `30` | Size of the root EBS volume in GB. |
| `root_volume_type` | `gp3` | EBS volume type for the root volume. |
| `iam_instance_profile` | `None` | Optional IAM instance profile name attached to launched instances. |
| `terminate_on_shutdown` | `false` | EC2 shutdown behavior (InstanceInitiatedShutdownBehavior) on an OS shutdown. False keeps the instance stoppable and resumable via `mngr start` (EBS preserved); True terminates it (ephemeral / self-cleaning). |
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

## Limitations

- No host snapshot workflow: restore from a fresh `mngr create` rather than rehydrating a killed host.
