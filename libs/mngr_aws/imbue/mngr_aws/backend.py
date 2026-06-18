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
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_aws import hookimpl
from imbue.mngr_aws.cli import aws_cli_group
from imbue.mngr_aws.client import AwsVpsClient
from imbue.mngr_aws.config import AwsProviderConfig
from imbue.mngr_aws.state_bucket import S3StateBucket
from imbue.mngr_aws.state_bucket import S3StateHostIdentity
from imbue.mngr_aws.state_bucket import S3StateHostIdentityError
from imbue.mngr_aws.state_bucket import host_dir_sync_target_for
from imbue.mngr_vps.build_args import ParsedVpsBuildOptions
from imbue.mngr_vps.build_args import extract_git_depth
from imbue.mngr_vps.build_args import extract_presence_flag
from imbue.mngr_vps.build_args import extract_single_value_arg
from imbue.mngr_vps.build_args import raise_if_unknown_provider_arg
from imbue.mngr_vps.build_args import raise_if_vps_migration_arg
from imbue.mngr_vps.host_state_store import BucketHostStateStore
from imbue.mngr_vps.host_state_store import HostDirBackend
from imbue.mngr_vps.host_state_store import HostStateStore
from imbue.mngr_vps.host_state_store import NullHostDirBackend
from imbue.mngr_vps.host_state_store import missing_state_bucket_error
from imbue.mngr_vps.instance_offline import BucketHostDirBackend
from imbue.mngr_vps.instance_offline import HOST_DIR_SYNC_SCRIPT_PATH
from imbue.mngr_vps.instance_offline import HOST_DIR_SYNC_UNIT_NAME
from imbue.mngr_vps.instance_offline import HostDirSyncInstallPlan
from imbue.mngr_vps.instance_offline import OfflineCapableVpsProvider
from imbue.mngr_vps.instance_offline import host_name_from_tags
from imbue.mngr_vps.instance_offline import normalized_tags_to_dict
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vps.systemd import render_systemd_unit

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

# Host-side host_dir sync daemon (Component 3 of specs/provider-state-bucket).
# The selected ``_S3HostDirBackend`` owns the install / before-pause sequence; it
# supplies the ``aws s3 sync`` service body and awscli install. When
# ``is_offline_host_dir_enabled`` is on and a state bucket is present, the create
# path attaches the prepare-provisioned IAM instance profile, then the backend installs
# a systemd oneshot ``.service`` + ``.timer`` pair that runs
# ``aws s3 sync <host_dir_on_outer>/ s3://<bucket>/hosts/<id>/host_dir/ --delete``
# every ``HOST_DIR_SYNC_INTERVAL_SECONDS`` using the instance profile's IMDS
# credentials (no long-lived keys on the box). The same oneshot is also triggered
# once on graceful stop so the offline copy is current. Offline reads are served
# from the bucket by the operator's credentials via ``get_volume_for_host``.
# host_dir can contain large transient build artifacts; exclude the obvious ones
# so a periodic full-tree sync stays cheap. Conservative -- only mngr-irrelevant
# caches that never need to be read offline.
_HOST_DIR_SYNC_EXCLUDES: Final[tuple[str, ...]] = ("*.tmp", "*/__pycache__/*", "*/node_modules/*")


def _build_host_dir_sync_command(host_dir_on_outer: str, sync_target_uri: str) -> str:
    """Build the ``aws s3 sync ... --delete`` command the oneshot service runs.

    Syncs the per-host ``host_dir`` tree to ``hosts/<id>/host_dir/`` in the
    bucket, with ``--delete`` so a removed file is removed offline too, and a few
    excludes for large transient caches. Uses the instance profile's IMDS
    credentials implicitly (no creds on the command line).
    """
    excludes = " ".join(f'--exclude "{pattern}"' for pattern in _HOST_DIR_SYNC_EXCLUDES)
    return f'aws s3 sync "{host_dir_on_outer}/" "{sync_target_uri}" --delete {excludes}'.rstrip()


def _build_host_dir_sync_service_unit() -> str:
    """Build the oneshot systemd ``.service`` that pushes host_dir to the bucket once.

    Triggered periodically by the paired ``.timer`` and once on graceful stop.
    ``Type=oneshot`` so a stop-time ``systemctl start`` blocks until the sync
    completes (the offline copy is current before the instance powers off).
    ``ExecStart`` runs the installed ``HOST_DIR_SYNC_SCRIPT_PATH`` script rather than an
    inline ``/bin/sh -c``, so the embedded host_dir path and S3 URI avoid systemd's +
    the shell's nested quoting.
    """
    return render_systemd_unit(
        {
            "Unit": [("Description", "Sync this host's host_dir to the mngr S3 state bucket for offline reads")],
            "Service": [("Type", "oneshot"), ("ExecStart", HOST_DIR_SYNC_SCRIPT_PATH)],
        }
    )


def _build_awscli_install_command() -> str:
    """Build the best-effort awscli install command (apt; no-op if already present).

    Installs Debian's ``awscli`` (v1) -- sufficient for ``aws s3 sync`` with IMDS
    instance-profile credentials -- only when ``aws`` is not already on PATH, so
    a re-run or a baked AMI is a no-op.
    """
    return "command -v aws >/dev/null 2>&1 || (apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y awscli)"


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

    def _host_identity(self) -> S3StateHostIdentity | None:
        """Return the bucket-write IAM host identity (uncached), or None when unresolvable.

        Built fresh each call (it is cheap and used only at create / on rare
        diagnostics), scoped to the same state-bucket name as ``_state_bucket``.
        """
        return self.aws_config.build_host_identity(self.aws_client.session)

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

        Selecting one store here lets the persist / remove / list / read paths stop
        branching on bucket presence. The bucket is required: when it does not
        exist, accessing this property raises an actionable error pointing at
        ``mngr aws prepare`` (so create / label / offline reads all fail loudly and
        uniformly). Offline ``host_dir`` reads are a separate, bucket-only feature
        keyed off ``_state_bucket``.
        """
        bucket = self._state_bucket
        if bucket is None:
            raise missing_state_bucket_error("S3 state bucket", "mngr aws prepare")
        return BucketHostStateStore(bucket=bucket, bucket_label="S3 state bucket")

    @cached_property
    def _host_dir_backend(self) -> HostDirBackend:
        """Select the offline host_dir backend once: bucket-backed when enabled + present, else no-op.

        The only place ``is_offline_host_dir_enabled`` and ``_state_bucket``
        presence are tested together; every host_dir call site dispatches through
        the selected backend instead of re-deriving the condition.
        """
        bucket = self._state_bucket
        if self.aws_config.is_offline_host_dir_enabled and bucket is not None:
            return _S3HostDirBackend(provider=self, bucket=bucket)
        return NullHostDirBackend()

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
            iam_instance_profile_override=self._host_dir_sync_instance_profile(),
        )

    def _host_dir_sync_instance_profile(self) -> str | None:
        """Return the prepare-provisioned instance-profile name to attach at create, or None.

        Delegates to the selected host_dir backend (the no-op backend returns None
        when the feature is off or no bucket exists). The operator-supplied
        ``iam_instance_profile`` (set on the client) takes precedence over this in
        ``create_instance``. Attaching a profile requires create credentials to
        hold iam:PassRole.
        """
        return self._host_dir_backend.create_identity()

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

    def _offline_discovered_host_from_instance(self, instance: Mapping[str, Any]) -> DiscoveredHost | None:
        """Build a STOPPED-state DiscoveredHost from a stopped instance's ``mngr-*`` EC2 tags, or None.

        Reads only the cheap identity tags stamped at create (host id + ``Name``),
        never the bucket -- the full record is read from the state store on demand.
        """
        tags = normalized_tags_to_dict(instance)
        host_id_str = tags.get("mngr-host-id")
        if host_id_str is None:
            return None
        return DiscoveredHost(
            host_id=HostId(host_id_str),
            host_name=host_name_from_tags(tags, _HOST_NAME_TAG_KEY),
            provider_name=self.name,
            host_state=HostState.STOPPED,
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


class _S3HostDirBackend(BucketHostDirBackend):
    """Bucket-backed offline host_dir for AWS: instance-profile + ``aws s3 sync`` to the S3 state bucket.

    Selected only when offline host_dir is on and the state bucket exists, so
    ``self.bucket`` is always present here and no method re-tests it. The
    offline-read + final-sync flow is inherited from ``BucketHostDirBackend``;
    this supplies the AWS-specific identity, sync-daemon install, and probes.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    provider: AwsProvider
    bucket: S3StateBucket

    def _sync_unit_name(self) -> str:
        return HOST_DIR_SYNC_UNIT_NAME

    def _pause_action(self) -> str:
        return "stop"

    def _cloud_label(self) -> str:
        return "AWS"

    def create_identity(self) -> str:
        """The bucket-write IAM identity to attach at launch.

        Raises when the identity is missing or cannot be resolved (a
        ``host_identity_exists`` storage error propagates): with host_dir sync on,
        an instance launched without it could never push its host_dir, so this is
        a create-time setup failure rather than a silently unreadable offline
        host_dir later. Set ``is_offline_host_dir_enabled = false`` to skip it.
        """
        identity = self.provider._host_identity()
        if identity is None:
            raise S3StateHostIdentityError(
                "host_dir sync is on but the bucket-write IAM identity could not be resolved; re-run "
                "`mngr aws prepare` with sufficient IAM, or set is_offline_host_dir_enabled = false."
            )
        if not identity.host_identity_exists():
            raise S3StateHostIdentityError(
                f"host_dir sync is on but the bucket-write IAM identity {identity.identity_name} does not "
                "exist; re-run `mngr aws prepare` with sufficient IAM to enable offline host_dir, "
                "or set is_offline_host_dir_enabled = false."
            )
        return identity.identity_name

    def _build_install_plan(self, host_id: HostId) -> HostDirSyncInstallPlan | None:
        host_dir_on_outer = self.provider._realizer.host_dir_path_on_outer(host_id)
        sync_target_uri = host_dir_sync_target_for(self.bucket.bucket_name, host_id)
        return HostDirSyncInstallPlan(
            install_command=_build_awscli_install_command(),
            sync_command=_build_host_dir_sync_command(str(host_dir_on_outer), sync_target_uri),
            service_unit=_build_host_dir_sync_service_unit(),
            sync_target_uri=sync_target_uri,
        )

    def _raise_if_identity_missing(self, host_id: HostId) -> None:
        """Raise when an empty host_dir prefix is explained by the instance having no IAM profile.

        An instance with no attached IAM instance profile could never push host_dir,
        which is why the prefix is empty -- an actionable, permanent misconfiguration
        (the host predates the identity), so it raises rather than yielding an empty
        offline read. Detection is best-effort: a probe failure means we can't
        confirm, so we return without raising and ``volume`` yields None.
        """
        try:
            instance = self.provider._find_instance_for_host(host_id)
            if instance is None:
                return
            profile_arn = self.provider.aws_client.get_instance_iam_profile_arn(VpsInstanceId(instance["id"]))
        except MngrError as e:
            logger.debug("Could not check IAM profile for host {} while diagnosing empty host_dir: {}", host_id, e)
            return
        if profile_arn is None:
            raise S3StateHostIdentityError(
                f"Host {host_id}'s instance has no attached IAM instance profile, so its host_dir was never "
                "pushed to the bucket and is not readable offline. Re-run `mngr aws prepare` with sufficient "
                "IAM, then recreate the host so it picks up the profile."
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

        # A missing/unresolvable AWS session means EC2 was never reached: the
        # state is *unknown* (agents may still exist on a configured account we
        # transiently couldn't auth to). That is ProviderUnavailableError, NOT
        # ProviderEmptyError -- read paths (mngr list / gc) catch it via the
        # generic catch-all in mngr.api.list._construct_and_discover_for_provider
        # and log at error level, so a misconfigured provider stays visible
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
            raise ProviderUnavailableError(name, str(e)) from e

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
