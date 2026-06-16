import os
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from functools import cached_property
from pathlib import Path
from typing import Any
from typing import Final

import click
from botocore.exceptions import BotoCoreError
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.logging import log_span
from imbue.imbue_common.model_update import to_update
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.interfaces.volume import HostVolume
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr_aws import hookimpl
from imbue.mngr_aws.cli import aws_cli_group
from imbue.mngr_aws.client import AwsVpsClient
from imbue.mngr_aws.config import AwsProviderConfig
from imbue.mngr_aws.state_bucket import S3StateBucket
from imbue.mngr_aws.state_bucket import S3StateBucketError
from imbue.mngr_aws.state_bucket import S3StateHostIdentity
from imbue.mngr_aws.state_bucket import S3StateHostIdentityError
from imbue.mngr_aws.state_bucket import host_dir_sync_target_for
from imbue.mngr_vps_docker.container_setup import host_volume_name_for
from imbue.mngr_vps_docker.container_setup import remove_host_from_known_hosts
from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.host_state_store import BucketHostStateStore
from imbue.mngr_vps_docker.host_state_store import HostStateStore
from imbue.mngr_vps_docker.host_store import VpsDockerHostRecord
from imbue.mngr_vps_docker.host_store import open_host_store
from imbue.mngr_vps_docker.instance import AGENT_TAG_FIELDS
from imbue.mngr_vps_docker.instance import AGENT_TAG_PREFIX
from imbue.mngr_vps_docker.instance import IDLE_SENTINEL_FILENAME
from imbue.mngr_vps_docker.instance import ParsedVpsBuildOptions
from imbue.mngr_vps_docker.instance import TagMirrorVpsDockerProvider
from imbue.mngr_vps_docker.instance import VpsDockerProvider
from imbue.mngr_vps_docker.instance import extract_git_depth
from imbue.mngr_vps_docker.instance import extract_presence_flag
from imbue.mngr_vps_docker.instance import extract_single_value_arg
from imbue.mngr_vps_docker.instance import raise_if_unknown_provider_arg
from imbue.mngr_vps_docker.instance import raise_if_vps_migration_arg
from imbue.mngr_vps_docker.primitives import VpsInstanceId

AWS_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("aws")

# EC2 allows 50 (non-``aws:``) tags per resource. When a host has so many agents
# that mirroring another would exceed this, we surface a NotImplementedError
# (which the CLI turns into an "open an issue" prompt) rather than failing
# obscurely -- the S3-backed agent store is the planned fix for many-agent hosts.
_AWS_MAX_TAGS_PER_INSTANCE: Final[int] = 50
# EC2 states in which the host OS is down (so the SSH-based sweep can't see the
# host) but the instance still exists and its agents must be reconstructed from
# tags. ``stopping`` is included so a host doesn't vanish from discovery during
# the (seconds-long) stop transition before it reaches the terminal ``stopped``.
_HOST_DOWN_STATES: Final[frozenset[str]] = frozenset({"stopping", "stopped"})
# The host name is mirrored into the EC2 ``Name`` tag (as ``mngr-<host_name>``).
_HOST_NAME_TAG_KEY: Final[str] = "Name"

# Self-stopping idle watcher (host-side). The in-container activity watcher
# writes ``IDLE_SENTINEL_FILENAME`` into the host_dir/commands directory on the
# shared volume when the host goes idle; a host-side systemd ``.path`` unit
# (``IDLE_WATCHER_UNIT_NAME``) watches the corresponding outer-filesystem path
# and triggers a oneshot ``.service`` that powers the instance off
# (``shutdown -P now``). EC2 then applies the instance's
# ``InstanceInitiatedShutdownBehavior`` -- ``stop`` (resumable idle-pause, the
# default) or ``terminate`` -- so no IAM role or awscli is needed on the box.
# See the README "Implementation details".
IDLE_WATCHER_UNIT_NAME: Final[str] = "mngr-aws-idle-watcher"

# Host-side host_dir sync daemon (Component 3 of specs/provider-state-bucket).
# When ``is_host_dir_synced_to_bucket`` is on and a state bucket is present, the
# create path attaches the prepare-provisioned IAM instance profile, then
# installs (over SSH on the outer) a systemd oneshot ``.service`` + ``.timer``
# pair: every ``HOST_DIR_SYNC_INTERVAL_SECONDS`` the oneshot runs
# ``aws s3 sync <host_dir_on_outer>/ s3://<bucket>/hosts/<id>/host_dir/ --delete``
# using the instance profile's IMDS credentials (no long-lived keys on the box).
# The same oneshot is also triggered once on graceful stop (``stop_host``) so the
# offline copy is current. Offline reads are served from the bucket by the
# operator's credentials via ``get_volume_for_host``.
HOST_DIR_SYNC_UNIT_NAME: Final[str] = "mngr-aws-host-dir-sync"
HOST_DIR_SYNC_INTERVAL_SECONDS: Final[int] = 60
# host_dir can contain large transient build artifacts; exclude the obvious ones
# so a periodic full-tree sync stays cheap. Conservative -- only mngr-irrelevant
# caches that never need to be read offline.
_HOST_DIR_SYNC_EXCLUDES: Final[tuple[str, ...]] = ("*.tmp", "*/__pycache__/*", "*/node_modules/*")


def _build_sentinel_shutdown_script(sentinel_in_container: str) -> str:
    """Build the in-container ``shutdown.sh`` that signals idle by touching the sentinel.

    Unlike the base ``VpsDockerProvider`` shutdown script (which runs
    ``kill -TERM 1`` to stop the container), the AWS variant only *signals*
    that the host is idle: it touches a sentinel file on the shared volume.
    The host-side systemd path unit observes that file and powers the whole
    EC2 instance off (a container cannot power off its host, so the signal has
    to cross the container boundary via the shared volume).
    """
    return f'#!/bin/bash\ntouch "{sentinel_in_container}"\n'


def _build_idle_watcher_path_unit(sentinel_on_outer: str) -> str:
    """Build the systemd ``.path`` unit that fires when the idle sentinel appears.

    ``PathExists`` triggers the paired ``.service`` once the sentinel file
    exists at ``sentinel_on_outer`` (the outer-filesystem location the
    container's sentinel write maps to on the per-host btrfs subvolume).
    """
    return (
        "[Unit]\n"
        "Description=Watch for the mngr idle sentinel and stop this EC2 instance when idle\n"
        "[Path]\n"
        f"PathExists={sentinel_on_outer}\n"
        f"Unit={IDLE_WATCHER_UNIT_NAME}.service\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _build_idle_watcher_service_unit(sentinel_on_outer: str) -> str:
    """Build the oneshot systemd ``.service`` that powers the host off when idle.

    Powers the instance off with ``shutdown -P now``; EC2 then applies the
    instance's ``InstanceInitiatedShutdownBehavior`` (``stop`` for resumable
    idle-pause -- the default -- or ``terminate`` for ephemeral hosts), so no IAM
    role or awscli is involved.

    It removes the sentinel file BEFORE powering off. This is what makes resume
    work: when ``mngr start`` boots the instance again, systemd re-arms the
    ``.path`` unit -- if the sentinel were still present it would fire
    immediately and re-power-off the just-resumed instance. Clearing it first
    guarantees a clean slate on the next boot (the in-container watcher only
    re-creates it if the host is idle again).
    """
    return (
        "[Unit]\n"
        "Description=Power off this instance when mngr signals the host is idle\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart=/bin/sh -c 'rm -f {sentinel_on_outer} && shutdown -P now'\n"
    )


def _build_host_dir_sync_command(host_dir_on_outer: str, sync_target_uri: str) -> str:
    """Build the ``aws s3 sync ... --delete`` command the oneshot service runs.

    Syncs the per-host ``host_dir`` tree to ``hosts/<id>/host_dir/`` in the
    bucket, with ``--delete`` so a removed file is removed offline too, and a few
    excludes for large transient caches. Uses the instance profile's IMDS
    credentials implicitly (no creds on the command line).
    """
    excludes = " ".join(f'--exclude "{pattern}"' for pattern in _HOST_DIR_SYNC_EXCLUDES)
    return f'aws s3 sync "{host_dir_on_outer}/" "{sync_target_uri}" --delete {excludes}'.rstrip()


def _build_host_dir_sync_service_unit(host_dir_on_outer: str, sync_target_uri: str) -> str:
    """Build the oneshot systemd ``.service`` that pushes host_dir to the bucket once.

    Triggered periodically by the paired ``.timer`` and once on graceful stop.
    ``Type=oneshot`` so a stop-time ``systemctl start`` blocks until the sync
    completes (the offline copy is current before the instance powers off).
    """
    return (
        "[Unit]\n"
        "Description=Sync this host's host_dir to the mngr S3 state bucket for offline reads\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart=/bin/sh -c '{_build_host_dir_sync_command(host_dir_on_outer, sync_target_uri)}'\n"
    )


def _build_host_dir_sync_timer_unit(interval_seconds: int) -> str:
    """Build the systemd ``.timer`` that fires the host_dir sync every ``interval_seconds``.

    ``OnBootSec`` gives the host a moment to finish bootstrapping before the
    first sync; ``OnUnitActiveSec`` then repeats at the interval.
    """
    return (
        "[Unit]\n"
        "Description=Periodically sync this host's host_dir to the mngr S3 state bucket\n"
        "[Timer]\n"
        f"OnBootSec={interval_seconds}\n"
        f"OnUnitActiveSec={interval_seconds}\n"
        f"Unit={HOST_DIR_SYNC_UNIT_NAME}.service\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
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


class AwsProvider(TagMirrorVpsDockerProvider):
    """AWS-specific provider that discovers hosts via the EC2 DescribeInstances API."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    aws_client: AwsVpsClient = Field(frozen=True, description="EC2 API client")
    aws_config: AwsProviderConfig = Field(frozen=True, description="AWS-specific configuration")

    def _host_name_tag_key(self) -> str:
        return _HOST_NAME_TAG_KEY

    def _host_identity(self) -> S3StateHostIdentity | None:
        """Return the bucket-write IAM host identity (uncached), or None when unresolvable.

        Built fresh each call (it is cheap and used only at create / on rare
        diagnostics), scoped to the same state-bucket name as ``_state_bucket``.
        """
        return self.aws_config.build_host_identity(self.aws_client.session)

    @cached_property
    def _state_bucket(self) -> S3StateBucket | None:
        """Return the S3 state bucket when it actually exists, else None.

        When present, the bucket is the source of truth for agent records and the
        offline host record (replacing the EC2 tag mirror); when None
        (no bucket configured/derivable, or one whose name resolves but does not
        yet exist because ``mngr aws prepare`` was never run), mngr falls back to
        the per-agent tag mirror. The existence probe runs at most once per
        provider lifetime (cached).
        """
        return self._resolve_existing_state_bucket()

    def _resolve_existing_state_bucket(self) -> S3StateBucket | None:
        """Build the configured/derived bucket and return it only if it exists."""
        bucket = self.aws_config.build_state_bucket(self.aws_client.session)
        if bucket is None:
            return None
        try:
            if not bucket.bucket_exists():
                logger.debug(
                    "S3 state bucket {} does not exist; using the EC2 tag mirror "
                    "(run `mngr aws prepare` to create it)",
                    bucket.bucket_name,
                )
                return None
        except S3StateBucketError as e:
            logger.warning("Could not check S3 state bucket {}; falling back to EC2 tags: {}", bucket.bucket_name, e)
            return None
        return bucket

    @cached_property
    def _state_store(self) -> HostStateStore:
        """The external host/agent-record mirror: the S3 bucket when present, else the EC2 tag mirror.

        Selecting one store here lets the persist / remove / list / read paths
        below stop branching on bucket-vs-tags. Offline ``host_dir`` reads are a
        separate, bucket-only feature and stay keyed off ``_state_bucket``.
        """
        bucket = self._state_bucket
        if bucket is not None:
            return BucketHostStateStore(
                bucket=bucket, bucket_error_type=S3StateBucketError, bucket_label="S3 state bucket"
            )
        return _Ec2TagHostStateStore(provider=self)

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
        match parsed:
            case ParsedAwsBuildOptions(ami_id_override=ami_id_override, spot=spot):
                pass
            case _:
                raise MngrError(
                    f"AwsProvider._create_vps_instance expected ParsedAwsBuildOptions, "
                    f"got {type(parsed).__name__}. This indicates the parser hook returned a "
                    "non-AWS shape; _parse_build_args must return ParsedAwsBuildOptions."
                )
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

        Returns the bucket-write identity's profile only when host_dir sync is on,
        a state bucket is present, and the identity was actually provisioned by
        ``mngr aws prepare``. The operator-supplied ``iam_instance_profile`` (set
        on the client) takes precedence over this in ``create_instance``. Probing
        identity existence is best-effort: a failure degrades to None (no profile
        attached -- offline host_dir just won't work) rather than blocking create.
        Attaching a profile requires the create credentials to hold iam:PassRole.
        """
        if not self.aws_config.is_host_dir_synced_to_bucket:
            return None
        if self._state_bucket is None:
            return None
        identity = self._host_identity()
        if identity is None:
            return None
        try:
            if not identity.host_identity_exists():
                logger.warning(
                    "host_dir sync is on but the bucket-write IAM identity {} does not exist; launching "
                    "without it (run `mngr aws prepare --use-offline-host-dir yes` to enable offline host_dir)",
                    identity.identity_name,
                )
                return None
        except S3StateHostIdentityError as e:
            logger.warning(
                "Could not check the bucket-write IAM identity {}; launching without it: {}",
                identity.identity_name,
                e,
            )
            return None
        return identity.identity_name

    def _list_provider_vps_hostnames(self) -> list[str]:
        """Return public IPs of EC2 instances tagged with this provider's name.

        Credentials are guaranteed to be resolvable here: ``build_provider_instance``
        raises ``ProviderUnavailableError`` when ``config.get_session()`` fails, so any
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

    # =========================================================================
    # Native EC2 stop/start (idle-pause + resume)
    # =========================================================================

    def stop_host(
        self,
        host: HostInterface | HostId,
        create_snapshot: bool = True,
        timeout_seconds: float = 60.0,
        stop_reason: HostState | None = None,
    ) -> None:
        """Stop the agent container *and* the EC2 instance, preserving the EBS volume.

        The base ``VpsDockerProvider.stop_host`` only stops the inner Docker
        container, leaving the EC2 instance running and billing. This override
        additionally calls ``ec2 stop-instances`` so a paused AWS agent costs
        only EBS storage; the root volume (and all on-disk state) survives, so
        ``start_host`` can resume it.

        ``create_snapshot`` is intentionally ignored -- native EC2 stop preserves
        the whole filesystem, so the base's docker-commit snapshot would be
        redundant. The base container-stop + record-write is reused via ``super()``,
        passing ``stop_reason=STOPPED`` so that single write marks the host STOPPED
        (so the offline-state derivation reports STOPPED, not CRASHED, while the
        instance is down) -- no second record write is needed. The write lands
        before the instance stops, since the volume is unreachable once it does.
        """
        del create_snapshot
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.config is None or host_record.vps_ip is None:
            raise HostNotFoundError(self.name, host_id)
        super().stop_host(
            host, create_snapshot=False, timeout_seconds=timeout_seconds, stop_reason=stop_reason or HostState.STOPPED
        )
        # Push host_dir one final time while the instance is still reachable, so
        # the offline copy in the bucket is current the moment it stops. The
        # container is already stopped (super() above), so host_dir is quiesced.
        self._trigger_final_host_dir_sync(host_id, host_record.vps_ip)
        with log_span("Stopping EC2 instance"):
            self.aws_client.stop_instance(host_record.config.vps_instance_id)

    def start_host(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId | None = None,
    ) -> Host:
        """Resume a stopped AWS agent: start the EC2 instance, then its container.

        A stopped EC2 instance is not SSH-reachable and has no public IP, so it
        is located by its ``mngr-host-id`` tag (not the SSH-based host-record
        lookup), started, and its fresh public IP read back. The instance keeps
        its SSH host keys across a stop/start (they live on the EBS volume), so
        we re-point known_hosts at the new IP and rewrite the persisted record's
        ``vps_ip`` before delegating the container start to ``super()`` (whose
        ``_find_host_record`` reads our refreshed cache entry, so no stale
        rediscovery is needed).
        """
        host_id = host.id if isinstance(host, HostInterface) else host
        instance = self._find_instance_for_host(host_id)
        if instance is None:
            raise HostNotFoundError(self.name, host_id)
        instance_id = VpsInstanceId(instance["id"])
        with log_span("Starting EC2 instance"):
            new_ip = self.aws_client.start_instance(instance_id)
        # The cached instance list predates the start (stale state / no public
        # IP); drop it so any later discovery sees the running instance + new IP.
        self._instances_cache = None
        # Rebind known_hosts to the new IP from mngr's local host keypairs BEFORE
        # connecting -- the instance kept its host keys across the stop/start, but
        # the IP changed, and the record (the other key source) can't be read
        # until we can SSH in. The local keypairs are what was injected at create,
        # so they match what the resumed instance presents.
        self._rebind_known_hosts_pre_connect(new_ip)
        with log_span("Waiting for VPS SSH after start"):
            self._wait_for_sshd_on_vps(new_ip, timeout_seconds=self.config.ssh_connect_timeout)
        with self._make_outer_for_vps_ip(new_ip) as outer:
            host_store = open_host_store(outer, host_volume_name_for(host_id))
            record = host_store.read_host_record()
            if record is None or record.config is None:
                raise HostNotFoundError(self.name, host_id)
            self._rebind_known_hosts(record, new_ip)
            # Clear any stale idle sentinel so the freshly-resumed instance isn't
            # immediately re-stopped by the systemd path unit (belt-and-suspenders;
            # the self-stop service also removes it when it fires).
            outer.execute_idempotent_command(f"rm -f {self._idle_sentinel_path_on_outer(host_id)}")
            certified = record.certified_host_data
            updated_data = certified.model_copy_update(
                to_update(certified.field_ref().stop_reason, None),
                to_update(certified.field_ref().updated_at, datetime.now(timezone.utc)),
            )
            updated_record = record.model_copy_update(
                to_update(record.field_ref().vps_ip, new_ip),
                to_update(record.field_ref().certified_host_data, updated_data),
            )
            host_store.write_host_record(updated_record)
            self._persist_host_record_externally(updated_record)
        # Drop any cached Host bound to the old IP, then seed the record cache so
        # super().start_host()'s _find_host_record returns the rebound record.
        self._evict_cached_host(host_id)
        self._host_record_cache[host_id] = updated_record
        started = super().start_host(host_id, snapshot_id)
        # The base ``start_host`` (called above) relaunches the in-container
        # activity watcher and refreshes BOOT activity on resume, so auto-stop-on-
        # idle keeps working across resumes without an AWS-specific step here.
        return started

    def _rebind_known_hosts(self, record: VpsDockerHostRecord, new_ip: str) -> None:
        """Re-point local known_hosts at ``new_ip`` using the instance's preserved host keys.

        EC2 stop/start keeps the instance's SSH host keys, so only the IP
        changes. Drop any stale entries for the old IP, then add the new IP with
        the recorded VPS (port 22) and container host keys.
        """
        old_ip = record.vps_ip
        if old_ip is not None and old_ip != new_ip:
            remove_host_from_known_hosts(self._vps_known_hosts_path(), old_ip, 22)
            remove_host_from_known_hosts(self._container_known_hosts_path(), old_ip, self.config.container_ssh_port)
        if record.ssh_host_public_key is not None:
            add_host_to_known_hosts(
                known_hosts_path=self._vps_known_hosts_path(),
                hostname=new_ip,
                port=22,
                public_key=record.ssh_host_public_key,
            )
        if record.container_ssh_host_public_key is not None:
            add_host_to_known_hosts(
                known_hosts_path=self._container_known_hosts_path(),
                hostname=new_ip,
                port=self.config.container_ssh_port,
                public_key=record.container_ssh_host_public_key,
            )

    def _rebind_known_hosts_pre_connect(self, new_ip: str) -> None:
        """Add ``new_ip`` to known_hosts using mngr's local, authoritative host keys.

        Runs on resume *before* any SSH connection (the host record, the other key
        source, can't be read until we can connect). The VPS/container host
        keypairs are generated and held locally by mngr -- per provider instance,
        in ``_key_dir()`` -- and injected into the instance at create time, so the
        public keys here are exactly the ones the resumed instance presents.
        Sourcing them locally (rather than from EC2 tags, which any principal with
        ``ec2:CreateTags`` could rewrite) keeps SSH host-key verification anchored
        to data mngr controls, not to account-writable instance metadata.
        """
        add_host_to_known_hosts(
            known_hosts_path=self._vps_known_hosts_path(),
            hostname=new_ip,
            port=22,
            public_key=self._get_vps_host_keypair()[1],
        )
        add_host_to_known_hosts(
            known_hosts_path=self._container_known_hosts_path(),
            hostname=new_ip,
            port=self.config.container_ssh_port,
            public_key=self._get_container_host_keypair()[1],
        )

    # =========================================================================
    # Self-stopping idle watcher (in-container sentinel + host-side systemd)
    # =========================================================================

    def _create_shutdown_script(self, host: Host) -> None:
        """Write an in-container ``shutdown.sh`` that signals idle via a sentinel file.

        The base ``VpsDockerProvider._create_shutdown_script`` writes a script
        that runs ``kill -TERM 1`` to stop the container on idle. For AWS, an
        idle container should stop the whole *instance* (so a paused agent costs
        only EBS), but a container cannot power off its host. Instead, the
        in-container watcher touches a sentinel file on the shared volume; a
        host-side systemd path unit (installed in ``_on_host_finalized``) observes
        it and powers the host off (EC2 then stops or terminates per
        ``InstanceInitiatedShutdownBehavior``). Mirrors the base's
        mkdir/write/chmod, swapping only the script body.
        """
        sentinel_in_container = str(host.host_dir / "commands" / IDLE_SENTINEL_FILENAME)
        shutdown_script = _build_sentinel_shutdown_script(sentinel_in_container)
        commands_dir = host.host_dir / "commands"
        host.execute_idempotent_command(f"mkdir -p {commands_dir}")
        host.write_file(commands_dir / "shutdown.sh", shutdown_script.encode())
        host.execute_idempotent_command(f"chmod +x {commands_dir / 'shutdown.sh'}")

    def _on_host_finalized(self, *, host_id: HostId, vps_ip: str) -> None:
        """Install the host-side systemd idle watcher that self-stops this instance.

        Runs after the host record is durably written. Installs (on the outer
        host) a systemd ``.path``/``.service`` pair: the path unit watches the
        outer-filesystem location of the in-container idle sentinel and, when it
        appears, the oneshot service powers the host off -- EC2 then stops or
        terminates the instance per ``InstanceInitiatedShutdownBehavior`` (no IAM
        or awscli needed).

        This is best-effort: per the base-class contract, it MUST NOT raise.
        Any failure (record lookup, SSH, unit install) is caught and logged at
        WARNING; the only consequence is that the agent will not auto-stop on
        idle (manual ``mngr stop --stop-host`` still works).
        """
        try:
            self._install_idle_watcher(host_id=host_id, vps_ip=vps_ip)
        except MngrError as e:
            # The install only issues SSH / file-write / command operations, which
            # surface as MngrError (HostConnectionError is a MngrError subclass);
            # a failure just means no auto-stop on idle, so log and move on rather
            # than fail create_host after the host record is already durable.
            logger.warning(
                "AWS idle watcher install failed for host {} ({}); the agent will not "
                "auto-stop on idle, but `mngr stop --stop-host` still works",
                host_id,
                e,
            )
        try:
            self._install_host_dir_sync(host_id=host_id, vps_ip=vps_ip)
        except MngrError as e:
            # Same best-effort contract as the idle watcher: a failure just means
            # the stopped host's host_dir won't be readable offline (manual reads
            # over SSH while running still work), so log and move on.
            logger.warning(
                "AWS host_dir sync install failed for host {} ({}); the stopped host's host_dir "
                "will not be readable offline",
                host_id,
                e,
            )

    def _install_host_dir_sync(self, *, host_id: HostId, vps_ip: str) -> None:
        """Install the host-side host_dir-to-bucket sync daemon on the outer host.

        Gated on ``is_host_dir_synced_to_bucket`` AND a state bucket being
        present (no bucket -> nothing to sync to). Installs awscli (best-effort,
        apt) and a systemd oneshot ``.service`` + ``.timer`` pair that runs
        ``aws s3 sync`` every ``HOST_DIR_SYNC_INTERVAL_SECONDS`` using the
        instance profile's IMDS credentials. Returns early (no-op) when the
        feature is off or no bucket is configured.
        """
        if not self.aws_config.is_host_dir_synced_to_bucket:
            return
        bucket = self._state_bucket
        if bucket is None:
            logger.debug("No S3 state bucket; skipping host_dir sync install for host {}", host_id)
            return
        host_dir_on_outer = self._host_dir_path_on_outer(host_id)
        sync_target_uri = host_dir_sync_target_for(bucket.bucket_name, host_id)
        service_unit = _build_host_dir_sync_service_unit(str(host_dir_on_outer), sync_target_uri)
        timer_unit = _build_host_dir_sync_timer_unit(HOST_DIR_SYNC_INTERVAL_SECONDS)
        with log_span("Installing AWS host_dir sync daemon"):
            with self._make_outer_for_vps_ip(vps_ip) as outer:
                outer.execute_idempotent_command(_build_awscli_install_command(), timeout_seconds=300.0)
                outer.write_text_file(Path(f"/etc/systemd/system/{HOST_DIR_SYNC_UNIT_NAME}.service"), service_unit)
                outer.write_text_file(Path(f"/etc/systemd/system/{HOST_DIR_SYNC_UNIT_NAME}.timer"), timer_unit)
                outer.execute_idempotent_command("systemctl daemon-reload")
                outer.execute_idempotent_command(f"systemctl enable --now {HOST_DIR_SYNC_UNIT_NAME}.timer")
        logger.info("AWS host_dir sync daemon installed for host {} (target {})", host_id, sync_target_uri)

    def _trigger_final_host_dir_sync(self, host_id: HostId, vps_ip: str) -> None:
        """Run the host_dir sync once (best-effort) so the offline copy is current before stop.

        Called from ``stop_host`` while the instance is still reachable. Starts
        the oneshot sync service synchronously (``--wait`` blocks until it
        finishes). Best-effort: any failure is logged at WARNING and swallowed so
        a sync hiccup never blocks the stop -- the offline copy is then simply
        "as of the last periodic sync".
        """
        if not self.aws_config.is_host_dir_synced_to_bucket or self._state_bucket is None:
            return
        try:
            with log_span("Triggering final host_dir sync before stop"):
                with self._make_outer_for_vps_ip(vps_ip) as outer:
                    outer.execute_idempotent_command(
                        f"systemctl start --wait {HOST_DIR_SYNC_UNIT_NAME}.service", timeout_seconds=300.0
                    )
        except MngrError as e:
            logger.warning(
                "Final host_dir sync before stopping host {} failed; the offline copy will be as of "
                "the last periodic sync: {}",
                host_id,
                e,
            )

    def _install_idle_watcher(self, *, host_id: HostId, vps_ip: str) -> None:
        """Install the systemd path/service idle watcher on the outer host.

        Separated from ``_on_host_finalized`` so the no-raise wrapping stays a
        thin try/except. Returns early (after a WARNING) when the host record is
        missing. The watcher powers the host off when the in-container idle
        sentinel appears; EC2's ``InstanceInitiatedShutdownBehavior`` decides
        stop-vs-terminate, so no awscli or IAM is involved.
        """
        record = self._find_host_record(host_id)
        if record is None or record.config is None:
            logger.warning(
                "AWS idle watcher: no host record for {}; skipping watcher install (no auto-stop)",
                host_id,
            )
            return
        sentinel_on_outer = self._idle_sentinel_path_on_outer(host_id)
        with log_span("Installing AWS idle self-stop watcher"):
            with self._make_outer_for_vps_ip(vps_ip) as outer:
                outer.write_text_file(
                    Path(f"/etc/systemd/system/{IDLE_WATCHER_UNIT_NAME}.path"),
                    _build_idle_watcher_path_unit(str(sentinel_on_outer)),
                )
                outer.write_text_file(
                    Path(f"/etc/systemd/system/{IDLE_WATCHER_UNIT_NAME}.service"),
                    _build_idle_watcher_service_unit(str(sentinel_on_outer)),
                )
                outer.execute_idempotent_command("systemctl daemon-reload")
                outer.execute_idempotent_command(f"systemctl enable --now {IDLE_WATCHER_UNIT_NAME}.path")
        logger.info("AWS idle self-stop watcher installed for host {}", host_id)

    # =========================================================================
    # Offline metadata via EC2 tags (so STOPPED hosts list + resolve by name)
    # =========================================================================

    def _persist_host_record_externally(self, record: VpsDockerHostRecord) -> None:
        """Mirror the full host record into the external store (best-effort).

        Delegates to the selected store: the S3 bucket writes the full record; the
        tag mirror is a no-op (the instance's own tags carry it).
        """
        self._state_store.persist_host_record(record)

    def _delete_host_record_externally(self, host_id: HostId) -> None:
        """Delete the host's state from the external store (best-effort, idempotent).

        Delegates to the selected store: the S3 bucket deletes the host's prefix;
        the tag mirror is a no-op (destroying the instance drops its tags).
        """
        self._state_store.delete_host_state(host_id)

    def persist_agent_data(self, host_id: HostId, agent_data: Mapping[str, object]) -> None:
        """Persist an agent's record on the host volume *and* in the external store.

        The base ``VpsDockerProvider`` writes the agent record to the on-volume
        host store (``agents/<id>.json``), the authoritative source the SSH-based
        discovery reads for *running* hosts -- so this override must keep doing
        that (via ``super()``) so running hosts list their agents. The on-volume
        write is best-effort: a *stopped* host raises ``HostNotFoundError`` (no
        reachable ``vps_ip``), in which case only the external mirror is written.

        The external mirror is the S3 bucket (full record, no size limit) when
        one is configured, else the per-field EC2 tag mirror
        (``mngr-agent-<id>-<field>``), which caps each value at 256 chars and the
        instance at 50 tags. ``_state_store`` selects between them.
        """
        try:
            super().persist_agent_data(host_id, agent_data)
        except HostNotFoundError:
            # Host stopped / unreachable: the on-volume store can't be written,
            # but the external mirror below still must be (e.g. offline `mngr label`).
            logger.debug("Host {} unreachable; persisting agent data to the external store only", host_id)
        agent_id = agent_data.get("id")
        if agent_id is None:
            logger.warning("Cannot mirror agent data without an id (name={!r})", agent_data.get("name"))
            return
        self._state_store.persist_agent_record(host_id, str(agent_id), agent_data)

    def _persist_agent_to_tags(self, host_id: HostId, agent_id: str, agent_data: Mapping[str, object]) -> None:
        """Mirror an agent record into per-field EC2 tags (no-bucket fallback)."""
        instance = self._find_instance_for_host(host_id)
        if instance is None:
            logger.warning("No EC2 instance found for host {}; cannot persist agent tags", host_id)
            return
        set_tags, delete_keys = self._agent_field_tags(agent_id, agent_data, instance)
        try:
            self.aws_client.add_tags(VpsInstanceId(instance["id"]), set_tags)
        except VpsApiError as e:
            # EC2 caps a resource at 50 (non-aws:) tags. Hitting it means the host
            # has more agents than the tag mirror can hold; surface it as a
            # NotImplementedError so the CLI offers to open an issue rather than
            # failing obscurely. Configuring a state bucket (mngr aws prepare)
            # removes this ceiling entirely.
            if "TagLimitExceeded" in str(e):
                raise NotImplementedError(
                    f"The AWS host for agent {agent_id!r} has reached EC2's {_AWS_MAX_TAGS_PER_INSTANCE}-tag-per-"
                    "instance limit, so this agent can't be mirrored to tags for stopped-host listing and "
                    "resume-by-name. Run `mngr aws prepare` to create an S3 state bucket (which has no such "
                    "limit), or open an issue at https://github.com/imbue-ai/mngr/issues."
                ) from e
            raise
        if delete_keys:
            self.aws_client.remove_tags(VpsInstanceId(instance["id"]), delete_keys)

    def remove_persisted_agent_data(self, host_id: HostId, agent_id: AgentId) -> None:
        """Remove the agent's on-volume record *and* its external mirror.

        Mirrors ``persist_agent_data``: the base removes the authoritative
        on-volume record (best-effort -- ``HostNotFoundError`` when the instance
        is stopped) and this override additionally drops the agent from the
        external store (S3 bucket when configured, else the per-field EC2 tags),
        so a destroyed agent stops appearing in both running- and stopped-host
        discovery. Both external removals are idempotent.
        """
        try:
            super().remove_persisted_agent_data(host_id, agent_id)
        except HostNotFoundError:
            logger.debug("Host {} unreachable; removing agent data from the external store only", host_id)
        self._state_store.remove_agent_record(host_id, str(agent_id))

    def _is_instance_offline(self, instance: Mapping[str, Any]) -> bool:
        """Whether the EC2 instance's OS is down (stopping or stopped).

        EC2 power state rides along for free in the ``DescribeInstances`` listing,
        so this needs no extra call. Gate on state, not ``main_ip`` -- a
        ``stopping`` instance can still report a public IP while its OS is already
        off, and gating on the IP would make the host vanish for the (seconds-long)
        stop transition.
        """
        return instance.get("state") in _HOST_DOWN_STATES

    def _offline_agent_dicts_for(self, host_id: HostId, instance: Mapping[str, Any] | None = None) -> list[dict]:
        """Read a stopped host's agent records from the external store (S3 bucket or EC2 tag mirror).

        Overrides the base tag/metadata reconstruction so a bucket-mode host --
        whose agents live in the bucket, not in EC2 tags -- still surfaces its
        agents offline. ``_state_store`` selects bucket vs tags; the ``instance``
        argument is unused (the store is keyed by ``host_id``).
        """
        del instance
        return self._state_store.list_agent_records(host_id)

    def to_offline_host(self, host_id: HostId) -> OfflineHost:
        """Return an offline host, reconstructing a STOPPED instance's record offline.

        Falls back to the base (SSH/volume-backed) path first; if that can't find
        the host (because it is stopped and unreachable), reconstruct it from the
        external store: the *full* ``VpsDockerHostRecord`` when the store has it
        (S3 ``host_state.json``), otherwise a minimal record rebuilt from the
        instance's own EC2 tags -- which also covers a bucket-mode host created
        before the bucket existed (so its ``host_state.json`` is absent). Calls
        the SSH-only ``VpsDockerProvider`` path directly so the
        ``OfflineCapableVpsDockerProvider`` tag fallback does not pre-empt the
        bucket-aware reconstruction below.
        """
        try:
            return VpsDockerProvider.to_offline_host(self, host_id)
        except HostNotFoundError:
            record = self._state_store.read_host_record(host_id)
            # In bucket mode, fall back to the instance's own tags for a host whose
            # host_state.json is absent (created before the bucket existed). The tag
            # store already reconstructs from tags, so this fallback is bucket-only.
            if record is None and self._state_bucket is not None:
                record = self._host_record_from_instance_tags(host_id)
            if record is None:
                raise
            return self._create_offline_host(record)

    # =========================================================================
    # Offline host_dir volume (reads via the operator's credentials)
    # =========================================================================

    def get_volume_reference_for_host(self, host: HostInterface | HostId) -> HostVolume | None:
        """Return a bucket-backed host_dir volume *reference* (cheap, no network probe).

        Used by ``make_readable_offline_host`` during discovery, so it must stay
        cheap: it only builds the scoped ``S3Volume`` (no S3 call) when host_dir
        sync is on and a state bucket is present. Reads use the operator's
        credentials, so no instance identity is needed here. Returns None when the
        feature is off or no bucket is configured.
        """
        if not self.aws_config.is_host_dir_synced_to_bucket:
            return None
        bucket = self._state_bucket
        if bucket is None:
            return None
        host_id = host.id if isinstance(host, HostInterface) else host
        return HostVolume(volume=bucket.volume_for_host(host_id))

    def get_volume_for_host(self, host: HostInterface | HostId) -> HostVolume | None:
        """Return the bucket-backed host_dir volume, with a light existence probe.

        Like ``get_volume_reference_for_host`` but additionally confirms the
        host's ``host_dir/`` prefix actually has objects (a cheap ``list`` with
        ``MaxKeys=1``). When the prefix is empty, runs the missing-identity
        diagnostic (Decision 7) -- a clear WARNING pointing at
        ``mngr aws prepare --use-offline-host-dir yes`` if the instance has no
        attached IAM profile -- and returns None (callers treat None as "not
        available"). This never raises.
        """
        reference = self.get_volume_reference_for_host(host)
        if reference is None:
            return None
        bucket = self._state_bucket
        if bucket is None:
            return None
        host_id = host.id if isinstance(host, HostInterface) else host
        try:
            if not bucket.host_dir_prefix_has_objects(host_id):
                self._warn_if_host_dir_identity_missing(host_id)
                return None
        except S3StateBucketError as e:
            logger.warning(
                "Could not probe host_dir prefix for host {}; treating volume as unavailable: {}", host_id, e
            )
            return None
        return reference

    def _warn_if_host_dir_identity_missing(self, host_id: HostId) -> None:
        """Warn (non-fatally) when an empty host_dir prefix is explained by a missing IAM identity.

        Detects the Decision-7 case directly from cloud state: when the host's
        instance has no attached IAM instance profile, the on-box sync daemon
        could never push host_dir, which is why the prefix is empty. Points the
        user at ``mngr aws prepare --use-offline-host-dir yes`` (and recreating
        the host so it picks up the profile). Best-effort: any probe failure is
        swallowed (this is purely advisory).
        """
        try:
            instance = self._find_instance_for_host(host_id)
            if instance is None:
                return
            profile_arn = self.aws_client.get_instance_iam_profile_arn(VpsInstanceId(instance["id"]))
        except MngrError as e:
            logger.debug("Could not check IAM profile for host {} while diagnosing empty host_dir: {}", host_id, e)
            return
        if profile_arn is None:
            logger.warning(
                "Host {}'s instance has no attached IAM instance profile, so its host_dir was never "
                "pushed to the bucket and is not readable offline. Run `mngr aws prepare "
                "--use-offline-host-dir yes`, then recreate the host so it picks up the profile.",
                host_id,
            )


class _Ec2TagHostStateStore(HostStateStore):
    """Tag-backed host-state mirror: the instance's own EC2 tags are the store (no-bucket fallback).

    Compact (256-char per value, 50-tag-per-instance) and keyed off the live
    instance, so the host record / agent records are reconstructed from the
    instance's ``mngr-*`` tags. Delegates the tag I/O to the owning provider,
    which already resolves instances from its cached ``DescribeInstances`` listing.
    """

    provider: AwsProvider

    def persist_host_record(self, record: VpsDockerHostRecord) -> None:
        # The instance's own create/stop tags carry the host record; nothing extra to write.
        pass

    def delete_host_state(self, host_id: HostId) -> None:
        # Destroying the instance drops its tags, so there is no separate state to delete.
        pass

    def persist_agent_record(self, host_id: HostId, agent_id: str, agent_data: Mapping[str, object]) -> None:
        self.provider._persist_agent_to_tags(host_id, agent_id, agent_data)

    def remove_agent_record(self, host_id: HostId, agent_id: str) -> None:
        instance = self.provider._find_instance_for_host(host_id)
        if instance is None:
            return
        keys = [f"{AGENT_TAG_PREFIX}{agent_id}-{field}" for field in AGENT_TAG_FIELDS]
        self.provider.aws_client.remove_tags(VpsInstanceId(instance["id"]), keys)

    def list_agent_records(self, host_id: HostId) -> list[dict]:
        instance = self.provider._find_instance_for_host(host_id)
        if instance is None:
            return []
        return self.provider._persisted_agent_dicts_from_instance(instance)

    def read_host_record(self, host_id: HostId) -> VpsDockerHostRecord | None:
        return self.provider._host_record_from_instance_tags(host_id)


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
