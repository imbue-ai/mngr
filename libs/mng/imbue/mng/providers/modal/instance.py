import argparse
import json
import os
import re
import tempfile
import uuid
from collections.abc import Callable
from concurrent.futures import Future
from datetime import datetime
from datetime import timezone
from functools import wraps
from pathlib import Path
from typing import Any
from typing import Final
from typing import Mapping
from typing import ParamSpec
from typing import Sequence
from typing import TypeVar
from typing import cast

import modal
import modal.exception
from dockerfile_parse import DockerfileParser
from loguru import logger
from modal.exception import NotFoundError
from modal.stream_type import StreamType
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pyinfra.api import Host as PyinfraHost

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.logging import trace_span
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.mng.errors import HostConnectionError
from imbue.mng.errors import HostNameConflictError
from imbue.mng.errors import HostNotFoundError
from imbue.mng.errors import MngError
from imbue.mng.errors import ModalAuthError
from imbue.mng.errors import SnapshotNotFoundError
from imbue.mng.hosts.common import compute_idle_seconds
from imbue.mng.hosts.common import determine_lifecycle_state
from imbue.mng.hosts.common import resolve_expected_process_name
from imbue.mng.hosts.common import timestamp_to_datetime
from imbue.mng.hosts.host import Host
from imbue.mng.hosts.offline_host import OfflineHost
from imbue.mng.hosts.offline_host import validate_and_create_agent_reference
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.data_types import AgentInfo
from imbue.mng.interfaces.data_types import CertifiedHostData
from imbue.mng.interfaces.data_types import CpuResources
from imbue.mng.interfaces.data_types import HostConfig
from imbue.mng.interfaces.data_types import HostInfo
from imbue.mng.interfaces.data_types import HostLifecycleOptions
from imbue.mng.interfaces.data_types import HostResources
from imbue.mng.interfaces.data_types import PyinfraConnector
from imbue.mng.interfaces.data_types import SSHInfo
from imbue.mng.interfaces.data_types import SnapshotInfo
from imbue.mng.interfaces.data_types import SnapshotRecord
from imbue.mng.interfaces.data_types import VolumeInfo
from imbue.mng.interfaces.host import HostInterface
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.interfaces.volume import HostVolume
from imbue.mng.primitives import ActivitySource
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentReference
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import HostReference
from imbue.mng.primitives import HostState
from imbue.mng.primitives import IdleMode
from imbue.mng.primitives import ImageReference
from imbue.mng.primitives import SnapshotId
from imbue.mng.primitives import SnapshotName
from imbue.mng.primitives import VolumeId
from imbue.mng.providers.base_provider import BaseProviderInstance
from imbue.mng.providers.modal.config import ModalProviderConfig
from imbue.mng.providers.modal.errors import NoSnapshotsModalMngError
from imbue.mng.providers.modal.routes.deployment import deploy_function
from imbue.mng.providers.modal.ssh_utils import add_host_to_known_hosts
from imbue.mng.providers.modal.ssh_utils import create_pyinfra_host
from imbue.mng.providers.modal.ssh_utils import load_or_create_host_keypair
from imbue.mng.providers.modal.ssh_utils import load_or_create_ssh_keypair
from imbue.mng.providers.modal.ssh_utils import wait_for_sshd
from imbue.mng.providers.modal.volume import ModalVolume
from imbue.mng.providers.ssh_host_setup import REQUIRED_HOST_PACKAGES
from imbue.mng.providers.ssh_host_setup import build_add_authorized_keys_command
from imbue.mng.providers.ssh_host_setup import build_add_known_hosts_command
from imbue.mng.providers.ssh_host_setup import build_check_and_install_packages_command
from imbue.mng.providers.ssh_host_setup import build_configure_ssh_command
from imbue.mng.providers.ssh_host_setup import build_start_activity_watcher_command
from imbue.mng.providers.ssh_host_setup import build_start_volume_sync_command
from imbue.mng.providers.ssh_host_setup import parse_warnings_from_output

# Constants
CONTAINER_SSH_PORT: Final[int] = 22
# 2 minutes default sandbox lifetime (so that we don't just leave tons of them running--we're not doing a good job of cleaning them up yet)
DEFAULT_SANDBOX_TIMEOUT: Final[int] = 2 * 60
# Seconds to wait for sshd to be ready
SSH_CONNECT_TIMEOUT: Final[int] = 60

# Tag key constants for sandbox metadata stored in Modal tags.
# Only host_id and host_name are stored as tags (for discovery). All other
# metadata is stored on the Modal Volume for persistence and sharing.
TAG_HOST_ID: Final[str] = "mng_host_id"
TAG_HOST_NAME: Final[str] = "mng_host_name"
TAG_USER_PREFIX: Final[str] = "mng_user_"

# Mount path for the persistent host volume inside the sandbox.
# The host_dir (e.g., /mng) is symlinked to this path so all data
# written to host_dir persists on the volume.
HOST_VOLUME_MOUNT_PATH: Final[str] = "/host_volume"

# Infix between the mng config prefix and the host hex in volume names.
# The full volume name is {config.prefix}vol-{host_id_hex} (e.g., "mng-vol-abc123def...").
HOST_VOLUME_INFIX: Final[str] = "vol-"

# Maximum length for Modal volume names.
MODAL_VOLUME_NAME_MAX_LENGTH: Final[int] = 64

# Fixed namespace for deterministic VolumeId derivation from Modal volume names.
_MODAL_VOLUME_ID_NAMESPACE: Final[uuid.UUID] = uuid.UUID("c8f1a2b3-d4e5-6789-abcd-ef0123456789")

P = ParamSpec("P")
T = TypeVar("T")


def _parse_volume_spec(spec: str) -> tuple[str, str]:
    """Parse a volume mount spec of the form 'volume_name:mount_path'."""
    parts = spec.split(":", 1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise MngError(f"Invalid volume spec '{spec}': expected format 'volume_name:/mount/path'")
    return (parts[0].strip(), parts[1].strip())


def _build_modal_volumes(
    volume_specs: tuple[tuple[str, str], ...],
    environment_name: str,
) -> dict[str | os.PathLike[str], modal.Volume | modal.CloudBucketMount]:
    """Build a dict of mount_path -> modal.Volume for Sandbox.create()."""
    volumes: dict[str | os.PathLike[str], modal.Volume | modal.CloudBucketMount] = {}
    for volume_name, mount_path in volume_specs:
        with log_span("Ensuring volume: {} at {}", volume_name, mount_path):
            volumes[mount_path] = modal.Volume.from_name(
                volume_name,
                create_if_missing=True,
                environment_name=environment_name,
            )
    return volumes


def build_sandbox_tags(
    host_id: HostId,
    name: HostName,
    user_tags: Mapping[str, str] | None,
) -> dict[str, str]:
    """Build the tags dict to store on a Modal sandbox.

    Only stores host_id, host_name, and user tags. All other metadata
    (SSH info, config, snapshots) is stored on the Modal Volume.
    """
    tags: dict[str, str] = {
        TAG_HOST_ID: str(host_id),
        TAG_HOST_NAME: str(name),
    }

    # Store user tags with a prefix to separate them from mng tags
    if user_tags:
        for key, value in user_tags.items():
            tags[TAG_USER_PREFIX + key] = value

    return tags


def parse_sandbox_tags(
    tags: dict[str, str],
) -> tuple[HostId, HostName, dict[str, str]]:
    """Parse tags from a Modal sandbox into structured data.

    Returns (host_id, name, user_tags). All other metadata is read from the volume.
    """
    host_id = HostId(tags[TAG_HOST_ID])
    name = HostName(tags[TAG_HOST_NAME])

    # Extract user tags (those with the user prefix)
    user_tags: dict[str, str] = {}
    for key, value in tags.items():
        if key.startswith(TAG_USER_PREFIX):
            user_key = key[len(TAG_USER_PREFIX) :]
            user_tags[user_key] = value

    return host_id, name, user_tags


def handle_modal_auth_error(func: Callable[P, T]) -> Callable[P, T]:
    """Decorator to convert modal.exception.AuthError to ModalAuthError.

    Wraps provider methods to catch Modal authentication errors at the boundary
    and convert them to our ModalAuthError with a helpful message.
    """

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return func(*args, **kwargs)
        except modal.exception.AuthError as e:
            raise ModalAuthError() from e

    return wrapper


# =========================================================================
# Listing Data Collection Helpers
# =========================================================================

# Unique delimiters for parsing the single-command output
_SEP_DATA_JSON_START: Final[str] = "---MNG_DATA_JSON_START---"
_SEP_DATA_JSON_END: Final[str] = "---MNG_DATA_JSON_END---"
_SEP_AGENT_START: Final[str] = "---MNG_AGENT_START:"
_SEP_AGENT_END: Final[str] = "---MNG_AGENT_END---"
_SEP_AGENT_DATA_START: Final[str] = "---MNG_AGENT_DATA_START---"
_SEP_AGENT_DATA_END: Final[str] = "---MNG_AGENT_DATA_END---"
_SEP_PS_START: Final[str] = "---MNG_PS_START---"
_SEP_PS_END: Final[str] = "---MNG_PS_END---"


@pure
def _build_listing_collection_script(host_dir: str, prefix: str) -> str:
    """Build a shell script that collects all listing data in one command."""
    return f"""
# Uptime
echo "UPTIME=$(cat /proc/uptime 2>/dev/null | awk '{{print $1}}')"

# Boot time
echo "BTIME=$(grep '^btime ' /proc/stat 2>/dev/null | awk '{{print $2}}')"

# Lock file mtime
echo "LOCK_MTIME=$(stat -c %Y '{host_dir}/host_lock' 2>/dev/null)"

# SSH activity mtime
echo "SSH_ACTIVITY_MTIME=$(stat -c %Y '{host_dir}/activity/ssh' 2>/dev/null)"

# Host data.json
echo '{_SEP_DATA_JSON_START}'
cat '{host_dir}/data.json' 2>/dev/null || echo '{{}}'
echo ''
echo '{_SEP_DATA_JSON_END}'

# ps output (shared by all agents for lifecycle detection)
echo '{_SEP_PS_START}'
ps -e -o pid=,ppid=,comm= 2>/dev/null
echo '{_SEP_PS_END}'

# Agents
if [ -d '{host_dir}/agents' ]; then
    for agent_dir in '{host_dir}/agents'/*/; do
        [ -d "$agent_dir" ] || continue
        data_file="${{agent_dir}}data.json"
        [ -f "$data_file" ] || continue
        agent_id=$(basename "$agent_dir")
        echo '{_SEP_AGENT_START}'"$agent_id"'---'
        echo '{_SEP_AGENT_DATA_START}'
        cat "$data_file"
        echo ''
        echo '{_SEP_AGENT_DATA_END}'
        echo "USER_MTIME=$(stat -c %Y "${{agent_dir}}activity/user" 2>/dev/null)"
        echo "AGENT_MTIME=$(stat -c %Y "${{agent_dir}}activity/agent" 2>/dev/null)"
        echo "START_MTIME=$(stat -c %Y "${{agent_dir}}activity/start" 2>/dev/null)"
        agent_name=$(jq -r '.name // empty' "$data_file" 2>/dev/null)
        session_name='{prefix}'"$agent_name"
        tmux_info=$(tmux list-panes -t "${{session_name}}:0" -F '#{{pane_dead}}|#{{pane_current_command}}|#{{pane_pid}}' 2>/dev/null | head -n 1)
        echo "TMUX_INFO=$tmux_info"
        if [ -f "${{agent_dir}}active" ]; then
            echo "ACTIVE=true"
        else
            echo "ACTIVE=false"
        fi
        url=$(cat "${{agent_dir}}status/url" 2>/dev/null | tr -d '\\n')
        echo "URL=$url"
        echo '{_SEP_AGENT_END}'
    done
fi
"""


@pure
def _parse_optional_int(value: str) -> int | None:
    """Parse an optional integer from a key=value line's value portion."""
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return int(stripped)
    except ValueError:
        return None


@pure
def _parse_optional_float(value: str) -> float | None:
    """Parse an optional float from a key=value line's value portion."""
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return float(stripped)
    except ValueError:
        return None


def _extract_delimited_block(lines: list[str], idx: int, end_marker: str) -> tuple[str, int]:
    """Extract lines between the current position and end_marker, returning the content and new index."""
    collected: list[str] = []
    while idx < len(lines) and lines[idx].strip() != end_marker:
        collected.append(lines[idx])
        idx += 1
    return "\n".join(collected).strip(), idx


def _parse_agent_section(lines: list[str], idx: int) -> tuple[dict[str, Any], int]:
    """Parse a single agent section, returning the agent dict and new index."""
    agent_raw: dict[str, Any] = {}

    while idx < len(lines) and lines[idx].strip() != _SEP_AGENT_END:
        aline = lines[idx]
        if aline.strip() == _SEP_AGENT_DATA_START:
            idx += 1
            agent_json_str, idx = _extract_delimited_block(lines, idx, _SEP_AGENT_DATA_END)
            if agent_json_str:
                try:
                    agent_raw["data"] = json.loads(agent_json_str)
                except json.JSONDecodeError as e:
                    logger.warning("Failed to parse agent data.json in listing output: {}", e)
        elif aline.startswith("USER_MTIME="):
            agent_raw["user_activity_mtime"] = _parse_optional_int(aline[len("USER_MTIME=") :])
        elif aline.startswith("AGENT_MTIME="):
            agent_raw["agent_activity_mtime"] = _parse_optional_int(aline[len("AGENT_MTIME=") :])
        elif aline.startswith("START_MTIME="):
            agent_raw["start_activity_mtime"] = _parse_optional_int(aline[len("START_MTIME=") :])
        elif aline.startswith("TMUX_INFO="):
            val = aline[len("TMUX_INFO=") :].strip()
            agent_raw["tmux_info"] = val if val else None
        elif aline.startswith("ACTIVE="):
            agent_raw["is_active"] = aline[len("ACTIVE=") :].strip() == "true"
        elif aline.startswith("URL="):
            val = aline[len("URL=") :].strip()
            agent_raw["url"] = val if val else None
        else:
            pass
        idx += 1

    return agent_raw, idx


def _parse_listing_collection_output(stdout: str) -> dict[str, Any]:
    """Parse the structured output of the listing collection script."""
    result: dict[str, Any] = {}
    agents: list[dict[str, Any]] = []
    lines = stdout.split("\n")
    idx = 0

    while idx < len(lines):
        line = lines[idx]

        if line.startswith("UPTIME=") and "uptime_seconds" not in result:
            result["uptime_seconds"] = _parse_optional_float(line[len("UPTIME=") :])
        elif line.startswith("BTIME=") and "btime" not in result:
            result["btime"] = _parse_optional_int(line[len("BTIME=") :])
        elif line.startswith("LOCK_MTIME=") and "lock_mtime" not in result:
            result["lock_mtime"] = _parse_optional_int(line[len("LOCK_MTIME=") :])
        elif line.startswith("SSH_ACTIVITY_MTIME=") and "ssh_activity_mtime" not in result:
            result["ssh_activity_mtime"] = _parse_optional_int(line[len("SSH_ACTIVITY_MTIME=") :])
        elif line.strip() == _SEP_DATA_JSON_START:
            idx += 1
            json_str, idx = _extract_delimited_block(lines, idx, _SEP_DATA_JSON_END)
            if json_str:
                try:
                    result["certified_data"] = json.loads(json_str)
                except json.JSONDecodeError as e:
                    logger.warning("Failed to parse host data.json in listing output: {}", e)
        elif line.strip() == _SEP_PS_START:
            idx += 1
            ps_content, idx = _extract_delimited_block(lines, idx, _SEP_PS_END)
            result["ps_output"] = ps_content
        elif line.strip().startswith(_SEP_AGENT_START):
            idx += 1
            agent_raw, idx = _parse_agent_section(lines, idx)
            if "data" in agent_raw:
                agents.append(agent_raw)
        else:
            pass
        idx += 1

    result["agents"] = agents
    return result


class SandboxConfig(HostConfig):
    """Configuration parsed from build arguments."""

    gpu: str | None = None
    cpu: float = 1.0
    memory: float = 1.0
    image: str | None = None
    dockerfile: str | None = None
    timeout: int = DEFAULT_SANDBOX_TIMEOUT
    region: str | None = None
    context_dir: str | None = None
    secrets: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Environment variable names to pass as secrets during image build",
    )
    cidr_allowlist: tuple[str, ...] = Field(
        default_factory=tuple,
        description="CIDR ranges to restrict network access to",
    )
    offline: bool = False
    volumes: tuple[tuple[str, str], ...] = Field(
        default_factory=tuple,
        description="Volume mounts as (volume_name, mount_path) pairs",
    )
    docker_build_args: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Docker build args as KEY=VALUE pairs to substitute into Dockerfile ARG defaults",
    )

    @property
    def effective_cidr_allowlist(self) -> list[str] | None:
        """Compute the cidr_allowlist to pass to Modal.

        Returns None (allow all) when neither --offline nor --cidr-allowlist is set.
        Returns [] (block all) when --offline is set without explicit CIDRs.
        Returns the explicit list when --cidr-allowlist is provided.
        """
        if self.cidr_allowlist:
            return list(self.cidr_allowlist)
        if self.offline:
            return []
        return None


class HostRecord(FrozenModel):
    """Host metadata stored on the Modal Volume.

    This record contains all information needed to connect to and restore a host.
    It is stored at /hosts/<host_id>.json on the volume.

    For failed hosts (those that failed during creation), only certified_host_data
    is required. The SSH fields and config will be None since the host never started.
    """

    certified_host_data: CertifiedHostData = Field(
        frozen=True,
        description="The certified host data loaded from data.json",
    )
    ssh_host: str | None = Field(default=None, description="SSH hostname for connecting to the sandbox")
    ssh_port: int | None = Field(default=None, description="SSH port number")
    ssh_host_public_key: str | None = Field(default=None, description="SSH host public key for verification")
    config: SandboxConfig | None = Field(default=None, description="Sandbox configuration")


class ModalProviderApp(FrozenModel):
    """Encapsulates a Modal app and its associated resources.

    This class manages the lifecycle of a Modal app, including:
    - The Modal app itself and its run context
    - Output capture for detecting build failures
    - The state volume for persisting host records

    Instances are created by ModalProviderBackend and passed to ModalProviderInstance.
    Multiple ModalProviderInstance objects can share the same ModalProviderApp if they
    use the same app_name.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    app_name: str = Field(frozen=True, description="The name of the Modal app")
    environment_name: str = Field(frozen=True, description="The Modal environment name for user isolation")
    app: modal.App = Field(frozen=True, description="The Modal app instance")
    volume: modal.Volume = Field(frozen=True, description="The Modal volume for state storage")
    close_callback: Callable[[], None] = Field(frozen=True, description="Callback to clean up the app context")
    get_output_callback: Callable[[], str] = Field(frozen=True, description="Callback to get the log output buffer")

    def get_captured_output(self) -> str:
        """Get all captured Modal output.

        Returns the contents of the output buffer that has been capturing Modal
        logs since the app was created. This can be used to detect build failures
        or other issues by inspecting the captured output.
        """
        return self.get_output_callback()

    def close(self) -> None:
        self.close_callback()


@pure
def check_host_name_is_unique(
    name: HostName,
    host_records: Sequence[HostRecord],
    running_host_ids: set[HostId],
) -> None:
    """Check that no non-destroyed host already uses the given name.

    Skips destroyed hosts (no snapshots, no failure_reason, not running) since
    their names should be reusable.

    Raises HostNameConflictError if a non-destroyed host with the same name exists.
    """
    for host_record in host_records:
        if HostName(host_record.certified_host_data.host_name) != name:
            continue

        # Skip destroyed hosts (not running, no snapshots, not failed)
        host_id = HostId(host_record.certified_host_data.host_id)
        is_running = host_id in running_host_ids
        has_snapshots = len(host_record.certified_host_data.snapshots) > 0
        is_failed = host_record.certified_host_data.failure_reason is not None
        if not is_running and not has_snapshots and not is_failed:
            continue

        raise HostNameConflictError(name)


class ModalProviderInstance(BaseProviderInstance):
    """Provider instance for managing Modal sandboxes as hosts.

    Each sandbox runs sshd and is accessed via pyinfra's SSH connector.
    Sandboxes have a maximum lifetime (timeout) after which they are automatically
    terminated by Modal.

    Host metadata (SSH info, config, snapshots) is stored on a Modal Volume
    for persistence and sharing between mng instances. Only host_id, host_name,
    and user tags are stored as sandbox tags for discovery via Sandbox.list().

    An instance-level cache maps host_id to sandbox objects to avoid relying on Modal's
    eventually consistent tag queries for recently created sandboxes.
    """

    # Instance-level caches of sandboxes. These avoid the need to query
    # Modal's eventually consistent tag API for recently created sandboxes.
    _sandbox_cache_by_id: dict[HostId, modal.Sandbox] = PrivateAttr(default_factory=dict)
    _sandbox_cache_by_name: dict[HostName, modal.Sandbox] = PrivateAttr(default_factory=dict)
    _host_by_id_cache: dict[HostId, HostInterface] = PrivateAttr(default_factory=dict)
    # Cache for host records read from the volume to avoid repeated reads
    _host_record_cache_by_id: dict[HostId, HostRecord] = PrivateAttr(default_factory=dict)

    config: ModalProviderConfig = Field(frozen=True, description="Modal provider configuration")
    modal_app: ModalProviderApp = Field(frozen=True, description="Modal app manager")

    @property
    def supports_snapshots(self) -> bool:
        return True

    @property
    def supports_shutdown_hosts(self) -> bool:
        return False

    @property
    def supports_volumes(self) -> bool:
        return True

    @property
    def supports_mutable_tags(self) -> bool:
        return True

    @property
    def app_name(self) -> str:
        """Get the Modal app name from the modal_app manager."""
        return self.modal_app.app_name

    @property
    def environment_name(self) -> str:
        """Get the Modal environment name from the modal_app manager."""
        return self.modal_app.environment_name

    @property
    def _keys_dir(self) -> Path:
        """Get the directory for SSH keys (profile-specific)."""
        return self.mng_ctx.profile_dir / "providers" / "modal"

    def _get_ssh_keypair(self) -> tuple[Path, str]:
        """Get or create the SSH keypair for this provider instance."""
        return load_or_create_ssh_keypair(self._keys_dir, key_name="modal_ssh_key")

    def get_ssh_public_key(self) -> str:
        """Get the SSH public key content for this provider instance.

        Loads or creates the keypair if it doesn't exist yet.
        """
        _private_key_path, public_key_content = self._get_ssh_keypair()
        return public_key_content

    def _get_host_keypair(self) -> tuple[Path, str]:
        """Get or create the SSH host keypair for Modal sandboxes.

        This key is used as the SSH host key for all sandboxes, allowing us to
        pre-trust the key and avoid host key verification prompts.
        """
        return load_or_create_host_keypair(self._keys_dir)

    @property
    def _known_hosts_path(self) -> Path:
        """Get the path to the known_hosts file for this provider instance."""
        return self._keys_dir / "known_hosts"

    # =========================================================================
    # Host Volume Methods
    # =========================================================================

    @property
    def _host_volume_prefix(self) -> str:
        """The prefix used for host volume names on Modal."""
        return f"{self.mng_ctx.config.prefix}{HOST_VOLUME_INFIX}"

    def _get_host_volume_name(self, host_id: HostId) -> str:
        """Derive the Modal volume name for a host's persistent volume.

        Uses the mng config prefix and the HostId hex part to produce a name
        like "mng-vol-abc123def...". Truncates to fit Modal's 64-char limit.
        """
        host_hex = str(host_id)[len("host-") :]
        name = f"{self._host_volume_prefix}{host_hex}"
        return name[:MODAL_VOLUME_NAME_MAX_LENGTH]

    def _build_host_volume(self, host_id: HostId) -> modal.Volume:
        """Get or create the persistent host volume for a sandbox."""
        volume_name = self._get_host_volume_name(host_id)
        return modal.Volume.from_name(
            volume_name,
            create_if_missing=True,
            environment_name=self.environment_name,
        )

    @handle_modal_auth_error
    def get_volume_for_host(self, host: HostInterface | HostId) -> HostVolume | None:
        """Get the host volume for reading data written by the sandbox.

        Returns a HostVolume wrapping the persistent volume mounted inside
        the sandbox. Returns None if the volume does not exist or if
        host volume creation is disabled.

        Probes the volume with a listdir to verify it actually exists, since
        modal.Volume.from_name returns a lazy reference that doesn't fail
        for deleted volumes.
        """
        if not self.config.is_host_volume_created:
            return None
        host_id = host.id if isinstance(host, HostInterface) else host
        volume_name = self._get_host_volume_name(host_id)
        try:
            modal_vol = modal.Volume.from_name(volume_name, environment_name=self.environment_name)
            # Probe the volume to verify it exists (from_name returns lazy references)
            modal_vol.listdir("/")
            modal_volume = ModalVolume.model_construct(modal_volume=modal_vol)
            return HostVolume.model_construct(volume=modal_volume)
        except (NotFoundError, modal.exception.InvalidError):
            return None

    # =========================================================================
    # Volume-based Host Record Methods
    # =========================================================================

    def get_state_volume(self) -> ModalVolume:
        """Get the state volume for persisting host records and agent data.

        This volume is used to persist host records (including snapshots) across
        sandbox termination. It is NOT the same as the host volume (which is
        mounted inside sandboxes and writable by untrusted code). The state
        volume is only accessed by mng itself and contains trusted data.
        """
        return ModalVolume.model_construct(modal_volume=self.modal_app.volume)

    def _get_host_record_path(self, host_id: HostId) -> str:
        """Get the path for a host record on the volume."""
        return f"/hosts/{host_id}.json"

    def _write_host_record(self, host_record: HostRecord) -> None:
        """Write a host record to the state volume."""
        volume = self.get_state_volume()
        host_id = HostId(host_record.certified_host_data.host_id)
        path = self._get_host_record_path(host_id)
        data = host_record.model_dump_json(indent=2)

        volume.write_files({path: data.encode("utf-8")})
        logger.trace("Wrote host record to volume: {}", path, host_data=data)

        # Update the cache with the new host record
        self._host_record_cache_by_id[host_id] = host_record

    def _save_failed_host_record(
        self,
        host_id: HostId,
        host_name: HostName,
        tags: Mapping[str, str] | None,
        failure_reason: str,
        build_log: str,
    ) -> None:
        """Save a host record for a host that failed during creation.

        This allows the failed host to be visible in 'mng list' so users can see
        what went wrong and debug build failures.
        """
        now = datetime.now(timezone.utc)
        host_data = CertifiedHostData(
            host_id=str(host_id),
            host_name=str(host_name),
            user_tags=dict(tags) if tags else {},
            snapshots=[],
            failure_reason=failure_reason,
            build_log=build_log,
            created_at=now,
            updated_at=now,
        )

        host_record = HostRecord(
            certified_host_data=host_data,
        )

        with log_span("Saving failed host record for host_id={}", host_id):
            self._write_host_record(host_record)

    def _read_host_record(self, host_id: HostId, use_cache: bool = True) -> HostRecord | None:
        """Read a host record from the volume.

        Returns None if the host record doesn't exist.
        Uses a cache to avoid repeated reads of the same host record.
        """

        # Check cache first
        if use_cache and host_id in self._host_record_cache_by_id:
            logger.trace("Used cached host record for host_id={}", host_id)
            return self._host_record_cache_by_id[host_id]

        volume = self.get_state_volume()
        path = self._get_host_record_path(host_id)

        try:
            data = volume.read_file(path)
            host_record = HostRecord.model_validate_json(data)
            logger.trace("Read host record from volume: {}", path, host_data=data.decode("utf-8"))
            # Cache the result
            self._host_record_cache_by_id[host_id] = host_record
            return host_record
        except (NotFoundError, FileNotFoundError):
            return None

    def _destroy_agents_on_host(self, host_id: HostId) -> None:
        """Remove the agents for this host from the state volume."""
        volume = self.get_state_volume()

        # delete all agent records for this host
        host_dir = f"/hosts/{host_id}"
        try:
            volume.remove_file(host_dir, recursive=True)
        except (NotFoundError, FileNotFoundError):
            pass
        logger.trace("Deleted agent records from state volume dir: {}", host_dir)

        # Clear cache entries for this host
        self._host_by_id_cache.pop(host_id, None)
        self._host_record_cache_by_id.pop(host_id, None)

    def _delete_host_record(self, host_id: HostId) -> None:
        """Delete a host record from the state volume and clear caches."""
        volume = self.get_state_volume()

        # finally, delete the actual host record itself
        path = self._get_host_record_path(host_id)
        try:
            volume.remove_file(path)
        except (NotFoundError, FileNotFoundError):
            pass
        logger.trace("Deleted host record from volume: {}", path)

        # Clear cache entries for this host
        self._host_by_id_cache.pop(host_id, None)
        self._host_record_cache_by_id.pop(host_id, None)

    def _clear_snapshots_from_host_record(self, host_id: HostId) -> None:
        """Clear all snapshot records from a host record on the state volume.

        This is called during destroy_host to mark the host as DESTROYED
        (no snapshots, cannot be restarted) while keeping the host record
        for visibility.
        """
        host_record = self._read_host_record(host_id, use_cache=False)
        if host_record is None:
            return

        if not host_record.certified_host_data.snapshots:
            return

        updated_certified_data = host_record.certified_host_data.model_copy_update(
            to_update(host_record.certified_host_data.field_ref().snapshots, []),
            to_update(host_record.certified_host_data.field_ref().updated_at, datetime.now(timezone.utc)),
        )
        self._write_host_record(
            host_record.model_copy_update(
                to_update(host_record.field_ref().certified_host_data, updated_certified_data),
            )
        )
        logger.debug("Cleared snapshots from host record: {}", host_id)

    def _list_all_host_records(self, cg: ConcurrencyGroup) -> list[HostRecord]:
        """List all host records stored on the state volume.

        Returns a list of all HostRecord objects found on the volume.
        Host records are stored at /hosts/<host_id>.json.
        """
        host_records, _agent_record_by_host_id = self._list_all_host_and_agent_records(cg, is_including_agents=False)
        return host_records

    # FOLLOWUP: this takes the vast majority of the time for most commands, eg, is a significant performance bottleneck
    #  In order to work around that, we should cache this data locally. We'll need to invalidate that cache any time we mutate a host record, but otherwise it will really help us speed up listing (and all operations that use listing, which is basically everything, because we often need to find a host/agent by name or id)
    #  If we use the cache *only for mng list*, we should be relatively safe--we can regenerate the cache after basically any command that changes it (and time it out), and then list will show you what you expect
    def _list_all_host_and_agent_records(
        self, cg: ConcurrencyGroup, is_including_agents: bool = True
    ) -> tuple[list[HostRecord], dict[str, Any]]:
        with trace_span("  _list_all_host_and_agent_records", _is_trace_span_enabled=False):
            volume = self.get_state_volume()

            futures: list[Future[HostRecord | None]] = []
            future_by_host_id: dict[HostId, Future[list[dict[str, Any]]]] = {}
            with ConcurrencyGroupExecutor(
                parent_cg=cg, name="modal_list_all_host_records", max_workers=32
            ) as executor:
                # List files in the /hosts/ directory on the volume
                try:
                    entries = volume.listdir("/hosts/")
                except (NotFoundError, FileNotFoundError):
                    entries = []

                for entry in entries:
                    filename = entry.path
                    # Host records are stored as hosts/<host_id>.json
                    if filename.endswith(".json"):
                        # Extract host_id from path like "hosts/host-abc.json"
                        basename = filename.rsplit("/", 1)[-1]
                        host_id_str = basename.removesuffix(".json")
                        host_id = HostId(host_id_str)
                        futures.append(executor.submit(self._read_host_record, host_id))
                        if is_including_agents:
                            future_by_host_id[host_id] = executor.submit(
                                self.list_persisted_agent_data_for_host, host_id
                            )

            result = [record for future in futures if (record := future.result()) is not None]
            logger.trace("Listed all host records from volume")
            other_result = {host_id: future.result() for host_id, future in future_by_host_id.items()}
            return result, other_result

    # FIXME: needs to be parallelized if there are many agents on a single host, pass in the concurrency group and use that if there are many entries
    def list_persisted_agent_data_for_host(self, host_id: HostId) -> list[dict[str, Any]]:
        """List persisted agent data for a stopped host.

        Agent records are stored at /hosts/{host_id}/{agent_id}.json on the state volume.
        These are persisted when a host shuts down so that mng list can
        show agents on stopped hosts.
        """
        volume = self.get_state_volume()

        agent_records: list[dict[str, Any]] = []
        host_dir = f"/hosts/{host_id}"
        try:
            entries = volume.listdir(host_dir)
        except (NotFoundError, FileNotFoundError):
            # Host directory doesn't exist yet (no agents persisted for this host)
            return agent_records

        for entry in entries:
            filename = entry.path
            if filename.endswith(".json"):
                # Read the agent record
                agent_path = filename.lstrip("/")
                try:
                    content = volume.read_file(agent_path)
                except (NotFoundError, FileNotFoundError):
                    # File was deleted between listdir and read (TOCTOU race on distributed volume)
                    continue
                try:
                    agent_data = json.loads(content.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    # Corrupted or partially written file. Log and skip it.
                    logger.warning("Skipped invalid agent record file {}: {}", agent_path, e)
                    continue
                else:
                    agent_records.append(agent_data)

        logger.trace("Listed agent records for host {} from volume", host_id)
        return agent_records

    def persist_agent_data(self, host_id: HostId, agent_data: Mapping[str, object]) -> None:
        """Persist agent data to the state volume.

        Called when an agent is created or its data.json is updated. Writes
        the agent data to /hosts/{host_id}/{agent_id}.json on the state volume.
        """
        agent_id = agent_data.get("id")
        if not agent_id:
            logger.warning("Cannot persist agent data without id field")
            return

        volume = self.get_state_volume()
        host_dir = f"/hosts/{host_id}"
        agent_path = f"{host_dir}/{agent_id}.json"

        # Serialize the agent data to JSON
        data = json.dumps(dict(agent_data), indent=2)

        volume.write_files({agent_path: data.encode("utf-8")})
        logger.trace("Persisted agent data to volume: {}", agent_path)

    def remove_persisted_agent_data(self, host_id: HostId, agent_id: AgentId) -> None:
        """Remove persisted agent data from the state volume.

        Called when an agent is destroyed. Removes the agent data file from
        /hosts/{host_id}/{agent_id}.json on the state volume.
        """
        volume = self.get_state_volume()
        agent_path = f"/hosts/{host_id}/{agent_id}.json"

        try:
            volume.remove_file(agent_path)
        except FileNotFoundError:
            # File doesn't exist, nothing to remove
            pass
        logger.trace("Removed agent data from volume: {}", agent_path)

    def _on_certified_host_data_updated(self, host_id: HostId, certified_data: CertifiedHostData) -> None:
        """Update the certified host data in the volume's host record.

        Called when the host's data.json is modified. Updates the
        certified_host_data field in the volume's host record to keep
        the volume in sync with the host.

        Reads the current host record from the volume to avoid overwriting
        any changes made by other operations (snapshots, tags, etc.).
        """
        with log_span("Updating certified host data on volume", host_id=str(host_id)):
            host_record = self._read_host_record(host_id, use_cache=False)
            if host_record is None:
                raise MngError(f"Host record not found on volume for {host_id}")
            updated_host_record = host_record.model_copy_update(
                to_update(host_record.field_ref().certified_host_data, certified_data),
            )
            self._write_host_record(updated_host_record)

    def _build_modal_image(
        self,
        base_image: str | None = None,
        dockerfile: Path | None = None,
        context_dir: Path | None = None,
        secrets: Sequence[str] = (),
        docker_build_args: Sequence[str] = (),
    ) -> modal.Image:
        """Build a Modal image.

        If dockerfile is provided, builds from that Dockerfile with per-layer caching.
        Each instruction is applied separately, so if a build fails at step N,
        steps 1 through N-1 are cached and don't need to be re-run.

        Elif base_image is provided (e.g., "python:3.11-slim"), uses that as the
        base. Otherwise uses debian:bookworm-slim.

        The context_dir specifies the directory for Dockerfile COPY/ADD instructions.
        If not provided, defaults to the Dockerfile's parent directory.

        The secrets parameter is a sequence of environment variable names whose values
        will be read from the current environment and passed to the Modal image build
        process. These are available during Dockerfile RUN commands via --mount=type=secret.

        The docker_build_args parameter is a sequence of KEY=VALUE strings that override
        ARG defaults in the Dockerfile. For example, passing 'CLAUDE_CODE_VERSION=2.1.50'
        substitutes the default value of ARG CLAUDE_CODE_VERSION in the Dockerfile.

        SSH and tmux setup is handled at runtime in _start_sshd_in_sandbox to
        allow warning if these tools are not pre-installed in the base image.
        """
        # Build modal secrets from environment variables
        modal_secrets = _build_modal_secrets_from_env(secrets)

        if dockerfile is not None:
            dockerfile_contents = dockerfile.read_text()
            # Substitute docker build args into ARG defaults
            if docker_build_args:
                dockerfile_contents = _substitute_dockerfile_build_args(dockerfile_contents, docker_build_args)
            effective_context_dir = context_dir if context_dir is not None else dockerfile.parent
            image = _build_image_from_dockerfile_contents(
                dockerfile_contents,
                context_dir=effective_context_dir,
                is_each_layer_cached=True,
                secrets=modal_secrets,
            )
        elif base_image:
            image = modal.Image.from_registry(base_image)
        else:
            image = modal.Image.debian_slim().apt_install(*(pkg.package for pkg in REQUIRED_HOST_PACKAGES))

        return image

    def _check_and_install_packages(
        self,
        sandbox: modal.Sandbox,
    ) -> None:
        """Check for required packages and install if missing, with warnings.

        Uses a single shell command to check for all packages and install missing ones,
        which is faster than multiple exec calls and allows the logic to be reused
        by other providers.

        Checks for sshd, tmux, curl, rsync, and git. If any is missing, logs a warning
        and installs via apt. This allows users to pre-configure their base images
        for faster startup while supporting images without these tools.
        """
        # Build and execute the combined check-and-install command.
        # Pass the host volume mount path so host_dir is symlinked to the volume,
        # or None to create host_dir as a regular directory.
        effective_volume_mount_path = HOST_VOLUME_MOUNT_PATH if self.config.is_host_volume_created else None
        check_install_cmd = build_check_and_install_packages_command(
            str(self.host_dir),
            host_volume_mount_path=effective_volume_mount_path,
        )
        process = sandbox.exec("sh", "-c", check_install_cmd)

        # Read output (implicitly waits for completion)
        stdout = process.stdout.read()

        # Parse warnings from output and log them
        warnings = parse_warnings_from_output(stdout)
        for warning in warnings:
            logger.warning(warning)

    def _start_sshd_in_sandbox(
        self,
        sandbox: modal.Sandbox,
        client_public_key: str,
        host_private_key: str,
        host_public_key: str,
        ssh_user: str = "root",
        known_hosts: Sequence[str] | None = None,
        authorized_keys: Sequence[str] | None = None,
    ) -> None:
        """Set up SSH access and start sshd in the sandbox.

        This method handles the complete SSH setup including package installation
        (if needed), key configuration, and starting the sshd daemon.

        All setup (except starting sshd) is done via a single shell command for
        speed and to allow reuse by other providers.
        """
        # Check for required packages and install if missing
        self._check_and_install_packages(sandbox)

        with log_span("Configuring SSH keys in sandbox", ssh_user=ssh_user):
            # Build and execute the SSH configuration command
            configure_ssh_cmd = build_configure_ssh_command(
                user=ssh_user,
                client_public_key=client_public_key,
                host_private_key=host_private_key,
                host_public_key=host_public_key,
            )
            sandbox.exec("sh", "-c", configure_ssh_cmd).wait()

        # Add known_hosts entries for outbound SSH if specified
        if known_hosts:
            add_known_hosts_cmd = build_add_known_hosts_command(ssh_user, tuple(known_hosts))
            if add_known_hosts_cmd is not None:
                with log_span("Adding {} known_hosts entries to sandbox", len(known_hosts)):
                    sandbox.exec("sh", "-c", add_known_hosts_cmd).wait()

        if authorized_keys:
            add_authorized_keys_cmd = build_add_authorized_keys_command(ssh_user, tuple(authorized_keys))
            if add_authorized_keys_cmd is not None:
                with log_span("Adding {} authorized_keys entries to sandbox", len(authorized_keys)):
                    sandbox.exec("sh", "-c", add_authorized_keys_cmd).wait()

        with log_span("Starting sshd in sandbox"):
            sshd_log_path = f"{self.host_dir}/logs/sshd.log"
            # Ensure the logs directory exists before sshd starts writing to it
            sandbox.exec("mkdir", "-p", f"{self.host_dir}/logs").wait()
            # Start sshd (-D: don't detach, -E: log to file instead of syslog)
            # stdout/stderr are suppressed so Modal doesn't track them for performance/stability reasons.
            sandbox.exec(
                "/usr/sbin/sshd", "-D", "-E", sshd_log_path, stdout=StreamType.DEVNULL, stderr=StreamType.DEVNULL
            )

    def _get_ssh_info_from_sandbox(self, sandbox: modal.Sandbox) -> tuple[str, int]:
        """Extract SSH connection info from a running sandbox."""
        tunnels = sandbox.tunnels()
        ssh_tunnel = tunnels[CONTAINER_SSH_PORT]
        return ssh_tunnel.tcp_socket

    def _wait_for_sshd(self, hostname: str, port: int, timeout_seconds: float = SSH_CONNECT_TIMEOUT) -> None:
        """Wait for sshd to be ready to accept connections."""
        wait_for_sshd(hostname, port, timeout_seconds)

    def _create_pyinfra_host(self, hostname: str, port: int, private_key_path: Path) -> PyinfraHost:
        """Create a pyinfra host with SSH connector."""
        return create_pyinfra_host(hostname, port, private_key_path, self._known_hosts_path)

    def _setup_sandbox_ssh_and_create_host(
        self,
        sandbox: modal.Sandbox,
        host_id: HostId,
        host_name: HostName,
        user_tags: Mapping[str, str] | None,
        config: SandboxConfig,
        host_data: CertifiedHostData,
        known_hosts: Sequence[str] | None = None,
        authorized_keys: Sequence[str] | None = None,
    ) -> tuple[Host, str, int, str]:
        """Set up SSH in a sandbox and create a Host object.

        This helper consolidates the common logic for setting up SSH access
        after a sandbox is created, used by both create_host and start_host.

        Returns a tuple of (Host, ssh_host, ssh_port, host_public_key) so callers
        can use the SSH info for creating/updating host records.
        """
        # Get SSH keypairs
        private_key_path, client_public_key = self._get_ssh_keypair()
        host_key_path, host_public_key = self._get_host_keypair()
        host_private_key = host_key_path.read_text()

        # Start sshd with our host key
        self._start_sshd_in_sandbox(
            sandbox,
            client_public_key,
            host_private_key,
            host_public_key,
            known_hosts=known_hosts,
            authorized_keys=authorized_keys,
        )

        # Get SSH connection info
        ssh_host, ssh_port = self._get_ssh_info_from_sandbox(sandbox)
        logger.trace("Found SSH endpoint available", ssh_host=ssh_host, ssh_port=ssh_port)

        # Add the host to our known_hosts file before waiting for sshd
        with log_span("Adding host to known_hosts", ssh_host=ssh_host, ssh_port=ssh_port):
            add_host_to_known_hosts(self._known_hosts_path, ssh_host, ssh_port, host_public_key)

        # Wait for sshd to be ready
        with log_span("Waiting for sshd to be ready..."):
            self._wait_for_sshd(ssh_host, ssh_port)

        # Set sandbox tags
        sandbox_tags = self._build_sandbox_tags(
            host_id=host_id,
            name=host_name,
            user_tags=user_tags,
        )
        with log_span("Setting sandbox tags: {}", list(sandbox_tags.keys())):
            sandbox.set_tags(sandbox_tags)

        # Create pyinfra host and connector
        pyinfra_host = self._create_pyinfra_host(ssh_host, ssh_port, private_key_path)
        connector = PyinfraConnector(pyinfra_host)

        # Create and write the initial host record to the volume
        # This must happen BEFORE host.set_certified_data() because the callback
        # _on_certified_host_data_updated expects the host record to already exist
        host_record = HostRecord(
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            ssh_host_public_key=host_public_key,
            config=config,
            certified_host_data=host_data,
        )
        self._write_host_record(host_record)

        # Create the Host object with callback for future certified data updates
        host = Host(
            id=host_id,
            connector=connector,
            provider_instance=self,
            mng_ctx=self.mng_ctx,
            on_updated_host_data=lambda callback_host_id, certified_data: self._on_certified_host_data_updated(
                callback_host_id, certified_data
            ),
        )

        # Record BOOT activity for idle detection
        host.record_activity(ActivitySource.BOOT)

        # Write the host data.json (will also update volume via callback since host record already exists)
        host.set_certified_data(host_data)

        # For persistent apps, deploy the snapshot function and create shutdown script
        if self.config.is_persistent:
            # it's a little sad that we're constantly re-deploying this, but it's a bit too easy to make mistakes otherwise
            #  (eg, we might end up with outdated code at that endpoint, which would be hard to debug)
            snapshot_url = deploy_function(
                "snapshot_and_shutdown", self.app_name, self.environment_name, self.mng_ctx.concurrency_group
            )
            self._create_shutdown_script(host, sandbox, host_id, snapshot_url)

        # Start the activity watcher. We have to start it here because we only created the shutdown script (with the hardcoded sandbox id)
        # in the above block, thus this cannot be started any earlier.
        # Plus we really want the boot time to be written, etc, as otherwise it can be a bit racey
        with log_span("Starting activity watcher in sandbox"):
            start_activity_watcher_cmd = build_start_activity_watcher_command(str(self.host_dir))
            sandbox.exec("sh", "-c", start_activity_watcher_cmd).wait()

        # Start periodic volume sync to flush writes to the host volume (only when a host volume is mounted)
        if self.config.is_host_volume_created:
            with log_span("Starting volume sync in sandbox"):
                volume_sync_cmd = build_start_volume_sync_command(HOST_VOLUME_MOUNT_PATH, str(self.host_dir))
                sandbox.exec("sh", "-c", volume_sync_cmd).wait()

        return host, ssh_host, ssh_port, host_public_key

    def _create_shutdown_script(
        self,
        host: Host,
        sandbox: modal.Sandbox,
        host_id: HostId,
        snapshot_url: str,
    ) -> None:
        """Create the shutdown.sh script on the host.

        The script uses curl to call the deployed snapshot_and_shutdown endpoint,
        passing the sandbox_id and host_id as JSON payload.
        """
        sandbox_id = sandbox.object_id
        host_dir_str = str(host.host_dir)

        # Build the optional volume sync section for the shutdown script
        volume_sync_section = (
            (
                f"# Sync the host volume to ensure all data is flushed before snapshot\n"
                f'log "Syncing host volume before shutdown..."\n'
                f'sync {HOST_VOLUME_MOUNT_PATH} 2>/dev/null || log "Warning: host volume sync failed"\n'
            )
            if self.config.is_host_volume_created
            else ""
        )

        # Create the shutdown script content
        # The script sends a POST request to the snapshot_and_shutdown endpoint
        # It also gathers agent data from the agents directory to persist to the volume
        # The stop_reason parameter indicates why the host stopped:
        # - PAUSED: Host became idle (called by activity_watcher.sh)
        # - STOPPED: User explicitly stopped the host
        script_content = f'''#!/bin/bash
set -euo pipefail

# Auto-generated shutdown script for mng Modal host
# This script snapshots and shuts down the host by calling the deployed Modal function
# It also gathers agent data to persist to the volume so agents show up in mng list
#
# Usage: shutdown.sh [stop_reason]
#   stop_reason: 'PAUSED' (idle shutdown, default) or 'STOPPED' (user requested)

LOG_FILE="{host_dir_str}/logs/shutdown.log"
mkdir -p "$(dirname "$LOG_FILE")"

log() {{
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"
    echo "$*"
}}

log "=== Shutdown script started ==="
log "SNAPSHOT_URL: {snapshot_url}"
log "SANDBOX_ID: {sandbox_id}"
log "HOST_ID: {host_id}"
log "STOP_REASON: ${{1:-PAUSED}}"

SNAPSHOT_URL="{snapshot_url}"
SANDBOX_ID="{sandbox_id}"
HOST_ID="{host_id}"
HOST_DIR="{host_dir_str}"
STOP_REASON="${{1:-PAUSED}}"

# Gather agent data from all agent directories
# This creates a JSON array of agent data objects
gather_agents() {{
    local agents_dir="$HOST_DIR/agents"
    local first=true
    echo -n "["
    if [ -d "$agents_dir" ]; then
        for agent_dir in "$agents_dir"/*/; do
            if [ -f "${{agent_dir}}data.json" ]; then
                if [ "$first" = true ]; then
                    first=false
                else
                    echo -n ","
                fi
                cat "${{agent_dir}}data.json"
            fi
        done
    fi
    echo -n "]"
}}

# Build the JSON payload with agent data
log "Gathering agents..."
AGENTS=$(gather_agents)
log "Agents: $AGENTS"

{volume_sync_section}# Send the shutdown request with agent data and stop reason
# Use --max-time to prevent hanging if the endpoint is slow
log "Sending shutdown request to $SNAPSHOT_URL"
if ! RESPONSE=$(curl -s --max-time 30 -w "\\n%{{http_code}}" -X POST "$SNAPSHOT_URL" \\
    -H "Content-Type: application/json" \\
    -d '{{"sandbox_id": "'"$SANDBOX_ID"'", "host_id": "'"$HOST_ID"'", "stop_reason": "'"$STOP_REASON"'", "agents": '"$AGENTS"'}}'); then
    log "curl request failed"
    log "=== Shutdown script completed with error ==="
    exit 1
fi

HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | sed '$d')
log "HTTP status: $HTTP_CODE"
log "Response: $BODY"
log "=== Shutdown script completed ==="
'''

        # Write the script to the host
        commands_dir = host.host_dir / "commands"
        script_path = commands_dir / "shutdown.sh"

        with log_span("Creating shutdown script at {}", script_path):
            host.write_text_file(script_path, script_content, mode="755")

    def _parse_build_args(
        self,
        build_args: Sequence[str] | None,
    ) -> SandboxConfig:
        """Parse build arguments into sandbox configuration.

        Accepts arguments in two formats:
        - Key-value: gpu=h100, cpu=2, memory=4
        - Flag style: --gpu=h100, --gpu h100

        Both formats can be mixed. Unknown arguments raise an error.
        """

        # Boolean flags that can be passed as bare words (e.g. -b offline)
        boolean_flags = {"offline"}

        # Normalize arguments: convert "key=value" to "--key=value" and
        # bare boolean flag names like "offline" to "--offline"
        normalized_args: list[str] = []
        for arg in build_args or []:
            if "=" in arg and not arg.startswith("-"):
                # Key-value format: gpu=h100 -> --gpu=h100
                normalized_args.append(f"--{arg}")
            elif not arg.startswith("-") and arg in boolean_flags:
                # Bare boolean flag: offline -> --offline
                normalized_args.append(f"--{arg}")
            else:
                normalized_args.append(arg)

        # Use argparse for robust parsing
        parser = argparse.ArgumentParser(
            prog="build_args",
            add_help=False,
            exit_on_error=False,
        )
        parser.add_argument("--gpu", type=str, default=self.config.default_gpu)
        parser.add_argument("--cpu", type=float, default=self.config.default_cpu)
        parser.add_argument("--memory", type=float, default=self.config.default_memory)
        parser.add_argument("--image", type=str, default=self.config.default_image)
        parser.add_argument("--file", type=str, default=None, dest="dockerfile")
        parser.add_argument("--timeout", type=int, default=self.config.default_sandbox_timeout)
        parser.add_argument("--region", type=str, default=self.config.default_region)
        parser.add_argument("--context-dir", type=str, default=None)
        parser.add_argument("--secret", type=str, action="append", default=[])
        parser.add_argument("--cidr-allowlist", type=str, action="append", default=[])
        parser.add_argument("--offline", action="store_true", default=False)
        parser.add_argument("--volume", type=str, action="append", default=[])
        parser.add_argument("--docker-build-arg", type=str, action="append", default=[])

        try:
            parsed, unknown = parser.parse_known_args(normalized_args)
        except argparse.ArgumentError as e:
            raise MngError(f"Invalid build argument: {e}") from None

        if unknown:
            raise MngError(f"Unknown build arguments: {unknown}")

        return SandboxConfig(
            gpu=parsed.gpu,
            cpu=parsed.cpu,
            memory=parsed.memory,
            image=parsed.image,
            dockerfile=parsed.dockerfile,
            timeout=parsed.timeout,
            region=parsed.region,
            context_dir=parsed.context_dir,
            secrets=tuple(parsed.secret),
            cidr_allowlist=tuple(parsed.cidr_allowlist),
            offline=parsed.offline,
            volumes=tuple(_parse_volume_spec(v) for v in parsed.volume),
            docker_build_args=tuple(parsed.docker_build_arg),
        )

    # =========================================================================
    # Tag Management Helpers
    # =========================================================================

    def _build_sandbox_tags(
        self,
        host_id: HostId,
        name: HostName,
        user_tags: Mapping[str, str] | None,
    ) -> dict[str, str]:
        """Build the tags dict to store on a Modal sandbox."""
        return build_sandbox_tags(host_id, name, user_tags)

    def _parse_sandbox_tags(
        self,
        tags: dict[str, str],
    ) -> tuple[HostId, HostName, dict[str, str]]:
        """Parse tags from a Modal sandbox into structured data."""
        return parse_sandbox_tags(tags)

    def _get_modal_app(self) -> modal.App:
        """Get or create the Modal app for this provider instance.

        The app is lazily created by the modal_app manager when first needed.
        This allows basic property tests to run without Modal credentials.

        Modal output is captured at the modal_app level.

        Raises modal.exception.AuthError if Modal credentials are not configured.
        """
        return self.modal_app.app

    def get_captured_output(self) -> str:
        """Get all captured Modal output for this provider instance.

        Returns the contents of the output buffer that has been capturing Modal
        logs since the app was created. This can be used to detect build failures
        or other issues by inspecting the captured output.

        Returns an empty string if no app has been created yet.
        """
        return self.modal_app.get_captured_output()

    def _lookup_sandbox_by_host_id_once(self, host_id: HostId) -> modal.Sandbox | None:
        """Perform a single lookup of a sandbox by host_id tag.

        This is a helper for _find_sandbox_by_host_id that does not retry.
        If the sandbox is found, it is appended to result_container and True is returned.
        Otherwise, returns False.
        """
        app = self._get_modal_app()
        # TODO: Unfortunately, this has the same error as the lookup by ID. We are waiting on modal to respond before we can fix this--it's a bug on their side
        # for sandbox in modal.Sandbox.list(app_id=app.app_id, tags={TAG_HOST_ID: str(host_id)}):
        #     result_container.append(sandbox)
        #     return True
        # return False
        for sandbox in modal.Sandbox.list(app_id=app.app_id):
            if sandbox.get_tags().get(TAG_HOST_ID) == str(host_id):
                return sandbox
        return None

    def _cache_sandbox(self, host_id: HostId, name: HostName, sandbox: modal.Sandbox) -> None:
        """Cache a sandbox by host_id and name for fast lookup."""
        self._sandbox_cache_by_id[host_id] = sandbox
        self._sandbox_cache_by_name[name] = sandbox

    def _uncache_sandbox(self, host_id: HostId, name: HostName | None = None) -> None:
        """Remove a sandbox from the caches."""
        self._sandbox_cache_by_id.pop(host_id, None)
        if name is not None:
            self._sandbox_cache_by_name.pop(name, None)

    def _uncache_host(self, host_id: HostId) -> None:
        """Remove a host from the host cache.

        This should be called when a host transitions state (e.g., from online to offline
        or vice versa) to ensure the next lookup returns the correct host type.
        """
        self._host_by_id_cache.pop(host_id, None)

    def reset_caches(self) -> None:
        """Reset all caches on this instance.

        This is primarily used for test isolation to ensure a clean state between tests.
        """
        self._sandbox_cache_by_id.clear()
        self._sandbox_cache_by_name.clear()
        self._host_by_id_cache.clear()
        self._host_record_cache_by_id.clear()

    def _find_sandbox_by_host_id(
        self, host_id: HostId, timeout: float = 5.0, poll_interval: float = 1.0
    ) -> modal.Sandbox | None:
        """Find a Modal sandbox by its mng host_id tag."""
        # Check cache first - this avoids eventual consistency issues for recently created sandboxes
        if host_id in self._sandbox_cache_by_id:
            sandbox = self._sandbox_cache_by_id[host_id]
            logger.trace("Found sandbox in cache for host_id={}", host_id)
            return sandbox

        # Fall back to querying Modal's API
        return self._lookup_sandbox_by_host_id_once(host_id)

    def _lookup_sandbox_by_name_once(self, name: HostName) -> modal.Sandbox | None:
        """Perform a single lookup of a sandbox by host_name tag."""
        app = self._get_modal_app()
        # TODO: Unfortunately, this has the same error as the lookup by ID. We are waiting on modal to respond before we can fix this--it's a bug on their side
        # for sandbox in modal.Sandbox.list(app_id=app.app_id, tags={TAG_HOST_NAME: str(name)}):
        #     return sandbox
        # return None
        for sandbox in modal.Sandbox.list(app_id=app.app_id):
            if sandbox.get_tags().get(TAG_HOST_NAME) == str(name):
                return sandbox
        return None

    def _find_sandbox_by_name(
        self, name: HostName, timeout: float = 5.0, poll_interval: float = 1.0
    ) -> modal.Sandbox | None:
        """Find a Modal sandbox by its mng host_name tag.

        First checks the local cache (populated when sandboxes are created), then
        falls back to querying Modal's API. The cache avoids Modal's eventual
        consistency issues for recently created sandboxes.

        The app_id identifies the app within its environment, so sandboxes created
        in that app's environment will be found via app_id alone.

        Due to Modal's eventual consistency, tags may not be immediately visible
        after a sandbox is created. This method polls for the sandbox with delays
        to handle this race condition when the sandbox isn't in the cache.
        """
        # Check cache first - this avoids eventual consistency issues for recently created sandboxes
        if name in self._sandbox_cache_by_name:
            sandbox = self._sandbox_cache_by_name[name]
            logger.trace("Found sandbox in cache for name={}", name)
            return sandbox

        # Fall back to querying Modal's API
        return self._lookup_sandbox_by_name_once(name)

    def _list_sandboxes(self) -> list[modal.Sandbox]:
        """List all Modal sandboxes managed by this mng provider instance.

        The app_id identifies the app within its environment, so sandboxes created
        in that app's environment will be found via app_id alone.
        """
        app = self._get_modal_app()
        sandboxes: list[modal.Sandbox] = []
        for sandbox in modal.Sandbox.list(app_id=app.app_id):
            tags = sandbox.get_tags()
            if TAG_HOST_ID in tags:
                sandboxes.append(sandbox)
        logger.trace("Listed all mng sandboxes for app={} in env={}", self.app_name, self.environment_name)
        return sandboxes

    def _create_host_from_sandbox(
        self,
        sandbox: modal.Sandbox,
        host_record: HostRecord | None = None,
    ) -> Host | None:
        """Create a Host object from a Modal sandbox.

        This reads host metadata from the volume and adds the host key to
        known_hosts for the sandbox's SSH endpoint, enabling SSH connections
        to succeed without host key verification prompts.

        The Host is configured with a callback to sync certified data changes
        back to the volume, ensuring operations like snapshot creation persist
        their metadata.

        Returns None if the host record doesn't exist on the volume.
        """
        tags = sandbox.get_tags()
        host_id, name, user_tags = self._parse_sandbox_tags(tags)

        with trace_span("Everything else for {}", host_id, _is_trace_span_enabled=False):
            # Read host metadata from the volume
            if host_record is None:
                with trace_span("Reading host record again pointlessly {}", host_id, _is_trace_span_enabled=False):
                    host_record = self._read_host_record(host_id, use_cache=False)
                    if host_record is None:
                        logger.warning("Skipped sandbox {}: no host record on volume", sandbox.object_id)
                        return None

            # Failed hosts don't have SSH info and can't be connected to
            if host_record.ssh_host is None or host_record.ssh_port is None or host_record.ssh_host_public_key is None:
                logger.warning(
                    "Skipped sandbox {}: host record missing SSH info (likely failed host)", sandbox.object_id
                )
                return None

            # Add the sandbox's host key to known_hosts so SSH connections will work
            with trace_span("Adding to known hosts {}", host_id, _is_trace_span_enabled=False):
                add_host_to_known_hosts(
                    self._known_hosts_path,
                    host_record.ssh_host,
                    host_record.ssh_port,
                    host_record.ssh_host_public_key,
                )

            with trace_span("Creating pyinfra {}", host_id, _is_trace_span_enabled=False):
                private_key_path, _ = self._get_ssh_keypair()
                pyinfra_host = self._create_pyinfra_host(
                    host_record.ssh_host,
                    host_record.ssh_port,
                    private_key_path,
                )
                connector = PyinfraConnector(pyinfra_host)

            return Host(
                id=host_id,
                connector=connector,
                provider_instance=self,
                mng_ctx=self.mng_ctx,
                on_updated_host_data=lambda callback_host_id, certified_data: self._on_certified_host_data_updated(
                    callback_host_id, certified_data
                ),
            )

    def _create_host_from_host_record(
        self,
        host_record: HostRecord,
    ) -> OfflineHost:
        """Create an OfflineHost object from a host record (for stopped hosts).

        This is used when there is no running sandbox but the host record
        exists on the volume. The OfflineHost provides read-only access to
        stored host data without SSH connectivity.

        The certified_host_data is populated with information available from
        the host record.
        """
        host_id = HostId(host_record.certified_host_data.host_id)
        return OfflineHost(
            id=host_id,
            certified_host_data=host_record.certified_host_data,
            provider_instance=self,
            mng_ctx=self.mng_ctx,
            on_updated_host_data=lambda callback_host_id, certified_data: self._on_certified_host_data_updated(
                callback_host_id, certified_data
            ),
        )

    # =========================================================================
    # Name Uniqueness
    # =========================================================================

    def _check_host_name_is_unique(self, name: HostName) -> None:
        """Check that no non-destroyed host on this provider already uses the given name."""
        with log_span("Checking host name uniqueness for {}", name):
            host_records, _agent_data = self._list_all_host_and_agent_records(
                cg=self.mng_ctx.concurrency_group, is_including_agents=False
            )
            running_host_ids = self._list_running_host_ids(cg=self.mng_ctx.concurrency_group)

        check_host_name_is_unique(name, host_records, running_host_ids)

    # =========================================================================
    # Core Lifecycle Methods
    # =========================================================================

    @handle_modal_auth_error
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
        """Create a new Modal sandbox host.

        If snapshot is provided, the host is created from the snapshot image
        instead of building a new one.
        """
        # Generate host ID
        host_id = HostId.generate()

        if start_args:
            # someday we can allow this by understanding if these result in a different configuration for the sandbox,
            # and if so, first building, then snapshotting, then starting a second sandbox from that snapshot
            # (with the right start args for that second sandbox)
            raise NotImplementedError(
                "separate start_args are not yet supported for Modal provider: use build_args instead"
            )

        # Check that no existing host already uses this name
        # FOLLOWUP: this check is not atomic -- a race condition exists where two concurrent
        # create_host calls could both pass the check and create hosts with the same name.
        # We will need some kind of locking (e.g. a volume-based lock file) to prevent this.
        self._check_host_name_is_unique(name)

        logger.info("Creating host {} in {} ...", name, self.name)

        # Parse build arguments (including --file if specified)
        config = self._parse_build_args(build_args)
        base_image = str(image) if image else config.image
        dockerfile_path = Path(config.dockerfile) if config.dockerfile else None
        context_dir_path = Path(config.context_dir) if config.context_dir else None

        if not base_image and not dockerfile_path:
            logger.warning(
                "No image or Dockerfile specified -- building from mng default Dockerfile. "
                "Consider using your own Dockerfile (-b --file=<path>) to include "
                "your project's dependencies for faster startup.",
            )

        try:
            if snapshot is not None:
                # Use the snapshot image instead of building
                with log_span("Loading Modal image from snapshot {}", str(snapshot)):
                    modal_image: modal.Image = modal.Image.from_id(str(snapshot))  # ty: ignore[invalid-assignment]
            else:
                # Build the Modal image
                with log_span("Building Modal image..."):
                    modal_image = self._build_modal_image(
                        base_image, dockerfile_path, context_dir_path, config.secrets, config.docker_build_args
                    )

            # Get or create the Modal app (uses singleton pattern with context manager)
            with log_span("Getting Modal app", app_name=self.app_name):
                app = self._get_modal_app()

            # Create the sandbox
            # Add shutdown buffer to the timeout sent to Modal so the activity watcher can
            # trigger a clean shutdown before Modal's hard timeout kills the host
            modal_timeout = config.timeout + self.config.shutdown_buffer_seconds

            # Build volume mounts from build args
            sandbox_volumes = _build_modal_volumes(config.volumes, self.environment_name)

            # Add the persistent host volume so all host_dir data is preserved (if enabled)
            if self.config.is_host_volume_created:
                with log_span("Ensuring host volume for {}", host_id):
                    sandbox_volumes[HOST_VOLUME_MOUNT_PATH] = self._build_host_volume(host_id)

            with log_span(
                "Creating Modal sandbox",
                timeout=config.timeout,
                modal_timeout=modal_timeout,
                shutdown_buffer=self.config.shutdown_buffer_seconds,
                cpu=config.cpu,
                memory_gb=config.memory,
            ):
                # Memory is in GB but Modal expects MB
                memory_mb = int(config.memory * 1024)
                sandbox = modal.Sandbox.create(
                    image=modal_image,
                    app=app,
                    # note: we do NOT pass the environment_name here because that is deprecated (it is inferred from the app)
                    # environment_name=self.environment_name,
                    timeout=modal_timeout,
                    cpu=config.cpu,
                    memory=memory_mb,
                    unencrypted_ports=[CONTAINER_SSH_PORT],
                    gpu=config.gpu,
                    region=config.region,
                    cidr_allowlist=config.effective_cidr_allowlist,
                    volumes=sandbox_volumes,
                )
                logger.trace("Created Modal sandbox", sandbox_id=sandbox.object_id)
        except (modal.exception.Error, MngError) as e:
            # On failure, save a failed host record so the user can see what happened
            failure_reason = str(e)
            build_log = self.get_captured_output()
            logger.error("Host creation failed: {}", failure_reason)
            self._save_failed_host_record(
                host_id=host_id,
                host_name=name,
                tags=tags,
                failure_reason=failure_reason,
                build_log=build_log,
            )
            if isinstance(e, modal.exception.RemoteError):
                raise MngError(f"Failed to create Modal sandbox: {e}\n{build_log}") from None
            else:
                raise

        # Cache the sandbox for fast lookup (avoids Modal's eventual consistency issues)
        self._cache_sandbox(host_id, name, sandbox)

        lifecycle_options = lifecycle if lifecycle is not None else HostLifecycleOptions()
        activity_config = lifecycle_options.to_activity_config(
            default_idle_timeout_seconds=self.config.default_idle_timeout,
            default_idle_mode=self.config.default_idle_mode,
            default_activity_sources=self.config.default_activity_sources,
        )

        # Store full host metadata on the volume for persistence
        # Note: max_host_age is the sandbox timeout (without the buffer we added to modal_timeout)
        # so the activity watcher can trigger a clean shutdown before Modal's hard kill
        now = datetime.now(timezone.utc)
        host_data = CertifiedHostData(
            idle_timeout_seconds=activity_config.idle_timeout_seconds,
            activity_sources=activity_config.activity_sources,
            max_host_age=config.timeout,
            host_id=str(host_id),
            host_name=str(name),
            user_tags=dict(tags) if tags else {},
            snapshots=[],
            tmux_session_prefix=self.mng_ctx.config.prefix,
            created_at=now,
            updated_at=now,
        )

        # Set up SSH and create host object using shared helper
        host, ssh_host, ssh_port, host_public_key = self._setup_sandbox_ssh_and_create_host(
            sandbox=sandbox,
            host_id=host_id,
            host_name=name,
            user_tags=tags,
            config=config,
            host_data=host_data,
            known_hosts=known_hosts,
            authorized_keys=authorized_keys,
        )

        return host

    def on_agent_created(self, agent: AgentInterface, host: OnlineHostInterface) -> None:
        # Optionally create an initial snapshot based on config
        # When enabled, this ensures the host can be restarted even after a hard kill
        if self.config.is_snapshotted_after_create:
            with log_span("Creating initial snapshot for host", host_id=str(host.id)):
                sandbox = self._find_sandbox_by_host_id(host.id)
                assert sandbox is not None, "Sandbox must exist for online host"
                self._create_initial_snapshot(sandbox, host.id)

    @handle_modal_auth_error
    def stop_host(
        self,
        host: HostInterface | HostId,
        create_snapshot: bool = True,
        timeout_seconds: float = 60.0,
    ) -> None:
        """Stop a Modal sandbox.

        Note: Modal sandboxes cannot be stopped and resumed - they can only be
        terminated. If create_snapshot is True (the default), a snapshot is
        created before termination to allow the host to be restarted later.
        """
        host_id = host.id if isinstance(host, HostInterface) else host
        logger.info("Stopping (terminating) Modal sandbox: {}", host_id)

        # Disconnect the SSH connection before terminating the sandbox.
        # This prevents stale socket state that can cause "Socket is closed" errors.
        # We check the cache first, then fall back to the passed host object.
        cached_host = self._host_by_id_cache.get(host_id)
        if cached_host is not None and isinstance(cached_host, Host):
            cached_host.disconnect()
        elif isinstance(host, Host):
            host.disconnect()
        else:
            # No Host instance available (e.g., only have a host_id string or HostInterface stub)
            pass

        sandbox = self._find_sandbox_by_host_id(host_id)
        if sandbox:
            # Create a snapshot before termination if requested
            if create_snapshot:
                try:
                    with log_span("Creating snapshot before termination", host_id=str(host_id)):
                        self.create_snapshot(host_id, SnapshotName("stop"))
                except (MngError, modal.exception.Error) as e:
                    logger.warning("Failed to create snapshot before termination: {}", e)

            try:
                sandbox.terminate()
            except modal.exception.Error as e:
                logger.warning("Error terminating sandbox: {}", e)
        else:
            logger.debug("Failed to find sandbox (may already be terminated)", host_id=str(host_id))

        # Record stop_reason=STOPPED to distinguish user-initiated stops from idle pauses
        # Note that we are explicitly avoiding going through the normal host.set_certified_data(host_data) call here
        # because A) we *don't* want to save this into the host record on the host, so that it makes more sense when it
        # is eventually started again, and B) this is a small optimization so that we don't need to get the host
        # record twice, since we use it to figure out the name below as well
        host_record = self._read_host_record(host_id, use_cache=False)
        if host_record is not None:
            updated_certified_data = host_record.certified_host_data.model_copy_update(
                to_update(host_record.certified_host_data.field_ref().stop_reason, HostState.STOPPED.value),
                to_update(host_record.certified_host_data.field_ref().updated_at, datetime.now(timezone.utc)),
            )
            self._write_host_record(
                host_record.model_copy_update(
                    to_update(host_record.field_ref().certified_host_data, updated_certified_data),
                )
            )

        # Remove from all caches since the sandbox is now terminated
        # Read host record to get the name for cache cleanup (re-read in case it was just updated)
        host_name = HostName(host_record.certified_host_data.host_name) if host_record else None
        self._uncache_sandbox(host_id, host_name)
        # Also invalidate host cache so next lookup returns an OfflineHost
        self._uncache_host(host_id)

    @handle_modal_auth_error
    def start_host(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId | None = None,
    ) -> Host:
        """Start a stopped host, optionally restoring from a specific snapshot.

        If the sandbox is still running, returns the existing host. If the
        sandbox has been terminated, creates a new sandbox from a snapshot.
        When snapshot_id is provided, that specific snapshot is used. When
        snapshot_id is None, the most recent snapshot is automatically used.

        Note: For a host to be restartable, it must have at least one snapshot.
        Snapshots are created in two cases:
        1. During host creation if is_snapshotted_after_create=True (default)
        2. During stop_host() if create_snapshot=True (default)

        If neither snapshot was created (e.g., is_snapshotted_after_create=False
        and the sandbox was hard-killed), this method raises NoSnapshotsModalMngError.
        """
        host_id = host.id if isinstance(host, HostInterface) else host

        # If sandbox is still running, return it
        sandbox = self._find_sandbox_by_host_id(host_id)
        if sandbox is not None:
            host_obj = self._create_host_from_sandbox(sandbox)
            if host_obj is not None:
                if snapshot_id is not None:
                    logger.warning(
                        "Sandbox {} is still running; ignoring snapshot_id parameter. "
                        "Stop the host first to restore from a snapshot.",
                        host_id,
                    )
                return host_obj

        # Sandbox is not running - restore from snapshot
        # First check if this is a failed host (can't be started)
        host_record = self._read_host_record(host_id, use_cache=False)
        if host_record is not None and host_record.certified_host_data.failure_reason is not None:
            raise MngError(
                f"Host {host_id} failed during creation and cannot be started. "
                f"Reason: {host_record.certified_host_data.failure_reason}"
            )

        # If no snapshot_id provided, use the most recent snapshot
        if snapshot_id is None:
            # Load host record to get available snapshots
            if host_record is None:
                raise HostNotFoundError(host_id)

            if not host_record.certified_host_data.snapshots:
                raise NoSnapshotsModalMngError(
                    f"Modal sandbox {host_id} is not running and has no snapshots. "
                    "Cannot restart. Create a new host instead."
                )

            # Use the most recent snapshot (sorted by created_at descending)
            sorted_snapshots = sorted(
                host_record.certified_host_data.snapshots, key=lambda s: s.created_at, reverse=True
            )
            snapshot_id = SnapshotId(sorted_snapshots[0].id)
            logger.info("Using most recent snapshot for restart", snapshot_id=str(snapshot_id))

        # Load host record from volume
        if host_record is None:
            raise HostNotFoundError(host_id)

        # Find the snapshot in the host record
        snapshot_data: SnapshotRecord | None = None
        for snap in host_record.certified_host_data.snapshots:
            if snap.id == str(snapshot_id):
                snapshot_data = snap
                break

        if snapshot_data is None:
            raise SnapshotNotFoundError(snapshot_id)

        # The snapshot id is the Modal image ID
        modal_image_id = snapshot_data.id
        logger.info("Restoring Modal sandbox from snapshot", host_id=str(host_id), snapshot_id=str(snapshot_id))

        # Use configuration from host record
        config = host_record.config
        if config is None:
            raise MngError(
                f"Host {host_id} has no configuration and cannot be started. "
                "This may indicate the host was never fully created."
            )
        host_name = HostName(host_record.certified_host_data.host_name)
        user_tags = host_record.certified_host_data.user_tags

        # Create the image reference from the snapshot (the id IS the Modal image ID)
        with log_span("Creating sandbox from snapshot image", image_id=modal_image_id):
            # Cast needed because modal.Image.from_id returns Self which the type checker can't resolve
            modal_image = cast(modal.Image, modal.Image.from_id(modal_image_id))

            # Get or create the Modal app
            app = self._get_modal_app()

            # Create the sandbox from the snapshot image
            # Add shutdown buffer to the timeout sent to Modal so the activity watcher can
            # trigger a clean shutdown before Modal's hard timeout kills the host
            modal_timeout = config.timeout + self.config.shutdown_buffer_seconds
            memory_mb = int(config.memory * 1024)

            # Build volume mounts from the stored config
            sandbox_volumes = _build_modal_volumes(config.volumes, self.environment_name)

            # Re-attach the persistent host volume (if enabled)
            if self.config.is_host_volume_created:
                sandbox_volumes[HOST_VOLUME_MOUNT_PATH] = self._build_host_volume(host_id)

            new_sandbox = modal.Sandbox.create(
                image=modal_image,
                app=app,
                # note: we do NOT pass the environment_name here because that is deprecated (it is inferred from the app)
                # environment_name=self.environment_name,
                timeout=modal_timeout,
                cpu=config.cpu,
                memory=memory_mb,
                unencrypted_ports=[CONTAINER_SSH_PORT],
                gpu=config.gpu,
                region=config.region,
                cidr_allowlist=config.effective_cidr_allowlist,
                volumes=sandbox_volumes,
            )
        logger.info("Created sandbox from snapshot", sandbox_id=new_sandbox.object_id)

        # Cache the sandbox for fast lookup (avoids Modal's eventual consistency issues)
        self._cache_sandbox(host_id, host_name, new_sandbox)
        # Invalidate any cached OfflineHost so we return the new online Host
        self._uncache_host(host_id)

        # Set up SSH and create host object using shared helper
        restored_host, ssh_host, ssh_port, host_public_key = self._setup_sandbox_ssh_and_create_host(
            sandbox=new_sandbox,
            host_id=host_id,
            host_name=host_name,
            user_tags=user_tags,
            config=config,
            host_data=host_record.certified_host_data,
        )

        # Cache the new online host
        self._host_by_id_cache[host_id] = restored_host

        return restored_host

    @handle_modal_auth_error
    def destroy_host(self, host: HostInterface | HostId) -> None:
        """Destroy a Modal sandbox permanently."""
        self.stop_host(host, create_snapshot=False)
        host_id = host.id if isinstance(host, HostInterface) else host
        self._destroy_agents_on_host(host_id)
        self._clear_snapshots_from_host_record(host_id)
        # FOLLOWUP: once Modal enables deleting Images, this will be the place to do it
        if self.config.is_host_volume_created:
            self._delete_host_volume(host_id)

    @handle_modal_auth_error
    def delete_host(self, host: HostInterface) -> None:
        self._destroy_agents_on_host(host.id)
        self._delete_host_record(host.id)
        if self.config.is_host_volume_created:
            self._delete_host_volume(host.id)

    def _delete_host_volume(self, host_id: HostId) -> None:
        """Delete the persistent host volume, logging but not raising on failure."""
        host_volume_name = self._get_host_volume_name(host_id)
        try:
            modal.Volume.objects.delete(host_volume_name, environment_name=self.environment_name)
            logger.debug("Deleted host volume: {}", host_volume_name)
        except NotFoundError:
            logger.trace("Host volume {} already deleted", host_volume_name)
        except (modal.exception.InvalidError, modal.exception.InternalError) as e:
            logger.warning("Failed to delete host volume {}: {}", host_volume_name, e)

    def on_connection_error(self, host_id: HostId) -> None:
        """Remove all caches if we notice a connection to the host fail"""
        host_record = self._host_record_cache_by_id.get(host_id)
        if host_record is not None:
            host_name = HostName(host_record.certified_host_data.host_name)
            self._sandbox_cache_by_name.pop(host_name, None)
        self._sandbox_cache_by_id.pop(host_id, None)
        self._host_by_id_cache.pop(host_id, None)
        self._host_record_cache_by_id.pop(host_id, None)

    # =========================================================================
    # Discovery Methods
    # =========================================================================

    def to_offline_host(self, host_id: HostId) -> OfflineHost:
        host_record = self._read_host_record(host_id)
        if host_record is None:
            raise HostNotFoundError(host_id)

        return self._create_host_from_host_record(host_record)

    @handle_modal_auth_error
    def get_host(
        self,
        host: HostId | HostName,
    ) -> HostInterface:
        return self._get_host(host)

    def _get_host(
        self,
        host: HostId | HostName,
        host_record: HostRecord | None = None,
    ) -> HostInterface:
        """Get a host by ID or name.

        First tries to find a running sandbox. If not found, falls back to
        the host record on the volume (for stopped hosts).

        Allows you to pass in the HostRecord if you know if (an optimization so that it doesnt need to be loaded again)
        """
        if isinstance(host, HostId) and host in self._host_by_id_cache:
            return self._host_by_id_cache[host]

        host_obj: HostInterface | None = None
        if isinstance(host, HostId):
            # Try to find a running sandbox first
            with trace_span("Finding sandbox for {}", host, _is_trace_span_enabled=False):
                sandbox = self._find_sandbox_by_host_id(host)
            if sandbox is not None:
                with trace_span("Creating host object from sandbox for {}", host, _is_trace_span_enabled=False):
                    try:
                        host_obj = self._create_host_from_sandbox(sandbox, host_record)
                    except HostConnectionError as e:
                        logger.debug("Failed to create host from sandbox {}, assuming it is offline: {}", host, e)

            if host_obj is None:
                # No sandbox or couldn't connect - try host record (for stopped hosts)
                host_record = self._read_host_record(host)
                if host_record is not None:
                    host_obj = self._create_host_from_host_record(host_record)
        else:
            # If it's a HostName, search by name
            sandbox = self._find_sandbox_by_name(host)
            if sandbox is not None:
                try:
                    host_obj = self._create_host_from_sandbox(sandbox)
                except HostConnectionError as e:
                    logger.debug("Failed to create host from sandbox {}, assuming it is offline: {}", host, e)

            # No sandbox or couldn't connect - search host records by name (for stopped hosts)
            if host_obj is None:
                for host_record in self._list_all_host_records(cg=self.mng_ctx.concurrency_group):
                    if host_record.certified_host_data.host_name == str(host):
                        host_obj = self._create_host_from_host_record(host_record)

        # finally save to the cache and return
        if host_obj is not None:
            self._host_by_id_cache[host_obj.id] = host_obj
            return host_obj
        # or raise:
        else:
            raise HostNotFoundError(host)

    @handle_modal_auth_error
    def list_hosts(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> list[HostInterface]:
        """List all Modal sandbox hosts, including stopped ones.

        Returns hosts in three states:
        - RUNNING: has an active sandbox
        - STOPPED: no sandbox but has snapshots (can be restarted)
        - DESTROYED: no sandbox and no snapshots (only if include_destroyed=True)

        If a ConcurrencyGroup is provided, it will be used for parallel fetching of
        sandboxes and host records, which is safer for concurrent operations.
        """

        hosts: list[HostInterface] = []
        processed_host_ids: set[HostId] = set()

        # Fetch sandboxes and host records in parallel since they are independent.
        # This reduces list_hosts latency by ~1.5s by overlapping the network calls.
        try:
            with ConcurrencyGroupExecutor(
                parent_cg=cg, name=f"modal_list_hosts_{self.name}", max_workers=2
            ) as executor:
                sandboxes_future = executor.submit(self._list_sandboxes)
                host_records_future = executor.submit(self._list_all_host_records, cg)

            sandboxes = sandboxes_future.result()
            all_host_records = host_records_future.result()
        except modal.exception.AuthError as e:
            raise ModalAuthError() from e

        # Map running sandboxes by host_id
        running_sandbox_by_host_id: dict[HostId, modal.Sandbox] = {}
        for sandbox in sandboxes:
            try:
                tags = sandbox.get_tags()
                host_id = HostId(tags[TAG_HOST_ID])
                running_sandbox_by_host_id[host_id] = sandbox
            except (KeyError, ValueError) as e:
                logger.warning("Skipped sandbox with invalid tags: {}", e)
                continue

        # First, process host records (includes both running and stopped hosts)
        for host_record in all_host_records:
            host_id = HostId(host_record.certified_host_data.host_id)
            processed_host_ids.add(host_id)

            host_obj: HostInterface | None = None
            if host_id in running_sandbox_by_host_id:
                # Host has a running sandbox - create from sandbox
                sandbox = running_sandbox_by_host_id[host_id]
                try:
                    host_obj = self._create_host_from_sandbox(sandbox)
                    if host_obj is not None:
                        hosts.append(host_obj)
                except (KeyError, ValueError, HostConnectionError) as e:
                    logger.warning("Failed to create host from sandbox {}: {}", host_id, e)
                    continue
            if host_id not in running_sandbox_by_host_id or host_obj is None:
                # Host has no running sandbox - it's stopped, failed, destroyed, or we couldn't connect
                has_snapshots = len(host_record.certified_host_data.snapshots) > 0
                is_failed = host_record.certified_host_data.failure_reason is not None

                if is_failed:
                    # Failed host - always include so users can warning() build failures
                    try:
                        host_obj = self._create_host_from_host_record(host_record)
                        hosts.append(host_obj)
                    except (OSError, IOError, ValueError, KeyError) as e:
                        logger.warning("Failed to create host from host record {}: {}", host_id, e)
                        continue
                elif has_snapshots:
                    # Stopped host (can be restarted)
                    try:
                        host_obj = self._create_host_from_host_record(host_record)
                        hosts.append(host_obj)
                    except (OSError, IOError, ValueError, KeyError) as e:
                        logger.warning("Failed to create host from host record {}: {}", host_id, e)
                        continue
                elif include_destroyed:
                    # Destroyed host (no snapshots, can't be restarted)
                    try:
                        host_obj = self._create_host_from_host_record(host_record)
                        hosts.append(host_obj)
                    except (OSError, IOError, ValueError, KeyError) as e:
                        logger.warning("Failed to create host from host record {}: {}", host_id, e)
                        continue
                else:
                    # Skip destroyed hosts when include_destroyed=False
                    pass

        # Second, include any running sandboxes that don't have host records yet
        # (handles eventual consistency of volume or legacy sandboxes)
        for host_id, sandbox in running_sandbox_by_host_id.items():
            if host_id in processed_host_ids:
                continue
            try:
                host_obj = self._create_host_from_sandbox(sandbox)
                if host_obj is not None:
                    hosts.append(host_obj)
            except (KeyError, ValueError, HostConnectionError) as e:
                logger.warning("Failed to create host from sandbox {}: {}", host_id, e)
                continue

        # add these hosts to a cache so we don't need to look them up by name or id again
        for host in hosts:
            self._host_by_id_cache[host.id] = host

        return hosts

    def _list_running_host_ids(self, cg: ConcurrencyGroup) -> set[HostId]:
        """List host IDs of all running sandboxes, fetching tags in parallel.

        Lists all sandboxes for this app, then fetches tags for each sandbox
        concurrently to determine which hosts are running.
        """
        with trace_span("  _list_running_host_ids", _is_trace_span_enabled=False):
            app = self._get_modal_app()
            sandboxes = list(modal.Sandbox.list(app_id=app.app_id))

            if not sandboxes:
                return set()

            # Fetch tags for all sandboxes in parallel
            tag_futures: list[Future[dict[str, str]]] = []
            with ConcurrencyGroupExecutor(parent_cg=cg, name="fetch_sandbox_tags", max_workers=32) as executor:
                for sandbox in sandboxes:
                    tag_futures.append(executor.submit(sandbox.get_tags))

            running_host_ids: set[HostId] = set()
            for future in tag_futures:
                try:
                    tags = future.result()
                    if TAG_HOST_ID in tags:
                        running_host_ids.add(HostId(tags[TAG_HOST_ID]))
                except (KeyError, ValueError) as e:
                    logger.warning("Skipped sandbox with invalid tags: {}", e)

            logger.trace("Listed {} running host ID(s) for app={}", len(running_host_ids), self.app_name)
            return running_host_ids

    @handle_modal_auth_error
    def load_agent_refs(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> dict[HostReference, list[AgentReference]]:
        """Load hosts and agent references entirely from the state volume and sandbox list.

        Optimized implementation that avoids SSH connections by reading all data
        from the Modal state volume in parallel with listing running sandboxes.

        Three operations run in parallel:
        1. List running sandbox host IDs (with tags fetched in parallel)
        2. Read all host records from the state volume
        3. Read all agent data from the state volume (for all hosts)
        """
        with trace_span("Loading data for refs", _is_trace_span_enabled=False):
            try:
                with ConcurrencyGroupExecutor(
                    parent_cg=cg, name=f"modal_load_agent_refs_{self.name}", max_workers=3
                ) as executor:
                    running_ids_future = executor.submit(self._list_running_host_ids, cg)
                    host_and_agent_future = executor.submit(self._list_all_host_and_agent_records, cg)

                running_host_ids = running_ids_future.result()
                all_host_records, agent_data_by_host_id = host_and_agent_future.result()
            except modal.exception.AuthError as e:
                raise ModalAuthError() from e

        # Build HostReference -> [AgentReference] mapping from host records
        result: dict[HostReference, list[AgentReference]] = {}

        for host_record in all_host_records:
            host_id = HostId(host_record.certified_host_data.host_id)
            host_name = HostName(host_record.certified_host_data.host_name)

            is_running = host_id in running_host_ids
            has_snapshots = len(host_record.certified_host_data.snapshots) > 0
            is_failed = host_record.certified_host_data.failure_reason is not None

            if not is_running and not is_failed and not has_snapshots and not include_destroyed:
                continue

            host_ref = HostReference(
                host_id=host_id,
                host_name=host_name,
                provider_name=self.name,
            )

            agent_refs: list[AgentReference] = []
            for agent_data in agent_data_by_host_id.get(host_id, []):
                ref = validate_and_create_agent_reference(agent_data, host_id, self.name)
                if ref is not None:
                    agent_refs.append(ref)

            result[host_ref] = agent_refs

        return result

    def get_host_resources(self, host: HostInterface) -> HostResources:
        """Get resource information for a Modal sandbox."""
        # Read host record from volume
        host_record = self._read_host_record(host.id)
        if host_record is None or host_record.config is None:
            # No config available (e.g., failed host that never started)
            return HostResources(
                cpu=CpuResources(count=1, frequency_ghz=None),
                memory_gb=1.0,
                disk_gb=None,
                gpu=None,
            )

        cpu = host_record.config.cpu
        memory = host_record.config.memory

        return HostResources(
            # Modal allows fractional CPUs (e.g., 0.5), but count must be at least 1.
            # All Modal sandboxes run on the same CPU at ~1.85 GHz.
            cpu=CpuResources(count=max(1, int(cpu)), frequency_ghz=1.85),
            memory_gb=memory,
            disk_gb=None,
            gpu=None,
        )

    # =========================================================================
    # Optimized Listing
    # =========================================================================

    def build_host_listing_data(
        self,
        host_ref: HostReference,
        agent_refs: Sequence[AgentReference],
    ) -> tuple[HostInfo, list[AgentInfo]] | None:
        """Build HostInfo and AgentInfo via a single SSH command."""
        with trace_span("Building host listing data for {}", host_ref.host_id, _is_trace_span_enabled=False):
            with trace_span("Reading host record for {}", host_ref.host_id, _is_trace_span_enabled=False):
                host_record = self._read_host_record(host_ref.host_id)

            with trace_span("Getting host for {}", host_ref.host_id, _is_trace_span_enabled=False):
                host = self._get_host(host_ref.host_id, host_record)

            # For offline hosts, fall back to the default per-field collection
            if not isinstance(host, Host):
                return None

            # Collect all data in one SSH command
            with trace_span("Collecting listing data for {}", host_ref.host_id, _is_trace_span_enabled=False):
                raw = self._collect_all_listing_data_via_ssh(host)
                if raw is None:
                    return None

            # Build HostInfo from cached host record + SSH-collected data
            with trace_span("Assembling host info for {}", host_ref.host_id, _is_trace_span_enabled=False):
                host_info = self._build_host_info_from_raw(host, host_ref, host_record, raw)

            # Build AgentInfo for each agent
            with trace_span("Assembling agent info for {}", host_ref.host_id, _is_trace_span_enabled=False):
                certified_data = host_record.certified_host_data if host_record is not None else None
                agent_infos = self._build_agent_infos_from_raw(host_info, certified_data, raw)

            return host_info, agent_infos

    def _collect_all_listing_data_via_ssh(self, host: Host) -> dict[str, Any] | None:
        """Execute a single SSH command to collect all data needed for listing."""
        host_dir = str(self.host_dir)
        prefix = self.mng_ctx.config.prefix

        # Build a shell script that collects everything we need
        script = _build_listing_collection_script(host_dir, prefix)

        with log_span("Collecting listing data via single SSH command", host_id=str(host.id)):
            result = host.execute_command(script, timeout_seconds=30.0)

        if not result.success:
            logger.warning("Failed to collect listing data from host {}: {}", host.id, result.stderr)
            return None

        return _parse_listing_collection_output(result.stdout)

    def _build_host_info_from_raw(
        self,
        host: Host,
        host_ref: HostReference,
        host_record: HostRecord | None,
        raw: dict[str, Any],
    ) -> HostInfo:
        """Construct HostInfo from cached host record and SSH-collected data."""
        # SSH info from host connector (local data, no SSH needed)
        ssh_info: SSHInfo | None = None
        ssh_connection = host._get_ssh_connection_info()
        if ssh_connection is not None:
            user, hostname, port, key_path = ssh_connection
            ssh_info = SSHInfo(
                user=user,
                host=hostname,
                port=port,
                key_path=key_path,
                command=f"ssh -i {key_path} -p {port} {user}@{hostname}",
            )

        # Boot time and uptime from SSH-collected data
        boot_time = timestamp_to_datetime(raw.get("btime"))
        uptime_seconds = raw.get("uptime_seconds")

        # Resources from cached host record (no remote call)
        resource = self.get_host_resources(host)

        # Lock status from SSH-collected data
        lock_mtime = raw.get("lock_mtime")
        is_locked = lock_mtime is not None
        locked_time = datetime.fromtimestamp(lock_mtime, tz=timezone.utc) if lock_mtime is not None else None

        # Certified data from SSH-collected data (parsed from data.json)
        certified_data: CertifiedHostData | None = None
        certified_data_dict = raw.get("certified_data")
        if certified_data_dict is not None:
            try:
                certified_data = CertifiedHostData.model_validate(certified_data_dict)
            except (ValueError, KeyError) as e:
                logger.warning("Failed to validate host data.json from SSH output, falling back to volume: {}", e)
        if certified_data is None and host_record is not None:
            certified_data = host_record.certified_host_data
        elif certified_data is None:
            now = datetime.now(timezone.utc)
            certified_data = CertifiedHostData(
                host_id=str(host.id),
                host_name=str(host_ref.host_name),
                created_at=now,
                updated_at=now,
            )
        else:
            pass

        host_name = certified_data.host_name
        host_plugin_data = certified_data.plugin

        # Tags from cached host record (no Modal API call)
        tags = dict(certified_data.user_tags)

        # SSH activity from SSH-collected data
        ssh_activity_mtime = raw.get("ssh_activity_mtime")
        ssh_activity = (
            datetime.fromtimestamp(ssh_activity_mtime, tz=timezone.utc) if ssh_activity_mtime is not None else None
        )

        # Snapshots from cached host record
        snapshots = self.list_snapshots(host)

        return HostInfo(
            id=host.id,
            name=host_name,
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
            plugin=host_plugin_data,
            ssh_activity_time=ssh_activity,
            failure_reason=certified_data.failure_reason,
        )

    def _build_agent_infos_from_raw(
        self,
        host_info: HostInfo,
        certified_host_data: CertifiedHostData | None,
        raw: dict[str, Any],
    ) -> list[AgentInfo]:
        """Build AgentInfo objects from SSH-collected agent data."""
        # Activity config from certified data
        if certified_host_data is not None:
            idle_timeout_seconds = certified_host_data.idle_timeout_seconds
            activity_sources = certified_host_data.activity_sources
            idle_mode = certified_host_data.idle_mode
        else:
            idle_timeout_seconds = 3600
            activity_sources = ()
            idle_mode = IdleMode.IO

        ssh_activity = timestamp_to_datetime(raw.get("ssh_activity_mtime"))
        ps_output = raw.get("ps_output", "")

        agent_infos: list[AgentInfo] = []
        for agent_raw in raw.get("agents", []):
            try:
                agent_info = self._build_single_agent_info(
                    agent_raw=agent_raw,
                    host_info=host_info,
                    ssh_activity=ssh_activity,
                    ps_output=ps_output,
                    idle_timeout_seconds=idle_timeout_seconds,
                    activity_sources=activity_sources,
                    idle_mode=idle_mode,
                )
                if agent_info is not None:
                    agent_infos.append(agent_info)
            except (ValueError, KeyError, TypeError) as e:
                agent_id = agent_raw.get("data", {}).get("id", "unknown")
                logger.warning("Failed to build listing info for agent {}: {}", agent_id, e)

        return agent_infos

    def _build_single_agent_info(
        self,
        agent_raw: dict[str, Any],
        host_info: HostInfo,
        ssh_activity: datetime | None,
        ps_output: str,
        idle_timeout_seconds: int,
        activity_sources: tuple[ActivitySource, ...],
        idle_mode: IdleMode,
    ) -> AgentInfo | None:
        """Build a single AgentInfo from raw SSH-collected data."""
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

        # Activity times and derived values
        user_activity = timestamp_to_datetime(agent_raw.get("user_activity_mtime"))
        agent_activity = timestamp_to_datetime(agent_raw.get("agent_activity_mtime"))
        start_time = timestamp_to_datetime(agent_raw.get("start_activity_mtime"))
        now = datetime.now(timezone.utc)
        runtime_seconds = (now - start_time).total_seconds() if start_time else None
        idle_seconds = compute_idle_seconds(user_activity, agent_activity, ssh_activity) or 0.0

        # Lifecycle state from tmux info
        expected_process_name = resolve_expected_process_name(agent_type, command, self.mng_ctx.config)
        state = determine_lifecycle_state(
            tmux_info=agent_raw.get("tmux_info"),
            is_active=agent_raw.get("is_active", False),
            expected_process_name=expected_process_name,
            ps_output=ps_output,
        )

        return AgentInfo(
            id=AgentId(agent_id_str),
            name=AgentName(agent_name_str),
            type=agent_type,
            command=command,
            work_dir=Path(agent_data.get("work_dir", "/")),
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
            host=host_info,
            plugin={},
        )

    # =========================================================================
    # Snapshot Methods
    # =========================================================================

    def _record_snapshot(
        self,
        sandbox: modal.Sandbox,
        host_id: HostId,
        name: SnapshotName,
    ) -> SnapshotId:
        """Create a filesystem snapshot and record it in the host record.

        This is the core snapshot logic used by both _create_initial_snapshot
        and create_snapshot. It reads the host record from the volume, creates
        a filesystem snapshot via Modal, records the snapshot metadata in the
        host record, and writes the updated host record back to the volume.
        """
        # Read existing host record from volume
        host_record = self._read_host_record(host_id, use_cache=False)
        if host_record is None:
            raise HostNotFoundError(host_id)

        # Create the filesystem snapshot
        with log_span("Creating filesystem snapshot", name=str(name)):
            # note that this can sometimes take quite a while kind of randomly, and it's sorta Modal's fault
            # I've observed > 60-second delays even without tons of files (or large files)
            # when there are lots of files (or the files are large), it can take even longer
            # this is just a best-effort compromise between waiting forever and giving up too early - in practice, if it takes more than 5 minutes, something has probably gone pretty wrong
            modal_image = sandbox.snapshot_filesystem(timeout=120)
        # Use the Modal image ID directly as the snapshot ID
        snapshot_id = SnapshotId(modal_image.object_id)
        created_at = datetime.now(timezone.utc)

        new_snapshot = SnapshotRecord(
            id=str(snapshot_id),
            name=str(name),
            created_at=created_at.isoformat(),
        )

        # Update host record with new snapshot and write to volume
        updated_certified_data = host_record.certified_host_data.model_copy_update(
            to_update(
                host_record.certified_host_data.field_ref().snapshots,
                list(host_record.certified_host_data.snapshots) + [new_snapshot],
            ),
        )
        self.get_host(host_id).set_certified_data(updated_certified_data)
        logger.debug(
            "Created snapshot: id={}, name={}",
            snapshot_id,
            name,
        )

        return snapshot_id

    def _create_initial_snapshot(
        self,
        sandbox: modal.Sandbox,
        host_id: HostId,
    ) -> SnapshotId:
        """Create an initial snapshot of a newly created host.

        This is called during host creation when is_snapshotted_after_create
        is True, ensuring the host can be restarted after being stopped.
        The initial state after SSH setup is captured as the "initial" snapshot.
        """
        return self._record_snapshot(sandbox, host_id, SnapshotName("initial"))

    @handle_modal_auth_error
    def create_snapshot(
        self,
        host: HostInterface | HostId,
        name: SnapshotName | None = None,
    ) -> SnapshotId:
        """Create a snapshot of a Modal sandbox's filesystem.

        Uses Modal's sandbox.snapshot_filesystem() to create an incremental snapshot.
        Snapshot metadata is stored on the Modal Volume for persistence across
        sandbox termination and sharing between mng instances.
        """
        host_id = host.id if isinstance(host, HostInterface) else host

        sandbox = self._find_sandbox_by_host_id(host_id)
        if sandbox is None:
            raise HostNotFoundError(host_id)

        # Generate snapshot name if not provided
        if name is None:
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S")
            name = SnapshotName(f"snapshot-{timestamp}")

        with log_span("Creating snapshot for Modal sandbox", host_id=str(host_id)):
            snapshot_id = self._record_snapshot(sandbox, host_id, name)
        logger.info("Created snapshot: id={}, name={}", snapshot_id, name)
        return snapshot_id

    def list_snapshots(
        self,
        host: HostInterface | HostId,
    ) -> list[SnapshotInfo]:
        """List all snapshots for a Modal sandbox.

        Reads snapshot metadata from the Modal Volume, which persists even
        after the sandbox has been terminated.
        """
        host_id = host.id if isinstance(host, HostInterface) else host

        # Read host record from volume
        host_record = self._read_host_record(host_id)
        if host_record is None:
            return []

        # Convert to SnapshotInfo objects, sorted by created_at (newest first)
        snapshots: list[SnapshotInfo] = []
        sorted_snapshots = sorted(host_record.certified_host_data.snapshots, key=lambda s: s.created_at, reverse=True)
        for idx, snap_record in enumerate(sorted_snapshots):
            created_at_str = snap_record.created_at
            created_at = datetime.fromisoformat(created_at_str) if created_at_str else datetime.now(timezone.utc)
            snapshots.append(
                SnapshotInfo(
                    id=SnapshotId(snap_record.id),
                    name=SnapshotName(snap_record.name),
                    created_at=created_at,
                    size_bytes=None,
                    recency_idx=idx,
                )
            )

        return snapshots

    def delete_snapshot(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId,
    ) -> None:
        """Delete a snapshot from a Modal sandbox.

        Removes the snapshot metadata from the Modal Volume. Note that the
        underlying Modal image is not deleted since Modal doesn't yet provide
        a way to delete images via their API; they will be garbage-collected
        by Modal when no longer referenced.
        """
        host_id = host.id if isinstance(host, HostInterface) else host

        with log_span("Deleting snapshot from Modal sandbox", snapshot_id=str(snapshot_id), host_id=str(host_id)):
            # Read host record from volume
            host_record = self._read_host_record(host_id, use_cache=False)
            if host_record is None:
                raise HostNotFoundError(host_id)

            # Find and remove the snapshot
            snapshot_id_str = str(snapshot_id)
            updated_snapshots = [s for s in host_record.certified_host_data.snapshots if s.id != snapshot_id_str]

            if len(updated_snapshots) == len(host_record.certified_host_data.snapshots):
                raise SnapshotNotFoundError(snapshot_id)

            # Update host record on volume
            updated_certified_data = host_record.certified_host_data.model_copy_update(
                to_update(host_record.certified_host_data.field_ref().snapshots, updated_snapshots),
            )
            self.get_host(host_id).set_certified_data(updated_certified_data)

        logger.info("Deleted snapshot", snapshot_id=str(snapshot_id))

    # =========================================================================
    # Volume Methods
    # =========================================================================

    @staticmethod
    def _volume_id_for_name(modal_volume_name: str) -> VolumeId:
        """Derive a deterministic VolumeId from a Modal volume name.

        Uses uuid5 with a fixed namespace to produce a valid VolumeId
        (vol-<32hex>) from any Modal volume name string.
        """
        derived = uuid.uuid5(_MODAL_VOLUME_ID_NAMESPACE, modal_volume_name)
        return VolumeId(f"vol-{derived.hex}")

    @handle_modal_auth_error
    def list_volumes(self) -> list[VolumeInfo]:
        """List all mng-managed host volumes on Modal.

        Returns volumes whose names start with this instance's host volume prefix.
        """
        prefix = self._host_volume_prefix
        results: list[VolumeInfo] = []
        for modal_vol in modal.Volume.objects.list(environment_name=self.environment_name):
            vol_name = modal_vol.name
            if vol_name is not None and vol_name.startswith(prefix):
                host_hex = vol_name[len(prefix) :]
                host_id = None
                try:
                    host_id = HostId(f"host-{host_hex}")
                except ValueError:
                    pass
                results.append(
                    VolumeInfo(
                        volume_id=self._volume_id_for_name(vol_name),
                        name=vol_name,
                        size_bytes=0,
                        host_id=host_id,
                    )
                )
        return results

    @handle_modal_auth_error
    def delete_volume(self, volume_id: VolumeId) -> None:
        """Delete a Modal host volume.

        Finds the Modal volume whose derived VolumeId matches, then deletes
        it by its Modal name.
        """
        for modal_vol in modal.Volume.objects.list(environment_name=self.environment_name):
            vol_name = modal_vol.name
            if vol_name is not None and self._volume_id_for_name(vol_name) == volume_id:
                try:
                    modal.Volume.objects.delete(vol_name, environment_name=self.environment_name)
                    logger.debug("Deleted Modal volume: {}", vol_name)
                except NotFoundError:
                    pass
                return
        raise MngError(f"Volume {volume_id} not found")

    # =========================================================================
    # Host Mutation Methods
    # =========================================================================

    def get_host_tags(
        self,
        host: HostInterface | HostId,
    ) -> dict[str, str]:
        """Get user-defined tags for a host (excludes internal mng tags)."""
        host_id = host.id if isinstance(host, HostInterface) else host

        # try getting live sandbox tags
        sandbox = self._find_sandbox_by_host_id(host_id)
        if sandbox is not None:
            tags = sandbox.get_tags()
            user_tags: dict[str, str] = {}
            for key, value in tags.items():
                if key.startswith(TAG_USER_PREFIX):
                    user_key = key[len(TAG_USER_PREFIX) :]
                    user_tags[user_key] = value
            return user_tags

        # Try to read from volume (maybe it's offline)
        host_record = self._read_host_record(host_id)
        if host_record is not None:
            return dict(host_record.certified_host_data.user_tags)

        raise HostNotFoundError(host_id)

    def set_host_tags(
        self,
        host: HostInterface | HostId,
        tags: Mapping[str, str],
    ) -> None:
        """Replace all user-defined tags on a host.

        Updates both sandbox tags (for quick access) and volume (for persistence).
        """
        host_id = host.id if isinstance(host, HostInterface) else host

        # Update sandbox tags if sandbox is running
        sandbox = self._find_sandbox_by_host_id(host_id)
        if sandbox is not None:
            current_tags = sandbox.get_tags()
            new_tags: dict[str, str] = {}
            for key, value in current_tags.items():
                if not key.startswith(TAG_USER_PREFIX):
                    new_tags[key] = value
            for key, value in tags.items():
                new_tags[TAG_USER_PREFIX + key] = value
            sandbox.set_tags(new_tags)

        # Update volume record
        host_obj = self.get_host(host_id)
        certified_data = host_obj.get_certified_data()
        updated_certified_data = certified_data.model_copy_update(
            to_update(certified_data.field_ref().user_tags, dict(tags)),
        )
        host_obj.set_certified_data(updated_certified_data)

    def add_tags_to_host(
        self,
        host: HostInterface | HostId,
        tags: Mapping[str, str],
    ) -> None:
        """Add or update tags on a host.

        Updates both sandbox tags (for quick access) and volume (for persistence).
        """
        host_id = host.id if isinstance(host, HostInterface) else host

        # Update sandbox tags if sandbox is running
        sandbox = self._find_sandbox_by_host_id(host_id)
        if sandbox is not None:
            current_tags = sandbox.get_tags()
            new_tags = dict(current_tags)
            for key, value in tags.items():
                new_tags[TAG_USER_PREFIX + key] = value
            sandbox.set_tags(new_tags)

        # Update volume record
        host_obj = self.get_host(host_id)
        certified_data = host_obj.get_certified_data()
        merged_tags = dict(certified_data.user_tags)
        merged_tags.update(tags)
        updated_certified_data = certified_data.model_copy_update(
            to_update(certified_data.field_ref().user_tags, merged_tags),
        )
        host_obj.set_certified_data(updated_certified_data)

    def remove_tags_from_host(
        self,
        host: HostInterface | HostId,
        keys: Sequence[str],
    ) -> None:
        """Remove tags from a host by key.

        Updates both sandbox tags (for quick access) and volume (for persistence).
        """
        host_id = host.id if isinstance(host, HostInterface) else host

        # Update sandbox tags if sandbox is running
        sandbox = self._find_sandbox_by_host_id(host_id)
        if sandbox is not None:
            current_tags = sandbox.get_tags()
            new_tags: dict[str, str] = {}
            keys_to_remove = {TAG_USER_PREFIX + k for k in keys}
            for key, value in current_tags.items():
                if key not in keys_to_remove:
                    new_tags[key] = value
            sandbox.set_tags(new_tags)

        # Update volume record
        host_obj = self.get_host(host_id)
        certified_data = host_obj.get_certified_data()
        updated_tags = {k: v for k, v in certified_data.user_tags.items() if k not in keys}
        updated_certified_data = certified_data.model_copy_update(
            to_update(certified_data.field_ref().user_tags, updated_tags),
        )
        host_obj.set_certified_data(updated_certified_data)

    def rename_host(
        self,
        host: HostInterface | HostId,
        name: HostName,
    ) -> HostInterface:
        """Rename a host.

        Updates both sandbox tags (for quick access) and volume (for persistence).
        """
        host_id = host.id if isinstance(host, HostInterface) else host

        # Update sandbox tags if sandbox is running
        sandbox = self._find_sandbox_by_host_id(host_id)
        if sandbox is not None:
            current_tags = sandbox.get_tags()
            current_tags[TAG_HOST_NAME] = str(name)
            sandbox.set_tags(current_tags)

        # Update volume record
        host_obj = self.get_host(host_id)
        certified_data = host_obj.get_certified_data()
        updated_certified_data = certified_data.model_copy_update(
            to_update(certified_data.field_ref().host_name, str(name)),
        )
        host_obj.set_certified_data(updated_certified_data)

        return host_obj

    # =========================================================================
    # Connector Method
    # =========================================================================

    def get_connector(
        self,
        host: HostInterface | HostId,
    ) -> PyinfraHost:
        """Get a pyinfra connector for the host."""
        host_id = host.id if isinstance(host, HostInterface) else host

        # Read host record from volume
        host_record = self._read_host_record(host_id)
        if host_record is None:
            raise HostNotFoundError(host_id)

        # Failed hosts don't have SSH info and can't be connected to
        if host_record.ssh_host is None or host_record.ssh_port is None or host_record.ssh_host_public_key is None:
            raise MngError(f"Cannot get connector for host {host_id}: host has no SSH info (likely a failed host)")

        # Add the host key to known_hosts so SSH connections will work
        add_host_to_known_hosts(
            self._known_hosts_path,
            host_record.ssh_host,
            host_record.ssh_port,
            host_record.ssh_host_public_key,
        )

        private_key_path, _ = self._get_ssh_keypair()
        return self._create_pyinfra_host(
            host_record.ssh_host,
            host_record.ssh_port,
            private_key_path,
        )

    # =========================================================================
    # Lifecycle Methods
    # =========================================================================

    def close(self) -> None:
        """Clean up the Modal app context.

        Exits the app.run() context manager if one was created for this app_name.
        This makes the app ephemeral and prevents accumulation.
        """
        self.modal_app.close()


def _build_modal_secrets_from_env(
    env_var_names: Sequence[str],
) -> list[modal.Secret]:
    """Build Modal secrets from environment variable names.

    Reads the values of the specified environment variables from the current
    environment and creates a Modal secret containing them. This allows
    Dockerfiles to access secrets during build via --mount=type=secret.

    Raises MngError if any specified environment variable is not set.
    """
    if not env_var_names:
        return []

    secret_dict: dict[str, str | None] = {}
    missing_vars: list[str] = []

    for var_name in env_var_names:
        value = os.environ.get(var_name)
        if value is None:
            missing_vars.append(var_name)
        else:
            secret_dict[var_name] = value

    if missing_vars:
        raise MngError(
            f"Environment variable(s) not set for secrets: {', '.join(missing_vars)}. "
            "Set these environment variables before building."
        )

    with log_span("Creating Modal secrets from environment variables", count=len(secret_dict)):
        return [modal.Secret.from_dict(secret_dict)]


@pure
def _substitute_dockerfile_build_args(dockerfile_contents: str, build_args: Sequence[str]) -> str:
    """Substitute Docker build arg defaults in Dockerfile contents.

    Parses KEY=VALUE pairs from build_args and replaces the default values of
    matching ARG instructions in the Dockerfile. For example, if build_args
    contains 'CLAUDE_CODE_VERSION=2.1.50', then 'ARG CLAUDE_CODE_VERSION=""'
    becomes 'ARG CLAUDE_CODE_VERSION="2.1.50"'.

    Raises MngError if a build arg is not in KEY=VALUE format or if the ARG
    is not found in the Dockerfile.
    """
    result = dockerfile_contents
    for arg_spec in build_args:
        if "=" not in arg_spec:
            raise MngError(f"Docker build arg must be in KEY=VALUE format, got: {arg_spec}")
        key, value = arg_spec.split("=", 1)
        # Replace ARG <key>=<anything> or ARG <key> (no default) with ARG <key>="<value>"
        # Use a lambda replacement to avoid re.sub interpreting backslash sequences in value.
        # Bind value via default arg to avoid B023 (closure over loop variable).
        new_result = re.sub(
            rf"^(ARG\s+{re.escape(key)})\b.*$",
            lambda m, v=value: f'{m.group(1)}="{v}"',
            result,
            flags=re.MULTILINE,
        )
        if new_result == result:
            raise MngError(
                f"Docker build arg {key!r} not found as an ARG instruction in the Dockerfile. "
                "Ensure the Dockerfile contains a matching ARG instruction."
            )
        result = new_result
    return result


def _build_image_from_dockerfile_contents(
    dockerfile_contents: str,
    # build context directory for COPY/ADD instructions
    context_dir: Path | None = None,
    # starting image; if not provided, uses FROM instruction in the dockerfile
    initial_image: modal.Image | None = None,
    # if True, apply each instruction separately for per-layer caching; if False, apply
    # all instructions at once (faster but no intermediate caching on failure)
    is_each_layer_cached: bool = True,
    # Modal secrets to make available during Dockerfile RUN commands
    secrets: Sequence[modal.Secret] = (),
) -> modal.Image:
    """Build a Modal image from Dockerfile contents with optional per-layer caching.

    When is_each_layer_cached=True (the default), each instruction is applied separately,
    allowing Modal to cache intermediate layers. This means if a build fails at step N,
    steps 1 through N-1 don't need to be re-run. Multistage dockerfiles are not supported.

    Secrets are passed to dockerfile_commands and are available during RUN commands
    via --mount=type=secret,id=<env_var_name>.
    """
    # DockerfileParser writes to a file, so use a temp directory to avoid conflicts
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpfile = Path(tmpdir) / "Dockerfile"
        dfp = DockerfileParser(str(tmpfile))
        dfp.content = dockerfile_contents

        assert not dfp.is_multistage, "Multistage Dockerfiles are not supported yet"

        last_from_index = None
        for i, instr in enumerate(dfp.structure):
            if instr["instruction"] == "FROM":
                last_from_index = i

        if initial_image is None:
            assert last_from_index is not None, "Dockerfile must have a FROM instruction"
            instructions = dfp.structure[last_from_index + 1 :]
            modal_image = modal.Image.from_registry(dfp.baseimage)
        else:
            assert last_from_index is None, "If initial_image is provided, Dockerfile cannot have a FROM instruction"
            instructions = list(dfp.structure)
            modal_image = initial_image

        if len(instructions) > 0:
            secrets_list = list(secrets)
            expanded_context_dir = context_dir.expanduser() if context_dir is not None else None
            if is_each_layer_cached:
                for instr in instructions:
                    if instr["instruction"] == "COMMENT":
                        continue
                    modal_image = modal_image.dockerfile_commands(
                        [instr["content"]],
                        context_dir=expanded_context_dir,
                        secrets=secrets_list,
                    )
            else:
                # The downside of doing them all at once is that if any one fails,
                # Modal will re-run all of them
                modal_image = modal_image.dockerfile_commands(
                    [x["content"] for x in instructions if x["instruction"] != "COMMENT"],
                    context_dir=expanded_context_dir,
                    secrets=secrets_list,
                )

        return modal_image
