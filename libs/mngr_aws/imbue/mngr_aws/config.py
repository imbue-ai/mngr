from collections.abc import Mapping
from collections.abc import Sequence
from typing import Annotated
from typing import Any
from typing import Final
from typing import Literal

import boto3
import botocore.session
from botocore.exceptions import BotoCoreError
from botocore.exceptions import ClientError
from botocore.exceptions import ProfileNotFound
from botocore.exceptions import SSOError
from botocore.exceptions import TokenRetrievalError
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr
from pydantic import model_validator

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_aws.boto_config import AWS_BOTO_CONFIG
from imbue.mngr_aws.boto_config import IMDS_CREDENTIAL_PROVIDER_NAME
from imbue.mngr_aws.state_bucket import S3StateBucket
from imbue.mngr_vps.config import PublicIpVpsProviderConfig
from imbue.mngr_vps.config import fold_legacy_root_disk_size_key


class AwsConfigError(MngrError, ValueError):
    """An AWS provider configuration is missing or unresolvable.

    Inherits from ``ValueError`` (so existing ``except ValueError`` call sites --
    e.g. ``AwsProvider.build_provider_instance`` wrapping ``get_session()`` into
    ``ProviderUnavailableError`` -- keep working) and from ``MngrError`` (so it
    renders as a clean CLI error and satisfies the no-bare-builtins ratchet).
    """

    # The concise command that resolves the failure, when there is one (e.g.
    # ``aws sso login --profile X``); the backend lifts it onto the provider error.
    short_remediation: str | None

    def __init__(self, message: str, short_remediation: str | None = None) -> None:
        self.short_remediation = short_remediation
        super().__init__(message)


class AwsProfileNotFoundError(AwsConfigError):
    """The named ``aws_profile`` does not exist in this machine's ``~/.aws/config`` or credentials file."""


class AwsProfileUnsupportedError(AwsConfigError):
    """The named ``aws_profile`` needs an interactive prompt mngr cannot answer (an ``mfa_serial``)."""


class AwsProfileCredentialsError(AwsConfigError):
    """The named ``aws_profile`` exists but its credentials could not be resolved (an expired SSO login, a refused assume-role)."""


class AwsProfileSsoLoginError(AwsProfileCredentialsError):
    """The named ``aws_profile``'s single-sign-on login is missing or expired on this machine (``aws sso login`` fixes it)."""


# The botocore errors that mean a profile's single-sign-on token is missing or
# expired, which ``aws sso login`` fixes. Everything else a profile can fail with
# is reported with the generic remediation.
_SSO_TOKEN_ERRORS: Final[tuple[type[BotoCoreError], ...]] = (SSOError, TokenRetrievalError)


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

# Debian's official AWS publisher account and the name patterns of its Debian 13
# "trixie" images, keyed by the EC2 architecture name ``DescribeInstanceTypes``
# reports. The default AMI is the newest matching image in the target region for
# the instance type's architecture, resolved at create time
# (``AwsVpsClient.resolve_default_ami_id``) rather than pinned by id: Debian
# deprecates old AMIs a few months after each point release, so a pinned id
# eventually stops launching, while the newest image is always launchable and
# carries the current security fixes.
DEBIAN_AMI_OWNER_ID: Final[str] = "136693071363"
DEBIAN_AMI_NAME_PATTERN_BY_ARCHITECTURE: Final[Mapping[str, str]] = {
    "x86_64": "debian-13-amd64-*",
    "arm64": "debian-13-arm64-*",
}


@pure
def pick_ami_architecture(supported_architectures: Sequence[str]) -> str | None:
    """The EC2 architecture to resolve a Debian AMI for, given an instance type's supported architectures.

    Prefers ``x86_64`` when an instance type supports several (Debian publishes
    no ``i386`` images), then ``arm64``; None when Debian publishes nothing for
    any of them.
    """
    for architecture in DEBIAN_AMI_NAME_PATTERN_BY_ARCHITECTURE:
        if architecture in supported_architectures:
            return architecture
    return None


@pure
def pick_newest_ami_id(images: Sequence[Mapping[str, Any]]) -> str | None:
    """The ``ImageId`` of the most recently created image in a ``DescribeImages`` response, or None if empty.

    ``CreationDate`` is an ISO-8601 UTC timestamp, so string order is
    chronological order.
    """
    if not images:
        return None
    newest = max(images, key=lambda image: str(image.get("CreationDate", "")))
    image_id = newest.get("ImageId")
    return str(image_id) if image_id else None


class AwsProviderConfig(PublicIpVpsProviderConfig):
    """Configuration for the AWS EC2 VPS Docker provider.

    Credentials resolve in three ways. By default this config stores none and
    boto3's default credential chain (``AWS_*`` env vars, ``~/.aws/credentials``,
    ``~/.aws/config``, EC2 IMDS) is used exclusively -- the Modal/GCP/Azure
    convention. When explicit ``aws_access_key_id`` + ``aws_secret_access_key``
    are set (the Imbue Studio "bring your own account" paste flow), they take
    precedence and are handed straight to ``boto3.Session``; the ambient chain
    is bypassed. Mirrors the OVH/Vultr providers, which also hold ``SecretStr``
    key material for exactly this reason. When ``aws_profile`` is set instead,
    that named profile of this machine's ``~/.aws/config`` is used, so the
    block itself carries no secret; the two are mutually exclusive.
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
            "Default AMI ID. When None, the newest Debian 13 AMI Debian publishes in the chosen "
            "region for the instance type's architecture (amd64 or arm64) is resolved at create "
            "time via ec2:DescribeInstanceTypes and ec2:DescribeImages."
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
    root_volume_size_gb: int | None = Field(
        default=None,
        description="Deprecated alias of root_disk_size_gb (the root EBS volume size in GB); set root_disk_size_gb instead.",
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
    use_ec2_instance_metadata: bool = Field(
        default=False,
        description=(
            "Whether to consult the EC2 instance-metadata service (IMDS, 169.254.169.254) as a "
            "credential source. Off by default: on a host that is not an EC2 instance, IMDS is "
            "frequently routed-but-blackholed, so probing it blocks on the OS TCP connect timeout "
            "(tens of seconds) instead of failing fast, which can stall discovery. Set True only "
            "when running mngr on an EC2 instance that should authenticate via its attached IAM "
            "instance role."
        ),
    )
    aws_access_key_id: SecretStr | None = Field(
        default=None,
        description=(
            "Explicit AWS access key id. When set together with aws_secret_access_key, these are "
            "passed directly to boto3 and take precedence over the ambient credential chain. Used by "
            "the Imbue Studio bring-your-own-account paste flow. Leave unset to use the ambient chain."
        ),
    )
    aws_secret_access_key: SecretStr | None = Field(
        default=None,
        description="Explicit AWS secret access key. See aws_access_key_id.",
    )
    aws_session_token: SecretStr | None = Field(
        default=None,
        description=(
            "Optional AWS session token, for temporary (STS/SSO) credentials. Only used when "
            "aws_access_key_id / aws_secret_access_key are set."
        ),
    )
    aws_profile: str | None = Field(
        default=None,
        description=(
            "Name of a profile in this machine's ~/.aws/config (and ~/.aws/credentials) to authenticate with, "
            "so the block carries no key material. Mutually exclusive with aws_access_key_id / "
            "aws_secret_access_key. The profile's own region is ignored (default_region applies), and a "
            "profile with an mfa_serial is refused because it would prompt on stdin. Leave unset to use the "
            "ambient chain."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _fold_legacy_root_volume_size(cls, raw_config: Any) -> Any:
        return fold_legacy_root_disk_size_key(raw_config, "root_volume_size_gb")

    def get_session(self) -> boto3.Session:
        """Build a boto3 Session that resolves credentials via boto3's default chain.

        By default the EC2 instance-metadata (IMDS) credential provider is removed
        from the chain (see ``use_ec2_instance_metadata``), so a non-EC2 host fails
        fast instead of hanging on a blackholed ``169.254.169.254`` probe. All
        other sources (``AWS_*`` env vars, ``~/.aws/credentials``, ``~/.aws/config``,
        SSO, etc.) are unaffected.

        Raises ``AwsConfigError`` (a ``ValueError``) when no credentials are
        resolvable from any (enabled) source. Lets ``botocore.exceptions.BotoCoreError``
        subclasses (e.g., ``ProfileNotFound``) propagate when boto3 itself rejects
        the environment.

        When explicit ``aws_access_key_id`` + ``aws_secret_access_key`` are set,
        they are passed straight to ``boto3.Session`` and the ambient chain is
        bypassed entirely (no IMDS surgery, no ``AwsConfigError``).

        When ``aws_profile`` is set, the session is scoped to that named profile
        and its credentials are resolved eagerly (see ``_profile_session``), so a
        missing profile, an unsupported one, or an expired login is reported
        here rather than mid-discovery.
        """
        access_key = self.aws_access_key_id.get_secret_value() if self.aws_access_key_id else None
        secret_key = self.aws_secret_access_key.get_secret_value() if self.aws_secret_access_key else None
        if bool(access_key) != bool(secret_key):
            # Half-pasted credentials must not silently fall back to the ambient
            # chain -- that would authenticate as whoever the environment says.
            raise AwsConfigError(
                "Both aws_access_key_id and aws_secret_access_key must be set together "
                "(one without the other is a misconfiguration, not a fallback to the ambient chain).",
                short_remediation="set both aws_access_key_id and aws_secret_access_key on the block, or neither",
            )
        if self.aws_profile is not None:
            if access_key or secret_key:
                raise AwsConfigError(
                    "aws_profile and aws_access_key_id / aws_secret_access_key are mutually exclusive: a block "
                    "authenticates either through the named profile or through the pasted keys, never both.",
                    short_remediation="keep either aws_profile or the pasted keys on the block, not both",
                )
            return self._profile_session(self.aws_profile)
        if access_key and secret_key:
            session_token = self.aws_session_token.get_secret_value() if self.aws_session_token else None
            return boto3.Session(
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                aws_session_token=session_token,
                region_name=self.default_region,
            )
        botocore_session = botocore.session.Session()
        if not self.use_ec2_instance_metadata:
            botocore_session.get_component("credential_provider").remove(IMDS_CREDENTIAL_PROVIDER_NAME)
        session = boto3.Session(botocore_session=botocore_session, region_name=self.default_region)
        if session.get_credentials() is None:
            raise AwsConfigError(
                "AWS credentials not configured. Set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, "
                "configure ~/.aws/credentials, set AWS_PROFILE, or (when running on an EC2 instance) "
                "set use_ec2_instance_metadata=true on the provider config to use its instance role."
            )
        return session

    def _profile_session(self, profile_name: str) -> boto3.Session:
        """A session scoped to one named profile, with its credentials resolved up front.

        A profile's failures otherwise arrive late and in the wrong shape: a
        single-sign-on profile returns deferred credentials whose expired token
        only surfaces on the first API call as a raw ``BotoCoreError``, and an
        ``mfa_serial`` profile would prompt on stdin, which hangs a headless
        ``mngr observe``. Resolving here turns each into a typed
        ``AwsConfigError`` carrying the command that fixes it.
        """
        botocore_session = botocore.session.Session(profile=profile_name)
        try:
            scoped_config = botocore_session.get_scoped_config()
        except ProfileNotFound as e:
            raise AwsProfileNotFoundError(
                f"AWS profile {profile_name!r} is not configured on this machine (no such section in "
                "~/.aws/config or ~/.aws/credentials).",
                short_remediation=f"run `aws configure --profile {profile_name}` (or `aws configure sso`)",
            ) from e
        if scoped_config.get("mfa_serial"):
            raise AwsProfileUnsupportedError(
                f"AWS profile {profile_name!r} sets mfa_serial, which prompts for a code on stdin; mngr runs "
                "without a terminal, so use a profile without MFA (an SSO profile, or static keys).",
                short_remediation="use a profile without mfa_serial (an SSO profile, or static keys)",
            )
        if not self.use_ec2_instance_metadata:
            botocore_session.get_component("credential_provider").remove(IMDS_CREDENTIAL_PROVIDER_NAME)
        session = boto3.Session(botocore_session=botocore_session, region_name=self.default_region)
        # botocore raises a profile's own configuration and retrieval errors (a
        # source_profile that does not exist, a failing credential_process) from
        # get_credentials() itself, and a deferred SSO token's from the first freeze.
        try:
            credentials = session.get_credentials()
            if credentials is not None:
                credentials.get_frozen_credentials()
        except _SSO_TOKEN_ERRORS as e:
            raise AwsProfileSsoLoginError(
                f"The single-sign-on login of AWS profile {profile_name!r} is missing or expired: {e}",
                short_remediation=f"run `aws sso login --profile {profile_name}`",
            ) from e
        except (BotoCoreError, ClientError) as e:
            raise AwsProfileCredentialsError(
                f"Could not resolve the credentials of AWS profile {profile_name!r}: {e}",
                short_remediation=f"check the profile with `aws sts get-caller-identity --profile {profile_name}`",
            ) from e
        if credentials is None:
            raise AwsProfileCredentialsError(
                f"AWS profile {profile_name!r} exists but names no credentials (no keys, source_profile, "
                "credential_process, or SSO settings).",
                short_remediation=f"run `aws configure --profile {profile_name}`",
            )
        return session

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
            identity = session.client(
                "sts", region_name=self.default_region, config=AWS_BOTO_CONFIG
            ).get_caller_identity()
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
