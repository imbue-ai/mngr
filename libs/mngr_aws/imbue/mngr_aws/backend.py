from typing import Any
from typing import Final

import boto3
from botocore.exceptions import BotoCoreError
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_aws import hookimpl
from imbue.mngr_aws.client import AwsVpsClient
from imbue.mngr_aws.config import AwsProviderConfig
from imbue.mngr_vps_docker.instance import VpsDockerProvider

AWS_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("aws")


class AwsProvider(VpsDockerProvider):
    """AWS-specific provider that discovers hosts via the EC2 DescribeInstances API."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    aws_client: AwsVpsClient = Field(frozen=True, description="EC2 API client")
    aws_config: AwsProviderConfig = Field(frozen=True, description="AWS-specific configuration")

    _instances_cache: list[dict[str, Any]] | None = PrivateAttr(default=None)

    def reset_caches(self) -> None:
        super().reset_caches()
        self._instances_cache = None

    def _list_instances_cached(self) -> list[dict[str, Any]]:
        """List EC2 instances tagged for this provider, caching for the duration of the command."""
        if self._instances_cache is not None:
            return self._instances_cache
        self._instances_cache = self.aws_client.list_instances(provider_tag=str(self.name))
        return self._instances_cache

    def _credentials_configured(self) -> bool:
        return self.aws_config.has_resolvable_credentials()

    def _get_tagged_vps_ips(self) -> list[str]:
        """Get public IPs of EC2 instances tagged with this provider's name."""
        if not self._credentials_configured():
            logger.warning("AWS credentials not configured, skipping EC2 discovery")
            return []
        instances = self._list_instances_cached()
        vps_ips: list[str] = []
        for instance in instances:
            main_ip = instance.get("main_ip", "")
            if main_ip:
                vps_ips.append(main_ip)
        return vps_ips


class AwsProviderBackend(ProviderBackendInterface):
    """Backend for creating AWS EC2 VPS Docker provider instances."""

    @staticmethod
    def get_name() -> ProviderBackendName:
        return AWS_BACKEND_NAME

    @staticmethod
    def get_description() -> str:
        return "Runs agents in Docker containers on AWS EC2 instances"

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return AwsProviderConfig

    @staticmethod
    def get_build_args_help() -> str:
        return (
            "VPS-specific args (consumed by provider, not passed to docker):\n"
            "  --vps-region=REGION  AWS region (default: us-east-1)\n"
            "  --vps-plan=TYPE      EC2 instance type (default: t3.small)\n"
            "  --git-depth=N        Shallow-clone build context to depth N before upload\n"
            "\n"
            "AMI selection is taken from the provider config (default_ami_id /\n"
            "default_ami_by_region) for v1; per-host AMI override is a future improvement.\n"
            "\n"
            "All other build args are passed to 'docker build' on the VPS.\n"
            "Example: -b --vps-plan=t3.medium -b --file=Dockerfile -b .\n"
        )

    @staticmethod
    def get_start_args_help() -> str:
        return "Start args are passed directly to 'docker run'. Run 'docker run --help' for details."

    @staticmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
    ) -> ProviderInstanceInterface:
        if not isinstance(config, AwsProviderConfig):
            raise MngrError(f"Expected AwsProviderConfig, got {type(config).__name__}")

        try:
            session = config.get_session()
        except (ValueError, BotoCoreError) as e:
            logger.warning(
                "AWS provider {} initialised without resolvable AWS credentials: {}. "
                "Discovery operations will return empty results.",
                name,
                e,
            )
            session = _make_unresolved_session(config)

        try:
            ami_id = config.get_ami_id_for_region(config.default_region)
        except ValueError as e:
            logger.warning(
                "AWS provider {} initialised without a resolvable AMI for region {}: {}. "
                "Host creation will fail until default_ami_id is configured.",
                name,
                config.default_region,
                e,
            )
            ami_id = ""

        aws_client = AwsVpsClient(
            session=session,
            region=config.default_region,
            ami_id=ami_id,
            security_group_id=config.security_group_id,
            security_group_name=config.security_group_name,
            subnet_id=config.subnet_id,
            vpc_id=config.vpc_id,
            allowed_ssh_cidr=config.allowed_ssh_cidr,
            associate_public_ip=config.associate_public_ip,
            root_volume_size_gb=config.root_volume_size_gb,
            root_volume_type=config.root_volume_type,
            iam_instance_profile=config.iam_instance_profile,
            container_ssh_port=config.container_ssh_port,
        )

        return AwsProvider(
            name=name,
            host_dir=config.host_dir,
            mngr_ctx=mngr_ctx,
            config=config,
            vps_client=aws_client,
            aws_client=aws_client,
            aws_config=config,
        )


def _make_unresolved_session(config: AwsProviderConfig) -> boto3.Session:
    """Return a plain boto3 Session bound to the configured region.

    Only called after ``config.get_session()`` has already failed to resolve
    credentials. The returned Session still walks boto3's default credential
    chain (env vars, profile, shared credentials file, IMDS); we don't strip
    credentials, we just don't require them at construction time. Any EC2
    call on this Session is expected to fail loudly with an AWS auth error,
    which is the desired behavior: we want the provider to be registrable
    even without credentials so that listing operations short-circuit in
    ``_get_tagged_vps_ips`` while host-creation operations fail clearly.
    """
    return boto3.Session(region_name=config.default_region)


@hookimpl
def register_provider_backend() -> tuple[type[ProviderBackendInterface], type[ProviderInstanceConfig]]:
    """Register the AWS provider backend."""
    return (AwsProviderBackend, AwsProviderConfig)
