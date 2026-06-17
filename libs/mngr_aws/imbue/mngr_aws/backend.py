import os
from collections.abc import Mapping
from collections.abc import Sequence
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
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.errors import TagLimitExceededError
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.interfaces.volume import HostVolume
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_aws import hookimpl
from imbue.mngr_aws.cli import aws_cli_group
from imbue.mngr_aws.client import AwsVpsClient
from imbue.mngr_aws.config import AwsProviderConfig
from imbue.mngr_aws.state_bucket import S3StateBucket
from imbue.mngr_aws.state_bucket import S3StateBucketError
from imbue.mngr_aws.state_bucket import S3StateHostIdentity
from imbue.mngr_aws.state_bucket import S3StateHostIdentityError
from imbue.mngr_aws.state_bucket import host_dir_sync_target_for
from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.host_state_store import BucketHostStateStore
from imbue.mngr_vps_docker.host_state_store import HostDirBackend
from imbue.mngr_vps_docker.host_state_store import HostStateStore
from imbue.mngr_vps_docker.host_state_store import NullHostDirBackend
from imbue.mngr_vps_docker.host_store import VpsDockerHostRecord
from imbue.mngr_vps_docker.instance import AGENT_TAG_FIELDS
from imbue.mngr_vps_docker.instance import AGENT_TAG_PREFIX
from imbue.mngr_vps_docker.instance import ParsedVpsBuildOptions
from imbue.mngr_vps_docker.instance import TagMirrorVpsDockerProvider
from imbue.mngr_vps_docker.instance import build_oneshot_sync_service_unit
from imbue.mngr_vps_docker.instance import build_poweroff_idle_watcher_service_unit
from imbue.mngr_vps_docker.instance import build_sync_timer_unit
from imbue.mngr_vps_docker.instance import extract_git_depth
from imbue.mngr_vps_docker.instance import extract_presence_flag
from imbue.mngr_vps_docker.instance import extract_single_value_arg
from imbue.mngr_vps_docker.instance import raise_if_unknown_provider_arg
from imbue.mngr_vps_docker.instance import raise_if_vps_migration_arg
from imbue.mngr_vps_docker.primitives import VpsInstanceId

AWS_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("aws")

# EC2 allows 50 (non-``aws:``) tags per resource. When a host has so many agents
# that mirroring another would exceed this, we surface a TagLimitExceededError
# pointing the user at the S3 state bucket (which has no such ceiling) rather
# than failing obscurely.
_AWS_MAX_TAGS_PER_INSTANCE: Final[int] = 50
# EC2 states in which the host OS is down (so the SSH-based sweep can't see the
# host) but the instance still exists and its agents must be reconstructed from
# tags. ``stopping`` is included so a host doesn't vanish from discovery during
# the (seconds-long) stop transition before it reaches the terminal ``stopped``.
_HOST_DOWN_STATES: Final[frozenset[str]] = frozenset({"stopping", "stopped"})
# The host name is mirrored into the EC2 ``Name`` tag (as ``mngr-<host_name>``).
_HOST_NAME_TAG_KEY: Final[str] = "Name"

# Host-side host_dir sync daemon (Component 3 of specs/provider-state-bucket).
# When ``is_offline_host_dir_enabled`` is on and a state bucket is present, the
# create path attaches the prepare-provisioned IAM instance profile, then
# installs (over SSH on the outer) a systemd oneshot ``.service`` + ``.timer``
# pair: every ``HOST_DIR_SYNC_INTERVAL_SECONDS`` the oneshot runs
# ``aws s3 sync <host_dir_on_outer>/ s3://<bucket>/hosts/<id>/host_dir/ --delete``
# using the instance profile's IMDS credentials (no long-lived keys on the box).
# The same oneshot is also triggered once on graceful stop so the offline copy is
# current. Offline reads are served from the bucket by the operator's credentials.
HOST_DIR_SYNC_UNIT_NAME: Final[str] = "mngr-aws-host-dir-sync"
HOST_DIR_SYNC_INTERVAL_SECONDS: Final[int] = 60
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


def _build_host_dir_sync_service_unit(host_dir_on_outer: str, sync_target_uri: str) -> str:
    """Build the oneshot systemd ``.service`` that pushes host_dir to the bucket once.

    Triggered periodically by the paired ``.timer`` and once on graceful stop.
    ``Type=oneshot`` so a stop-time ``systemctl start`` blocks until the sync
    completes (the offline copy is current before the instance powers off).
    """
    return build_oneshot_sync_service_unit(
        "Sync this host's host_dir to the mngr S3 state bucket for offline reads",
        _build_host_dir_sync_command(host_dir_on_outer, sync_target_uri),
    )


def _build_host_dir_sync_timer_unit(interval_seconds: int) -> str:
    """Build the systemd ``.timer`` that fires the host_dir sync every ``interval_seconds``."""
    return build_sync_timer_unit(
        "Periodically sync this host's host_dir to the mngr S3 state bucket",
        interval_seconds,
        HOST_DIR_SYNC_UNIT_NAME,
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
                bucket=bucket,
                bucket_error_type=S3StateBucketError,
                bucket_label="S3 state bucket",
                fallback=_Ec2TagHostStateStore(provider=self),
            )
        return _Ec2TagHostStateStore(provider=self)

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

        Delegates to the selected host_dir backend (the no-op backend returns None
        when the feature is off or no bucket exists). The operator-supplied
        ``iam_instance_profile`` (set on the client) takes precedence over this in
        ``create_instance``. Attaching a profile requires create credentials to
        hold iam:PassRole.
        """
        return self._host_dir_backend.create_identity()

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
    # Native EC2 stop/start + idle-watcher hooks (for OfflineCapableVpsDockerProvider)
    # =========================================================================

    def _pause_cloud_instance(self, instance_id: VpsInstanceId) -> None:
        """Stop the EC2 instance; the EBS root volume and all on-disk state survive."""
        with log_span("Stopping EC2 instance"):
            self.aws_client.stop_instance(instance_id)

    def _resume_cloud_instance(self, instance_id: VpsInstanceId) -> str:
        """Start the EC2 instance and return its fresh public IP (a stop/start reassigns it)."""
        with log_span("Starting EC2 instance"):
            return self.aws_client.start_instance(instance_id)

    def _idle_watcher_service_unit(self, sentinel_on_outer: str) -> str:
        """Idle action: power the host off; EC2 then applies InstanceInitiatedShutdownBehavior."""
        return build_poweroff_idle_watcher_service_unit(sentinel_on_outer)

    def _on_host_finalized(self, *, host_id: HostId, vps_ip: str) -> None:
        """Install the base idle watcher and, when enabled, the host_dir-to-bucket sync daemon.

        Best-effort per the base contract: a host_dir-sync install failure only costs
        offline host_dir readability, so it is logged rather than failing create_host.
        """
        super()._on_host_finalized(host_id=host_id, vps_ip=vps_ip)
        try:
            self._host_dir_backend.install_sync(host_id=host_id, vps_ip=vps_ip)
        except MngrError as e:
            logger.warning(
                "AWS host_dir sync install failed for host {} ({}); the stopped host's host_dir "
                "will not be readable offline",
                host_id,
                e,
            )

    # =========================================================================
    # Offline metadata via EC2 tags (so STOPPED hosts list + resolve by name)
    # =========================================================================

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
            # has more agents than the tag mirror can hold; surface an actionable
            # TagLimitExceededError pointing at the S3 state bucket, which has no
            # such ceiling and supersedes the tag mirror once it exists.
            if "TagLimitExceeded" in str(e):
                raise TagLimitExceededError(
                    self.name,
                    limit=_AWS_MAX_TAGS_PER_INSTANCE,
                    remediation=(
                        f"The AWS host for agent {agent_id!r} has more agents than EC2 tags can mirror for "
                        "stopped-host listing and resume-by-name. Run `mngr aws prepare` to create an S3 state "
                        "bucket (no tag ceiling); the provider uses it automatically once it exists. To pin a "
                        "specific bucket name first, run "
                        f"`mngr config set --scope user providers.{self.name}.state_bucket_name <name>`."
                    ),
                ) from e
            raise
        if delete_keys:
            self.aws_client.remove_tags(VpsInstanceId(instance["id"]), delete_keys)

    def _is_instance_offline(self, instance: Mapping[str, Any]) -> bool:
        """Whether the EC2 instance's OS is down (stopping or stopped).

        EC2 power state rides along for free in the ``DescribeInstances`` listing,
        so this needs no extra call. Gate on state, not ``main_ip`` -- a
        ``stopping`` instance can still report a public IP while its OS is already
        off, and gating on the IP would make the host vanish for the (seconds-long)
        stop transition.
        """
        return instance.get("state") in _HOST_DOWN_STATES


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
        return self.provider._agent_dicts_from_tags(host_id)

    def read_host_record(self, host_id: HostId) -> VpsDockerHostRecord | None:
        return self.provider._host_record_from_instance_tags(host_id)


class _S3HostDirBackend(HostDirBackend):
    """Bucket-backed offline host_dir for AWS: instance-profile + ``aws s3 sync`` to the S3 state bucket.

    Selected only when offline host_dir is on and the state bucket exists, so
    ``self.bucket`` is always present here and no method re-tests it. Holds a
    back-reference to the provider for the SSH-to-outer / cloud-client / path
    plumbing the sync needs (the same pattern as ``_Ec2TagHostStateStore``).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    provider: AwsProvider
    bucket: S3StateBucket

    def create_identity(self) -> str | None:
        identity = self.provider._host_identity()
        if identity is None:
            return None
        try:
            if not identity.host_identity_exists():
                logger.warning(
                    "host_dir sync is on but the bucket-write IAM identity {} does not exist; launching "
                    "without it (re-run `mngr aws prepare` with sufficient IAM to enable offline host_dir)",
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

    def install_sync(self, *, host_id: HostId, vps_ip: str) -> None:
        host_dir_on_outer = self.provider._host_dir_path_on_outer(host_id)
        sync_target_uri = host_dir_sync_target_for(self.bucket.bucket_name, host_id)
        service_unit = _build_host_dir_sync_service_unit(str(host_dir_on_outer), sync_target_uri)
        timer_unit = _build_host_dir_sync_timer_unit(HOST_DIR_SYNC_INTERVAL_SECONDS)
        with log_span("Installing AWS host_dir sync daemon"):
            with self.provider._make_outer_for_vps_ip(vps_ip) as outer:
                outer.execute_idempotent_command(_build_awscli_install_command(), timeout_seconds=300.0)
                outer.write_text_file(Path(f"/etc/systemd/system/{HOST_DIR_SYNC_UNIT_NAME}.service"), service_unit)
                outer.write_text_file(Path(f"/etc/systemd/system/{HOST_DIR_SYNC_UNIT_NAME}.timer"), timer_unit)
                outer.execute_idempotent_command("systemctl daemon-reload")
                outer.execute_idempotent_command(f"systemctl enable --now {HOST_DIR_SYNC_UNIT_NAME}.timer")
        logger.info("AWS host_dir sync daemon installed for host {} (target {})", host_id, sync_target_uri)

    def trigger_final_sync(self, host_id: HostId, vps_ip: str) -> None:
        try:
            with log_span("Triggering final host_dir sync before stop"):
                with self.provider._make_outer_for_vps_ip(vps_ip) as outer:
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

    def volume_reference(self, host_id: HostId) -> HostVolume | None:
        return HostVolume(volume=self.bucket.volume_for_host(host_id))

    def volume(self, host_id: HostId) -> HostVolume | None:
        try:
            if not self.bucket.host_dir_prefix_has_objects(host_id):
                self._warn_if_identity_missing(host_id)
                return None
        except S3StateBucketError as e:
            logger.warning(
                "Could not probe host_dir prefix for host {}; treating volume as unavailable: {}", host_id, e
            )
            return None
        return self.volume_reference(host_id)

    def _warn_if_identity_missing(self, host_id: HostId) -> None:
        """Warn when an empty host_dir prefix is explained by the instance having no IAM profile.

        Detects the missing-identity case directly from cloud state: an instance
        with no attached IAM instance profile could never push host_dir, which is
        why the prefix is empty. Best-effort -- any probe failure is swallowed.
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
            logger.warning(
                "Host {}'s instance has no attached IAM instance profile, so its host_dir was never "
                "pushed to the bucket and is not readable offline. Re-run `mngr aws prepare` "
                "with sufficient IAM, then recreate the host so it picks up the profile.",
                host_id,
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
