# mngr AWS Provider [experimental]

AWS provider backend plugin for mngr. Runs agents in Docker containers on Amazon EC2 instances.

> This plugin is **experimental**. The shared `mngr_vps_docker` machinery underneath it is well-tested, but AWS-specific defaults may change. Treat the security defaults (see "AWS-specific configuration") as a starting point: review the security group, AMI choice, and `auto_shutdown_seconds` before pointing this at production resources.

See `mngr_vps_docker` for the base architecture and shared infrastructure.

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
default_ami_id = ""                # leave empty to use default_ami_by_region

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

These fields extend the base `VpsDockerProviderConfig` (see `mngr_vps_docker`):

| Field | Default | Description |
|-------|---------|-------------|
| `default_region` | `us-east-1` | AWS region for new instances (e.g., `us-east-1`). |
| `default_instance_type` | `t3.small` | EC2 instance type (e.g., `t3.small` for 2 vCPU, 2GB RAM). Surfaced as the `--aws-instance-type=` build arg. |
| `default_ami_id` | `""` | Explicit AMI override; takes precedence over the per-region map. |
| `default_ami_by_region` | (pinned Debian 12 amd64 per region) | Per-region default AMIs. These ship no GPU / NVIDIA drivers; supply your own AMI via `default_ami_id` / `--aws-ami` for GPU workloads. |
| `security_group` | `AutoCreateSecurityGroup(name="mngr-aws")` | Tagged union: `{kind = "existing", id = "sg-..."}` to attach an existing SG, or `{kind = "auto_create", name = "..."}` to look up / create one. |
| `subnet_id` | `None` | Optional explicit subnet. |
| `vpc_id` | `None` | Scopes auto-SG lookup. |
| `allowed_ssh_cidrs` | `("0.0.0.0/0",)` | Inbound CIDRs for tcp/22 and tcp/`container_ssh_port`. Default is open to the internet; a warning is logged at provision time, so tighten for production. An empty tuple adds no ingress, leaving the SG unreachable from outside its VPC. |
| `associate_public_ip` | `True` | Assign a public IPv4 to instances. |
| `root_volume_size_gb` | `30` | Root EBS volume size. |
| `root_volume_type` | `gp3` | Root EBS volume type. |
| `iam_instance_profile` | `None` | Optional IAM instance profile name attached to launched instances. |
| `terminate_on_shutdown` | `false` | EC2 shutdown behavior on an OS shutdown. `false` keeps the instance stoppable and resumable (EBS preserved); `true` terminates it (ephemeral / self-cleaning). |
| `auto_shutdown_seconds` | `None` | When set, the instance halts itself after about this many seconds (a hard max-lifetime cap, distinct from the activity-based idle timeout). Whether that stops or terminates it follows `terminate_on_shutdown`. Useful for ephemeral test / scratch hosts. |

## One-time setup: `mngr aws prepare`

Run this once per region, with credentials that can create security groups, before any developer runs `mngr create --provider aws`:

```bash
mngr aws prepare --region us-east-1
# Or with explicit ingress restriction:
mngr aws prepare --region us-east-1 --allowed-ssh-cidr 203.0.113.4/32
```

`prepare` creates (or reuses) the `mngr-aws` security group in the region and authorizes the configured CIDRs on tcp/22 and the container SSH port. It is read-only when the group already exists with the required ingress, so it is safe to re-run before every create even with a describe-only key.

## Teardown: `mngr aws cleanup`

`mngr aws cleanup` deletes the `mngr-aws` security group, returning the region to its pre-`prepare` state.

```bash
mngr aws cleanup --region us-east-1
```

It refuses (deletes nothing) if any mngr-managed instance still exists in the region, so it can never strand a running agent. Destroy those first with `mngr destroy <agent>`, then re-run. It is idempotent.

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

No `iam:*` actions are required by default; `iam:PassRole` is needed only if you set `iam_instance_profile`.

For `mngr aws prepare` (one-time admin setup), additionally:

```
ec2:CreateSecurityGroup, ec2:AuthorizeSecurityGroupIngress
```

For `mngr aws cleanup` (teardown), additionally:

```
ec2:DeleteSecurityGroup
```

## Limitations

- No host snapshot workflow: restore from a fresh `mngr create` rather than rehydrating a killed host.
