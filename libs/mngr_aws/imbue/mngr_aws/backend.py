import os
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Final

import boto3
import click
from botocore.exceptions import BotoCoreError
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderEmptyError
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_aws import hookimpl
from imbue.mngr_aws.cli import aws_cli_group
from imbue.mngr_aws.client import AwsVpsClient
from imbue.mngr_aws.config import AwsProviderConfig
from imbue.mngr_vps_docker.instance import ParsedVpsBuildOptions
from imbue.mngr_vps_docker.instance import VpsDockerProvider
from imbue.mngr_vps_docker.instance import extract_git_depth
from imbue.mngr_vps_docker.instance import extract_presence_flag
from imbue.mngr_vps_docker.instance import extract_single_value_arg
from imbue.mngr_vps_docker.instance import raise_if_unknown_provider_arg
from imbue.mngr_vps_docker.instance import raise_if_vps_migration_arg
from imbue.mngr_vps_docker.primitives import VpsInstanceId

AWS_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("aws")


def _resolve_session_and_ami_or_empty(
    name: ProviderInstanceName, config: AwsProviderConfig
) -> tuple[boto3.Session, str]:
    """Resolve AWS credentials + the default-region AMI, raising ``ProviderEmptyError`` on any failure.

    Two independent failure modes are funneled through a single exit type so
    callers do not need to discriminate: ``config.get_session()`` fails when
    no credential source resolves (no env vars, no profile, no IMDS, etc.);
    ``config.get_ami_id_for_region()`` fails when neither ``default_ami_id``
    nor a matching ``default_ami_by_region`` entry is set. Either surfaces
    here as ``ProviderEmptyError`` so the provider is treated as unreachable
    rather than half-constructed.

    Shared by ``build_provider_instance`` (the read-path entry) and
    ``bootstrap_for_host_creation`` (the create-path entry) so both fail
    identically. Deliberately does NOT log: each caller decides whether to
    warn (read paths, where the skip would otherwise be silent) or let the
    error surface directly to the user (the create path).
    """
    try:
        session = config.get_session()
    except (ValueError, BotoCoreError) as e:
        raise ProviderEmptyError(name, str(e)) from e
    try:
        ami_id = config.get_ami_id_for_region(config.default_region)
    except ValueError as e:
        raise ProviderEmptyError(name, str(e)) from e
    return session, ami_id


class ParsedAwsBuildOptions(ParsedVpsBuildOptions):
    """``ParsedVpsBuildOptions`` extended with AWS-specific knobs.

    Returned by ``AwsProvider._parse_build_args`` and consumed by
    ``AwsProvider._create_vps_instance`` so the AWS-only ``--aws-ami=``
    override flows through to ``AwsVpsClient.create_instance`` without
    touching the shared ``VpsClientInterface``.
    """

    ami_id_override: str | None = Field(
        default=None,
        description=(
            "Per-host AMI override from ``--aws-ami=<ami-id>``. When set, "
            "``AwsVpsClient.create_instance`` launches this AMI instead of the "
            "provider config's default. When unset, the client's configured "
            "default AMI applies."
        ),
    )
    spot: bool = Field(
        default=False,
        description=(
            "Per-host opt-in for EC2 spot capacity, from the presence-only "
            "``--aws-spot`` build arg. When True, ``AwsVpsClient.create_instance`` "
            "passes ``InstanceMarketOptions={'MarketType': 'spot'}`` to RunInstances "
            "so the host is billed at the spot price. AWS may reclaim spot instances "
            "with ~2 minutes' interruption notice; the host is terminated, not "
            "stopped, on reclaim. Opt-in only -- safe for ephemeral / experimental "
            "agents, risky for long-lived ones."
        ),
    )


class AwsProvider(VpsDockerProvider):
    """AWS-specific provider that discovers hosts via the EC2 DescribeInstances API."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    aws_client: AwsVpsClient = Field(frozen=True, description="EC2 API client")
    aws_config: AwsProviderConfig = Field(frozen=True, description="AWS-specific configuration")

    def _fetch_provider_instances(self) -> list[dict[str, Any]]:
        """List EC2 instances tagged with this provider's name."""
        return self.aws_client.list_instances(provider_tag=str(self.name))

    def _validate_provider_args_for_create(self) -> None:
        """Refuse to create an EC2 instance under pytest without auto_shutdown_minutes set.

        Mirrors the Modal pattern in ``mngr_modal.backend._create_environment``:
        when ``PYTEST_CURRENT_TEST`` is set, the test harness is responsible
        for configuring the safety net that prevents leaked cost if pytest
        itself is killed. For AWS, that safety net is cloud-init
        ``shutdown -P +N`` combined with the launch flag
        ``InstanceInitiatedShutdownBehavior=terminate`` (both rely on
        ``auto_shutdown_minutes`` being set on the provider config). If it
        isn't, fail closed at the pre-create hook rather than silently leak
        an instance.
        """
        if "PYTEST_CURRENT_TEST" not in os.environ:
            return
        minutes = self._get_effective_auto_shutdown_minutes()
        if not (minutes and minutes > 0):
            raise MngrError(
                "Refusing to create EC2 instance during pytest without "
                "auto_shutdown_minutes set on the AWS provider config. "
                "Set [providers.<instance>] auto_shutdown_minutes = <N> in "
                "the project settings.toml so cloud-init schedules "
                "'shutdown -P +N' (combined with the launch flag "
                "InstanceInitiatedShutdownBehavior=terminate) and the "
                "instance self-terminates even if pytest is killed."
            )

    def _parse_build_args(self, build_args: Sequence[str] | None) -> ParsedAwsBuildOptions:
        """Parse AWS-prefixed build args.

        Accepts ``--aws-region=REGION``, ``--aws-instance-type=TYPE``,
        ``--aws-ami=AMI-ID``, ``--aws-spot`` (presence-only), and the shared
        ``--git-depth=N``. ``--aws-ami=`` is the per-host AMI override (falls
        back to the provider config when omitted); ``--aws-spot`` opts the
        host into EC2 spot capacity.

        Composed from the shared low-level helpers rather than the convenience
        ``parse_vps_build_args`` because AWS has knobs beyond region + plan.
        """
        args = list(build_args or ())
        region, args = extract_single_value_arg(args, "--aws-region=")
        instance_type, args = extract_single_value_arg(args, "--aws-instance-type=")
        ami_override, args = extract_single_value_arg(args, "--aws-ami=")
        spot, args = extract_presence_flag(args, "--aws-spot")
        git_depth, args = extract_git_depth(args)
        valid_args = (
            "--aws-region=",
            "--aws-instance-type=",
            "--aws-ami=",
            "--aws-spot",
            "--git-depth=",
        )
        docker_build_args: list[str] = []
        for arg in args:
            raise_if_vps_migration_arg(arg)
            raise_if_unknown_provider_arg(arg, "aws", valid_args)
            docker_build_args.append(arg)
        return ParsedAwsBuildOptions(
            region=region or self.aws_config.default_region,
            plan=instance_type or self.aws_config.default_instance_type,
            ami_id_override=ami_override,
            spot=spot,
            git_depth=git_depth,
            docker_build_args=tuple(docker_build_args),
        )

    def _create_vps_instance(
        self,
        parsed: ParsedVpsBuildOptions,
        label: str,
        user_data: str,
        ssh_key_ids: Sequence[str],
        tags: Mapping[str, str],
    ) -> VpsInstanceId:
        """AWS override: thread the per-host AMI override into ``AwsVpsClient.create_instance``.

        Calls through ``self.aws_client`` (the concrete typed AWS client) rather
        than the shared ``self.vps_client`` interface so the AWS-only
        ``ami_id_override`` kwarg is statically visible. ``ami_id_override``
        comes from ``--aws-ami=<ami-id>``; when None, the client's configured
        default AMI applies.
        """
        match parsed:
            case ParsedAwsBuildOptions(ami_id_override=ami_id_override, spot=spot):
                pass
            case _:
                raise MngrError(
                    f"AwsProvider._create_vps_instance expected ParsedAwsBuildOptions, "
                    f"got {type(parsed).__name__}. This indicates the parser hook returned a "
                    "non-AWS shape; _parse_build_args must return ParsedAwsBuildOptions."
                )
        return self.aws_client.create_instance(
            label=label,
            region=parsed.region,
            plan=parsed.plan,
            user_data=user_data,
            ssh_key_ids=ssh_key_ids,
            tags=tags,
            ami_id_override=ami_id_override,
            spot=spot,
        )

    def _list_provider_vps_hostnames(self) -> list[str]:
        """Return public IPs of EC2 instances tagged with this provider's name.

        Credentials are guaranteed to be resolvable here: ``build_provider_instance``
        raises ``ProviderEmptyError`` when ``config.get_session()`` fails, so any
        AwsProvider that reaches this point has working credentials. The shared
        ``VpsClientInterface`` base method that calls this is invoked for both
        listing and create-host flows, so AWS does not need a separate
        ``_credentials_configured`` override.
        """
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
            "EC2-specific args (consumed by provider, not passed to docker):\n"
            "  --aws-region=REGION         Must match the provider config's default_region;\n"
            "                              the client is bound to one region at construction\n"
            "                              and refuses cross-region creates. To target multiple\n"
            "                              regions, define one [providers.aws-<region>] block\n"
            "                              per region (see mngr_aws README 'Multiple regions').\n"
            "  --aws-instance-type=TYPE    EC2 instance type (default: t3.small)\n"
            "  --aws-ami=AMI-ID            Override the per-host AMI for this create only\n"
            "                              (default: provider config's default_ami_id /\n"
            "                              default_ami_by_region for the chosen region)\n"
            "  --aws-spot                  Run on EC2 spot capacity (presence-only flag).\n"
            "                              AWS may reclaim with ~2 min notice; the host is\n"
            "                              terminated, not stopped, on reclaim. Opt-in only.\n"
            "  --git-depth=N               Shallow-clone build context to depth N before upload\n"
            "\n"
            "All other build args are passed to 'docker build' on the EC2 instance.\n"
            "Example: -b --aws-instance-type=t3.medium -b --file=Dockerfile -b .\n"
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
            session, ami_id = _resolve_session_and_ami_or_empty(name, config)
        except ProviderEmptyError as e:
            # Read paths (mngr list / connect / gc / discovery) reach this. The
            # shared discovery loop swallows ProviderEmptyError at logger.debug
            # (mngr.api.list._construct_and_discover_for_provider), so without
            # this warning a misconfigured AWS provider would silently vanish
            # from those listings with no user-visible reason. Warn once per
            # skip, naming the provider and the actionable cause. Use e.reason
            # (the bare env vars / profile / instance role / default_ami_id
            # guidance) rather than str(e), which would double the provider name
            # and the "has no state yet" framing already in the wrapped message.
            # Mirrors Vultr's read-path warning shape (mngr_vultr.backend.
            # VultrProvider._list_provider_vps_hostnames).
            #
            # The create path never reaches this warning: bootstrap_for_host_
            # creation resolves the same credentials + AMI first and lets the
            # error surface directly, so `mngr create --provider aws` shows the
            # failure without a misleading "skipping discovery" line.
            logger.warning("AWS provider {!r}: {} -- skipping discovery", name, e.reason)
            raise

        aws_client = AwsVpsClient(
            session=session,
            region=config.default_region,
            ami_id=ami_id,
            security_group=config.security_group,
            subnet_id=config.subnet_id,
            vpc_id=config.vpc_id,
            allowed_ssh_cidrs=config.allowed_ssh_cidrs,
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

    @staticmethod
    def bootstrap_for_host_creation(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
    ) -> None:
        """Fail fast on the create path when AWS credentials or AMI are unresolvable.

        The create-host flow calls this exactly once, before
        ``build_provider_instance`` -- and it is the only call path that does
        (see ``ProviderBackendInterface.bootstrap_for_host_creation``). Resolving
        credentials + AMI here serves two purposes:

        1. A misconfigured ``mngr create --provider aws`` surfaces the actionable
           ``ProviderEmptyError`` (carrying the env vars / profile / instance role
           / ``default_ami_id`` guidance) directly, as the create command's
           top-level error.
        2. Because this raises before ``build_provider_instance`` runs, that
           method's read-path "skipping discovery" warning is never reached on
           the create path. So an explicit create never emits a misleading
           discovery warning -- the warning is reserved for the read paths that
           would otherwise drop the provider silently.

        Idempotent and cheap (the subsequent ``build_provider_instance`` resolves
        the same credentials and AMI again to construct the client). Unlike
        Modal's / Docker's overrides, this creates no backend-side resources --
        AWS's one-time resource (the per-region security group) is provisioned
        out of band by ``mngr aws prepare``, not here.
        """
        del mngr_ctx
        if not isinstance(config, AwsProviderConfig):
            raise MngrError(f"Expected AwsProviderConfig, got {type(config).__name__}")
        _resolve_session_and_ami_or_empty(name, config)


@hookimpl
def register_provider_backend() -> tuple[type[ProviderBackendInterface], type[ProviderInstanceConfig]]:
    """Register the AWS provider backend."""
    return (AwsProviderBackend, AwsProviderConfig)


@hookimpl
def register_cli_commands() -> Sequence[click.Command]:
    """Register the ``mngr aws ...`` operator command group."""
    return [aws_cli_group]
