import json
import os
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Final

import click
from botocore.exceptions import BotoCoreError
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.model_update import to_update
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.hosts.offline_host import validate_and_create_discovered_agent
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import SnapshotInfo
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr_aws import hookimpl
from imbue.mngr_aws.cli import aws_cli_group
from imbue.mngr_aws.client import AwsVpsClient
from imbue.mngr_aws.config import AwsProviderConfig
from imbue.mngr_vps_docker.container_setup import host_volume_name_for
from imbue.mngr_vps_docker.container_setup import remove_host_from_known_hosts
from imbue.mngr_vps_docker.host_store import VpsDockerHostRecord
from imbue.mngr_vps_docker.host_store import open_host_store
from imbue.mngr_vps_docker.instance import ParsedVpsBuildOptions
from imbue.mngr_vps_docker.instance import VpsDockerProvider
from imbue.mngr_vps_docker.instance import extract_git_depth
from imbue.mngr_vps_docker.instance import extract_presence_flag
from imbue.mngr_vps_docker.instance import extract_single_value_arg
from imbue.mngr_vps_docker.instance import raise_if_unknown_provider_arg
from imbue.mngr_vps_docker.instance import raise_if_vps_migration_arg
from imbue.mngr_vps_docker.primitives import VpsInstanceId

AWS_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("aws")

# Per-agent metadata is persisted as one EC2 tag per agent, keyed
# ``mngr-agent-<agent_id>``, so a *stopped* instance (no public IP, SSH
# unreachable) still surfaces its agents in discovery and resolves by name.
# The value is a compact JSON record kept under EC2's 256-char tag-value limit.
AGENT_TAG_PREFIX: Final[str] = "mngr-agent-"
_MAX_TAG_VALUE_LEN: Final[int] = 256
# The ``Name`` tag is set to ``mngr-<host_name>`` at launch; strip the prefix to
# recover the host name when reconstructing a stopped host from tags.
_HOST_NAME_TAG_PREFIX: Final[str] = "mngr-"


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
        )

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
    ) -> None:
        """Stop the agent container *and* the EC2 instance, preserving the EBS volume.

        The base ``VpsDockerProvider.stop_host`` only stops the inner Docker
        container, leaving the EC2 instance running and billing. This override
        additionally calls ``ec2 stop-instances`` so a paused AWS agent costs
        only EBS storage; the root volume (and all on-disk state) survives, so
        ``start_host`` can resume it.

        ``create_snapshot`` is intentionally ignored -- native EC2 stop preserves
        the whole filesystem, so the base's docker-commit snapshot would be
        redundant. The base container-stop + record-write is reused via
        ``super()``; we then record ``stop_reason=STOPPED`` so the offline-state
        derivation reports STOPPED (not CRASHED) while the instance is down, and
        finally stop the instance. The ``stop_reason`` write must happen *before*
        the instance stops, since the volume is unreachable once it does.
        """
        del create_snapshot
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.config is None or host_record.vps_ip is None:
            raise HostNotFoundError(self.name, host_id)
        super().stop_host(host, create_snapshot=False, timeout_seconds=timeout_seconds)
        self._record_stop_reason(host_id, host_record, HostState.STOPPED)
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
        with log_span("Waiting for VPS SSH after start"):
            self._wait_for_sshd_on_vps(new_ip, timeout_seconds=self.config.ssh_connect_timeout)
        with self._make_outer_for_vps_ip(new_ip) as outer:
            host_store = open_host_store(outer, host_volume_name_for(host_id))
            record = host_store.read_host_record()
            if record is None or record.config is None:
                raise HostNotFoundError(self.name, host_id)
            self._rebind_known_hosts(record, new_ip)
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
        # Drop any cached Host bound to the old IP, then seed the record cache so
        # super().start_host()'s _find_host_record returns the rebound record.
        self._evict_cached_host(host_id)
        self._host_record_cache[host_id] = updated_record
        return super().start_host(host_id, snapshot_id)

    def _find_instance_for_host(self, host_id: HostId) -> dict[str, Any] | None:
        """Locate this host's EC2 instance by its ``mngr-host-id`` tag (works while stopped).

        Unlike ``_find_host_record`` (which SSHes into the VPS), this reads only
        EC2 ``DescribeInstances`` tags, so it resolves an instance that is
        stopped and therefore unreachable over SSH. ``list_instances`` already
        filters out terminated instances, so a destroyed host returns ``None``.
        """
        wanted_tag = f"mngr-host-id={host_id}"
        for instance in self._list_instances_cached():
            if wanted_tag in instance.get("tags", ()):
                return instance
        return None

    def _record_stop_reason(
        self,
        host_id: HostId,
        host_record: VpsDockerHostRecord,
        state: HostState,
    ) -> None:
        """Persist ``stop_reason`` on the host record while the instance is still reachable."""
        if host_record.config is None or host_record.vps_ip is None:
            raise HostNotFoundError(self.name, host_id)
        certified = host_record.certified_host_data
        updated_data = certified.model_copy_update(
            to_update(certified.field_ref().stop_reason, state.value),
            to_update(certified.field_ref().updated_at, datetime.now(timezone.utc)),
        )
        updated_record = host_record.model_copy_update(
            to_update(host_record.field_ref().certified_host_data, updated_data)
        )
        with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
            host_store = open_host_store(outer, host_record.config.volume_name)
            host_store.write_host_record(updated_record)
        self._host_record_cache[host_id] = updated_record

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

    # =========================================================================
    # Offline metadata via EC2 tags (so STOPPED hosts list + resolve by name)
    # =========================================================================

    def persist_agent_data(self, host_id: HostId, agent_data: Mapping[str, object]) -> None:
        """Mirror an agent's record into an EC2 tag so it survives an instance stop.

        A stopped instance's volume (where agent records normally live) is
        unreadable, so without this a paused host would list with no agents and
        ``mngr start <agent>`` could not resolve. Stores a compact record (id,
        name, type) under ``mngr-agent-<agent_id>``; called on agent create and
        on every ``data.json`` update, so it is an idempotent upsert.
        """
        value = self._compact_agent_tag_value(agent_data)
        if value is None:
            logger.warning("Cannot persist agent data without id+name for host {}", host_id)
            return
        instance = self._find_instance_for_host(host_id)
        if instance is None:
            logger.warning("No EC2 instance found for host {}; cannot persist agent tag", host_id)
            return
        agent_id = agent_data["id"]
        self.aws_client.add_tags(VpsInstanceId(instance["id"]), {f"{AGENT_TAG_PREFIX}{agent_id}": value})

    def remove_persisted_agent_data(self, host_id: HostId, agent_id: AgentId) -> None:
        """Delete the ``mngr-agent-<agent_id>`` tag when an agent is destroyed."""
        instance = self._find_instance_for_host(host_id)
        if instance is None:
            return
        self.aws_client.remove_tags(VpsInstanceId(instance["id"]), [f"{AGENT_TAG_PREFIX}{agent_id}"])

    def list_persisted_agent_data_for_host(self, host_id: HostId) -> list[dict]:
        """Return the per-agent records persisted in the instance's EC2 tags.

        Works while the instance is stopped (tags are returned by
        ``DescribeInstances`` regardless of state), which is the whole point.
        """
        instance = self._find_instance_for_host(host_id)
        if instance is None:
            return []
        return self._persisted_agent_dicts_from_instance(instance)

    def discover_hosts_and_agents(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> dict[DiscoveredHost, list[DiscoveredAgent]]:
        """Add STOPPED instances (which the SSH-based base discovery misses) from tags.

        The base discovery reaches hosts over SSH via their public IP, so a
        stopped instance (no IP) is invisible. Here we reconstruct those hosts
        and their agents from EC2 tags so they still appear in ``mngr list`` and
        resolve for ``mngr start``.
        """
        result = super().discover_hosts_and_agents(cg, include_destroyed=include_destroyed)
        online_host_ids = {ref.host_id for ref in result}
        for instance in self._list_instances_cached():
            if instance.get("main_ip") or instance.get("state") != "stopped":
                continue
            host_ref = self._discovered_host_from_tags(instance)
            if host_ref is None or host_ref.host_id in online_host_ids:
                continue
            agent_refs: list[DiscoveredAgent] = []
            for agent_data in self._persisted_agent_dicts_from_instance(instance):
                ref = validate_and_create_discovered_agent(agent_data, host_ref.host_id, self.name)
                if ref is not None:
                    agent_refs.append(ref)
            result[host_ref] = agent_refs
        return result

    def list_snapshots(self, host: HostInterface | HostId) -> list[SnapshotInfo]:
        """Return [] for a stopped host instead of raising.

        ``OfflineHost.get_state`` calls ``get_snapshots`` (-> ``list_snapshots``)
        while deriving state. The base reads the snapshot list from the on-volume
        host record, which is unreadable while the instance is stopped, so it
        raises ``HostNotFoundError``. AWS has no EBS-snapshot lifecycle and a
        stopped host's docker-commit snapshots (if any) live on that unreadable
        volume, so a stopped host simply has no visible snapshots.
        """
        try:
            return super().list_snapshots(host)
        except HostNotFoundError:
            return []

    def to_offline_host(self, host_id: HostId) -> OfflineHost:
        """Return an offline host, reconstructing a STOPPED instance's record from tags.

        Falls back to the base (SSH/volume-backed) path first; if that can't find
        the host (because it is stopped and unreachable), rebuild a minimal
        ``CertifiedHostData`` from EC2 tags so the offline host is still usable.
        """
        try:
            return super().to_offline_host(host_id)
        except HostNotFoundError:
            instance = self._find_instance_for_host(host_id)
            if instance is None:
                raise
            return self._offline_host_from_tags(host_id, instance)

    def _compact_agent_tag_value(self, agent_data: Mapping[str, object]) -> str | None:
        """Build a compact JSON tag value (id/name[/type]) kept under the 256-char tag limit.

        Returns ``None`` when the required ``id``/``name`` are missing. Drops the
        optional ``type`` if including it would exceed the limit, so the
        required-minimum (id + name, enough to resolve by name) always fits.
        """
        agent_id = agent_data.get("id")
        agent_name = agent_data.get("name")
        if agent_id is None or agent_name is None:
            return None
        minimal: dict[str, object] = {"id": agent_id, "name": agent_name}
        full = dict(minimal)
        agent_type = agent_data.get("type")
        if agent_type is not None:
            full["type"] = agent_type
        value = json.dumps(full, separators=(",", ":"))
        if len(value) > _MAX_TAG_VALUE_LEN:
            value = json.dumps(minimal, separators=(",", ":"))
        return value

    def _persisted_agent_dicts_from_instance(self, instance: Mapping[str, Any]) -> list[dict]:
        """Parse the ``mngr-agent-*`` tags off a normalized instance dict into agent records."""
        agents: list[dict] = []
        for kv in instance.get("tags", ()):
            key, sep, value = kv.partition("=")
            if not sep or not key.startswith(AGENT_TAG_PREFIX):
                continue
            try:
                agents.append(json.loads(value))
            except json.JSONDecodeError:
                logger.warning("Skipping unparseable persisted-agent tag {!r}", key)
        return agents

    def _tag_dict_from_normalized(self, instance: Mapping[str, Any]) -> dict[str, str]:
        """Turn the normalized ``["key=value", ...]`` tag list into a dict (split on first ``=``)."""
        tags: dict[str, str] = {}
        for kv in instance.get("tags", ()):
            key, sep, value = kv.partition("=")
            if sep:
                tags[key] = value
        return tags

    def _host_name_from_tags(self, tags: Mapping[str, str]) -> HostName:
        """Recover the host name from the ``Name=mngr-<host_name>`` tag (fallback: host_id)."""
        name_tag = tags.get("Name", "")
        if name_tag.startswith(_HOST_NAME_TAG_PREFIX):
            return HostName(name_tag[len(_HOST_NAME_TAG_PREFIX) :])
        if name_tag:
            return HostName(name_tag)
        return HostName(tags.get("mngr-host-id", "unknown"))

    def _discovered_host_from_tags(self, instance: Mapping[str, Any]) -> DiscoveredHost | None:
        """Build a STOPPED-state DiscoveredHost from an instance's tags, or None if not a mngr host."""
        tags = self._tag_dict_from_normalized(instance)
        host_id_str = tags.get("mngr-host-id")
        if host_id_str is None:
            return None
        return DiscoveredHost(
            host_id=HostId(host_id_str),
            host_name=self._host_name_from_tags(tags),
            provider_name=self.name,
            host_state=HostState.STOPPED,
        )

    def _offline_host_from_tags(self, host_id: HostId, instance: Mapping[str, Any]) -> OfflineHost:
        """Reconstruct a minimal offline host (STOPPED) for a stopped instance from its tags."""
        tags = self._tag_dict_from_normalized(instance)
        created_at_raw = tags.get("mngr-created-at")
        now = datetime.now(timezone.utc)
        try:
            created_at = datetime.fromisoformat(created_at_raw) if created_at_raw else now
        except ValueError:
            created_at = now
        certified = CertifiedHostData(
            host_id=str(host_id),
            host_name=str(self._host_name_from_tags(tags)),
            created_at=created_at,
            updated_at=now,
            stop_reason=HostState.STOPPED.value,
        )
        return self._create_offline_host(VpsDockerHostRecord(certified_host_data=certified))


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
            attach_self_stop_role=config.attach_self_stop_role,
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
