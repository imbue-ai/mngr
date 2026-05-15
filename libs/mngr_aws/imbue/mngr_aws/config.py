from typing import Final

import boto3
from botocore.exceptions import BotoCoreError
from pydantic import Field
from pydantic import SecretStr

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
    """Configuration for the AWS EC2 VPS Docker provider."""

    backend: ProviderBackendName = Field(
        default=ProviderBackendName("aws"),
        description="Provider backend (always 'aws' for this type)",
    )
    access_key_id: SecretStr | None = Field(
        default=None,
        description="AWS access key ID. Falls back to AWS_ACCESS_KEY_ID env var or ~/.aws/credentials.",
    )
    secret_access_key: SecretStr | None = Field(
        default=None,
        description="AWS secret access key. Falls back to AWS_SECRET_ACCESS_KEY env var or ~/.aws/credentials.",
    )
    session_token: SecretStr | None = Field(
        default=None,
        description="Optional AWS session token (for STS / SSO).",
    )
    profile: str | None = Field(
        default=None,
        description="Optional named profile from ~/.aws/credentials.",
    )
    default_region: str = Field(
        default="us-east-1",
        description="Default AWS region (e.g., 'us-east-1').",
    )
    default_plan: str = Field(
        default="t3.small",
        description="Default EC2 instance type (e.g., 't3.small' for 2 vCPU, 2GB RAM).",
    )
    default_os_id: int = Field(
        default=0,
        description=(
            "Unused on AWS — kept only to override the Vultr-flavored default (2136) that "
            "the shared VpsDockerProviderConfig carries. AWS selects images by AMI string; "
            "see default_ami_id / default_ami_by_region. Dropping this field from the shared "
            "base would let AWS omit it entirely, but that refactor cascades into "
            "VpsClientInterface.create_instance and is deferred to a follow-up."
        ),
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
    allowed_ssh_cidr: str = Field(
        default="0.0.0.0/0",
        description="CIDR block allowed inbound on tcp/22 and tcp/<container_ssh_port> on the auto-created SG.",
    )
    associate_public_ip: bool = Field(
        default=True,
        description="Assign a public IPv4 address to the instance.",
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
        """Build a boto3 Session using configured credentials or the default credential chain.

        Raises ``ValueError`` if no credentials can be resolved from any source
        (config fields, AWS_* env vars, ~/.aws/credentials, IMDS) or if boto3
        itself rejects the request (e.g., a configured profile name does not
        exist).
        """
        kwargs: dict[str, object] = {}
        if self.access_key_id is not None:
            kwargs["aws_access_key_id"] = self.access_key_id.get_secret_value()
        if self.secret_access_key is not None:
            kwargs["aws_secret_access_key"] = self.secret_access_key.get_secret_value()
        if self.session_token is not None:
            kwargs["aws_session_token"] = self.session_token.get_secret_value()
        if self.profile is not None:
            kwargs["profile_name"] = self.profile
        kwargs["region_name"] = self.default_region

        try:
            session = boto3.Session(**kwargs)
            credentials = session.get_credentials()
        except BotoCoreError as e:
            raise ValueError(f"AWS credentials not resolvable: {e}") from e
        if credentials is None:
            raise ValueError(
                "AWS credentials not configured. Set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, "
                "configure ~/.aws/credentials, or set access_key_id/secret_access_key in the "
                "provider config."
            )
        return session

    def has_resolvable_credentials(self) -> bool:
        """Return True if a credentials chain can be resolved without raising.

        Delegates to ``get_session`` so that the resolution surface matches
        boto3's actual default chain (config fields, env vars, ``~/.aws/credentials``
        / ``~/.aws/config``, instance role / IMDS) and stays in sync with what
        downstream EC2 calls will see.
        """
        try:
            self.get_session()
        except ValueError:
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
