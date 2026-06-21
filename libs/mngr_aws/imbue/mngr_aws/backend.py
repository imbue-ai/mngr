import os
from collections.abc import Mapping
from collections.abc import Sequence
from functools import cached_property
from typing import Any
from typing import Final

import click
from botocore.exceptions import BotoCoreError
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.logging import log_span
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderNotAuthorizedError
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_aws import hookimpl
from imbue.mngr_aws.cli import aws_cli_group
from imbue.mngr_aws.client import AwsVpsClient
from imbue.mngr_aws.config import AwsProviderConfig
from imbue.mngr_aws.state_bucket import S3StateBucket
from imbue.mngr_vps.build_args import ParsedVpsBuildOptions
from imbue.mngr_vps.build_args import extract_git_depth
from imbue.mngr_vps.build_args import extract_presence_flag
from imbue.mngr_vps.build_args import extract_single_value_arg
from imbue.mngr_vps.build_args import raise_if_unknown_provider_arg
from imbue.mngr_vps.build_args import raise_if_vps_migration_arg
from imbue.mngr_vps.host_state_store import HostDirBackend
from imbue.mngr_vps.host_state_store import HostStateStore
from imbue.mngr_vps.host_store import VpsHostRecord
from imbue.mngr_vps.instance_offline import OfflineCapableVpsProvider
from imbue.mngr_vps.primitives import VpsInstanceId

AWS_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("aws")

# EC2 states in which the host OS is down (so the SSH-based sweep can't see the
# host) but the instance still exists and must be reconstructed offline.
# ``stopping`` is included so a host doesn't vanish from discovery during the
# (seconds-long) stop transition before it reaches the terminal ``stopped``.
_HOST_DOWN_STATES: Final[frozenset[str]] = frozenset({"stopping", "stopped"})
# The host name is mirrored into the EC2 ``Name`` tag (as ``mngr-<host_name>``).
_HOST_NAME_TAG_KEY: Final[str] = "Name"

# The self-stopping idle watcher (in-container sentinel + host-side systemd
# ``.path``/``.service``) is shared by the base ``OfflineCapableVpsProvider``.
# The AWS oneshot ``.service`` powers the instance off (``shutdown -P now``); EC2
# then applies the instance's ``InstanceInitiatedShutdownBehavior`` -- ``stop``
# (resumable idle-pause, the default) or ``terminate`` -- so no IAM role or awscli
# is needed on the box. See the README "Implementation details".


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


class AwsProvider(OfflineCapableVpsProvider):
    """AWS-specific provider that discovers hosts via the EC2 DescribeInstances API."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    aws_client: AwsVpsClient = Field(frozen=True, description="EC2 API client")
    aws_config: AwsProviderConfig = Field(frozen=True, description="AWS-specific configuration")

    @cached_property
    def _state_bucket(self) -> S3StateBucket | None:
        """Return the S3 state bucket when it actually exists, else None.

        The bucket is the sole source of truth for agent records and the offline
        host record. None means the bucket does not exist yet (no name
        configured/derivable, or one whose name resolves but ``mngr aws prepare``
        was never run); ``_state_store`` then raises an actionable error. A storage
        error while probing existence propagates rather than masquerading as
        "absent". The existence probe runs at most once per provider lifetime
        (cached).
        """
        return self._resolve_existing_state_bucket()

    def _resolve_existing_state_bucket(self) -> S3StateBucket | None:
        """Build the configured/derived bucket and return it only if it exists.

        Returns None only when the bucket genuinely does not exist (or its name is
        unresolvable). A ``bucket_exists`` storage error propagates -- the bucket
        is required, so an inability to check is an operational failure, not a
        silent "no bucket".
        """
        bucket = self.aws_config.build_state_bucket(self.aws_client.session)
        if bucket is None:
            return None
        if not bucket.bucket_exists():
            logger.debug(
                "S3 state bucket {} does not exist; offline host state is unavailable "
                "(run `mngr aws prepare` to create it)",
                bucket.bucket_name,
            )
            return None
        return bucket

    @cached_property
    def _state_store(self) -> HostStateStore:
        """The external host/agent-record mirror: the S3 bucket, or raise when it is absent.

        Delegates to the shared ``_select_bucket_store``, supplying only the resolved
        S3 bucket, its label, and the ``mngr aws prepare`` remediation command. The
        bucket is required: when it does not exist, the helper raises an actionable
        error pointing at ``mngr aws prepare``. Offline ``host_dir`` reads are a
        separate, bucket-only feature keyed off ``_state_bucket``.
        """
        return self._select_bucket_store(
            self._state_bucket, store_label="S3 state bucket", prepare_command="mngr aws prepare"
        )

    @cached_property
    def _host_dir_backend(self) -> HostDirBackend:
        """Select the offline host_dir backend once: bucket-backed when enabled + present, else no-op.

        Delegates to the shared ``_select_bucket_host_dir_backend``, supplying the
        resolved S3 bucket and the config's ``is_offline_host_dir_enabled`` flag.
        """
        return self._select_bucket_host_dir_backend(
            self._state_bucket, enabled=self.aws_config.is_offline_host_dir_enabled
        )

    def _fetch_provider_instances(self) -> list[dict[str, Any]]:
        """List EC2 instances tagged with this provider's name."""
        return self.aws_client.list_instances(provider_tag=str(self.name))

    def _validate_provider_args_for_create(self) -> None:
        """Refuse to create an EC2 instance under pytest without auto_shutdown_seconds set.

        Mirrors the Modal pattern in ``mngr_modal.backend._create_environment``:
        when ``PYTEST_CURRENT_TEST`` is set, the test harness is responsible
        for configuring the safety net that bounds leaked cost if pytest
        itself is killed. For AWS, that net is cloud-init ``shutdown -P +N``
        (the ``auto_shutdown_seconds`` time cap). Its effect depends on
        ``terminate_on_shutdown``: with the release-test default of ``True``
        the instance self-terminates at the cap (self-cleaning); with ``False``
        (resumable idle-stop) it self-stops, and the conftest session-end
        scanner reaps it. Either way ``auto_shutdown_seconds`` must be set, so
        fail closed here rather than risk an unbounded leak.
        """
        if "PYTEST_CURRENT_TEST" not in os.environ:
            return
        seconds = self._get_effective_auto_shutdown_seconds()
        if not (seconds and seconds > 0):
            raise MngrError(
                "Refusing to create EC2 instance during pytest without "
                "auto_shutdown_seconds set on the AWS provider config. "
                "Set [providers.<instance>] auto_shutdown_seconds = <N> in "
                "the project settings.toml so cloud-init schedules "
                "'shutdown -P +N' and the instance is bounded (terminated or "
                "stopped per terminate_on_shutdown) even if pytest is killed."
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
        # FIXME: this allowlist only covers the per-host knobs wired up so far.
        # Other AwsProviderConfig fields could plausibly be exposed as per-host
        # build args but are not yet (today they are settings.toml-only):
        #   --aws-subnet=            (subnet_id)
        #   --aws-vpc=               (vpc_id)
        #   --aws-security-group=    (security_group; existing id or auto-create name)
        #   --aws-ssh-cidr=          (allowed_ssh_cidrs; repeatable)
        #   --aws-iam-profile=       (iam_instance_profile)
        #   --aws-root-volume-size=  (root_volume_size_gb)
        #   --aws-root-volume-type=  (root_volume_type)
        #   --aws-associate-public-ip / --aws-no-associate-public-ip (associate_public_ip)
        #   --aws-eip                (planned, when the destroy-path lifecycle work lands)
        # Add the corresponding extract_* parse and this allowlist entry together.
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
        comes from ``--aws-ami=<ami-id>``; when None, the default AMI for the
        target region is resolved from the config just in time. Resolving AMI
        here (the only create-path call site) rather than in
        ``build_provider_instance`` keeps AMI selection a create-only concern
        so a misconfigured AMI does not hide already-running instances from
        ``mngr list`` / ``connect`` / ``gc``. The create path's ``create_host``
        except handler reverses any SSH key upload that may have happened
        before this raise, so the missing-AMI failure leaves no leaked state.
        """
        aws_parsed = self._require_parsed(parsed, ParsedAwsBuildOptions)
        ami_id_override = aws_parsed.ami_id_override
        spot = aws_parsed.spot
        if ami_id_override:
            effective_ami_id = ami_id_override
        else:
            try:
                effective_ami_id = self.aws_config.get_ami_id_for_region(parsed.region)
            except ValueError as e:
                raise MngrError(f"AWS provider {self.name!r}: {e}") from e
        return self.aws_client.create_instance(
            label=label,
            region=parsed.region,
            plan=parsed.plan,
            user_data=user_data,
            ssh_key_ids=ssh_key_ids,
            tags=tags,
            ami_id_override=effective_ami_id,
            spot=spot,
        )

    # The shared ``OfflineCapableVpsProvider._list_provider_vps_hostnames``
    # (cached listing -> non-empty main_ip) covers AWS unchanged: a stopped EC2
    # instance loses its ephemeral IP and is excluded by the non-empty IP check.

    # =========================================================================
    # Native EC2 stop/start (idle-pause + resume) -- the base
    # OfflineCapableVpsProvider owns the orchestration; here we supply only the
    # EC2-specific cloud-API hooks.
    # =========================================================================

    def _pause_cloud_instance(self, instance_id: VpsInstanceId) -> None:
        with log_span("Stopping EC2 instance"):
            self.aws_client.stop_instance(instance_id)

    def _resume_cloud_instance(self, instance_id: VpsInstanceId) -> str:
        with log_span("Starting EC2 instance"):
            return self.aws_client.start_instance(instance_id)

    # =========================================================================
    # Self-stopping idle watcher (in-container sentinel + host-side systemd)
    # =========================================================================

    @property
    def _supports_bare_isolation(self) -> bool:
        # EC2 supports stop/start, and the bare idle path self-stops the instance
        # via InstanceInitiatedShutdownBehavior, so bare placement is supported.
        return True

    def _provider_instance_kind(self) -> str:
        return "EC2 instance"

    # The base ``OfflineCapableVpsProvider`` owns the idle-watcher install (the
    # in-container sentinel ``shutdown.sh``, the host-side systemd ``.path``/
    # ``.service`` pair, and the bare poweroff). AWS's ``.service`` body is the
    # default ``shutdown -P now`` (EC2 then applies its
    # ``InstanceInitiatedShutdownBehavior``), so AWS overrides none of those hooks.

    # =========================================================================
    # Offline discovery (so STOPPED hosts list + resolve by name from the bucket)
    # =========================================================================

    def _host_name_tag_key(self) -> str:
        # The host name is mirrored into the EC2 ``Name`` tag (as ``mngr-<host_name>``);
        # the shared ``_offline_discovered_host_from_instance`` reads it through here.
        return _HOST_NAME_TAG_KEY

    def _remirror_host_name(self, host_record: VpsHostRecord, name: HostName) -> None:
        """Re-stamp the EC2 ``Name`` tag (read by offline discovery) after a rename."""
        if host_record.config is None:
            return
        self.aws_client.set_instance_tags(
            host_record.config.vps_instance_id, {self._host_name_tag_key(): f"mngr-{name}"}
        )

    def _is_instance_offline(self, instance: Mapping[str, Any]) -> bool:
        """Whether the EC2 instance's OS is down (stopping or stopped).

        EC2 power state rides along for free in the ``DescribeInstances`` listing,
        so this needs no extra call. Gate on state, not ``main_ip`` -- a
        ``stopping`` instance can still report a public IP while its OS is already
        off, and gating on the IP would make the host vanish for the (seconds-long)
        stop transition.
        """
        return instance.get("state") in _HOST_DOWN_STATES


def _aws_not_authorized_error(
    name: ProviderInstanceName, reason: str, short_remediation: str
) -> ProviderNotAuthorizedError:
    """Build a ``ProviderNotAuthorizedError`` with AWS-specific, actionable help text.

    The generic unavailable help text tells the user to "start Docker", which is wrong
    advice for an AWS credential failure -- so we curate the guidance toward resolving
    the boto3 credential chain. ``ProviderNotAuthorizedError`` is a
    ``ProviderUnavailableError`` subclass, so read paths (``mngr list`` / ``gc`` /
    discovery) still treat the provider as unavailable rather than silently empty.
    """
    help_text = (
        "AWS could not be reached. Check, in order:\n"
        "  - credentials: run `aws configure` (or set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY, "
        "or AWS_PROFILE);\n"
        "  - one-time setup: run `mngr aws prepare` if you have not yet.\n"
        f"Or disable the provider: mngr config set --scope user providers.{name}.is_enabled false"
    )
    return ProviderNotAuthorizedError(
        name, reason=reason, short_remediation=short_remediation, user_help_text=help_text
    )


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
            "                              (default: provider config's default_ami_id, or the\n"
            "                              pinned per-region default for the chosen region)\n"
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

        # A missing/unresolvable AWS session means EC2 was never reached: the
        # state is *unknown* (agents may still exist on a configured account we
        # transiently couldn't auth to). That is ProviderNotAuthorizedError (a
        # ProviderUnavailableError), NOT ProviderEmptyError -- read paths (mngr
        # list / gc) treat it as an unavailable provider that stays visible
        # rather than silently vanishing from the listing. Host-creation paths
        # surface this same error directly (no override -- create just calls
        # build_provider_instance first), so we use a single exit shape for
        # both read and create paths, matching the Azure pattern. AMI selection
        # is a create-only concern and is deliberately NOT validated here --
        # see AwsProvider._create_vps_instance for the just-in-time resolution
        # and the rationale.
        try:
            session = config.get_session()
        except (ValueError, BotoCoreError) as e:
            raise _aws_not_authorized_error(
                name,
                reason="AWS credentials not configured",
                short_remediation="run `aws configure` (or set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY, or AWS_PROFILE)",
            ) from e

        aws_client = AwsVpsClient(
            session=session,
            region=config.default_region,
            security_group=config.security_group,
            subnet_id=config.subnet_id,
            vpc_id=config.vpc_id,
            allowed_ssh_cidrs=config.allowed_ssh_cidrs,
            associate_public_ip=config.associate_public_ip,
            root_volume_size_gb=config.root_volume_size_gb,
            root_volume_type=config.root_volume_type,
            iam_instance_profile=config.iam_instance_profile,
            terminate_on_shutdown=config.terminate_on_shutdown,
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


@hookimpl
def register_provider_backend() -> tuple[type[ProviderBackendInterface], type[ProviderInstanceConfig]]:
    """Register the AWS provider backend."""
    return (AwsProviderBackend, AwsProviderConfig)


@hookimpl
def register_cli_commands() -> Sequence[click.Command]:
    """Register the ``mngr aws ...`` operator command group."""
    return [aws_cli_group]
