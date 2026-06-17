from typing import Annotated
from typing import Final
from typing import Literal

import boto3
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.config.data_types import ScalarStrTuple
from imbue.mngr.config.data_types import ScalarTuple
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_vps_docker.config import VpsDockerProviderConfig


class AwsConfigError(MngrError, ValueError):
    """An AWS provider configuration is missing or unresolvable.

    Inherits from ``ValueError`` (so existing ``except ValueError`` call sites --
    e.g. ``AwsProvider.build_provider_instance`` wrapping ``get_session()`` into
    ``ProviderUnavailableError`` -- keep working) and from ``MngrError`` (so it
    renders as a clean CLI error and satisfies the no-bare-builtins ratchet).
    """


class ExistingSecurityGroup(FrozenModel):
    """Use an existing AWS security group by ID; the SG must already permit SSH ingress."""

    kind: Literal["existing"] = "existing"
    id: str = Field(description="EC2 security group ID (e.g. 'sg-0123abcd').")


class AutoCreateSecurityGroup(FrozenModel):
    """Auto-create an AWS security group, opening SSH ingress to the configured CIDRs.

    When ``AwsProviderConfig.allowed_ssh_cidrs`` is empty, the auto-created SG
    gets no ingress rules and the resulting instance will be unreachable from
    outside its VPC; ``ensure_security_group`` logs a warning in that case.
    """

    kind: Literal["auto_create"] = "auto_create"
    name: str = Field(
        default="mngr-aws",
        description="Name used when looking up / creating the security group.",
    )


# Tagged union: either reuse an existing SG by id, or auto-create one by name.
# Discriminator on ``kind`` so pydantic can pick the right concrete type from a
# TOML object without ambiguity.
SecurityGroupSpec = Annotated[
    ExistingSecurityGroup | AutoCreateSecurityGroup,
    Field(discriminator="kind"),
]

DEFAULT_AMI_BY_REGION: Final[dict[str, str]] = {
    # Debian 12 amd64. Fetched via
    #   aws ec2 describe-images --owners 136693071363 \\
    #       --filters Name=name,Values=debian-12-amd64-* Name=architecture,Values=x86_64 \\
    #                 Name=state,Values=available \\
    #       --query 'sort_by(Images, &CreationDate)[-1].ImageId'
    # Periodically validated by ``test_default_amis_describe_successfully``
    # in ``test_release_aws.py``; refresh when that release test starts
    # flagging entries.
    "us-east-1": "ami-05b5db63304a51103",
    "us-east-2": "ami-07863ce80fb4e7190",
    "us-west-1": "ami-07f5877f993ca15f3",
    "us-west-2": "ami-04730af737bd6ef2e",
    "eu-west-1": "ami-049f2bbc51711e7d3",
    "eu-central-1": "ami-0eabf0a4c5d86ddb6",
    "ap-southeast-1": "ami-0728f47e064ce89f5",
    "ap-northeast-1": "ami-084b599f3a2dd0895",
}


class AwsProviderConfig(VpsDockerProviderConfig):
    """Configuration for the AWS EC2 VPS Docker provider.

    Credentials are deliberately not stored in this config. boto3's default
    credential resolution chain (``AWS_*`` env vars, ``~/.aws/credentials``,
    ``~/.aws/config``, EC2 IMDS) is used exclusively. This matches the
    Modal provider convention and the broader project preference: do not
    handle credentials in mngr configs when an SDK can do it for us.
    """

    backend: ProviderBackendName = Field(
        default=ProviderBackendName("aws"),
        description="Provider backend (always 'aws' for this type)",
    )
    default_region: str = Field(
        default="us-east-1",
        description="AWS region for new instances (e.g., 'us-east-1').",
    )
    default_instance_type: str = Field(
        default="t3.small",
        description=(
            "EC2 instance type (e.g., 't3.small' for 2 vCPU, 2GB RAM). "
            "Surfaced as the `--aws-instance-type=` build arg."
        ),
    )
    default_ami_id: str = Field(
        default="",
        description="Explicit AMI override; takes precedence over default_ami_by_region. When empty, default_ami_by_region is consulted for the chosen region.",
    )
    default_ami_by_region: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_AMI_BY_REGION),
        description=(
            "Per-region default AMI IDs, used when default_ami_id is empty. These ship no "
            "GPU / NVIDIA drivers; supply your own AMI via default_ami_id / --aws-ami for "
            "GPU workloads."
        ),
    )
    security_group: SecurityGroupSpec = Field(
        default_factory=AutoCreateSecurityGroup,
        description=(
            "Either {'kind': 'existing', 'id': 'sg-...'} to attach an existing security group, "
            "or {'kind': 'auto_create', 'name': '...'} to auto-create one by name. Default is "
            "auto-create with name 'mngr-aws'. The auto-create path consults allowed_ssh_cidrs."
        ),
    )
    subnet_id: str | None = Field(
        default=None,
        description="Subnet ID. When None, EC2 picks the default-VPC subnet for the AZ.",
    )
    vpc_id: str | None = Field(
        default=None,
        description="VPC ID. Only used to scope auto-created security group lookups.",
    )
    allowed_ssh_cidrs: ScalarStrTuple = Field(
        default=ScalarTuple(("0.0.0.0/0",)),
        description=(
            "Inbound (ingress) CIDRs for tcp/22 and the container SSH port on the "
            "auto-created security group. Default ('0.0.0.0/0',) allows any IP; use e.g. "
            "('203.0.113.4/32',) to restrict, or () for no ingress. A warning is logged "
            "when the effective range is 0.0.0.0/0 or empty."
        ),
    )
    associate_public_ip: bool = Field(
        default=True,
        description=(
            "Assign a public IPv4 address to the instance. Required for the current "
            "mngr-from-developer-laptop SSH access model. For a more secure deployment, "
            "set to False and run mngr from a bastion or via Session Manager."
        ),
    )
    root_volume_size_gb: int = Field(
        default=30,
        description="Size of the root EBS volume in GB.",
    )
    root_volume_type: str = Field(
        default="gp3",
        description="EBS volume type for the root volume (e.g., gp3, gp2, io2).",
    )
    iam_instance_profile: str | None = Field(
        default=None,
        description="Optional IAM instance profile name attached to launched instances.",
    )
    terminate_on_shutdown: bool = Field(
        default=False,
        description=(
            "EC2 shutdown behavior (InstanceInitiatedShutdownBehavior) on an OS shutdown. "
            "False keeps the instance stoppable and resumable via `mngr start` (EBS preserved); "
            "True terminates it (ephemeral / self-cleaning)."
        ),
    )

    def get_session(self) -> boto3.Session:
        """Build a boto3 Session that resolves credentials via boto3's default chain.

        Raises ``AwsConfigError`` (a ``ValueError``) when no credentials are
        resolvable from any source (``AWS_*`` env vars, ``~/.aws/credentials``,
        ``~/.aws/config``, EC2 IMDS). Lets ``botocore.exceptions.BotoCoreError``
        subclasses (e.g., ``ProfileNotFound``) propagate when boto3 itself
        rejects the environment.
        """
        session = boto3.Session(region_name=self.default_region)
        if session.get_credentials() is None:
            raise AwsConfigError(
                "AWS credentials not configured. Set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, "
                "configure ~/.aws/credentials, set AWS_PROFILE, or attach an EC2 instance role."
            )
        return session

    def get_ami_id_for_region(self, region: str) -> str:
        """Return the AMI ID to use for the given region.

        Priority: ``default_ami_id`` (explicit override) > per-region map. Raises
        ``AwsConfigError`` (a ``ValueError``) when neither is set.
        """
        if self.default_ami_id:
            return self.default_ami_id
        ami = self.default_ami_by_region.get(region)
        if ami:
            return ami
        raise AwsConfigError(
            f"No AMI configured for region {region!r}. Set default_ami_id or add an entry to "
            "default_ami_by_region (Debian 12 amd64 AMIs are typically what you want; see the "
            "Debian AMI finder at https://wiki.debian.org/Cloud/AmazonEC2Image)."
        )
