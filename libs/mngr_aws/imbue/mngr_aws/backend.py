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

from imbue.imbue_common.logging import log_span
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_aws import hookimpl
from imbue.mngr_aws.cli import aws_cli_group
from imbue.mngr_aws.client import AwsVpsClient
from imbue.mngr_aws.config import AwsProviderConfig
from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.host_store import VpsDockerHostRecord
from imbue.mngr_vps_docker.instance import OfflineCapableVpsDockerProvider
from imbue.mngr_vps_docker.instance import ParsedVpsBuildOptions
from imbue.mngr_vps_docker.instance import build_poweroff_idle_watcher_service_unit
from imbue.mngr_vps_docker.instance import extract_git_depth
from imbue.mngr_vps_docker.instance import extract_presence_flag
from imbue.mngr_vps_docker.instance import extract_single_value_arg
from imbue.mngr_vps_docker.instance import raise_if_unknown_provider_arg
from imbue.mngr_vps_docker.instance import raise_if_vps_migration_arg
from imbue.mngr_vps_docker.primitives import VpsInstanceId

AWS_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("aws")

# Per-agent metadata is mirrored onto the instance as up to three EC2 tags per
# agent, keyed ``mngr-agent-<agent_id>-<field>`` (the agent id lives in the key,
# not a value), so a *stopped* instance (no public IP, SSH unreachable) still
# surfaces its agents in discovery and resolves by name. ``name``/``type`` are
# stored raw; ``labels`` as compact JSON. One tag per field (rather than one
# packed tag per agent) gives ``labels`` the full 256-char value budget; a field
# whose value still overflows is dropped, not failed.
AGENT_TAG_PREFIX: Final[str] = "mngr-agent-"
_AGENT_TAG_FIELDS: Final[tuple[str, ...]] = ("name", "type", "labels")
_MAX_TAG_VALUE_LEN: Final[int] = 256
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


class AwsProvider(OfflineCapableVpsDockerProvider):
    """AWS-specific provider that discovers hosts via the EC2 DescribeInstances API."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    aws_client: AwsVpsClient = Field(frozen=True, description="EC2 API client")
    aws_config: AwsProviderConfig = Field(frozen=True, description="AWS-specific configuration")

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

    # =========================================================================
    # Offline metadata via EC2 tags (so STOPPED hosts list + resolve by name)
    # =========================================================================

    def persist_agent_data(self, host_id: HostId, agent_data: Mapping[str, object]) -> None:
        """Persist an agent's record on the host volume *and* mirror it into an EC2 tag.

        The base ``VpsDockerProvider`` writes the agent record to the on-volume
        host store (``agents/<id>.json``), which is the authoritative source the
        SSH-based discovery reads for *running* hosts -- so this override must
        keep doing that (via ``super()``) or running AWS hosts would list with no
        agents. *Additionally*, it mirrors a compact record into an EC2 tag so a
        *stopped* instance (whose volume is unreadable) still surfaces its agents
        and resolves for ``mngr start``. Called on agent create and on every
        ``data.json`` update, so it is an idempotent upsert.

        The on-volume write is best-effort: when the instance is stopped the base
        raises ``HostNotFoundError`` (no reachable ``vps_ip``), in which case only
        the tag is written -- exactly the path an offline ``mngr label`` needs.
        """
        try:
            super().persist_agent_data(host_id, agent_data)
        except HostNotFoundError:
            # Host stopped / unreachable: the on-volume store can't be written,
            # but the tag mirror below still must be (e.g. offline `mngr label`).
            logger.debug("Host {} unreachable; persisting agent data to EC2 tags only", host_id)
        agent_id = agent_data.get("id")
        if agent_id is None:
            logger.warning("Cannot mirror agent data to EC2 tags without an id (name={!r})", agent_data.get("name"))
            return
        instance = self._find_instance_for_host(host_id)
        if instance is None:
            logger.warning("No EC2 instance found for host {}; cannot persist agent tags", host_id)
            return
        set_tags, delete_keys = self._agent_field_tags(str(agent_id), agent_data, instance)
        try:
            self.aws_client.add_tags(VpsInstanceId(instance["id"]), set_tags)
        except VpsApiError as e:
            # EC2 caps a resource at 50 (non-aws:) tags. Hitting it means the host
            # has more agents than the tag mirror can hold; surface it as a
            # NotImplementedError so the CLI offers to open an issue rather than
            # failing obscurely. (Updating an existing agent overwrites its keys,
            # so this only fires when a *new* agent's tags don't fit.)
            if "TagLimitExceeded" in str(e):
                raise NotImplementedError(
                    f"The AWS host for agent {agent_id!r} has reached EC2's {_AWS_MAX_TAGS_PER_INSTANCE}-tag-per-"
                    "instance limit, so this agent can't be mirrored to tags for stopped-host listing and "
                    "resume-by-name. Running this many agents on a single AWS host isn't supported yet -- please "
                    "open an issue at https://github.com/imbue-ai/mngr/issues so we can prioritize the planned "
                    "S3-backed agent store."
                ) from e
            raise
        if delete_keys:
            self.aws_client.remove_tags(VpsInstanceId(instance["id"]), delete_keys)

    def remove_persisted_agent_data(self, host_id: HostId, agent_id: AgentId) -> None:
        """Remove the agent's on-volume record *and* its ``mngr-agent-<id>-*`` tags.

        Mirrors ``persist_agent_data``: the base removes the authoritative
        on-volume record (best-effort -- ``HostNotFoundError`` when the instance
        is stopped) and this override additionally drops the agent's per-field EC2
        tags, so a destroyed agent stops appearing in both running- and
        stopped-host discovery. ``DeleteTags`` is idempotent, so deleting a field
        key the agent never had is a harmless no-op.
        """
        try:
            super().remove_persisted_agent_data(host_id, agent_id)
        except HostNotFoundError:
            logger.debug("Host {} unreachable; removing agent data from EC2 tags only", host_id)
        instance = self._find_instance_for_host(host_id)
        if instance is None:
            return
        keys = [f"{AGENT_TAG_PREFIX}{agent_id}-{field}" for field in _AGENT_TAG_FIELDS]
        self.aws_client.remove_tags(VpsInstanceId(instance["id"]), keys)

    def _is_instance_offline(self, instance: Mapping[str, Any]) -> bool:
        """Whether the EC2 instance's OS is down (stopping or stopped).

        EC2 power state rides along for free in the ``DescribeInstances`` listing,
        so this needs no extra call. Gate on state, not ``main_ip`` -- a
        ``stopping`` instance can still report a public IP while its OS is already
        off, and gating on the IP would make the host vanish for the (seconds-long)
        stop transition.
        """
        return instance.get("state") in _HOST_DOWN_STATES

    def _agent_field_value(self, field: str, agent_data: Mapping[str, object]) -> str | None:
        """Render one agent field as a tag-value string, or ``None`` if absent/empty.

        ``name``/``type`` are stored raw; ``labels`` as compact JSON (empty labels
        are treated as absent so no ``-labels`` tag is written).
        """
        if field == "labels":
            labels = agent_data.get("labels")
            return json.dumps(labels, separators=(",", ":")) if labels else None
        value = agent_data.get(field)
        return None if value is None else str(value)

    def _agent_field_tags(
        self, agent_id: str, agent_data: Mapping[str, object], instance: Mapping[str, Any]
    ) -> tuple[dict[str, str], list[str]]:
        """Compute the ``mngr-agent-<id>-<field>`` tags to set, and stale ones to delete.

        Returns ``(tags_to_set, keys_to_delete)``. ``persist_agent_data`` is an
        upsert that is sometimes called with a *partial* record (e.g. an update
        carrying only ``id``/``type``), so a field absent from ``agent_data`` means
        "unchanged" -- it is left alone, NOT removed (deleting it would clobber the
        ``name`` tag that offline resolve-by-name depends on). A field that *is*
        present but renders empty (e.g. ``labels={}``, an explicit removal) or
        overflows the 256-char tag limit (realistically only ``labels``) is dropped
        and its existing tag, if any, is deleted so no stale value lingers. The
        agent id is carried in the tag *key*, not a value.
        """
        set_tags: dict[str, str] = {}
        delete_keys: list[str] = []
        for field in _AGENT_TAG_FIELDS:
            if field not in agent_data:
                continue
            key = f"{AGENT_TAG_PREFIX}{agent_id}-{field}"
            value = self._agent_field_value(field, agent_data)
            if value is not None and len(value) <= _MAX_TAG_VALUE_LEN:
                set_tags[key] = value
                continue
            # Present but empty (an explicit removal, e.g. labels={}) or too large
            # for a single tag: drop it, and delete any existing tag so no stale
            # value lingers. Only oversized values warrant a warning.
            if value is not None:
                logger.warning(
                    "Agent {} {} ({} chars) exceeds the {}-char EC2 tag limit; omitted from the "
                    "stopped-host tag mirror",
                    agent_data.get("name", agent_id),
                    field,
                    len(value),
                    _MAX_TAG_VALUE_LEN,
                )
            delete_keys.append(key)
        existing = set(self._tag_dict_from_normalized(instance))
        return set_tags, [key for key in delete_keys if key in existing]

    def _persisted_agent_dicts_from_instance(self, instance: Mapping[str, Any]) -> list[dict]:
        """Reassemble agent records from this instance's ``mngr-agent-<id>-<field>`` tags.

        Groups the per-field tags by agent id (recovered from the tag key, split on
        the final ``-`` so ids may themselves contain dashes), and rebuilds one
        dict per agent. A malformed/externally-edited ``-labels`` tag (not valid
        JSON, or not a JSON object) is skipped for that field with a warning rather
        than crashing the discovery sweep.
        """
        by_id: dict[str, dict] = {}
        for key, value in self._tag_dict_from_normalized(instance).items():
            if not key.startswith(AGENT_TAG_PREFIX):
                continue
            agent_id, sep, field = key[len(AGENT_TAG_PREFIX) :].rpartition("-")
            if not sep or field not in _AGENT_TAG_FIELDS:
                continue
            record = by_id.setdefault(agent_id, {"id": agent_id})
            if field == "labels":
                try:
                    parsed = json.loads(value)
                except json.JSONDecodeError:
                    logger.warning("Skipping unparseable agent labels tag {!r}", key)
                    continue
                if not isinstance(parsed, dict):
                    logger.warning("Skipping agent labels tag {!r}: value is not a JSON object", key)
                    continue
                record["labels"] = parsed
            else:
                record[field] = value
        return list(by_id.values())

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

    def _offline_discovered_host_from_instance(self, instance: Mapping[str, Any]) -> DiscoveredHost | None:
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

    def _offline_host_from_instance(self, host_id: HostId, instance: Mapping[str, Any]) -> OfflineHost:
        """Reconstruct a minimal offline host (STOPPED) for a stopped instance from its tags."""
        tags = self._tag_dict_from_normalized(instance)
        created_at_raw = tags.get("mngr-created-at")
        now = datetime.now(timezone.utc)
        created_at = now
        if created_at_raw:
            try:
                created_at = datetime.fromisoformat(created_at_raw)
            except ValueError as e:
                # mngr writes this tag at launch, so a parse failure means the tag
                # was externally edited/corrupted: surface it rather than silently
                # using now() (which would misreport a long-stopped host as fresh).
                logger.opt(exception=e).warning(
                    "Malformed mngr-created-at tag {!r} on host {}; falling back to now()",
                    created_at_raw,
                    host_id,
                )
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
