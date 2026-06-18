import json
import os
import shlex
import time
from abc import abstractmethod
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pyinfra.api import Host as PyinfraHost

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.model_update import to_update
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.common import check_agent_type_known
from imbue.mngr.hosts.common import compute_idle_seconds
from imbue.mngr.hosts.common import determine_lifecycle_state
from imbue.mngr.hosts.common import resolve_expected_process_name
from imbue.mngr.hosts.common import timestamp_to_datetime
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.hosts.offline_host import derive_offline_host_state
from imbue.mngr.hosts.offline_host import make_readable_offline_host
from imbue.mngr.hosts.offline_host import validate_and_create_discovered_agent
from imbue.mngr.hosts.outer_host import OuterHost
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.cleanup_failures import collecting_cleanup_failures
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import CleanupFailure
from imbue.mngr.interfaces.data_types import CleanupFailureCategory
from imbue.mngr.interfaces.data_types import CpuResources
from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.interfaces.data_types import HostLifecycleOptions
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.interfaces.data_types import SnapshotInfo
from imbue.mngr.interfaces.data_types import SnapshotRecord
from imbue.mngr.interfaces.data_types import VolumeInfo
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.interfaces.volume import HostVolume
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import IdleMode
from imbue.mngr.primitives import ImageReference
from imbue.mngr.primitives import LogLevel
from imbue.mngr.primitives import SSHInfo
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.primitives import VolumeId
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.providers.listing_utils import build_listing_collection_script
from imbue.mngr.providers.listing_utils import build_outer_listing_collection_script
from imbue.mngr.providers.listing_utils import parse_listing_collection_output
from imbue.mngr.providers.ssh_host_setup import build_start_activity_watcher_command
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr.providers.ssh_utils import create_pyinfra_host
from imbue.mngr.providers.ssh_utils import load_or_create_host_keypair
from imbue.mngr.providers.ssh_utils import load_or_create_ssh_keypair
from imbue.mngr.providers.ssh_utils import wait_for_sshd
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr_vps_docker.cloud_init import generate_cloud_init_user_data
from imbue.mngr_vps_docker.config import VpsDockerProviderConfig
from imbue.mngr_vps_docker.container_setup import CONTAINER_ENTRYPOINT_CMD
from imbue.mngr_vps_docker.container_setup import HOST_DIR_SUBPATH
from imbue.mngr_vps_docker.container_setup import HOST_VOLUME_MOUNT_PATH
from imbue.mngr_vps_docker.container_setup import LABEL_HOST_ID
from imbue.mngr_vps_docker.container_setup import LABEL_HOST_NAME
from imbue.mngr_vps_docker.container_setup import LABEL_PROVIDER
from imbue.mngr_vps_docker.container_setup import LABEL_TAGS
from imbue.mngr_vps_docker.container_setup import SNAPSHOT_READ_MOUNT_PATH
from imbue.mngr_vps_docker.container_setup import SNAPSHOT_TRIGGER_MOUNT_PATH
from imbue.mngr_vps_docker.container_setup import build_image_on_outer_from_build_args
from imbue.mngr_vps_docker.container_setup import check_file_exists_on_outer
from imbue.mngr_vps_docker.container_setup import commit_container
from imbue.mngr_vps_docker.container_setup import create_bind_volume_on_outer
from imbue.mngr_vps_docker.container_setup import delete_btrfs_subvolume_on_outer
from imbue.mngr_vps_docker.container_setup import docker_inspect_running
from imbue.mngr_vps_docker.container_setup import ensure_depot_token_available
from imbue.mngr_vps_docker.container_setup import exec_in_container
from imbue.mngr_vps_docker.container_setup import host_volume_name_for
from imbue.mngr_vps_docker.container_setup import prepare_btrfs_on_outer
from imbue.mngr_vps_docker.container_setup import provision_snapshot_helper_on_outer
from imbue.mngr_vps_docker.container_setup import pull_image
from imbue.mngr_vps_docker.container_setup import remove_container
from imbue.mngr_vps_docker.container_setup import remove_host_from_known_hosts
from imbue.mngr_vps_docker.container_setup import remove_volume
from imbue.mngr_vps_docker.container_setup import run_container
from imbue.mngr_vps_docker.container_setup import run_docker
from imbue.mngr_vps_docker.container_setup import seed_host_volume_layout_on_outer
from imbue.mngr_vps_docker.container_setup import setup_container_ssh
from imbue.mngr_vps_docker.container_setup import snapshot_trigger_volume_name_for
from imbue.mngr_vps_docker.container_setup import start_container
from imbue.mngr_vps_docker.container_setup import start_container_sshd
from imbue.mngr_vps_docker.container_setup import stop_container
from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.host_setup import MNGR_READY_MARKER_PATH
from imbue.mngr_vps_docker.host_state_store import HostDirBackend
from imbue.mngr_vps_docker.host_state_store import HostStateStore
from imbue.mngr_vps_docker.host_state_store import NullHostDirBackend
from imbue.mngr_vps_docker.host_state_store import StateBucket
from imbue.mngr_vps_docker.host_store import VpsDockerHostRecord
from imbue.mngr_vps_docker.host_store import VpsHostConfig
from imbue.mngr_vps_docker.host_store import open_host_store
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vps_docker.vps_client import VpsClientInterface


class ParsedVpsBuildOptions(FrozenModel):
    """Result of parsing VPS-specific build args from Docker build args."""

    region: str = Field(description="VPS region")
    plan: str = Field(description="VPS plan")
    git_depth: int | None = Field(
        default=None, description="Git clone depth for build context, or None for full clone"
    )
    docker_build_args: tuple[str, ...] = Field(description="Remaining args passed to docker build")


def extract_single_value_arg(args: Sequence[str], flag: str) -> tuple[str | None, list[str]]:
    """Pop ``--flag=VALUE`` once from ``args``. Returns ``(value or None, remaining)``.

    If ``flag`` appears multiple times the last occurrence wins (matching how
    docker's CLI treats repeated single-value flags). Composable building
    block: each provider's ``_parse_build_args`` chains a few of these to
    peel off its own knobs before walking the remainder for docker forwarding.
    """
    value: str | None = None
    remaining: list[str] = []
    for arg in args:
        if arg.startswith(flag):
            value = arg.split("=", 1)[1]
        else:
            remaining.append(arg)
    return value, remaining


def extract_git_depth(args: Sequence[str]) -> tuple[int | None, list[str]]:
    """Pop ``--git-depth=N`` from ``args``. Shared because it's about the *local*
    mngr build context (shallow-cloning the upload tarball), not the VPS, so
    every provider accepts it under the same name.
    """
    raw, remaining = extract_single_value_arg(args, "--git-depth=")
    return (int(raw) if raw is not None else None), remaining


def extract_presence_flag(args: Sequence[str], flag: str) -> tuple[bool, list[str]]:
    """Pop a presence-only flag like ``--aws-spot`` from ``args``.

    Returns ``(True, remaining)`` if any element of ``args`` equals ``flag``
    exactly, else ``(False, args_as_list)``. The flag MUST be passed with no
    value: ``--aws-spot=true`` or ``--aws-spot=`` raises because that shape
    suggests the caller expected a value-bearing flag (likely a typo).

    Composable building block for boolean opt-in knobs (e.g. ``--aws-spot``).
    """
    present = False
    remaining: list[str] = []
    for arg in args:
        if arg == flag:
            present = True
        elif arg.startswith(f"{flag}="):
            raise MngrError(f"{flag} is a presence-only flag; pass it as bare {flag!r} (no value). Got: {arg!r}")
        else:
            remaining.append(arg)
    return present, remaining


_VPS_MIGRATION_HINT: Final[str] = (
    "Build args are now per-provider: use --aws-region= / --aws-instance-type= / --aws-ami=, "
    "--vultr-region= / --vultr-plan=, or --ovh-datacenter= (alias --ovh-region=) / --ovh-plan= "
    "(matching your provider). The old --vps-os= / --vps-image= / --vps-ami= image-selection args "
    "are also removed; image selection lives on the provider config (default_os_id for Vultr, "
    "default_image_name for OVH, default_ami_id / default_ami_by_region for AWS)."
)


def raise_if_vps_migration_arg(arg: str) -> None:
    """Raise the dedicated migration error if ``arg`` uses the dropped shared ``--vps-*`` prefix.

    Called by every provider's parser (and by ``MinimalVpsDockerProvider``)
    so callers still passing ``--vps-region=`` etc. get a clear pointer at
    the new per-provider name rather than having the arg silently forwarded
    to docker (which would either error opaquely or, worse, succeed for a
    flag that happens to be a valid docker flag).
    """
    if arg.startswith("--vps-"):
        raise MngrError(f"{arg.split('=', 1)[0]} is no longer supported. {_VPS_MIGRATION_HINT}")


def raise_if_unknown_provider_arg(arg: str, provider_prefix: str, valid_args: Sequence[str]) -> None:
    """Raise if ``arg`` starts with ``--<provider_prefix>-`` but isn't one of ``valid_args``.

    Lets a provider's parser catch typos / unknown flags up front, with a
    specific error that lists what was actually accepted. ``valid_args``
    should be the full flag spellings (e.g. ``("--aws-region=", ...)``) so
    the error message matches the user-facing names exactly.
    """
    if not arg.startswith(f"--{provider_prefix}-"):
        return
    raise MngrError(f"Unknown {provider_prefix} build arg: {arg}. Valid args: {', '.join(valid_args)}")


def parse_vps_build_args(
    build_args: Sequence[str] | None,
    *,
    provider_prefix: str,
    default_region: str,
    default_plan: str,
    plan_arg_name: str,
) -> ParsedVpsBuildOptions:
    """Convenience parser for the common provider shape (region + plan + git-depth).

    Builds the standard four-step parse out of ``extract_single_value_arg``,
    ``extract_git_depth``, ``raise_if_vps_migration_arg``, and
    ``raise_if_unknown_provider_arg``. Vultr and OVH (which only have a
    region + plan) call this directly; AWS has its own composition because
    it also accepts ``--aws-ami=``. Custom providers with their own knobs
    should compose the helpers directly rather than extending this function.
    """
    args = list(build_args or ())
    region_arg = f"--{provider_prefix}-region="
    plan_arg = f"--{provider_prefix}-{plan_arg_name}="
    region, args = extract_single_value_arg(args, region_arg)
    plan, args = extract_single_value_arg(args, plan_arg)
    git_depth, args = extract_git_depth(args)
    docker_build_args: list[str] = []
    for arg in args:
        raise_if_vps_migration_arg(arg)
        raise_if_unknown_provider_arg(arg, provider_prefix, (region_arg, plan_arg, "--git-depth="))
        docker_build_args.append(arg)
    return ParsedVpsBuildOptions(
        region=region or default_region,
        plan=plan or default_plan,
        git_depth=git_depth,
        docker_build_args=tuple(docker_build_args),
    )


def _read_host_id_label_from_vps(outer: OuterHostInterface) -> HostId | None:
    """Return the host_id label of the (single) mngr container on this VPS, if any.

    Each VPS hosts at most one mngr container (1:1 invariant), so the value
    of the ``com.imbue.mngr.host-id`` label on any container with that label
    set uniquely identifies the VPS's host. Returns ``None`` when no such
    container exists yet (e.g., the VPS is still being provisioned).

    Includes stopped containers so a paused host is still discoverable.
    """
    fmt = "{{index .Config.Labels " + json.dumps(LABEL_HOST_ID) + "}}"
    result = outer.execute_idempotent_command(
        "docker ps -a -q "
        f"--filter {shlex.quote('label=' + LABEL_HOST_ID)} | "
        f"xargs -r docker inspect --format {shlex.quote(fmt)}",
    )
    if not result.success:
        raise MngrError(
            f"Failed to list mngr containers on VPS: stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}"
        )
    for raw_line in result.stdout.splitlines():
        value = raw_line.strip()
        if not value:
            continue
        try:
            return HostId(value)
        except InvalidRandomIdError as e:
            # A corrupted/manually-edited label must not crash discovery for
            # the whole VPS; surface as MngrError so the existing fallback
            # path in _read_records_from_vps logs and continues.
            raise MngrError(f"Container on VPS has malformed {LABEL_HOST_ID} label {value!r}: {e}") from e
    return None


def _read_live_listing_from_vps(
    outer: OuterHostInterface,
    host_id: HostId,
    host_dir: str,
    prefix: str,
) -> dict[str, Any]:
    """Run the outer listing script on the VPS and return the parsed live listing.

    Reads agent state directly from the running container's live ``host_dir``
    (or, for a stopped container, from a ``docker cp``-extracted copy), so
    agents created *inside* the container -- which are never written to the
    persisted outer store -- are discovered. This mirrors the read path
    ``ImbueCloudProvider`` already uses.
    """
    script = build_outer_listing_collection_script(str(host_id), host_dir, prefix, host_id_label=LABEL_HOST_ID)
    result = outer.execute_idempotent_command(script, timeout_seconds=60.0)
    if not result.success:
        raise MngrError(
            f"Outer listing script failed on VPS for host {host_id}: "
            f"stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}"
        )
    return parse_listing_collection_output(result.stdout)


def _extract_live_agent_data(parsed_listing: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Pull each agent's ``data.json`` dict out of a parsed listing."""
    agent_data: list[dict[str, Any]] = []
    for agent_raw in parsed_listing.get("agents", []):
        data = agent_raw.get("data")
        if isinstance(data, dict):
            agent_data.append(data)
    return agent_data


class _VpsDiscoveryData(FrozenModel):
    """Host records, live agent data, and container running state gathered during discovery.

    Used both for a single VPS read and for the aggregate across all of a
    provider's VPSes; the shapes are identical, so combining is a per-field merge.
    """

    records: tuple[VpsDockerHostRecord, ...] = Field(default=(), description="Discovered host records")
    live_agent_data_by_host_id: dict[HostId, list[dict[str, Any]]] = Field(
        default_factory=dict, description="Live in-container agent data.json dicts keyed by host id"
    )
    is_running_by_host_id: dict[HostId, bool] = Field(
        default_factory=dict, description="Container running state keyed by host id"
    )


def build_vps_tags(host_id: HostId, provider_name: str, extra_tags_raw: str) -> dict[str, str]:
    """Compose the tag mapping passed to the VPS create call.

    Always emits ``mngr-host-id=<id>`` and ``mngr-provider=<name>``. The
    ``extra_tags_raw`` string is a comma-separated list of ``key=value``
    tags that the spawning caller wants attached at create time -- e.g.
    minds-side pool-bake sets ``MNGR_VPS_EXTRA_TAGS=minds_env=<name>``
    so the env's destroy can later find + delete the instance via the
    Vultr tag filter. Empty / whitespace-only entries are skipped so
    trailing commas don't produce blank tags. Entries without an ``=``
    are rejected so misconfigured callers fail fast instead of silently
    losing the tag.

    Pulled out to module scope so the comma-splitting behaviour is unit
    testable without standing up an entire provisioning flow.
    """
    tags: dict[str, str] = {"mngr-host-id": str(host_id), "mngr-provider": provider_name}
    for entry in extra_tags_raw.split(","):
        stripped = entry.strip()
        if not stripped:
            continue
        if "=" not in stripped:
            raise MngrError(f"Invalid VPS extra tag {stripped!r}: expected ``key=value``")
        key, _, value = stripped.partition("=")
        tags[key.strip()] = value.strip()
    return tags


# HTTP status codes from the VPS provider API that mean the resource the teardown
# step targeted was already gone -- a benign outcome that should not be recorded as
# a cleanup failure.
_VPS_RESOURCE_ALREADY_GONE_STATUS_CODES: Final = (404, 410)


def _is_vps_resource_already_gone(error: MngrError) -> bool:
    """Return True iff ``error`` is a VPS API "already gone" (not-found) response.

    Both the Vultr and OVH clients raise ``VpsApiError`` carrying the HTTP
    ``status_code`` (OVH maps its SDK's ``ResourceNotFoundError`` to 404), so we
    classify by that status rather than fragile error-text matching. A real
    failure (the resource exists but could not be destroyed) carries some other
    status and is recorded.
    """
    return isinstance(error, VpsApiError) and error.status_code in _VPS_RESOURCE_ALREADY_GONE_STATUS_CODES


def _is_mngr_ready_marker_present_or_none(outer: OuterHostInterface) -> bool | None:
    """Return True if the ``mngr-ready`` marker exists, else None (so polling continues).

    A ``HostConnectionError`` counts as "not ready yet" (None): the bootstrap runs
    ``apt-get install`` and Docker setup which can momentarily disrupt SSH (e.g.
    ``systemctl restart ssh`` after writing the sshd tuning).
    """
    try:
        return True if check_file_exists_on_outer(outer, Path(MNGR_READY_MARKER_PATH)) else None
    except HostConnectionError as e:
        logger.debug("Transient SSH error during host-bootstrap poll (will retry): {}", e)
        return None


def _wait_for_cloud_init_marker(
    outer: OuterHostInterface,
    timeout_seconds: float,
    *,
    poll_interval_seconds: float = 5.0,
    slow_threshold_seconds: float = 30.0,
) -> None:
    """Poll the VPS for the ``mngr-ready`` first-boot completion marker.

    Returns once the marker file appears. The marker is written by whichever
    first-boot mechanism the backend uses -- cloud-init ``runcmd`` (Vultr / AWS /
    OVH) or the GCE ``startup-script`` (GCP). Keeps polling until ``timeout_seconds``
    -- the hard wall (see ``_is_mngr_ready_marker_present_or_none`` for the
    transient-error handling).

    ``poll_interval_seconds`` and ``slow_threshold_seconds`` are parameters so
    tests can drive this with short intervals; defaults preserve the production
    cadence (poll every 5s, warn if total > 30s).
    """
    value, _, elapsed = poll_for_value(
        lambda: _is_mngr_ready_marker_present_or_none(outer),
        timeout=timeout_seconds,
        poll_interval=poll_interval_seconds,
    )
    if value is None:
        raise MngrError(
            f"Cloud-init did not complete within {timeout_seconds}s. Docker may not be installed on the VPS."
        )
    if elapsed > slow_threshold_seconds:
        logger.warning("Host bootstrap took {:.1f}s (threshold: {:.0f}s)", elapsed, slow_threshold_seconds)


class VpsDockerProvider(BaseProviderInstance):
    """Provider that runs agents in Docker containers on VPS instances.

    Each host maps to exactly one VPS running exactly one Docker container.
    The VPS stays running at all times; stop/start operates on the container.
    Destroying the host destroys both the container and the VPS.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: VpsDockerProviderConfig = Field(frozen=True, description="VPS Docker provider configuration")
    vps_client: VpsClientInterface = Field(frozen=True, description="VPS provider API client")

    _host_record_cache: dict[HostId, VpsDockerHostRecord] = PrivateAttr(default_factory=dict)
    _instances_cache: list[dict[str, Any]] | None = PrivateAttr(default=None)

    @property
    def supports_snapshots(self) -> bool:
        return True

    @property
    def supports_shutdown_hosts(self) -> bool:
        return True

    @property
    def supports_volumes(self) -> bool:
        return True

    @property
    def supports_mutable_tags(self) -> bool:
        return False

    def reset_caches(self) -> None:
        for host_id in list(self._host_by_id_cache):
            self._evict_cached_host(host_id)
        self._host_record_cache.clear()
        self._instances_cache = None

    def _fetch_provider_instances(self) -> list[dict[str, Any]]:
        """Provider-specific listing hook: return the raw instance dicts from the API.

        Default returns ``[]`` so subclasses without a tag-based listing API
        (e.g. OVH, which uses ``_list_provider_vps_hostnames`` directly) can
        opt out. Subclasses with one (currently AWS and Vultr) override to
        call their typed client's ``list_instances``; the result is cached
        for the duration of a single command via ``_list_instances_cached``.
        """
        return []

    def _list_instances_cached(self) -> list[dict[str, Any]]:
        """List instances tagged for this provider, caching for the duration of the command.

        Subclasses customise *what* gets listed by overriding
        ``_fetch_provider_instances``; the cache scaffolding lives here.
        """
        if self._instances_cache is not None:
            return self._instances_cache
        self._instances_cache = self._fetch_provider_instances()
        return self._instances_cache

    def _validate_provider_args_for_create(self) -> None:
        """Hook called by ``create_host`` before the first provider write.

        Default no-op. Subclasses override to enforce provider-specific
        pre-create invariants (e.g. GCP's firewall-rule existence, AWS's
        pytest-only "must have auto_shutdown_seconds set" guard). It runs before
        any provider API write -- in particular before the SSH key upload and
        instance creation -- so a failed precondition surfaces cleanly with no
        leaked resources and no cleanup path. Keep these checks cheap (local
        state or a single read-only API call); anything expensive runs on every
        ``mngr create``.
        """

    # =========================================================================
    # Key Management
    # =========================================================================

    def _key_dir(self) -> Path:
        """Directory for SSH keys for this provider instance."""
        key_dir = self.mngr_ctx.profile_dir / "providers" / str(self.config.backend) / str(self.name) / "keys"
        key_dir.mkdir(parents=True, exist_ok=True)
        return key_dir

    def _get_vps_ssh_keypair(self) -> tuple[Path, str]:
        """Load or create the SSH keypair for authenticating to the VPS."""
        return load_or_create_ssh_keypair(self._key_dir(), "vps_ssh_key")

    def _get_container_ssh_keypair(self) -> tuple[Path, str]:
        """Load or create the SSH keypair for authenticating to the container."""
        return load_or_create_ssh_keypair(self._key_dir(), "container_ssh_key")

    def _get_vps_host_keypair(self) -> tuple[Path, str]:
        """Load or create the Ed25519 host keypair injected into VPS via cloud-init."""
        return load_or_create_host_keypair(self._key_dir(), "host_key")

    def _get_container_host_keypair(self) -> tuple[Path, str]:
        """Load or create the Ed25519 host keypair for the container's sshd."""
        return load_or_create_host_keypair(self._key_dir(), "container_host_key")

    def _vps_known_hosts_path(self) -> Path:
        return self._key_dir() / "vps_known_hosts"

    def record_outer_host_key(self, host: str, port: int, public_key: str) -> None:
        """Pin an outer (VPS-root) sshd host key in this provider's known_hosts.

        Callers operating on a VPS this provider did not itself order (e.g. the
        imbue_cloud rebuild on a leased host) use this so the provider's own outer
        connections -- including the certified-data sync callback -- pass strict
        host-key checking instead of failing on a missing entry.
        """
        add_host_to_known_hosts(
            known_hosts_path=self._vps_known_hosts_path(),
            hostname=host,
            port=port,
            public_key=public_key,
        )

    def _container_known_hosts_path(self) -> Path:
        return self._key_dir() / "container_known_hosts"

    # =========================================================================
    # Outer host helper
    # =========================================================================

    @contextmanager
    def _make_outer_for_vps_ip(self, vps_ip: str) -> Iterator[OuterHostInterface]:
        """Open an outer host targeting root@vps_ip:22 via the provider's VPS SSH key.

        Use this during create_host (when host_id is not yet known); use
        ``outer_host_for(host_id)`` once a host record exists.
        """
        vps_key_path, _pub = self._get_vps_ssh_keypair()
        pyinfra_host = create_pyinfra_host(
            hostname=vps_ip,
            port=22,
            private_key_path=vps_key_path,
            known_hosts_path=self._vps_known_hosts_path(),
            ssh_user="root",
        )
        outer = OuterHost(
            id=HostId.generate(),
            connector=PyinfraConnector(pyinfra_host),
            mngr_ctx=self.mngr_ctx,
        )
        try:
            yield outer
        finally:
            outer.disconnect()

    # =========================================================================
    # Host Store
    # =========================================================================
    # The store is opened via ``open_host_store(outer, volume_name)`` (free
    # function in ``host_store``) which resolves the volume's bind-source path
    # via ``docker volume inspect --format '{{.Options.device}}'`` -- the docker
    # named volume is a bind-options entry pointing at the per-host btrfs
    # subvolume, so ``Options.device`` (not the unused ``Mountpoint``
    # placeholder under ``/var/lib/docker/volumes``) is the real on-disk path.
    # This used to be the per-user state container.

    # =========================================================================
    # Host Object Construction
    # =========================================================================

    def _create_host_object(
        self,
        host_id: HostId,
        host_name: HostName,
        vps_ip: str,
    ) -> Host:
        """Create a Host object with direct SSH to the container via the VPS's exposed port."""
        container_key_path, _container_pub = self._get_container_ssh_keypair()

        # Container sshd port is exposed on the VPS's public IP.
        # We connect directly to vps_ip:container_ssh_port.
        pyinfra_host = create_pyinfra_host(
            hostname=vps_ip,
            port=self.config.container_ssh_port,
            private_key_path=container_key_path,
            known_hosts_path=self._container_known_hosts_path(),
        )

        connector = PyinfraConnector(pyinfra_host)
        host = Host(
            id=host_id,
            host_name=host_name,
            connector=connector,
            provider_instance=self,
            mngr_ctx=self.mngr_ctx,
            on_updated_host_data=lambda callback_host_id, certified_data: self._on_certified_host_data_updated(
                callback_host_id, certified_data, vps_ip
            ),
        )
        self._evict_cached_host(host_id, replacement=host)
        return host

    def _create_offline_host(
        self,
        host_record: VpsDockerHostRecord,
    ) -> OfflineHost:
        """Create an OfflineHost from a host record.

        Wrapped so the offline host is readable (file reads served from its
        persisted volume) whether reached via ``get_host`` or
        ``to_offline_host``; the volume is resolved lazily, so this is free.
        """
        host_id = HostId(host_record.certified_host_data.host_id)
        vps_ip = host_record.vps_ip or ""
        offline = make_readable_offline_host(
            OfflineHost(
                id=host_id,
                certified_host_data=host_record.certified_host_data,
                provider_instance=self,
                mngr_ctx=self.mngr_ctx,
                on_updated_host_data=lambda callback_host_id, certified_data: self._on_certified_host_data_updated(
                    callback_host_id, certified_data, vps_ip
                ),
            )
        )
        self._evict_cached_host(host_id, replacement=offline)
        return offline

    def _on_certified_host_data_updated(self, host_id: HostId, certified_data: CertifiedHostData, vps_ip: str) -> None:
        """Callback when host data.json is updated -- sync to the unified host volume."""
        try:
            with self._make_outer_for_vps_ip(vps_ip) as outer:
                host_store = open_host_store(outer, host_volume_name_for(host_id))
                existing = host_store.read_host_record()
                if existing is not None:
                    updated = existing.model_copy_update(
                        to_update(existing.field_ref().certified_host_data, certified_data)
                    )
                    host_store.write_host_record(updated)
                    self._persist_host_record_externally(updated)
        except MngrError as e:
            logger.warning("Failed to sync certified data to VPS host volume: {}", e)

    # =========================================================================
    # VPS Provisioning
    # =========================================================================

    def _wait_for_cloud_init(self, outer: OuterHostInterface, timeout_seconds: float) -> None:
        """Wait for cloud-init to finish (Docker installed, marker file present)."""
        _wait_for_cloud_init_marker(outer, timeout_seconds=timeout_seconds)

    def _wait_for_sshd_on_vps(self, vps_ip: str, timeout_seconds: float) -> None:
        """Wait for sshd on the VPS to be ready."""
        wait_for_sshd(hostname=vps_ip, port=22, timeout_seconds=timeout_seconds)

    # =========================================================================
    # Container Setup
    # =========================================================================

    def _setup_container_ssh(
        self,
        outer: OuterHostInterface,
        container_name: str,
        host_volume_mount_path: str | None,
        known_hosts_entries: tuple[str, ...],
        authorized_keys_entries: tuple[str, ...],
    ) -> None:
        """Set up SSH inside the container via docker exec.

        Delegates to the shared ``setup_container_ssh`` helper, supplying the
        container client/host keypairs this provider manages. The container is
        reached via <vps_ip>:<container_ssh_port> directly; the caller is
        responsible for adding the matching known_hosts entry once the VPS IP
        is known.
        """
        _container_key_path, container_public_key = self._get_container_ssh_keypair()
        container_host_key_path, container_host_public_key = self._get_container_host_keypair()
        setup_container_ssh(
            outer,
            container_name,
            mngr_host_dir=str(self.host_dir),
            host_volume_mount_path=host_volume_mount_path,
            container_public_key=container_public_key,
            container_host_private_key=container_host_key_path.read_text(),
            container_host_public_key=container_host_public_key,
            known_hosts_entries=known_hosts_entries,
            authorized_keys_entries=authorized_keys_entries,
        )

    def _prepare_btrfs_on_outer(self, outer: OuterHostInterface, host_id: HostId) -> Path:
        """Ensure btrfs loop FS + per-host subvolume exist on the outer; return the subvolume path.

        Thin wrapper around :func:`prepare_btrfs_on_outer` that pulls the
        loop-file path, mount path, and reserved-GB knob out of
        ``self.config``. See the free function for the full step-by-step.
        """
        return prepare_btrfs_on_outer(
            outer,
            host_id=host_id,
            btrfs_mount_path=self.config.btrfs_mount_path,
            loop_file_path=self.config.btrfs_loop_file_path,
            outer_disk_reserved_gb=self.config.outer_disk_reserved_gb,
        )

    # =========================================================================
    # Core Lifecycle: create_host
    # =========================================================================

    def create_host(
        self,
        name: HostName,
        image: ImageReference | None = None,
        tags: Mapping[str, str] | None = None,
        build_args: Sequence[str] | None = None,
        start_args: Sequence[str] | None = None,
        lifecycle: HostLifecycleOptions | None = None,
        known_hosts: Sequence[str] | None = None,
        authorized_keys: Sequence[str] | None = None,
        snapshot: SnapshotName | None = None,
    ) -> Host:
        host_id = HostId.generate()
        logger.info("Creating VPS Docker host {} ({}) ...", name, host_id)

        parsed = self._parse_build_args(build_args)

        # Fail fast before provisioning a (billable) VPS: a DEPOT build needs
        # DEPOT_TOKEN, but the build only runs after the VPS exists and
        # cloud-init completes -- so a missing token would otherwise waste a
        # full provision. Only an actual build (non-empty docker_build_args)
        # needs the token; a plain image pull does not.
        if parsed.docker_build_args:
            ensure_depot_token_available(self.config.builder)

        # Provider-specific pre-create checks (e.g. GCP's firewall-rule
        # existence, AWS's pytest auto-shutdown guard). Run before the first
        # provider write (the SSH key upload just below) so a failed
        # precondition -- like a missing `mngr gcp prepare` firewall rule --
        # surfaces cleanly: no instance created, no SSH key uploaded, and no
        # "Host creation failed, attempting cleanup..." path.
        self._validate_provider_args_for_create()

        _vps_key_path, vps_public_key = self._get_vps_ssh_keypair()
        vps_host_key_path, vps_host_public_key = self._get_vps_host_keypair()

        with log_span("Uploading SSH key to provider"):
            key_name = f"mngr-{self.name}-{host_id}"
            vps_ssh_key_id = self.vps_client.upload_ssh_key(key_name, vps_public_key)

        vps_instance_id: VpsInstanceId | None = None
        vps_ip: str | None = None
        try:
            vps_instance_id, vps_ip = self._provision_vps(
                host_id=host_id,
                name=name,
                parsed=parsed,
                vps_host_key_path=vps_host_key_path,
                vps_host_public_key=vps_host_public_key,
                vps_ssh_key_id=vps_ssh_key_id,
                vps_public_key=vps_public_key,
            )

            with self._make_outer_for_vps_ip(vps_ip) as outer:
                host = self.create_host_on_existing_vps(
                    outer=outer,
                    host_id=host_id,
                    name=name,
                    vps_ip=vps_ip,
                    vps_instance_id=vps_instance_id,
                    vps_ssh_key_id=vps_ssh_key_id,
                    vps_host_public_key=vps_host_public_key,
                    region=parsed.region,
                    plan=parsed.plan,
                    image=image,
                    tags=tags,
                    build_args=build_args,
                    start_args=start_args,
                    lifecycle=lifecycle,
                    known_hosts=known_hosts,
                    authorized_keys=authorized_keys,
                )

            logger.info("VPS Docker host {} created successfully (VPS: {}, IP: {})", name, vps_instance_id, vps_ip)
            return host

        except Exception:
            keep_failed = os.environ.get("MNGR_KEEP_FAILED_HOSTS", "0") == "1"
            if keep_failed:
                logger.error(
                    "Host creation failed. MNGR_KEEP_FAILED_HOSTS=1 is set, "
                    "skipping cleanup so you can debug. VPS instance: {}, IP: {}",
                    vps_instance_id,
                    vps_ip,
                )
            else:
                logger.error("Host creation failed, attempting cleanup...")
                try:
                    if vps_instance_id is not None:
                        self.vps_client.destroy_instance(vps_instance_id)
                except Exception as cleanup_err:
                    logger.warning("Failed to clean up VPS instance: {}", cleanup_err)
                try:
                    self.vps_client.delete_ssh_key(vps_ssh_key_id)
                except Exception as cleanup_err:
                    logger.warning("Failed to clean up SSH key: {}", cleanup_err)
            raise

    def create_host_on_existing_vps(
        self,
        *,
        outer: OuterHostInterface,
        host_id: HostId,
        name: HostName,
        vps_ip: str,
        vps_instance_id: VpsInstanceId,
        vps_ssh_key_id: str,
        vps_host_public_key: str,
        region: str,
        plan: str,
        image: ImageReference | None,
        tags: Mapping[str, str] | None,
        build_args: Sequence[str] | None,
        start_args: Sequence[str] | None,
        lifecycle: HostLifecycleOptions | None,
        known_hosts: Sequence[str] | None,
        authorized_keys: Sequence[str] | None,
    ) -> Host:
        """Build the container and finalize host state on an already-reachable VPS.

        This is the single canonical "set up the host after the VPS exists"
        code path. ``create_host`` calls it once it has ordered (or recycled)
        a VPS and opened an outer; other providers that operate on a VPS they
        did not order themselves (e.g. ``mngr_imbue_cloud``'s slow path, which
        rebuilds the container on a leased pool VPS) call it directly with
        their own ``outer``. It makes no VPS-client (ordering) calls.

        The unified host volume is provisioned at the top of
        ``_setup_container_on_vps`` -- that runs before the (potentially slow
        and failure-prone) image pull/build so the volume still exists by the
        time ``_finalize_host_creation`` writes ``host_state.json``.
        """
        base_image = str(image) if image else self.config.default_image
        # Prepend `--runtime <value>` (e.g. 'runsc' for gVisor) when configured; absent by default.
        runtime_args = ("--runtime", self.config.docker_runtime) if self.config.docker_runtime is not None else ()
        effective_start_args = runtime_args + tuple(self.config.default_start_args) + tuple(start_args or ())
        parsed = self._parse_build_args(build_args)

        container_name, container_id, volume_name = self._setup_container_on_vps(
            outer=outer,
            host_id=host_id,
            name=name,
            vps_ip=vps_ip,
            base_image=base_image,
            effective_start_args=effective_start_args,
            docker_build_args=parsed.docker_build_args,
            git_depth=parsed.git_depth,
            tags=tags,
            known_hosts=known_hosts,
            authorized_keys=authorized_keys,
        )

        return self._finalize_host_creation(
            host_id=host_id,
            name=name,
            vps_ip=vps_ip,
            outer=outer,
            container_name=container_name,
            container_id=container_id,
            volume_name=volume_name,
            base_image=base_image,
            effective_start_args=effective_start_args,
            tags=tags,
            lifecycle=lifecycle,
            region=region,
            plan=plan,
            vps_instance_id=vps_instance_id,
            vps_ssh_key_id=vps_ssh_key_id,
            vps_host_public_key=vps_host_public_key,
        )

    def teardown_container_on_existing_vps(self, outer: OuterHostInterface, host_id: HostId) -> None:
        """Remove the container + per-host volumes/subvolume for ``host_id`` on a reachable VPS.

        The VPS itself is left running (no VPS-client calls). Used to clear a
        pre-existing container before rebuilding on the same VPS -- e.g. the
        imbue_cloud slow path reclaiming a leased pool host whose baked
        container must be torn down before ``create_host_on_existing_vps``
        rebuilds it under the same ``host_id``. Each step is best-effort and
        logged; a missing resource is a no-op.
        """
        # Remove every workspace container identified by its host-id label.
        list_result = outer.execute_idempotent_command(
            f"docker ps -aq --filter label={LABEL_HOST_ID}={shlex.quote(str(host_id))}"
        )
        if list_result.success:
            for container_id in list_result.stdout.split():
                try:
                    remove_container(outer, container_id, force=True)
                except (HostConnectionError, MngrError) as e:
                    logger.warning("Failed to remove container {} for host {}: {}", container_id, host_id, e)
        else:
            logger.warning("Failed to list containers for host {}: {}", host_id, list_result.stderr.strip())

        # Delete the per-host btrfs subvolume (the bind source for the unified
        # volume) so the rebuild can recreate it cleanly.
        subvolume_path = self.config.btrfs_mount_path / host_id.get_uuid().hex
        try:
            delete_btrfs_subvolume_on_outer(outer, subvolume_path)
        except (HostConnectionError, MngrError) as e:
            logger.warning("Failed to delete btrfs subvolume {}: {}", subvolume_path, e)

        # Remove the named docker volumes so a recreate with the same names
        # doesn't collide.
        for volume_name in (host_volume_name_for(host_id), snapshot_trigger_volume_name_for(host_id)):
            try:
                remove_volume(outer, volume_name)
            except (HostConnectionError, MngrError) as e:
                logger.warning("Failed to remove volume {} for host {}: {}", volume_name, host_id, e)

    def _create_vps_instance(
        self,
        parsed: ParsedVpsBuildOptions,
        label: str,
        user_data: str,
        ssh_key_ids: Sequence[str],
        tags: Mapping[str, str],
    ) -> VpsInstanceId:
        """Provider hook that issues the cloud-API instance create.

        Default implementation: call the shared ``self.vps_client.create_instance``
        with the standard (label, region, plan, user_data, ssh_key_ids, tags)
        shape, which is what every current backend's wire contract needs.

        Providers that have per-host knobs beyond region + plan (e.g. AWS's
        ``--aws-ami=`` override) override this hook to pass their extra kwargs
        through their own concrete typed client (``self.aws_client`` rather
        than the shared interface). That way the shared ``VpsClientInterface``
        contract stays minimal and per-provider extensions don't ripple across
        every other provider's signature.
        """
        return self.vps_client.create_instance(
            label=label,
            region=parsed.region,
            plan=parsed.plan,
            user_data=user_data,
            ssh_key_ids=ssh_key_ids,
            tags=tags,
        )

    def _generate_bootstrap_payload(
        self,
        *,
        host_private_key: str,
        host_public_key: str,
        authorized_user_public_key: str,
    ) -> str:
        """Render the first-boot bootstrap payload threaded into instance creation.

        Default: cloud-init ``user-data``. GCP overrides this to render a GCE
        ``startup-script`` (stock GCE images run the google-guest-agent, not
        cloud-init). Both render the same shared ``host_setup`` steps and write the
        same ``mngr-ready`` marker, so the rest of provisioning is backend-agnostic.
        """
        return generate_cloud_init_user_data(
            host_private_key=host_private_key,
            host_public_key=host_public_key,
            install_gvisor_runtime=self.config.install_gvisor_runtime,
            auto_shutdown_seconds=self._get_effective_auto_shutdown_seconds(),
            authorized_user_public_key=authorized_user_public_key,
        )

    def _wait_for_expected_host_key(self, vps_ip: str, expected_host_public_key: str, timeout_seconds: float) -> None:
        """Hook to wait until the VPS serves our SSH host key before strict-checking.

        Default no-op: cloud-init backends set the host key before sshd starts.
        Backends that set it after boot (GCP's ``startup-script``) override this to
        poll until the live key matches, closing the mismatch window.
        """
        return

    def _provision_vps(
        self,
        host_id: HostId,
        name: HostName,
        parsed: ParsedVpsBuildOptions,
        vps_host_key_path: Path,
        vps_host_public_key: str,
        vps_ssh_key_id: str,
        vps_public_key: str,
    ) -> tuple[VpsInstanceId, str]:
        """Provision a VPS, wait for it to boot, and wait for Docker to install.

        Returns (vps_instance_id, vps_ip).

        ``vps_public_key`` is the provider SSH public key already loaded by
        ``create_host`` (the sole caller); it is threaded in rather than re-read
        from disk here. The bootstrap (``_generate_bootstrap_payload``) injects it
        straight into root (in addition to the copy-from-default-user step), which
        removes any reliance on a cloud image's default-user key landing in root --
        notably on GCE, where the google guest agent provisions the key
        asynchronously and races the copy.

        Provider-specific pre-create checks (``_validate_provider_args_for_create``)
        already ran in ``create_host`` before the first provider write, so by the
        time we get here the create preconditions are known to hold.
        """
        vps_host_private_key = vps_host_key_path.read_text()
        user_data = self._generate_bootstrap_payload(
            host_private_key=vps_host_private_key,
            host_public_key=vps_host_public_key,
            authorized_user_public_key=vps_public_key,
        )

        logger.log(
            LogLevel.BUILD.value,
            "Creating VPS instance (region: {}, plan: {})...",
            parsed.region,
            parsed.plan,
            source="vps",
        )
        with log_span("Creating VPS instance"):
            vps_tags = build_vps_tags(host_id, self.name, os.environ.get("MNGR_VPS_EXTRA_TAGS", ""))
            vps_instance_id = self._create_vps_instance(
                parsed=parsed,
                label=f"mngr-{name}",
                user_data=user_data,
                ssh_key_ids=[vps_ssh_key_id],
                tags=vps_tags,
            )

        logger.log(LogLevel.BUILD.value, "Waiting for VPS to become active...", source="vps")
        with log_span("Waiting for VPS to become active"):
            vps_ip = self.vps_client.wait_for_instance_active(
                vps_instance_id,
                timeout_seconds=self.config.instance_boot_timeout,
            )
        logger.log(LogLevel.BUILD.value, "VPS active (IP: {})", vps_ip, source="vps")

        add_host_to_known_hosts(
            known_hosts_path=self._vps_known_hosts_path(),
            hostname=vps_ip,
            port=22,
            public_key=vps_host_public_key,
        )

        logger.log(LogLevel.BUILD.value, "Waiting for SSH to be ready on VPS...", source="vps")
        with log_span("Waiting for VPS SSH"):
            self._wait_for_sshd_on_vps(vps_ip, timeout_seconds=self.config.ssh_connect_timeout)

        # Backends that install the host key after sshd starts (GCP's
        # startup-script) serve a boot-generated key first; wait for our key to
        # land before the strict-host-key-checked connection below. No-op for
        # cloud-init backends, which set the key pre-sshd.
        with log_span("Waiting for expected VPS host key"):
            self._wait_for_expected_host_key(
                vps_ip, vps_host_public_key, timeout_seconds=self.config.ssh_connect_timeout
            )

        logger.log(
            LogLevel.BUILD.value, "Waiting for host bootstrap to complete (Docker installation)...", source="vps"
        )
        with log_span("Waiting for host bootstrap (Docker install)"):
            with self._make_outer_for_vps_ip(vps_ip) as outer:
                self._wait_for_cloud_init(outer, timeout_seconds=self.config.docker_install_timeout)
        logger.log(LogLevel.BUILD.value, "Host bootstrap complete, Docker is ready", source="vps")

        return vps_instance_id, vps_ip

    def _setup_container_on_vps(
        self,
        outer: OuterHostInterface,
        host_id: HostId,
        name: HostName,
        vps_ip: str,
        base_image: str,
        effective_start_args: tuple[str, ...],
        docker_build_args: tuple[str, ...],
        git_depth: int | None,
        tags: Mapping[str, str] | None,
        known_hosts: Sequence[str] | None,
        authorized_keys: Sequence[str] | None,
    ) -> tuple[str, str, str]:
        """Create the Docker container and configure SSH inside it.

        If docker_build_args are provided, uploads the build context to the VPS
        and runs docker build there. Otherwise pulls the base image directly.

        Returns (container_name, container_id, volume_name).

        The btrfs loop FS and the per-host unified docker named volume are
        provisioned at the top of this method, before the (slow, failure-prone)
        image pull/build, so the bind-source path always exists by the time
        ``_finalize_host_creation`` writes ``host_state.json`` -- even if a
        later step in this method fails.
        """
        volume_name = host_volume_name_for(host_id)
        snapshot_trigger_volume_name = snapshot_trigger_volume_name_for(host_id)

        with log_span("Provisioning unified host volume on btrfs subvolume"):
            subvolume_path = self._prepare_btrfs_on_outer(outer, host_id)
            seed_host_volume_layout_on_outer(outer, subvolume_path)
            create_bind_volume_on_outer(outer, volume_name=volume_name, device_path=subvolume_path)

        # Snapshot helper: lets the in-container host_backup service request
        # `btrfs subvolume snapshot` against the per-host subvolume via a
        # request.json / result.json file protocol in a dedicated docker
        # volume. Both the systemd unit on the outer and the docker volume
        # mounted at /mngr-snapshot/ in the container need to exist before
        # the agent boots. The helper provisioning is internally a 2-phase
        # parallel pipeline (see provision_snapshot_helper_on_outer) so
        # the ~7 SSH round-trips it would otherwise serialize collapse to
        # the latency of 2.
        provision_snapshot_helper_on_outer(
            outer,
            self.mngr_ctx.concurrency_group,
            host_id=host_id,
            btrfs_mount_path=self.config.btrfs_mount_path,
            subvolume_path=subvolume_path,
            trigger_volume_name=snapshot_trigger_volume_name,
        )

        if docker_build_args:
            base_image = self._build_image_on_vps(outer, host_id, base_image, docker_build_args, git_depth)
        else:
            logger.log(LogLevel.BUILD.value, "Pulling Docker image {} on VPS...", base_image, source="vps")
            with log_span("Pulling Docker image on VPS"):
                pull_image(outer, base_image, timeout_seconds=300.0)

        container_name = f"{self.mngr_ctx.config.prefix}{name}"
        labels = {
            LABEL_HOST_ID: str(host_id),
            LABEL_HOST_NAME: str(name),
            LABEL_PROVIDER: str(self.name),
            LABEL_TAGS: json.dumps(dict(tags) if tags else {}),
        }
        logger.log(LogLevel.BUILD.value, "Starting Docker container on VPS...", source="vps")
        snapshots_dir_on_outer = self.config.btrfs_mount_path / "snapshots"
        with log_span("Starting Docker container"):
            container_id = run_container(
                outer,
                image=base_image,
                name=container_name,
                port_mappings={f"0.0.0.0:{self.config.container_ssh_port}": "22"},
                volumes=[
                    f"{volume_name}:{HOST_VOLUME_MOUNT_PATH}:rw",
                    # Snapshot helper IPC volume (bind-shared with the outer
                    # at OUTER_SNAPSHOT_TRIGGER_DIR via the named volume created
                    # above; host_backup writes request.json / reads result.json).
                    f"{snapshot_trigger_volume_name}:{SNAPSHOT_TRIGGER_MOUNT_PATH}:rw",
                    # Read-only view of the outer's <btrfs-mount>/snapshots/
                    # directory so restic-in-container can read the per-request
                    # snapshots the outer helper produces at
                    # <btrfs-mount>/snapshots/<name>.
                    f"{snapshots_dir_on_outer}:{SNAPSHOT_READ_MOUNT_PATH}:ro",
                ],
                labels=labels,
                extra_args=list(effective_start_args),
                entrypoint_cmd=CONTAINER_ENTRYPOINT_CMD,
            )

        logger.log(LogLevel.BUILD.value, "Setting up SSH in container...", source="vps")
        with log_span("Setting up SSH in container"):
            self._setup_container_ssh(
                outer=outer,
                container_name=container_name,
                host_volume_mount_path=f"{HOST_VOLUME_MOUNT_PATH}/{HOST_DIR_SUBPATH}",
                known_hosts_entries=tuple(known_hosts or ()),
                authorized_keys_entries=tuple(authorized_keys or ()),
            )

        _container_host_key_path, container_host_public_key = self._get_container_host_keypair()
        add_host_to_known_hosts(
            known_hosts_path=self._container_known_hosts_path(),
            hostname=vps_ip,
            port=self.config.container_ssh_port,
            public_key=container_host_public_key,
        )
        logger.log(LogLevel.BUILD.value, "Waiting for container SSH to be ready...", source="vps")
        with log_span("Waiting for container SSH"):
            self._wait_for_container_sshd(vps_ip)
        logger.log(LogLevel.BUILD.value, "Container SSH ready", source="vps")

        return container_name, container_id, volume_name

    def _finalize_host_creation(
        self,
        host_id: HostId,
        name: HostName,
        vps_ip: str,
        outer: OuterHostInterface,
        container_name: str,
        container_id: str,
        volume_name: str,
        base_image: str,
        effective_start_args: tuple[str, ...],
        tags: Mapping[str, str] | None,
        lifecycle: HostLifecycleOptions | None,
        region: str,
        plan: str,
        vps_instance_id: VpsInstanceId,
        vps_ssh_key_id: str,
        vps_host_public_key: str,
    ) -> Host:
        """Create the Host object, configure activity watching, and persist state."""
        host = self._create_host_object(host_id, name, vps_ip)

        lifecycle_options = lifecycle if lifecycle is not None else HostLifecycleOptions()
        activity_config = lifecycle_options.to_activity_config(
            default_idle_timeout_seconds=self.config.default_idle_timeout,
            default_idle_mode=self.config.default_idle_mode,
            default_activity_sources=self.config.default_activity_sources,
        )

        now = datetime.now(timezone.utc)
        host_data = CertifiedHostData(
            host_id=str(host_id),
            host_name=str(name),
            idle_timeout_seconds=activity_config.idle_timeout_seconds,
            activity_sources=activity_config.activity_sources,
            image=base_image,
            user_tags=dict(tags) if tags else {},
            created_at=now,
            updated_at=now,
        )
        host.record_activity(ActivitySource.BOOT)
        host.set_certified_data(host_data)

        self._create_shutdown_script(host)
        with log_span("Starting activity watcher"):
            self._start_activity_watcher(outer, container_name)

        host_record = VpsDockerHostRecord(
            certified_host_data=host_data,
            vps_ip=vps_ip,
            ssh_host_public_key=vps_host_public_key,
            container_ssh_host_public_key=self._get_container_host_keypair()[1],
            config=VpsHostConfig(
                vps_instance_id=vps_instance_id,
                region=region,
                plan=plan,
                start_args=effective_start_args,
                image=base_image,
                container_name=container_name,
                volume_name=volume_name,
                vps_ssh_key_id=vps_ssh_key_id,
            ),
            container_id=container_id,
        )
        host_store = open_host_store(outer, volume_name)
        host_store.write_host_record(host_record)
        self._persist_host_record_externally(host_record)

        # Cache so that persist_agent_data (called moments later) can find
        # the record without re-querying the Vultr API, which would return
        # a stale instance list that doesn't include the VPS we just created.
        self._host_record_cache[host_id] = host_record

        self._on_host_finalized(host_id=host_id, vps_ip=vps_ip)

        return host

    def _on_host_finalized(self, *, host_id: HostId, vps_ip: str) -> None:
        """Hook called at the very end of ``_finalize_host_creation``.

        Fires after the host record has been written to the unified host
        volume and is therefore the "point of no return" for ``create_host``.
        Subclasses can override to commit any deferred provisioning side
        effects that must only become durable once the host is fully
        usable -- e.g. OVH classic VPS un-cancellation, which must wait
        until container setup has succeeded so that a failure earlier in
        the flow lets the VPS auto-decommission instead of leaking
        a still-billing orphan.

        Default no-op. Must not raise; any errors should be caught and
        logged by the override.
        """

    def _persist_host_record_externally(self, record: VpsDockerHostRecord) -> None:
        """Mirror the authoritative on-volume host record to an external store.

        Lets a provider copy the record to a compute-decoupled object store
        (e.g. an S3 bucket) so a stopped/offline instance's full record stays
        readable without SSH. Called right after every on-volume
        ``write_host_record``. Default no-op, so providers without an external
        store are unaffected.
        """

    def _delete_host_record_externally(self, host_id: HostId) -> None:
        """Remove a host's record from the external store, if any.

        The inverse of ``_persist_host_record_externally``: called when a host
        is destroyed/deleted so its mirrored record does not linger in the
        external store. Default no-op.
        """

    def _wait_for_container_sshd(self, vps_ip: str) -> None:
        """Wait for sshd in the container to be reachable via the VPS's exposed port."""
        wait_for_sshd(
            hostname=vps_ip,
            port=self.config.container_ssh_port,
            timeout_seconds=self.config.ssh_connect_timeout,
        )

    def _build_image_on_vps(
        self,
        outer: OuterHostInterface,
        host_id: HostId,
        base_image: str,
        docker_build_args: tuple[str, ...],
        git_depth: int | None,
    ) -> str:
        """Build a Docker image on the VPS from the provided build args.

        Thin wrapper around the shared ``build_image_on_outer_from_build_args``
        helper, supplying this provider's configured docker builder. Returns the
        image tag to use.
        """
        return build_image_on_outer_from_build_args(
            outer,
            self.mngr_ctx.concurrency_group,
            host_id=host_id,
            docker_build_args=docker_build_args,
            git_depth=git_depth,
            builder=self.config.builder,
        )

    def _create_shutdown_script(self, host: Host) -> None:
        """Create the shutdown script that stops the container on idle."""
        shutdown_script = "#!/bin/bash\nkill -TERM 1\n"
        commands_dir = host.host_dir / "commands"
        host.execute_idempotent_command(f"mkdir -p {commands_dir}")
        host.write_file(commands_dir / "shutdown.sh", shutdown_script.encode())
        host.execute_idempotent_command(f"chmod +x {commands_dir / 'shutdown.sh'}")

    @abstractmethod
    def _parse_build_args(self, build_args: Sequence[str] | None) -> ParsedVpsBuildOptions:
        """Parse build args, separating provisioning knobs from docker build args.

        Each concrete VpsDockerProvider subclass implements its own parser
        because the set of accepted flags is per-provider (AWS has
        ``--aws-region=`` / ``--aws-instance-type=`` / ``--aws-ami=``; Vultr
        and OVH have a simpler region + plan shape; ``MinimalVpsDockerProvider``
        accepts only ``--git-depth=`` and docker passthrough). For the common
        region + plan + git-depth shape, ``parse_vps_build_args`` is a ready-made
        convenience; for custom shapes, compose the lower-level helpers
        (``extract_single_value_arg``, ``extract_git_depth``,
        ``raise_if_vps_migration_arg``, ``raise_if_unknown_provider_arg``).
        """

    # =========================================================================
    # Core Lifecycle: stop_host
    # =========================================================================

    def stop_host(
        self,
        host: HostInterface | HostId,
        create_snapshot: bool = True,
        timeout_seconds: float = 60.0,
        stop_reason: HostState | None = None,
    ) -> None:
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.config is None or host_record.vps_ip is None:
            raise HostNotFoundError(self.name, host_id)

        if create_snapshot:
            try:
                self.create_snapshot(host_id)
            except MngrError as e:
                logger.warning("Failed to create snapshot before stop: {}", e)

        # Disconnect SSH before stopping (also disconnect the passed-in host
        # in case it is a different instance than the cached one).
        if isinstance(host, Host):
            host.disconnect()
        self._evict_cached_host(host_id)

        with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
            with log_span("Stopping container on VPS"):
                stop_container(outer, host_record.config.container_name, timeout_seconds=int(timeout_seconds))

            # Update the host record (bump updated_at). A subclass that stops more
            # than the container -- e.g. AWS stopping the EC2 instance -- passes a
            # ``stop_reason`` so the offline-state derivation reports it correctly
            # (STOPPED, not CRASHED) while the host is down; this single write
            # carries it, so the subclass needs no second write. The write must
            # land before any deeper stop, since the volume is unreachable after.
            host_store = open_host_store(outer, host_record.config.volume_name)
            certified = host_record.certified_host_data
            data_updates = [to_update(certified.field_ref().updated_at, datetime.now(timezone.utc))]
            if stop_reason is not None:
                data_updates.append(to_update(certified.field_ref().stop_reason, stop_reason.value))
            updated_data = certified.model_copy_update(*data_updates)
            updated_record = host_record.model_copy_update(
                to_update(host_record.field_ref().certified_host_data, updated_data)
            )
            host_store.write_host_record(updated_record)
            self._persist_host_record_externally(updated_record)

        self._host_record_cache[host_id] = updated_record
        logger.info("Host {} stopped", host_id)

    # =========================================================================
    # Core Lifecycle: start_host
    # =========================================================================

    def _start_activity_watcher(self, outer: OuterHostInterface, container_name: str) -> None:
        """Launch the in-container activity watcher (the idle/auto-shutdown driver).

        The watcher is a backgrounded process inside the agent container (not part
        of its entrypoint), so it does not survive a container stop/start. It is
        started here at create time and re-started by ``start_host`` on resume.
        """
        exec_in_container(outer, container_name, build_start_activity_watcher_command(str(self.host_dir)))

    def start_host(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId | None = None,
    ) -> Host:
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.config is None or host_record.vps_ip is None:
            raise HostNotFoundError(self.name, host_id)

        with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
            with log_span("Starting container on VPS"):
                start_container(outer, host_record.config.container_name)
            # sshd is launched via `docker exec` (see start_container_sshd), not the
            # container's entrypoint, so a `docker start` brings the container back
            # WITHOUT sshd (the idle watcher's container stop, a manual `mngr stop`, a
            # VPS reboot, or a host VM stop/start that takes the container down with it
            # -- e.g. an AWS instance stop/start -- all land here). Re-exec it before
            # waiting, or `_wait_for_container_sshd` would block until timeout and the
            # agent would be unrecoverable via `mngr start`/`conn`. `docker start` is a
            # no-op on an already-running container, so this also repairs the
            # container-up-but-sshd-down state.
            with log_span("Restarting sshd in container"):
                start_container_sshd(outer, host_record.config.container_name)

            with log_span("Waiting for container SSH"):
                self._wait_for_container_sshd(host_record.vps_ip)

            host_obj = self._create_host_object(
                host_id, HostName(host_record.certified_host_data.host_name), host_record.vps_ip
            )

            # A resume is itself activity: refresh the BOOT activity file (whose
            # mtime is what the idle watcher reads) so the watcher relaunched just
            # below starts a fresh idle window. Without this, a resumed-but-idle
            # host keeps the pre-stop activity mtimes and the watcher re-stops it
            # within one poll -- so e.g. `mngr start` on an idle host would race a
            # near-immediate auto-stop. Must happen before the watcher relaunch.
            host_obj.record_activity(ActivitySource.BOOT)

            # The in-container activity watcher is a backgrounded process that does
            # not survive the container restart, so relaunch it -- else auto-stop-
            # on-idle would silently stop working after the first resume (for every
            # vps_docker provider, not just AWS). Best-effort: a resumed host that
            # can't auto-stop is better than a failed resume.
            with log_span("Relaunching activity watcher"):
                try:
                    self._start_activity_watcher(outer, host_record.config.container_name)
                except MngrError as e:
                    logger.warning(
                        "Failed to relaunch the activity watcher on resume for host {} ({}); "
                        "this host will not auto-stop on idle until it is recreated",
                        host_id,
                        e,
                    )

        logger.info("Host {} started", host_id)
        return host_obj

    # =========================================================================
    # Core Lifecycle: destroy_host
    # =========================================================================

    def destroy_host(self, host: HostInterface | HostId) -> None:
        """Destroy a VPS-backed Docker host permanently.

        Best-effort: every teardown step is attempted. A failure that means a
        resource is already gone is benign (dropped). A failure that means a
        resource exists but could not be removed is real -- it is recorded as a
        ``CleanupFailure`` and collected, and the remaining steps still run. The
        collected failures are raised as a ``CleanupFailedGroup`` rather than
        aborting early or being silently swallowed. See
        specs/cleanup-error-aggregation.md.

        A missing host record still raises ``HostNotFoundError``; the
        orchestration layer classifies that abort.
        """
        host_id = host.id if isinstance(host, HostInterface) else host

        # Disconnect SSH before destroying (also disconnect the passed-in host
        # in case it is a different instance than the cached one).
        if isinstance(host, Host):
            host.disconnect()
        self._evict_cached_host(host_id)

        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.config is None:
            raise HostNotFoundError(self.name, host_id)

        vps_config = host_record.config
        vps_ip = host_record.vps_ip

        with collecting_cleanup_failures() as failures:
            if vps_ip is not None:
                with self._make_outer_for_vps_ip(vps_ip) as outer:
                    # Stop and remove the agent container; removing the volume below
                    # will fail otherwise because the container still holds it open.
                    # ``tolerate_missing`` makes an already-gone container a no-op, so any
                    # error raised here means a container that exists but could not be removed.
                    try:
                        remove_container(outer, vps_config.container_name, force=True, tolerate_missing=True)
                    except MngrError as e:
                        logger.warning("Failed to remove container: {}", e)
                        failures.append(
                            CleanupFailure(
                                category=CleanupFailureCategory.HOST_RESOURCE_REMAINS,
                                message=f"failed to remove container {vps_config.container_name} for host {host_id}: {e}",
                                host_id=host_id,
                            )
                        )

                    # Delete the per-host btrfs subvolume before the named volume.
                    # The VPS-destroy that follows takes the whole loop file with it,
                    # so this is primarily belt-and-suspenders for the rare case of
                    # a destroy retried on a still-existing VPS (e.g. the operator
                    # re-runs `mngr destroy` after VPS termination has failed).
                    # ``delete_btrfs_subvolume_on_outer`` already no-ops on an absent
                    # subvolume, so any raised error means a present subvolume remains.
                    subvolume_path = self.config.btrfs_mount_path / host_id.get_uuid().hex
                    try:
                        delete_btrfs_subvolume_on_outer(outer, subvolume_path)
                    except MngrError as e:
                        logger.warning("Failed to delete btrfs subvolume {}: {}", subvolume_path, e)
                        failures.append(
                            CleanupFailure(
                                category=CleanupFailureCategory.HOST_RESOURCE_REMAINS,
                                message=f"failed to delete btrfs subvolume {subvolume_path} for host {host_id}: {e}",
                                host_id=host_id,
                            )
                        )

                    # Remove the unified host volume. With bind options the volume
                    # itself holds no data (the subvolume above did), but the named
                    # entry still needs cleanup so a later create with the same
                    # volume name doesn't collide. ``docker volume rm -f`` already
                    # no-ops on a missing volume, so any raised error means the named
                    # volume entry remains.
                    try:
                        remove_volume(outer, vps_config.volume_name)
                    except MngrError as e:
                        logger.warning("Failed to remove host volume: {}", e)
                        failures.append(
                            CleanupFailure(
                                category=CleanupFailureCategory.HOST_RESOURCE_REMAINS,
                                message=f"failed to remove host volume {vps_config.volume_name} for host {host_id}: {e}",
                                host_id=host_id,
                            )
                        )

                    # Remove the per-host snapshot-trigger volume (the named entry;
                    # the bind source at OUTER_SNAPSHOT_TRIGGER_DIR is shared across
                    # all containers on this outer and is left alone). Same ``-f``
                    # no-op-on-missing semantics as the host volume above.
                    trigger_volume_name = snapshot_trigger_volume_name_for(host_id)
                    try:
                        remove_volume(outer, trigger_volume_name)
                    except MngrError as e:
                        logger.warning("Failed to remove snapshot trigger volume: {}", e)
                        failures.append(
                            CleanupFailure(
                                category=CleanupFailureCategory.HOST_RESOURCE_REMAINS,
                                message=f"failed to remove snapshot trigger volume {trigger_volume_name} for host {host_id}: {e}",
                                host_id=host_id,
                            )
                        )

            # Destroy the VPS instance. An "already gone" (HTTP 404/410) response is benign;
            # any other error means a VPS instance that may still exist (and incur cost).
            with log_span("Destroying VPS instance"):
                try:
                    self.vps_client.destroy_instance(vps_config.vps_instance_id)
                except MngrError as e:
                    logger.warning("Failed to destroy VPS: {}", e)
                    if not _is_vps_resource_already_gone(e):
                        failures.append(
                            CleanupFailure(
                                category=CleanupFailureCategory.HOST_RESOURCE_REMAINS,
                                message=f"failed to destroy VPS instance {vps_config.vps_instance_id} for host {host_id}: {e}",
                                host_id=host_id,
                            )
                        )

            # Clean up SSH key from provider. An "already gone" (HTTP 404/410) response is
            # benign; any other error means a key that may still be registered.
            if vps_config.vps_ssh_key_id is not None:
                try:
                    self.vps_client.delete_ssh_key(vps_config.vps_ssh_key_id)
                except MngrError as e:
                    logger.warning("Failed to delete SSH key from provider: {}", e)
                    if not _is_vps_resource_already_gone(e):
                        failures.append(
                            CleanupFailure(
                                category=CleanupFailureCategory.HOST_RESOURCE_REMAINS,
                                message=f"failed to delete SSH key {vps_config.vps_ssh_key_id} for host {host_id}: {e}",
                                host_id=host_id,
                            )
                        )

            # Clean up local known_hosts. These are cosmetic local-file edits; a
            # missing file or OS error here leaves no infrastructure behind, so it is
            # always benign and never recorded as a failure.
            if vps_ip is not None:
                try:
                    remove_host_from_known_hosts(self._vps_known_hosts_path(), vps_ip, 22)
                except (OSError, UnicodeDecodeError) as e:
                    logger.trace("Failed to clean up VPS known_hosts: {}", e)
                try:
                    remove_host_from_known_hosts(
                        self._container_known_hosts_path(), vps_ip, self.config.container_ssh_port
                    )
                except (OSError, UnicodeDecodeError) as e:
                    logger.trace("Failed to clean up container known_hosts: {}", e)

            self._delete_host_record_externally(host_id)
            logger.info("Host {} destroyed (VPS {})", host_id, vps_config.vps_instance_id)

    def delete_host(self, host: HostInterface) -> None:
        """Delete all local records for a destroyed host (does not destroy VPS)."""
        self._evict_cached_host(host.id)
        self._delete_host_record_externally(host.id)

    def on_connection_error(self, host_id: HostId) -> None:
        self._evict_cached_host(host_id)

    def outer_host_id_for(self, host_id: HostId) -> str | None:
        """Stable id for the outer (the VPS) of host_id, keyed by VPS IP."""
        host_record = self._find_host_record(host_id)
        if host_record is None:
            raise HostNotFoundError(self.name, host_id)
        if host_record.vps_ip is None:
            return None
        return f"outer:{self.name}:{host_record.vps_ip}"

    @contextmanager
    def outer_host_for(self, host_id: HostId) -> Iterator[OuterHostInterface | None]:
        """Open the outer host (the VPS itself, root@vps_ip:22).

        Uses this provider's per-instance VPS SSH key (the one cloud-init
        injected on VPS provisioning).
        """
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.vps_ip is None:
            raise HostNotFoundError(self.name, host_id)
        with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
            yield outer

    # =========================================================================
    # Discovery
    # =========================================================================

    def get_host(self, host: HostId | HostName) -> HostInterface:
        if isinstance(host, HostId) and host in self._host_by_id_cache:
            return self._host_by_id_cache[host]

        # Try to find via host records on all known VPSes
        # For now, we iterate all host records
        host_record = self._find_host_record(host)
        if host_record is None:
            raise HostNotFoundError(self.name, host)

        host_id = HostId(host_record.certified_host_data.host_id)
        vps_ip = host_record.vps_ip

        if vps_ip is not None and host_record.config is not None:
            with self._make_outer_for_vps_ip(vps_ip) as outer:
                # Check if container is running
                if docker_inspect_running(outer, host_record.config.container_name):
                    return self._create_host_object(
                        host_id, HostName(host_record.certified_host_data.host_name), vps_ip
                    )

        return self._create_offline_host(host_record)

    def to_offline_host(self, host_id: HostId) -> OfflineHost:
        host_record = self._find_host_record(host_id)
        if host_record is None:
            raise HostNotFoundError(self.name, host_id)
        return self._create_offline_host(host_record)

    def discover_hosts(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> list[DiscoveredHost]:
        """Discover all hosts managed by this provider."""
        discovered: list[DiscoveredHost] = []

        # Query all VPS instances from the provider API that have our tags
        # then SSH to each VPS to read host records from its unified host volume.

        # First, try to find any VPS instances for this provider
        # We'll need the host records from each VPS
        all_records = self._discover_host_records()

        for record in all_records:
            host_id = HostId(record.certified_host_data.host_id)
            host_name = HostName(record.certified_host_data.host_name)
            discovered.append(
                DiscoveredHost(
                    host_id=host_id,
                    host_name=host_name,
                    provider_name=self.name,
                )
            )
            # Cache the host object
            if record.vps_ip is not None and record.config is not None:
                with self._make_outer_for_vps_ip(record.vps_ip) as outer:
                    if docker_inspect_running(outer, record.config.container_name):
                        self._create_host_object(host_id, host_name, record.vps_ip)
                    else:
                        self._create_offline_host(record)
            else:
                self._create_offline_host(record)

        return discovered

    def discover_hosts_and_agents(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> dict[DiscoveredHost, list[DiscoveredAgent]]:
        """Load hosts and agent references from each VPS, reading agents live.

        For each VPS, reads the host record directly from the docker volume's
        bind-source path (the per-host btrfs subvolume the volume's
        ``Options.device`` points at) and reads agent data **live** from the
        container (so in-container-created agents are discovered). The same
        live read reports the container's running state, so no separate
        inspect round-trip is needed.
        """
        with log_span("VPS Docker discover_hosts_and_agents for provider={}", self.name):
            discovery = self._discover_host_records_with_agents()
        agent_data_by_host_id = discovery.live_agent_data_by_host_id

        result: dict[DiscoveredHost, list[DiscoveredAgent]] = {}
        for record in discovery.records:
            host_id = HostId(record.certified_host_data.host_id)
            host_name = HostName(record.certified_host_data.host_name)

            # Cache the host record for later use by get_host_and_agent_details
            self._host_record_cache[host_id] = record

            # Running status came from the same live listing read. An entry in
            # is_running_by_host_id exists only when that read succeeded, which
            # requires the VPS to have been reachable -- so its presence doubles
            # as the reachability signal. A VPS that was unreachable during
            # discovery has no entry here, so it falls through to the
            # offline-state derivation rather than crashing the listing -- one
            # bad VPS must not drop the other VPSes' hosts.
            is_running = discovery.is_running_by_host_id.get(host_id, False)
            is_outer_reachable = host_id in discovery.is_running_by_host_id

            has_snapshots = len(record.certified_host_data.snapshots) > 0
            is_failed = record.certified_host_data.failure_reason is not None
            # A reachable VPS whose container is simply stopped (idle-watcher shutdown,
            # a manual `mngr stop`, or a VPS reboot) is cleanly STOPPED and restartable
            # via `mngr start` -- NOT crashed. The live listing read only succeeded
            # because the VPS is up, so this is never a host crash. Keep such hosts
            # visible to `mngr conn`/`start` (which pass include_destroyed=False)
            # instead of hiding them as if destroyed. An *unreachable* VPS with no
            # recorded stop_reason still derives to CRASHED below -- there the host
            # itself is down, which is the genuine failure case.
            is_cleanly_stopped = is_outer_reachable and not is_running

            if (
                not is_running
                and not is_cleanly_stopped
                and not is_failed
                and not has_snapshots
                and not include_destroyed
            ):
                continue

            if is_running and record.vps_ip is not None:
                host_state = HostState.RUNNING
                self._create_host_object(host_id, host_name, record.vps_ip)
            elif is_cleanly_stopped:
                host_state = HostState.STOPPED
                self._create_offline_host(record)
            else:
                host_state = derive_offline_host_state(
                    certified_data=record.certified_host_data,
                    supports_shutdown_hosts=self.supports_shutdown_hosts,
                    supports_snapshots=self.supports_snapshots,
                    has_snapshots=has_snapshots,
                )
                self._create_offline_host(record)

            host_ref = DiscoveredHost(
                host_id=host_id,
                host_name=host_name,
                provider_name=self.name,
                host_state=host_state,
            )

            # Build agent refs from live agent data
            agent_refs: list[DiscoveredAgent] = []
            for agent_data in agent_data_by_host_id.get(host_id, []):
                ref = validate_and_create_discovered_agent(agent_data, host_id, self.name)
                if ref is not None:
                    agent_refs.append(ref)

            result[host_ref] = agent_refs

        return result

    def _get_effective_auto_shutdown_seconds(self) -> int | None:
        """Return the auto-shutdown TTL (in seconds) to inject into cloud-init.

        Subclasses can override this to add provider-specific escape hatches
        (e.g., a test-only env-var that forces a TTL regardless of project
        config). The base implementation simply returns the configured value.
        """
        return self.config.auto_shutdown_seconds

    def _list_provider_vps_hostnames(self) -> list[str]:
        """Return SSH-reachable hostnames for VPSes owned by this provider instance.

        Each entry is whatever the provider hands back as the SSH target
        for one of its VPSes -- a public IPv4 (Vultr, AWS) or a provider DNS
        name (OVH classic VPS like ``vps-eec8860b.vps.ovh.us``). The
        discovery machinery below treats it as an opaque ``hostname``
        passed to paramiko.

        Concrete subclasses implement this by querying their provider's
        listing API (e.g. by tag) and resolving the matching instances'
        SSH targets. The remaining discovery machinery -- parallel SSH
        into each VPS, reading host records and agent data from each
        VPS's unified host volume, caching -- is shared and lives in
        this base class.

        Default returns ``[]`` so test doubles and providers without a
        listing API can opt out without overriding.
        """
        return []

    def _credentials_configured(self) -> bool:
        """Return True iff the provider's API credentials are resolvable.

        Used by ``_find_host_record`` to short-circuit a full discovery sweep
        when no credentials are available. Subclasses override to check their
        own credential source. The default returns True so providers that
        don't carry credentials (test doubles, OVH IAM via env) opt in
        automatically.
        """
        return True

    def _read_records_from_vps(
        self,
        vps_ip: str,
    ) -> _VpsDiscoveryData:
        """Read the (single) host record + live agent data from one VPS.

        Each VPS hosts exactly one mngr container (1:1 invariant). We find
        that container by its host-id label, derive its unified volume name,
        and read host_state.json from the volume's bind-source path (the
        per-host btrfs subvolume the volume's ``Options.device`` points at,
        resolved via ``docker volume inspect --format '{{.Options.device}}'``;
        the docker-managed ``Mountpoint`` placeholder is never consulted).

        Agent data is read **live** from the container's ``host_dir`` via the
        outer listing script -- not from the persisted ``agents/*.json`` outer
        store. The outer store is only written by the host-side mngr at
        agent-create time, so it misses agents created *inside* the container
        (e.g. by an in-container ``mngr create``); reading live ensures those
        agents are discoverable by ``mngr message`` and friends. The same call
        reports the container's running state, avoiding a separate inspect.

        If the container does not exist yet (e.g., the VPS is still being set
        up by a concurrent ``mngr create``), returns empty results. If outer
        SSH to the VPS fails, fall back to any in-process cached records for
        that VPS so the hosts still appear in the listing (with an offline
        state) instead of disappearing entirely; one bad VPS must not silently
        drop its hosts.
        """
        try:
            with self._make_outer_for_vps_ip(vps_ip) as outer:
                host_id = _read_host_id_label_from_vps(outer)
                if host_id is None:
                    logger.debug("No mngr container on VPS {} yet, skipping", vps_ip)
                    return _VpsDiscoveryData()
                host_store = open_host_store(outer, host_volume_name_for(host_id))
                record = host_store.read_host_record()
                if record is None:
                    logger.debug("No host record on VPS {} volume yet, skipping", vps_ip)
                    return _VpsDiscoveryData()
                # The host record read above succeeded, so the host exists and
                # must appear in the listing. A failure of the *live-listing*
                # read alone (e.g. ``docker exec`` racing a container restart)
                # must not drop the host -- degrade to no live agents and an
                # offline (not-running) state instead. Genuine VPS-unreachable
                # failures fail earlier (host-id probe / record read) and are
                # handled by the outer cache-fallback branch.
                try:
                    parsed_listing = _read_live_listing_from_vps(
                        outer, host_id, str(self.host_dir), self.mngr_ctx.config.prefix
                    )
                except MngrError as listing_exc:
                    logger.warning(
                        "Live listing read failed for host {} on VPS {}; surfacing host as offline: {}",
                        host_id,
                        vps_ip,
                        listing_exc,
                    )
                    return _VpsDiscoveryData(records=(record,))
                live_agent_data = _extract_live_agent_data(parsed_listing)
                is_running = parsed_listing.get("container_state") == "running"
                return _VpsDiscoveryData(
                    records=(record,),
                    live_agent_data_by_host_id={host_id: live_agent_data},
                    is_running_by_host_id={host_id: is_running},
                )
        except MngrError as e:
            cached_records = [r for r in self._host_record_cache.values() if r.vps_ip == vps_ip]
            if cached_records:
                logger.warning(
                    "Failed to read records from VPS {} ({}); surfacing {} cached host record(s) as offline",
                    vps_ip,
                    e,
                    len(cached_records),
                )
            else:
                logger.warning("Failed to read records from VPS {}: {}", vps_ip, e)
            return _VpsDiscoveryData(records=tuple(cached_records))

    def _discover_host_records_with_agents(self) -> _VpsDiscoveryData:
        """Discover host records and live agent data from all VPSes for this provider.

        Calls ``_list_provider_vps_hostnames`` to enumerate VPSes
        (provider-specific), then SSHes to each in parallel. Within each
        VPS, ``_read_records_from_vps`` finds the mngr container by host-id
        label, reads ``host_state.json``, and reads live agent data from the
        container; the parallel fan-out across VPSes keeps wall time bounded
        by the slowest VPS rather than the sum.
        """
        vps_ips = self._list_provider_vps_hostnames()
        if not vps_ips:
            return _VpsDiscoveryData()

        all_records: list[VpsDockerHostRecord] = []
        all_agent_data: dict[HostId, list[dict[str, Any]]] = {}
        all_running: dict[HostId, bool] = {}

        cg_name = f"{type(self).__name__}-discover"
        with log_span("Reading records from {} VPS instance(s) in parallel", len(vps_ips)):
            cg = ConcurrencyGroup(name=cg_name)
            with cg:
                with ConcurrencyGroupExecutor(
                    parent_cg=cg,
                    name=f"{cg_name}_read_records",
                    max_workers=min(len(vps_ips), 32),
                ) as executor:
                    futures = [executor.submit(self._read_records_from_vps, ip) for ip in vps_ips]

                for future in futures:
                    vps_data = future.result()
                    all_records.extend(vps_data.records)
                    for host_id, agents in vps_data.live_agent_data_by_host_id.items():
                        all_agent_data.setdefault(host_id, []).extend(agents)
                    all_running.update(vps_data.is_running_by_host_id)

        return _VpsDiscoveryData(
            records=tuple(all_records),
            live_agent_data_by_host_id=all_agent_data,
            is_running_by_host_id=all_running,
        )

    def _discover_host_records(self) -> list[VpsDockerHostRecord]:
        """Discover host records by enumerating this provider's VPSes."""
        return list(self._discover_host_records_with_agents().records)

    def _find_host_record(self, host: HostId | HostName) -> VpsDockerHostRecord | None:
        """Find a host record by ID or name, using cache first."""
        if isinstance(host, HostId) and host in self._host_record_cache:
            return self._host_record_cache[host]
        if isinstance(host, HostName):
            for cached_record in self._host_record_cache.values():
                if cached_record.certified_host_data.host_name == str(host):
                    return cached_record

        if not self._credentials_configured():
            logger.warning("{} credentials not configured, cannot resolve host", self.config.backend)
            return None

        records = self._discover_host_records()
        for record in records:
            host_id = HostId(record.certified_host_data.host_id)
            self._host_record_cache[host_id] = record
            if isinstance(host, HostId) and record.certified_host_data.host_id == str(host):
                return record
            elif isinstance(host, HostName) and record.certified_host_data.host_name == str(host):
                return record
        return None

    # =========================================================================
    # Optimized Listing
    # =========================================================================

    def get_host_and_agent_details(
        self,
        host_ref: DiscoveredHost,
        agent_refs: Sequence[DiscoveredAgent],
        field_generators: Mapping[str, Mapping[str, Callable[[AgentInterface, OnlineHostInterface], Any]]]
        | None = None,
        offline_field_generators: Mapping[str, Mapping[str, Callable[[DiscoveredAgent, HostDetails], Any]]]
        | None = None,
        on_error: Callable[[DiscoveredAgent | DiscoveredHost, BaseException], None] | None = None,
    ) -> tuple[HostDetails, list[AgentDetails]]:
        """Build HostDetails and AgentDetails via a single SSH command."""
        # Look up cached host record (populated during discover_hosts_and_agents)
        host_record = self._host_record_cache.get(host_ref.host_id)
        if host_record is None:
            host_record = self._find_host_record(host_ref.host_id)

        # For offline hosts or hosts without a record, fall back to default
        if host_record is None or host_record.vps_ip is None or host_record.config is None:
            return super().get_host_and_agent_details(
                host_ref,
                agent_refs,
                field_generators=field_generators,
                offline_field_generators=offline_field_generators,
                on_error=on_error,
            )

        try:
            host = self.get_host(host_ref.host_id)

            if not isinstance(host, Host):
                return super().get_host_and_agent_details(
                    host_ref,
                    agent_refs,
                    field_generators=field_generators,
                    offline_field_generators=offline_field_generators,
                    on_error=on_error,
                )

            # Collect all data in one SSH command
            script = build_listing_collection_script(str(self.host_dir), self.mngr_ctx.config.prefix)

            with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
                with log_span("Collecting listing data via single SSH command"):
                    raw_output = exec_in_container(
                        outer,
                        host_record.config.container_name,
                        script,
                        timeout_seconds=30.0,
                    )

            raw = parse_listing_collection_output(raw_output)

        except HostConnectionError as e:
            self.on_connection_error(host_ref.host_id)
            logger.debug(
                "Host {} unreachable during optimized listing, falling back to default: {}",
                host_ref.host_id,
                e,
            )
            return super().get_host_and_agent_details(
                host_ref,
                agent_refs,
                field_generators=field_generators,
                offline_field_generators=offline_field_generators,
                on_error=on_error,
            )
        except MngrError as e:
            if on_error:
                on_error(host_ref, e)
                return HostDetails(
                    id=host_ref.host_id,
                    name=str(host_ref.host_name),
                    provider_name=host_ref.provider_name,
                    state=HostState.RUNNING,
                ), []
            else:
                raise

        host_details = self._build_host_details_from_raw(host, host_ref, host_record, raw)
        agent_details_list = self._build_agent_details_from_raw(host_details, host_record.certified_host_data, raw)
        return host_details, agent_details_list

    def _build_host_details_from_raw(
        self,
        host: Host,
        host_ref: DiscoveredHost,
        host_record: VpsDockerHostRecord,
        raw: dict[str, Any],
    ) -> HostDetails:
        """Construct HostDetails from cached host record and SSH-collected data."""
        ssh_info: SSHInfo | None = None
        ssh_connection = host.get_ssh_connection_info()
        if ssh_connection is not None:
            user, hostname, port, key_path = ssh_connection
            ssh_info = SSHInfo(
                user=user,
                host=hostname,
                port=port,
                key_path=key_path,
                command=f"ssh -i {key_path} -p {port} {user}@{hostname}",
            )

        boot_time = timestamp_to_datetime(raw.get("btime"))
        uptime_seconds = raw.get("uptime_seconds")
        resource = self.get_host_resources(host)

        lock_mtime = raw.get("lock_mtime")
        is_locked = lock_mtime is not None
        locked_time = datetime.fromtimestamp(lock_mtime, tz=timezone.utc) if lock_mtime is not None else None

        certified_data: CertifiedHostData | None = None
        certified_data_dict = raw.get("certified_data")
        if certified_data_dict is not None:
            try:
                certified_data = CertifiedHostData.model_validate(certified_data_dict)
            except (ValueError, KeyError) as e:
                logger.warning("Failed to validate host data.json from SSH output: {}", e)
        if certified_data is None:
            certified_data = host_record.certified_host_data

        tags = dict(certified_data.user_tags)

        ssh_activity_mtime = raw.get("ssh_activity_mtime")
        ssh_activity = (
            datetime.fromtimestamp(ssh_activity_mtime, tz=timezone.utc) if ssh_activity_mtime is not None else None
        )

        snapshots = self.list_snapshots(host)

        return HostDetails(
            id=host.id,
            name=certified_data.host_name,
            provider_name=host_ref.provider_name,
            state=HostState.RUNNING,
            image=certified_data.image,
            tags=tags,
            boot_time=boot_time,
            uptime_seconds=uptime_seconds,
            resource=resource,
            ssh=ssh_info,
            snapshots=snapshots,
            is_locked=is_locked,
            locked_time=locked_time,
            plugin=certified_data.plugin,
            ssh_activity_time=ssh_activity,
            failure_reason=certified_data.failure_reason,
        )

    def _build_agent_details_from_raw(
        self,
        host_details: HostDetails,
        certified_host_data: CertifiedHostData,
        raw: dict[str, Any],
    ) -> list[AgentDetails]:
        """Build AgentDetails objects from SSH-collected agent data."""
        idle_timeout_seconds = certified_host_data.idle_timeout_seconds
        activity_sources = certified_host_data.activity_sources
        idle_mode = certified_host_data.idle_mode

        ssh_activity = timestamp_to_datetime(raw.get("ssh_activity_mtime"))
        ps_output = raw.get("ps_output", "")

        agent_details_list: list[AgentDetails] = []
        for agent_raw in raw.get("agents", []):
            try:
                agent_details = self._build_single_agent_details(
                    agent_raw=agent_raw,
                    host_details=host_details,
                    ssh_activity=ssh_activity,
                    ps_output=ps_output,
                    idle_timeout_seconds=idle_timeout_seconds,
                    activity_sources=activity_sources,
                    idle_mode=idle_mode,
                )
                if agent_details is not None:
                    agent_details_list.append(agent_details)
            except (ValueError, KeyError, TypeError) as e:
                agent_id = agent_raw.get("data", {}).get("id", "unknown")
                logger.warning("Failed to build listing info for agent {}: {}", agent_id, e)

        return agent_details_list

    def _build_single_agent_details(
        self,
        agent_raw: dict[str, Any],
        host_details: HostDetails,
        ssh_activity: datetime | None,
        ps_output: str,
        idle_timeout_seconds: int,
        activity_sources: tuple[ActivitySource, ...],
        idle_mode: IdleMode,
    ) -> AgentDetails | None:
        """Build a single AgentDetails from raw SSH-collected data."""
        agent_data = agent_raw.get("data", {})
        agent_id_str = agent_data.get("id")
        agent_name_str = agent_data.get("name")
        if not agent_id_str or not agent_name_str:
            logger.warning("Skipped agent with missing id or name in listing data: {}", agent_data)
            return None

        agent_type = str(agent_data.get("type", "unknown"))
        command = CommandString(agent_data.get("command", "bash"))
        create_time_str = agent_data.get("create_time")
        try:
            create_time = (
                datetime.fromisoformat(create_time_str)
                if create_time_str
                else datetime(1970, 1, 1, tzinfo=timezone.utc)
            )
        except (ValueError, TypeError) as e:
            logger.warning("Failed to parse create_time for agent {}: {}", agent_id_str, e)
            create_time = datetime(1970, 1, 1, tzinfo=timezone.utc)

        user_activity = timestamp_to_datetime(agent_raw.get("user_activity_mtime"))
        agent_activity = timestamp_to_datetime(agent_raw.get("agent_activity_mtime"))
        start_time = timestamp_to_datetime(agent_raw.get("start_activity_mtime"))
        now = datetime.now(timezone.utc)
        runtime_seconds = (now - start_time).total_seconds() if start_time else None
        idle_seconds = compute_idle_seconds(user_activity, agent_activity, ssh_activity)

        expected_process_name = resolve_expected_process_name(agent_type, command, self.mngr_ctx.config)
        is_type_known = check_agent_type_known(agent_type, self.mngr_ctx.config)
        state = determine_lifecycle_state(
            tmux_info=agent_raw.get("tmux_info"),
            is_active=agent_raw.get("is_active", False),
            expected_process_name=expected_process_name,
            ps_output=ps_output,
            is_agent_type_known=is_type_known,
        )

        return AgentDetails(
            id=AgentId(agent_id_str),
            name=AgentName(agent_name_str),
            type=agent_type,
            command=command,
            work_dir=Path(agent_data.get("work_dir", "/")),
            initial_branch=agent_data.get("created_branch_name"),
            create_time=create_time,
            start_on_boot=agent_data.get("start_on_boot", False),
            state=state,
            url=agent_raw.get("url"),
            start_time=start_time,
            runtime_seconds=runtime_seconds,
            user_activity_time=user_activity,
            agent_activity_time=agent_activity,
            idle_seconds=idle_seconds,
            idle_mode=idle_mode.value,
            idle_timeout_seconds=idle_timeout_seconds,
            activity_sources=tuple(s.value for s in activity_sources),
            labels=agent_data.get("labels", {}),
            host=host_details,
            plugin={},
        )

    # =========================================================================
    # Snapshots
    # =========================================================================

    def create_snapshot(
        self,
        host: HostInterface | HostId,
        name: SnapshotName | None = None,
    ) -> SnapshotId:
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.config is None or host_record.vps_ip is None:
            raise HostNotFoundError(self.name, host_id)

        snapshot_name = name or SnapshotName(f"mngr-snapshot-{host_id}-{int(time.time())}")
        image_tag = f"mngr-snapshot-{host_id.get_uuid().hex}-{int(time.time())}"

        with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
            with log_span("Creating Docker snapshot"):
                image_id = commit_container(outer, host_record.config.container_name, image_tag)

            # Store snapshot record in host data
            snapshot_record = SnapshotRecord(
                id=image_id,
                name=str(snapshot_name),
                created_at=datetime.now(timezone.utc).isoformat(),
            )

            # Update certified data with new snapshot
            existing_snapshots = host_record.certified_host_data.snapshots
            updated_snapshots = list(existing_snapshots) + [snapshot_record]
            certified = host_record.certified_host_data
            updated_data = certified.model_copy_update(
                to_update(certified.field_ref().snapshots, updated_snapshots),
                to_update(certified.field_ref().updated_at, datetime.now(timezone.utc)),
            )
            updated_record = host_record.model_copy_update(
                to_update(host_record.field_ref().certified_host_data, updated_data)
            )

            # ``host_record.config`` is guaranteed non-None by the guard at the top of this method.
            host_store = open_host_store(outer, host_record.config.volume_name)
            host_store.write_host_record(updated_record)
            self._persist_host_record_externally(updated_record)

        logger.info("Created snapshot {} for host {}", snapshot_name, host_id)
        return SnapshotId(image_id)

    def list_snapshots(self, host: HostInterface | HostId) -> list[SnapshotInfo]:
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None:
            raise HostNotFoundError(self.name, host_id)

        snapshots = host_record.certified_host_data.snapshots
        return [
            SnapshotInfo(
                id=SnapshotId(s.id),
                name=SnapshotName(s.name),
                created_at=datetime.fromisoformat(s.created_at),
            )
            for s in snapshots
        ]

    def delete_snapshot(self, host: HostInterface | HostId, snapshot_id: SnapshotId) -> None:
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.vps_ip is None:
            raise HostNotFoundError(self.name, host_id)

        with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
            try:
                run_docker(outer, ["rmi", str(snapshot_id)])
            except MngrError as e:
                logger.warning("Failed to delete snapshot image: {}", e)

    # =========================================================================
    # Tags
    # =========================================================================

    def get_host_tags(self, host: HostInterface | HostId) -> dict[str, str]:
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None:
            raise HostNotFoundError(self.name, host_id)
        return dict(host_record.certified_host_data.user_tags)

    def set_host_tags(self, host: HostInterface | HostId, tags: Mapping[str, str]) -> None:
        raise MngrError("VPS Docker provider does not support mutable tags")

    def add_tags_to_host(self, host: HostInterface | HostId, tags: Mapping[str, str]) -> None:
        raise MngrError("VPS Docker provider does not support mutable tags")

    def remove_tags_from_host(self, host: HostInterface | HostId, keys: Sequence[str]) -> None:
        raise MngrError("VPS Docker provider does not support mutable tags")

    def rename_host(self, host: HostInterface | HostId, name: HostName) -> HostInterface:
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None:
            raise HostNotFoundError(self.name, host_id)

        certified = host_record.certified_host_data
        updated_data = certified.model_copy_update(
            to_update(certified.field_ref().host_name, str(name)),
            to_update(certified.field_ref().updated_at, datetime.now(timezone.utc)),
        )
        updated_record = host_record.model_copy_update(
            to_update(host_record.field_ref().certified_host_data, updated_data)
        )

        if host_record.vps_ip is not None:
            if host_record.config is None:
                raise MngrError(
                    f"Host record for {host_id} on VPS {host_record.vps_ip} is missing config -- "
                    "cannot determine unified volume name to rename"
                )
            with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
                host_store = open_host_store(outer, host_record.config.volume_name)
                host_store.write_host_record(updated_record)
                self._persist_host_record_externally(updated_record)

        return self.get_host(host_id)

    # =========================================================================
    # Volumes
    # =========================================================================

    def list_volumes(self) -> list[VolumeInfo]:
        return []

    def delete_volume(self, volume_id: VolumeId) -> None:
        pass

    # =========================================================================
    # Resources
    # =========================================================================

    def get_host_resources(self, host: HostInterface) -> HostResources:
        return HostResources(
            cpu=CpuResources(count=1, frequency_ghz=None),
            memory_gb=1.0,
            disk_gb=None,
            gpu=None,
        )

    # =========================================================================
    # Connector
    # =========================================================================

    def get_connector(self, host: HostInterface | HostId) -> PyinfraHost:
        resolved = self.get_host(host.id if isinstance(host, HostInterface) else host)
        if isinstance(resolved, Host):
            return resolved.connector.host
        raise MngrError("Cannot get connector for offline host")

    # =========================================================================
    # Agent Data Persistence
    # =========================================================================

    def list_persisted_agent_data_for_host(self, host_id: HostId) -> list[dict]:
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.vps_ip is None or host_record.config is None:
            raise HostNotFoundError(self.name, host_id)

        with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
            host_store = open_host_store(outer, host_record.config.volume_name)
            return host_store.list_persisted_agent_data()

    def persist_agent_data(self, host_id: HostId, agent_data: Mapping[str, object]) -> None:
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.vps_ip is None or host_record.config is None:
            raise HostNotFoundError(self.name, host_id)

        with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
            host_store = open_host_store(outer, host_record.config.volume_name)
            host_store.persist_agent_data(agent_data)

    def remove_persisted_agent_data(self, host_id: HostId, agent_id: AgentId) -> None:
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.vps_ip is None or host_record.config is None:
            raise HostNotFoundError(self.name, host_id)

        with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
            host_store = open_host_store(outer, host_record.config.volume_name)
            host_store.remove_persisted_agent_data(agent_id)


# Self-stopping idle watcher, shared by the cloud providers. The in-container
# activity watcher writes ``IDLE_SENTINEL_FILENAME`` onto the shared volume when
# idle; a host-side systemd ``.path`` unit observes it and fires a oneshot
# ``.service`` that pauses the instance (poweroff for AWS/GCP; a self-deallocate
# script for Azure). A single mngr provider owns a host, so one unit name suffices.
IDLE_SENTINEL_FILENAME: Final[str] = "stop-instance-requested"
IDLE_WATCHER_UNIT_NAME: Final[str] = "mngr-idle-watcher"


def build_sentinel_shutdown_script(sentinel_in_container: str) -> str:
    """Build the in-container ``shutdown.sh`` that signals idle by touching the sentinel.

    Unlike the base ``VpsDockerProvider`` script (``kill -TERM 1``, which stops only
    the container), the cloud variant signals idle by touching a sentinel on the
    shared volume; a host-side systemd path unit observes it and pauses the whole
    instance (a container cannot pause its host).
    """
    return f'#!/bin/bash\ntouch "{sentinel_in_container}"\n'


def build_idle_watcher_path_unit(sentinel_on_outer: str) -> str:
    """Build the systemd ``.path`` unit that fires the watcher service when the sentinel appears."""
    return (
        "[Unit]\n"
        "Description=Watch for the mngr idle sentinel and pause this instance when idle\n"
        "[Path]\n"
        f"PathExists={sentinel_on_outer}\n"
        f"Unit={IDLE_WATCHER_UNIT_NAME}.service\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def build_poweroff_idle_watcher_service_unit(sentinel_on_outer: str) -> str:
    """Build the oneshot ``.service`` that powers the host off when idle (AWS/GCP).

    Removes the sentinel BEFORE powering off so that, when the instance is resumed,
    the re-armed ``.path`` unit does not immediately fire again and re-pause the
    just-started host. On EC2 the poweroff applies the instance's
    ``InstanceInitiatedShutdownBehavior``; on GCE a guest poweroff stops the
    instance -- neither needs an API call or IAM.
    """
    return (
        "[Unit]\n"
        "Description=Power off this instance when mngr signals the host is idle\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart=/bin/sh -c 'rm -f {sentinel_on_outer} && shutdown -P now'\n"
    )


def build_oneshot_sync_service_unit(
    description: str, command: str, *, environment: Mapping[str, str] | None = None
) -> str:
    """Build a ``Type=oneshot`` systemd ``.service`` that runs ``command`` once.

    Shared by the cloud host_dir-sync daemons (AWS ``aws s3 sync`` / Azure
    ``azcopy sync``). ``environment`` adds ``Environment=K=V`` lines before
    ``ExecStart`` (Azure pins the azcopy MSI login this way; AWS passes none).
    """
    env_lines = "".join(f"Environment={key}={value}\n" for key, value in (environment or {}).items())
    return f"[Unit]\nDescription={description}\n[Service]\nType=oneshot\n{env_lines}ExecStart=/bin/sh -c '{command}'\n"


def build_sync_timer_unit(description: str, interval_seconds: int, unit_name: str) -> str:
    """Build a systemd ``.timer`` that fires ``<unit_name>.service`` every ``interval_seconds``.

    ``OnBootSec`` gives the host a moment to finish bootstrapping before the first
    run; ``OnUnitActiveSec`` then repeats at the interval. Shared by the cloud
    host_dir-sync daemons.
    """
    return (
        "[Unit]\n"
        f"Description={description}\n"
        "[Timer]\n"
        f"OnBootSec={interval_seconds}\n"
        f"OnUnitActiveSec={interval_seconds}\n"
        f"Unit={unit_name}.service\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


class OfflineCapableVpsDockerProvider(VpsDockerProvider):
    """``VpsDockerProvider`` for cloud providers whose hosts can be stopped while
    their disk persists, with host/agent identity mirrored into instance
    tags/metadata.

    A stopped (deallocated / powered-off) instance keeps its disk but is
    SSH-unreachable, so the volume-backed base discovery and host resolution
    cannot see it. This class adds the shared offline recovery (reconstructing such
    hosts and their agents from the provider's instance listing) *and* the shared
    stop/start lifecycle + self-stopping idle watcher. Per-provider specifics are
    supplied through the hooks below; the cloud-API calls (pause/resume the
    instance), the agent-record mirror (tags vs metadata), and -- for Azure -- the
    static-IP/self-deallocate variations are the only parts that differ.
    """

    # -- Stop/start lifecycle (idle-pause + resume) -----------------------------

    def stop_host(
        self,
        host: HostInterface | HostId,
        create_snapshot: bool = True,
        timeout_seconds: float = 60.0,
        stop_reason: HostState | None = None,
    ) -> None:
        """Stop the agent container *and* pause the cloud instance, preserving its disk.

        The base ``VpsDockerProvider.stop_host`` only stops the inner Docker
        container, leaving the instance running and billing. This override reuses
        that container-stop + record-write via ``super()`` (passing
        ``stop_reason=STOPPED`` so the single write marks the host STOPPED before
        its volume goes unreachable), then calls ``_pause_cloud_instance`` to stop /
        deallocate the instance so a paused agent costs only disk storage. The disk
        (and all on-disk state) survives, so ``start_host`` can resume it.
        ``create_snapshot`` is ignored -- pausing preserves the whole filesystem.
        """
        del create_snapshot
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.config is None or host_record.vps_ip is None:
            raise HostNotFoundError(self.name, host_id)
        super().stop_host(
            host, create_snapshot=False, timeout_seconds=timeout_seconds, stop_reason=stop_reason or HostState.STOPPED
        )
        # The container is stopped (so host_dir is quiesced) but the instance is
        # still reachable: flush any offline host_dir mirror now, before the pause.
        self._sync_host_dir_before_pause(host_id, host_record.vps_ip)
        self._pause_cloud_instance(host_record.config.vps_instance_id)

    @property
    def _host_dir_backend(self) -> HostDirBackend:
        """The offline ``host_dir`` capability: bucket-backed when enabled + present, else a no-op.

        Offline ``host_dir`` requires an object-storage bucket to sync into, so it
        is a separate concern from the ``_state_store`` host/agent-record mirror.
        The default is the no-op ``NullHostDirBackend`` -- correct for a provider
        with no bucket (e.g. GCP). Providers that mirror host_dir to a bucket
        override this with a selected-once cached property, so the host_dir paths
        below never re-test ``is_offline_host_dir_enabled`` / bucket presence.
        """
        return NullHostDirBackend()

    def _sync_host_dir_before_pause(self, host_id: HostId, vps_ip: str) -> None:
        """Push host_dir to the external store one final time before the instance pauses.

        Runs in ``stop_host`` after the container has stopped (host_dir quiesced)
        and before the instance is paused (still SSH-reachable), so the offline view
        is current the moment the instance stops. The no-op backend makes this a
        no-op for providers without a bucket.
        """
        self._host_dir_backend.trigger_final_sync(host_id, vps_ip)

    def get_volume_reference_for_host(self, host: HostInterface | HostId) -> HostVolume | None:
        """Return the bucket-backed host_dir volume *reference* (cheap, no network probe), or None.

        Delegates to the selected host_dir backend (the no-op backend returns None
        when the feature is off or no bucket exists).
        """
        host_id = host.id if isinstance(host, HostInterface) else host
        return self._host_dir_backend.volume_reference(host_id)

    def get_volume_for_host(self, host: HostInterface | HostId) -> HostVolume | None:
        """Return the bucket-backed host_dir volume, with a light existence probe, or None.

        Delegates to the selected host_dir backend, which probes that the host's
        ``host_dir/`` prefix has objects and, when empty, warns if the instance was
        never granted the bucket-write identity. Returns None when unavailable.
        """
        host_id = host.id if isinstance(host, HostInterface) else host
        return self._host_dir_backend.volume(host_id)

    def start_host(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId | None = None,
    ) -> Host:
        """Resume a paused agent: start the cloud instance, then its container.

        A paused instance is SSH-unreachable, so it is located by its
        ``mngr-host-id`` tag/label (not the SSH-based record lookup), resumed via
        ``_resume_cloud_instance`` (which returns the instance's address -- fresh
        for ephemeral-IP providers, unchanged for Azure's static IP), and its SSH
        known_hosts re-pointed at that address (a no-op when the IP did not change).
        We then clear the idle sentinel + ``stop_reason`` and rewrite the record's
        ``vps_ip`` before delegating the container start to ``super()`` (whose
        ``_find_host_record`` reads the refreshed cache entry).
        """
        host_id = host.id if isinstance(host, HostInterface) else host
        instance = self._find_instance_for_host(host_id)
        if instance is None:
            raise HostNotFoundError(self.name, host_id)
        instance_id = VpsInstanceId(instance["id"])
        new_ip = self._resume_cloud_instance(instance_id)
        # The cached instance list predates the start (stale power state / IP); drop
        # it so any later discovery sees the running instance and its address.
        self._instances_cache = None
        # Rebind known_hosts to the address from mngr's local host keypairs BEFORE
        # connecting -- the instance kept its host keys across the pause (they live
        # on the disk), but the record (the other key source) can't be read until we
        # can SSH in. A no-op for static-IP providers (Azure).
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
            # immediately re-paused by the systemd path unit (belt-and-suspenders;
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
            # Re-mirror the rebound record to the external store (bucket / tags) so
            # the offline view reflects the new vps_ip and cleared stop_reason; a
            # no-op for providers without an external store.
            self._persist_host_record_externally(updated_record)
        # Drop any cached Host bound to the old IP, then seed the record cache so
        # super().start_host()'s _find_host_record returns the rebound record.
        self._evict_cached_host(host_id)
        self._host_record_cache[host_id] = updated_record
        # The base ``start_host`` relaunches the in-container activity watcher and
        # refreshes BOOT activity on resume, so auto-stop-on-idle keeps working
        # across resumes with no provider-specific step here.
        return super().start_host(host_id, snapshot_id)

    @abstractmethod
    def _pause_cloud_instance(self, instance_id: VpsInstanceId) -> None:
        """Stop / deallocate the cloud instance (the provider's own log span + API call)."""
        ...

    @abstractmethod
    def _resume_cloud_instance(self, instance_id: VpsInstanceId) -> str:
        """Start the cloud instance and return its SSH address (fresh IP, or the static one)."""
        ...

    def _find_instance_for_host(self, host_id: HostId) -> dict[str, Any] | None:
        """Locate this host's instance by its ``mngr-host-id`` tag/label (works while stopped), or None.

        Reads only the cached instance listing (no SSH), so it resolves an instance
        that is paused and therefore unreachable. The listing already excludes
        destroyed instances, so a destroyed host returns ``None``. Refuses (raises)
        when more than one instance carries the same ``mngr-host-id``: the tag/label
        is account-writable, so a duplicate could otherwise silently steer ``mngr
        start`` (and the agent-record writes keyed off this lookup) onto the wrong
        instance.
        """
        matches = self._instances_matching_host_id(host_id)
        if not matches:
            # The cached list can predate this instance (e.g. a discovery/name-conflict
            # check during create populated it first); refresh once and retry.
            self._instances_cache = None
            matches = self._instances_matching_host_id(host_id)
        if len(matches) > 1:
            ids = sorted(str(m.get("id")) for m in matches)
            raise MngrError(
                f"Provider {self.name!r}: {len(matches)} instances are tagged "
                f"mngr-host-id={host_id} ({', '.join(ids)}); refusing to act on an ambiguous match. "
                "Resolve the duplicate tags/labels (or remove the stray instance) and retry."
            )
        return matches[0] if matches else None

    def _instances_matching_host_id(self, host_id: HostId) -> list[dict[str, Any]]:
        """Return every cached instance tagged ``mngr-host-id=<host_id>``.

        Providers whose tag/label values are encoded (e.g. GCE labels) override this
        to match on the encoded value.
        """
        wanted = f"mngr-host-id={host_id}"
        return [instance for instance in self._list_instances_cached() if wanted in instance.get("tags", ())]

    def _rebind_known_hosts(self, record: VpsDockerHostRecord, new_ip: str) -> None:
        """Re-point local known_hosts at ``new_ip`` using the instance's preserved host keys.

        A pause/resume keeps the instance's SSH host keys (on the disk), so only the
        IP changes. Drop any stale entries for the old IP, then add the new IP with
        the recorded VPS (port 22) and container host keys. Providers whose IP is
        stable across a pause (Azure) override this to a no-op.
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
        source, can't be read until we can connect). The VPS/container host keypairs
        are generated and held locally by mngr and injected at create time, so the
        public keys here are exactly what the resumed instance presents (its host
        keys persist on the disk across a pause). Providers whose IP is stable across
        a pause (Azure) override this to a no-op.
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

    # -- Self-stopping idle watcher --------------------------------------------

    def _create_shutdown_script(self, host: Host) -> None:
        """Write an in-container ``shutdown.sh`` that signals idle via a sentinel file.

        The base writes ``kill -TERM 1`` (stops the container); for a cloud provider
        an idle container should pause the whole *instance* (so a paused agent costs
        only disk), but a container cannot pause its host. Instead the in-container
        watcher touches a sentinel on the shared volume; a host-side systemd path
        unit (installed in ``_on_host_finalized``) observes it and pauses the host.
        """
        sentinel_in_container = str(host.host_dir / "commands" / IDLE_SENTINEL_FILENAME)
        shutdown_script = build_sentinel_shutdown_script(sentinel_in_container)
        commands_dir = host.host_dir / "commands"
        host.execute_idempotent_command(f"mkdir -p {commands_dir}")
        host.write_file(commands_dir / "shutdown.sh", shutdown_script.encode())
        host.execute_idempotent_command(f"chmod +x {commands_dir / 'shutdown.sh'}")

    def _idle_sentinel_path_on_outer(self, host_id: HostId) -> Path:
        """Outer-filesystem path of the in-container idle sentinel for this host.

        The container writes the sentinel at ``<host_dir>/commands/<file>`` on the
        shared volume; on the outer host that maps to
        ``<btrfs_mount_path>/<host_id_hex>/host_dir/commands/<file>``.
        """
        return (
            self.config.btrfs_mount_path
            / host_id.get_uuid().hex
            / HOST_DIR_SUBPATH
            / "commands"
            / IDLE_SENTINEL_FILENAME
        )

    def _host_dir_path_on_outer(self, host_id: HostId) -> Path:
        """Outer-filesystem path of this host's host_dir (the btrfs subvolume's host_dir tree).

        The per-host host_dir lives at ``<btrfs_mount_path>/<host_id_hex>/host_dir``
        on the outer (the same subvolume layout the idle sentinel path uses). Used
        by the bucket host_dir sync daemon (AWS/Azure).
        """
        return self.config.btrfs_mount_path / host_id.get_uuid().hex / HOST_DIR_SUBPATH

    def _on_host_finalized(self, *, host_id: HostId, vps_ip: str) -> None:
        """Install the host-side systemd idle watcher that self-pauses this instance.

        Runs after the host record is durably written. Best-effort, per the base
        contract this MUST NOT raise: any failure just means no auto-stop on idle
        (manual ``mngr stop`` still works). ``_prepare_idle_self_stop`` is a hook for
        any provider-specific prerequisite (e.g. Azure's self-deallocate role
        assignment); it must not raise either.
        """
        self._prepare_idle_self_stop(host_id)
        try:
            self._install_idle_watcher(host_id=host_id, vps_ip=vps_ip)
        except MngrError as e:
            logger.warning(
                "{} idle watcher install failed for host {} ({}); the agent will not "
                "auto-stop on idle, but `mngr stop` still works",
                self.name,
                host_id,
                e,
            )

    def _install_idle_watcher(self, *, host_id: HostId, vps_ip: str) -> None:
        """Install the systemd path/service idle watcher on the outer host.

        Writes any provider-specific auxiliary files (``_write_idle_watcher_aux_files``,
        e.g. Azure's self-deallocate script), the shared ``.path`` unit, and the
        provider's ``.service`` body (``_idle_watcher_service_unit``), then enables
        the path unit. Returns early (after a WARNING) when the host record is missing.
        """
        record = self._find_host_record(host_id)
        if record is None or record.config is None:
            logger.warning(
                "{} idle watcher: no host record for {}; skipping watcher install (no auto-stop)",
                self.name,
                host_id,
            )
            return
        sentinel_on_outer = self._idle_sentinel_path_on_outer(host_id)
        with log_span("Installing idle self-stop watcher"):
            with self._make_outer_for_vps_ip(vps_ip) as outer:
                self._write_idle_watcher_aux_files(outer, str(sentinel_on_outer))
                outer.write_text_file(
                    Path(f"/etc/systemd/system/{IDLE_WATCHER_UNIT_NAME}.path"),
                    build_idle_watcher_path_unit(str(sentinel_on_outer)),
                )
                outer.write_text_file(
                    Path(f"/etc/systemd/system/{IDLE_WATCHER_UNIT_NAME}.service"),
                    self._idle_watcher_service_unit(str(sentinel_on_outer)),
                )
                outer.execute_idempotent_command("systemctl daemon-reload")
                outer.execute_idempotent_command(f"systemctl enable --now {IDLE_WATCHER_UNIT_NAME}.path")
        logger.info("Idle self-stop watcher installed for host {} on provider {}", host_id, self.name)

    @abstractmethod
    def _idle_watcher_service_unit(self, sentinel_on_outer: str) -> str:
        """Return the oneshot ``.service`` body that pauses the host when idle.

        AWS/GCP power off (``build_poweroff_idle_watcher_service_unit``); Azure runs
        its self-deallocate script.
        """
        ...

    def _prepare_idle_self_stop(self, host_id: HostId) -> None:
        """Provider-specific prerequisite for idle self-stop, run before the watcher install.

        Default: nothing. Azure overrides this to assign its self-deallocate role.
        Best-effort -- it MUST NOT raise (``_on_host_finalized`` relies on that).
        """

    def _write_idle_watcher_aux_files(self, outer: OuterHostInterface, sentinel_on_outer: str) -> None:
        """Write any provider-specific files the watcher service needs (default: none).

        Azure overrides this to install the self-deallocate script (and ensure curl).
        """

    # -- Offline discovery / resolution ----------------------------------------

    @property
    @abstractmethod
    def _state_store(self) -> HostStateStore:
        """The external host/agent-record mirror this provider reads and writes offline state through.

        Every offline-capable provider selects exactly one store (implemented as a
        cached property so any bucket-existence probe runs at most once): the
        object-storage ``BucketHostStateStore`` (AWS S3, Azure Blob) or the GCP
        instance-metadata store. When a required object-storage bucket has not yet
        been provisioned the property raises an actionable error (see
        ``missing_state_bucket_error``) rather than returning a degraded store, so
        every persist / remove / list / read below fails loudly and uniformly.
        Selecting the store here lets those paths stop branching on the backing
        store.
        """
        ...

    @abstractmethod
    def _offline_discovered_host_from_instance(self, instance: Mapping[str, Any]) -> DiscoveredHost | None:
        """Build a STOPPED ``DiscoveredHost`` from an instance's identity tags/labels/metadata.

        Returns ``None`` when the instance is not a mngr host. Raises ``ValueError``
        when the instance carries a mngr host identity that is malformed (a
        corrupt/externally-edited host-id or name). Reads only the cheap cached
        listing (host id + name), never the state store, so a discovery sweep stays
        cheap; the full record is reconstructed from the store only on demand
        (``to_offline_host``).
        """
        ...

    @abstractmethod
    def _is_instance_offline(self, instance: Mapping[str, Any]) -> bool:
        """Whether this instance's OS is down (stopped/deallocated, and their in-flight transitions).

        Called only for mngr instances the live SSH sweep did NOT surface, so a
        provider that must spend a per-instance API call to read power state pays
        for it only on the unreachable ones.
        """
        ...

    def _offline_agent_dicts_for(self, host_id: HostId, instance: Mapping[str, Any] | None = None) -> list[dict]:
        """Return a stopped host's mirrored agent records from the external state store.

        Keyed by ``host_id``; the ``instance`` argument is accepted (the discovery
        loop passes the instance it is already iterating) but unused, since the
        store resolves everything from ``host_id``. A read against a provider whose
        required bucket is absent (or a bucket storage error) raises, which the
        discovery wrapper attributes to this provider and surfaces per the caller's
        ``--on-error``.
        """
        del instance
        return self._state_store.list_agent_records(host_id)

    def _mirror_agent_record(self, host_id: HostId, agent_id: str, agent_data: Mapping[str, object]) -> None:
        """Mirror one agent record into the external state store (upsert).

        Propagates a storage/missing-bucket error: the bucket is required, so a
        dropped mirror would let a stopped host show stale agents.
        """
        self._state_store.persist_agent_record(host_id, agent_id, agent_data)

    def _remove_mirrored_agent_record(self, host_id: HostId, agent_id: str) -> None:
        """Remove one agent's mirrored record from the external state store (idempotent; errors propagate)."""
        self._state_store.remove_agent_record(host_id, agent_id)

    def _persist_host_record_externally(self, record: VpsDockerHostRecord) -> None:
        """Mirror the full host record into the external state store (errors propagate)."""
        self._state_store.persist_host_record(record)

    def _delete_host_record_externally(self, host_id: HostId) -> None:
        """Delete the host's state from the external state store (idempotent; errors propagate)."""
        self._state_store.delete_host_state(host_id)

    def persist_agent_data(self, host_id: HostId, agent_data: Mapping[str, object]) -> None:
        """Persist an agent's record on the host volume *and* mirror it for offline reads.

        The base ``VpsDockerProvider`` writes the authoritative on-volume record
        (read by the SSH-based discovery for *running* hosts), so this keeps doing
        that via ``super()``. That write is best-effort: a *stopped* host raises
        ``HostNotFoundError`` (no reachable ``vps_ip``), in which case only the
        offline mirror is written, so e.g. an offline ``mngr label`` still updates
        the record a stopped host lists from. ``_mirror_agent_record`` is the only
        per-provider step (instance tags/metadata, or an external store).
        """
        try:
            super().persist_agent_data(host_id, agent_data)
        except HostNotFoundError:
            logger.debug("Host {} unreachable; mirroring agent data to the offline store only", host_id)
        agent_id = agent_data.get("id")
        if agent_id is None:
            logger.warning("Cannot mirror agent data without an id (name={!r})", agent_data.get("name"))
            return
        self._mirror_agent_record(host_id, str(agent_id), agent_data)

    def remove_persisted_agent_data(self, host_id: HostId, agent_id: AgentId) -> None:
        """Remove the agent's on-volume record *and* its offline mirror.

        Mirrors ``persist_agent_data``: the base removes the authoritative on-volume
        record (best-effort -- ``HostNotFoundError`` when the host is stopped) and
        ``_remove_mirrored_agent_record`` drops the offline copy, so a destroyed
        agent stops appearing in both running- and stopped-host discovery. Both
        removals are idempotent.
        """
        try:
            super().remove_persisted_agent_data(host_id, agent_id)
        except HostNotFoundError:
            logger.debug("Host {} unreachable; removing agent data from the offline store only", host_id)
        self._remove_mirrored_agent_record(host_id, str(agent_id))

    def discover_hosts_and_agents(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> dict[DiscoveredHost, list[DiscoveredAgent]]:
        """Augment the SSH-based base discovery with STOPPED instances it cannot reach.

        The base sweep reaches hosts over SSH, so a stopped instance (OS down) is
        invisible. Here we reconstruct those hosts and their agents from the
        instance listing so they still appear in ``mngr list`` and resolve for
        ``mngr start``.

        One bad instance never aborts the sweep: a malformed mngr host identity is
        logged and skipped. The offline check runs only for instances the live
        sweep did not already surface (and after the cheap not-a-mngr-host / dedup
        filters), so a healthy ``mngr list`` does no extra per-instance work and a
        running-but-transiently-unreachable instance is not misreported as STOPPED.
        """
        result = super().discover_hosts_and_agents(cg, include_destroyed=include_destroyed)
        online_host_ids = {ref.host_id for ref in result}
        for instance in self._list_instances_cached():
            try:
                host_ref = self._offline_discovered_host_from_instance(instance)
            except ValueError as e:
                logger.opt(exception=e).warning(
                    "Skipping instance {} in offline discovery: malformed mngr host identity",
                    instance.get("id"),
                )
                continue
            # Drop non-mngr instances and ones already surfaced live BEFORE the
            # offline check, since that check may cost a per-instance API call.
            if host_ref is None or host_ref.host_id in online_host_ids:
                continue
            if not self._is_instance_offline(instance):
                continue
            # An external-store provider reads agents from its store here (keyed by
            # host_id); an operational store failure propagates so the api/list
            # discovery wrapper attributes it to this provider and honors the
            # caller's --on-error (the malformed-identity data error above is the
            # only thing skipped per-instance).
            agent_refs: list[DiscoveredAgent] = []
            for agent_data in self._offline_agent_dicts_for(host_ref.host_id, instance):
                ref = validate_and_create_discovered_agent(agent_data, host_ref.host_id, self.name)
                if ref is not None:
                    agent_refs.append(ref)
            result[host_ref] = agent_refs
        return result

    def get_host(self, host: HostId | HostName) -> HostInterface:
        """Resolve a host, falling back to the instance-data offline host when stopped.

        The base reads the record over SSH, so a stopped instance raises
        ``HostNotFoundError``; ``mngr start`` calls this directly, so without the
        fallback a paused host could not be resumed by name. Only the ``HostId``
        form is recovered (the resume path passes a ``HostId``); a bare
        ``HostName`` for a stopped host still surfaces via discovery.
        """
        try:
            return super().get_host(host)
        except HostNotFoundError:
            if isinstance(host, HostId):
                return self.to_offline_host(host)
            raise

    def to_offline_host(self, host_id: HostId) -> OfflineHost:
        """Return an offline host, reconstructing a stopped host's full record from the external store.

        Falls back to the SSH/volume-backed base path first; if that can't find the
        host (stopped and unreachable), reconstruct the full ``VpsDockerHostRecord``
        from the external state store. Calls the SSH-only ``VpsDockerProvider`` path
        directly so this override does not recurse into itself.
        """
        try:
            return VpsDockerProvider.to_offline_host(self, host_id)
        except HostNotFoundError:
            record = self._state_store.read_host_record(host_id)
            if record is None:
                raise
            return self._create_offline_host(record)

    def list_snapshots(self, host: HostInterface | HostId) -> list[SnapshotInfo]:
        """Return ``[]`` for a stopped host instead of raising.

        ``OfflineHost.get_state`` derives state via ``list_snapshots``; the base
        reads the list from the on-volume record, which is unreadable while
        stopped. These providers have no host-snapshot lifecycle, so a stopped
        host simply has none.
        """
        try:
            return super().list_snapshots(host)
        except HostNotFoundError:
            return []

    def list_persisted_agent_data_for_host(self, host_id: HostId) -> list[dict]:
        """Return the host's persisted agent records, on-volume when reachable else offline.

        For a running host the SSH/volume-backed base reads the authoritative
        on-volume records; for a stopped host it raises ``HostNotFoundError`` and
        we fall back to the offline source (instance tags/metadata, or an external
        store for providers that have one).
        """
        try:
            return super().list_persisted_agent_data_for_host(host_id)
        except HostNotFoundError:
            return self._offline_agent_dicts_for(host_id)


class BucketHostDirBackend(HostDirBackend):
    """Shared offline ``host_dir`` backend for the object-storage providers (AWS S3, Azure Blob).

    Selected only when offline host_dir is on and the state bucket exists, so
    ``bucket`` is always present and no method re-tests it. Holds a back-reference
    to the provider for the SSH-to-outer / path plumbing the sync needs. The
    offline-read (``volume`` / ``volume_reference``) and final-sync-before-pause
    flow live here once; subclasses supply only the cloud-specific pieces
    (identity provisioning, the sync-daemon install, the missing-identity probe)
    and three small hooks (unit name, stop-action word, bucket error type).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    provider: OfflineCapableVpsDockerProvider
    bucket: StateBucket

    @abstractmethod
    def _sync_unit_name(self) -> str:
        """systemd unit base name for the host_dir sync daemon (e.g. ``mngr-aws-host-dir-sync``)."""
        ...

    @abstractmethod
    def _pause_action(self) -> str:
        """The pause verb for log context: ``stop`` (AWS/EC2) or ``deallocate`` (Azure)."""
        ...

    def volume_reference(self, host_id: HostId) -> HostVolume | None:
        return HostVolume(volume=self.bucket.volume_for_host(host_id))

    def volume(self, host_id: HostId) -> HostVolume | None:
        # A bucket probe error propagates (operational failure, surfaced per
        # --on-error); only a genuinely empty prefix yields None.
        if not self.bucket.host_dir_prefix_has_objects(host_id):
            self._warn_if_identity_missing(host_id)
            return None
        return self.volume_reference(host_id)

    def trigger_final_sync(self, host_id: HostId, vps_ip: str) -> None:
        try:
            with log_span(f"Triggering final host_dir sync before {self._pause_action()}"):
                with self.provider._make_outer_for_vps_ip(vps_ip) as outer:
                    outer.execute_idempotent_command(
                        f"systemctl start --wait {self._sync_unit_name()}.service", timeout_seconds=300.0
                    )
        except MngrError as e:
            logger.warning(
                "Final host_dir sync before stopping host {} failed; the offline copy will be as of "
                "the last periodic sync: {}",
                host_id,
                e,
            )

    @abstractmethod
    def _warn_if_identity_missing(self, host_id: HostId) -> None:
        """Warn (best-effort) when an empty host_dir prefix is explained by a missing cloud identity."""
        ...


# The host-name tag value is stored as ``mngr-<host_name>``; strip the prefix to
# recover the bare host name when reconstructing a stopped host's identity for
# discovery (the full record is read from the external state store on demand).
_HOST_NAME_TAG_PREFIX: Final[str] = "mngr-"


def normalized_tags_to_dict(instance: Mapping[str, Any]) -> dict[str, str]:
    """Turn an instance's normalized ``["key=value", ...]`` tag/label list into a dict (split on first ``=``).

    Shared by the tag/label-keyed providers (AWS EC2 tags, Azure VM tags, GCE
    labels) to read the ``mngr-*`` identity tags a stopped instance carries.
    """
    tags: dict[str, str] = {}
    for kv in instance.get("tags", ()):
        key, sep, value = kv.partition("=")
        if sep:
            tags[key] = value
    return tags


def host_name_from_tags(tags: Mapping[str, str], name_tag_key: str) -> HostName:
    """Recover the host name from the ``<name_tag_key>=mngr-<host_name>`` identity tag.

    Strips the ``mngr-`` prefix; falls back to the raw tag value, then to the
    ``mngr-host-id`` tag, when the name tag is missing/unprefixed. Used only to
    label a STOPPED host in discovery -- the authoritative name lives in the full
    record in the external state store.
    """
    name_tag = tags.get(name_tag_key, "")
    if name_tag.startswith(_HOST_NAME_TAG_PREFIX):
        return HostName(name_tag[len(_HOST_NAME_TAG_PREFIX) :])
    if name_tag:
        return HostName(name_tag)
    return HostName(tags.get("mngr-host-id", "unknown"))


class MinimalVpsDockerProvider(VpsDockerProvider):
    """``VpsDockerProvider`` for use cases where VPS provisioning is externally managed.

    Pairs with a ``vps_client`` whose provisioning calls raise (e.g.
    ``ExternallyManagedVpsClient``): some other system (an imbue_cloud pool
    lease, a hand-rolled provisioner, etc.) already created the VPS. This
    provider only ever runs the post-provisioning host-setup machinery --
    ``teardown_container_on_existing_vps`` and ``create_host_on_existing_vps``
    -- which take a caller-supplied ``outer`` and make no cloud-API calls.

    Build args here are just the docker-side knobs that flow through to
    ``docker build`` on the leased VPS: there is no cloud provider to
    select a region / plan / image for. The parser extracts ``--git-depth=N``
    (still relevant -- it controls the *local* mngr build context, not the
    VPS) and forwards everything else verbatim. The legacy shared
    ``--vps-*`` prefix is rejected with a migration error so a caller that
    still passes the old shape gets a clear pointer rather than having the
    arg silently land in docker.
    """

    def _parse_build_args(self, build_args: Sequence[str] | None) -> ParsedVpsBuildOptions:
        # Composed from the shared helpers rather than hand-rolling the same
        # git-depth / --vps-* migration handling: this is the same pattern
        # ``parse_vps_build_args`` and the AWS provider use, just without any
        # region / plan extraction (provisioning lives outside this provider).
        args = list(build_args or ())
        git_depth, args = extract_git_depth(args)
        docker_build_args: list[str] = []
        for arg in args:
            raise_if_vps_migration_arg(arg)
            docker_build_args.append(arg)
        # ``region`` / ``plan`` are unused on the externally-managed path
        # (callers pass them as explicit kwargs to ``create_host_on_existing_vps``),
        # so the sentinels are harmless.
        return ParsedVpsBuildOptions(
            region="",
            plan="",
            git_depth=git_depth,
            docker_build_args=tuple(docker_build_args),
        )
