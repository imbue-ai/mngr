from typing import Final

import boto3
from botocore.exceptions import BotoCoreError
from pydantic import Field

from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_vps_docker.config import VpsDockerProviderConfig

DEFAULT_AMI_BY_REGION: Final[dict[str, str]] = {
    "us-east-1": "ami-064519b8c76274859",
    "us-east-2": "ami-0a78fdf26eaf90eed",
    "us-west-1": "ami-0cd7c0f3f5b4d6f29",
    "us-west-2": "ami-024c80694b5b3e51a",
    "eu-west-1": "ami-09e3d23a5d8b3a466",
    "eu-central-1": "ami-0a9fa2b8b7a3d9e8a",
    "ap-southeast-1": "ami-038c5add0e8ce40fa",
    "ap-northeast-1": "ami-0d52744d6551d851e",
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
        description="Default AWS region (e.g., 'us-east-1').",
    )
    default_plan: str = Field(
        default="t3.small",
        description="Default EC2 instance type (e.g., 't3.small' for 2 vCPU, 2GB RAM).",
    )
    default_ami_id: str = Field(
        default="",
        description="Default AMI ID. When empty, default_ami_by_region is consulted for the chosen region.",
    )
    default_ami_by_region: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_AMI_BY_REGION),
        description="Per-region default AMI IDs. Used when default_ami_id is empty.",
    )
    security_group_id: str | None = Field(
        default=None,
        description="Existing security group ID to attach. When None, one is auto-created per region.",
    )
    security_group_name: str = Field(
        default="mngr-aws",
        description="Name used when auto-creating the security group.",
    )
    subnet_id: str | None = Field(
        default=None,
        description="Subnet ID. When None, EC2 picks the default-VPC subnet for the AZ.",
    )
    vpc_id: str | None = Field(
        default=None,
        description="VPC ID. Only used to scope auto-created security group lookups.",
    )
    allowed_ssh_cidrs: tuple[str, ...] = Field(
        default=(),
        description=(
            "CIDR blocks allowed inbound on tcp/22 and tcp/<container_ssh_port> on the "
            "auto-created security group. Empty by default (fail-closed): without an explicit "
            "list, ensure_security_group raises rather than create a permissive SG. Use e.g. "
            "['203.0.113.4/32'] to allow only your own IP, or ['0.0.0.0/0'] to expose to the "
            "public internet (NOT recommended for production)."
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

    def get_session(self) -> boto3.Session:
        """Build a boto3 Session that resolves credentials via boto3's default chain.

        Raises ``ValueError`` when no credentials are resolvable from any source
        (``AWS_*`` env vars, ``~/.aws/credentials``, ``~/.aws/config``, EC2
        IMDS). Lets ``botocore.exceptions.BotoCoreError`` subclasses (e.g.,
        ``ProfileNotFound``) propagate when boto3 itself rejects the
        environment.
        """
        session = boto3.Session(region_name=self.default_region)
        if session.get_credentials() is None:
            raise ValueError(
                "AWS credentials not configured. Set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, "
                "configure ~/.aws/credentials, set AWS_PROFILE, or attach an EC2 instance role."
            )
        return session

    def has_resolvable_credentials(self) -> bool:
        """Return True iff boto3's default credential chain can resolve credentials.

        Both the "no credentials at all" ``ValueError`` and boto3's own
        ``BotoCoreError`` subclasses (e.g., ``ProfileNotFound``) are treated
        as unresolvable.
        """
        try:
            self.get_session()
        except (ValueError, BotoCoreError):
            return False
        return True

    def get_ami_id_for_region(self, region: str) -> str:
        """Return the AMI ID to use for the given region.

        Priority: ``default_ami_id`` (explicit override) > per-region map. Raises
        ``ValueError`` when neither is set.
        """
        if self.default_ami_id:
            return self.default_ami_id
        ami = self.default_ami_by_region.get(region)
        if ami is not None and ami:
            return ami
        raise ValueError(
            f"No AMI configured for region {region!r}. Set default_ami_id or add an entry to "
            "default_ami_by_region (Debian 12 amd64 AMIs are typically what you want; see the "
            "Debian AMI finder at https://wiki.debian.org/Cloud/AmazonEC2Image)."
        )
