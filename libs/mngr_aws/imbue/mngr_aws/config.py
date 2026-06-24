from typing import Annotated
from typing import Final
from typing import Literal

import boto3
from botocore.exceptions import BotoCoreError
from botocore.exceptions import ClientError
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_aws.state_bucket import S3StateBucket
from imbue.mngr_vps.config import PublicIpVpsProviderConfig


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


class AwsProviderConfig(PublicIpVpsProviderConfig):
    """Configuration for the AWS EC2 VPS Docker provider.

    Credentials are deliberately not stored in this config. boto3's default
    credential resolution chain (``AWS_*`` env vars, ``~/.aws/credentials``,
    ``~/.aws/config``, EC2 IMDS) is used exclusively. This matches the
    Modal provider convention and the broader project preference: do not
    handle credentials in mngr configs when an SDK can do it for us.
    """

    # Cache for the resolved AWS account id (from sts:GetCallerIdentity), used
    # to derive the default state-bucket name. Cached so repeated bucket
    # resolution does not re-hit STS.
    _cached_account_id: str | None = PrivateAttr(default=None)

    backend: ProviderBackendName = Field(
        default=ProviderBackendName("aws"),
        description="Provider backend (always 'aws' for this type)",
    )
    default_region: str = Field(
        default="us-east-1",
        description="Default AWS region.",
    )
    default_instance_type: str = Field(
        default="t3.small",
        description=("EC2 instance type. Surfaced as the `--aws-instance-type=` build arg."),
    )
    default_ami_id: str | None = Field(
        default=None,
        description=(
            "Default AMI ID. When None, the pinned per-region default (DEFAULT_AMI_BY_REGION) "
            "is consulted for the chosen region."
        ),
    )
    security_group: SecurityGroupSpec = Field(
        default_factory=AutoCreateSecurityGroup,
        description=(
            "Either {'kind': 'existing', 'id': 'sg-...'} to attach an existing security group, "
            "or {'kind': 'auto_create', 'name': '...'} to auto-create one by name. "
            "The auto-create path consults allowed_ssh_cidrs."
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
    root_volume_size_gb: int = Field(
        default=30,
        description="Size of the root EBS volume in GB.",
    )
    root_volume_type: str = Field(
        default="gp3",
        description="EBS volume type for the root volume.",
    )
    iam_instance_profile: str | None = Field(
        default=None,
        description="Optional IAM instance profile name attached to launched instances.",
    )
    state_bucket_name: str | None = Field(
        default=None,
        description=(
            "S3 bucket where mngr stores a stopped instance's state so it is readable without "
            "starting the instance. When None, named 'mngr-state-<account_id>-<region>'. The bucket "
            "is required infrastructure (run `mngr aws prepare`); there is no tag fallback."
        ),
    )
    is_offline_host_dir_enabled: bool = Field(
        default=True,
        description=(
            "When on (default), a stopped instance's host_dir is readable without starting it, so "
            "`mngr event` / `mngr transcript` / `mngr file` work against it. `mngr aws prepare` sets "
            "up the access it needs. Set False to turn it off."
        ),
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

        Priority: ``default_ami_id`` (explicit override) > pinned per-region
        default (``DEFAULT_AMI_BY_REGION``). Raises ``AwsConfigError`` (a
        ``ValueError``) when neither yields an AMI.
        """
        if self.default_ami_id:
            return self.default_ami_id
        ami = DEFAULT_AMI_BY_REGION.get(region)
        if ami:
            return ami
        raise AwsConfigError(
            f"No AMI configured for region {region!r}. Set default_ami_id (Debian 12 amd64 AMIs "
            "are typically what you want; see the Debian AMI finder at "
            "https://wiki.debian.org/Cloud/AmazonEC2Image)."
        )

    def resolve_state_bucket_name(self, session: boto3.Session, region: str | None = None) -> str | None:
        """Return the effective state-bucket name, or None when it can't be resolved.

        ``state_bucket_name`` wins when set. Otherwise derive
        ``mngr-state-<account_id>-<region>`` (lowercased, DNS-valid), resolving
        the account id from ``sts:GetCallerIdentity`` (cached). Returns None when
        the account id can't be fetched (e.g. missing STS permission); the bucket
        is required, so callers turn a None into an actionable "run `mngr aws
        prepare`" error rather than silently proceeding without it.

        ``region`` overrides the region embedded in the derived name (the runtime
        path passes nothing and uses ``default_region``); the operator CLI passes
        the same ``effective_region`` it builds the bucket in, so the name and the
        bucket's actual region always agree.
        """
        if self.state_bucket_name:
            return self.state_bucket_name
        account_id = self._resolve_account_id(session)
        if account_id is None:
            return None
        return f"mngr-state-{account_id}-{region or self.default_region}".lower()

    def _resolve_account_id(self, session: boto3.Session) -> str | None:
        """Return the AWS account id (cached), or None when STS can't be reached."""
        if self._cached_account_id is not None:
            return self._cached_account_id
        try:
            identity = session.client("sts", region_name=self.default_region).get_caller_identity()
        except (ClientError, BotoCoreError) as e:
            logger.warning("Could not resolve AWS account id via sts:GetCallerIdentity: {}", e)
            return None
        account_id = identity.get("Account")
        if not account_id:
            return None
        self._cached_account_id = account_id
        return account_id

    def build_state_bucket(self, session: boto3.Session) -> S3StateBucket | None:
        """Build an ``S3StateBucket`` when a bucket name is configured/derivable, else None."""
        bucket_name = self.resolve_state_bucket_name(session)
        if bucket_name is None:
            return None
        return S3StateBucket(session=session, region=self.default_region, bucket_name=bucket_name)
