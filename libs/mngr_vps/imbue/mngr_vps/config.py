from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Self

from pydantic import Field
from pydantic import model_validator

from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.config.data_types import ScalarStrTuple
from imbue.mngr.config.data_types import ScalarTuple
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import DockerBuilder
from imbue.mngr.primitives import IdleMode
from imbue.mngr_vps.errors import VpsConfigError
from imbue.mngr_vps.primitives import IsolationMode


class VpsProviderConfig(ProviderInstanceConfig):
    """Base configuration for VPS providers (container or bare placement)."""

    isolation: IsolationMode = Field(
        default=IsolationMode.CONTAINER,
        description=(
            "How the agent is isolated on its VPS. CONTAINER (the default) runs the agent in a "
            "Docker container; NONE runs it directly on the VPS OS. Selects the realizer the "
            "provider uses; the default preserves the original container behavior."
        ),
    )
    host_dir: Path = Field(
        default=Path("/mngr"),
        description=(
            "Base directory for mngr data on the agent host. With container isolation this is the "
            "path inside the container; with bare isolation it is the path on the VM's OS."
        ),
    )
    volume_home_path: Path | None = Field(
        default=None,
        description=(
            "Container path (e.g. /home/user) symlinked onto the unified host volume's home/ "
            "subdirectory, so the whole tree persists on the volume while mngr data lives in a "
            "subdirectory (host_dir must then be a path strictly inside it, e.g. "
            "/home/user/.mngr). When unset, host_dir symlinks to the volume's host_dir/ "
            "subdirectory exactly as before. Only used by the container (docker) isolation mode."
        ),
    )
    default_image: str = Field(
        default="debian:bookworm-slim",
        description="Default Docker image",
    )
    default_idle_timeout: int = Field(
        default=800,
        description="Idle timeout in seconds",
    )
    default_idle_mode: IdleMode = Field(
        default=IdleMode.IO,
        description="Idle detection mode",
    )
    default_activity_sources: tuple[ActivitySource, ...] = Field(
        default_factory=lambda: tuple(ActivitySource),
        description="Default activity sources",
    )
    ssh_connect_timeout: float = Field(
        default=60.0,
        description="SSH connection timeout in seconds",
    )
    instance_boot_timeout: float = Field(
        default=300.0,
        description="Timeout for the cloud instance to become reachable, in seconds",
    )
    docker_install_timeout: float = Field(
        default=300.0,
        description="Docker installation timeout in seconds",
    )
    container_ssh_port: int = Field(
        default=2222,
        description="Container sshd port exposed on VPS",
    )
    default_region: str = Field(
        default="ewr",
        description="Default cloud region (provider subclasses override the default)",
    )
    default_start_args: tuple[str, ...] = Field(
        default=(),
        description="Default `docker run` arguments",
    )
    auto_shutdown_seconds: int | None = Field(
        default=None,
        description=(
            "When set, the host OS halts itself after about this many seconds (rounded up to "
            "whole minutes, the granularity `shutdown` accepts) -- a hard max-lifetime cap, "
            "distinct from the activity-based default_idle_timeout. Whether the halt stops, "
            "terminates, or deletes the instance is provider-specific (see the provider's README)."
        ),
    )
    docker_runtime: str | None = Field(
        default=None,
        description=(
            "Container runtime to pass to `docker run --runtime` (e.g. 'runsc' for gVisor). "
            "When None (the default), no `--runtime` flag is added and the VPS Docker daemon uses "
            "its configured default. The named runtime must be installed and registered on the VPS "
            "(see `install_gvisor_runtime`), otherwise container creation fails with Docker's native "
            "'unknown runtime' error. Override via MNGR__PROVIDERS__<NAME>__DOCKER_RUNTIME."
        ),
    )
    install_gvisor_runtime: bool = Field(
        default=False,
        description=(
            "When True, VPS provisioning installs and registers the gVisor `runsc` runtime with the "
            "Docker daemon (idempotent; a no-op when runsc is already present, e.g. baked into the "
            "image). This only installs the runtime -- set `docker_runtime='runsc'` to actually run "
            "containers under it."
        ),
    )
    builder: DockerBuilder = Field(
        default=DockerBuilder.DOCKER,
        description=(
            "Image builder used on the VPS. DOCKER (default) runs native `docker build` over SSH. "
            "DEPOT runs `depot build --load` over SSH, auto-installs the depot CLI on the VPS the "
            "first time, and requires DEPOT_TOKEN in the agent's environment (DEPOT_PROJECT_ID "
            "optional, only forwarded when set)."
        ),
    )
    btrfs_mount_path: Path = Field(
        default=Path("/mngr-btrfs"),
        description=(
            "Path on the outer where the loop-mounted btrfs filesystem holding the per-host "
            "unified docker volume is mounted. The per-host subvolume lives at "
            "``<btrfs_mount_path>/<host_id_hex>`` and is bound into the agent container via "
            "``docker volume create --opt device=...``."
        ),
    )
    btrfs_loop_file_path: Path = Field(
        default=Path("/var/lib/mngr-btrfs.img"),
        description=(
            "Path on the outer's root filesystem where the loop-backed btrfs image file is "
            "stored. Allocated with ``fallocate`` and mounted via an ``/etc/fstab`` entry so "
            "it survives VPS reboots."
        ),
    )
    outer_disk_reserved_gb: int = Field(
        default=20,
        description=(
            "Gigabytes of free space on the outer's root filesystem to hold back from the "
            "btrfs loop file at provisioning time. Loop file size is computed as "
            "``free_gb - outer_disk_reserved_gb``; ``VpsProvisioningError`` is raised when "
            "the result is not positive."
        ),
    )

    @model_validator(mode="after")
    def _validate_volume_home_path_contains_host_dir(self) -> Self:
        if self.volume_home_path is None:
            return self
        if not self.host_dir.is_relative_to(self.volume_home_path) or self.host_dir == self.volume_home_path:
            raise VpsConfigError(
                f"host_dir ({self.host_dir}) must be a path strictly inside "
                f"volume_home_path ({self.volume_home_path}) so mngr data persists on the volume"
            )
        return self


class OfflineCapableVpsProviderConfig(VpsProviderConfig):
    """Config base for cloud VPS providers with a managed SSH-ingress rule.

    Carries the root-disk size and the SSH-ingress allow-list shared by the AWS /
    Azure / GCP providers, each of which threads them into its VM create and into
    the security group / NSG / firewall rule that ``mngr <cloud> prepare``
    creates. ``associate_public_ip`` (whether to give the instance a public IP)
    is *not* lifted here because GCP names its equivalent field
    ``associate_external_ip``; see ``PublicIpVpsProviderConfig`` for the
    AWS/Azure-only field.
    """

    root_disk_size_gb: int = Field(
        default=30,
        description=(
            "Size of the VM's root disk in GB (the EBS root volume on AWS, the boot disk on GCP, "
            "the OS managed disk on Azure). With container isolation the agent's data lives on a "
            "loop file carved from this disk minus `outer_disk_reserved_gb`."
        ),
    )
    allowed_ssh_cidrs: ScalarStrTuple = Field(
        default=ScalarTuple(("0.0.0.0/0",)),
        description=(
            "Inbound CIDR blocks allowed on tcp/22 and the container SSH port in the security "
            "group / NSG / firewall rule the provider's `prepare` command creates. Default "
            "('0.0.0.0/0',) allows any IP; use e.g. ('203.0.113.4/32',) to restrict to your own, "
            "or () for no ingress (no rule is created, so the instance is unreachable from outside "
            "its network). A warning is logged when the effective range is 0.0.0.0/0 or empty. "
            "Replaced, not merged, across config layers."
        ),
    )


# CLEANUP: drop ``fold_legacy_root_disk_size_key`` and the three deprecated
# per-cloud disk fields (``root_volume_size_gb`` / ``boot_disk_size_gb`` /
# ``os_disk_size_gb``) once no settings file names them any more.
def fold_legacy_root_disk_size_key(raw_config: Any, legacy_key: str) -> Any:
    """Rewrite a deprecated per-cloud disk key in a raw config mapping into ``root_disk_size_gb``.

    The three clouds used to name the knob differently; the legacy key is still
    accepted so existing settings keep working. The value moves onto the unified
    key (so only that field records as explicitly set) and the legacy key is
    dropped. Raises ``VpsConfigError`` when both are set to different values, since
    silently picking one would hide a misconfiguration. Anything that is not a
    mapping (an already-built model, for instance) passes through untouched.
    """
    if not isinstance(raw_config, Mapping):
        return raw_config
    legacy_value = raw_config.get(legacy_key)
    if legacy_value is None:
        return raw_config
    folded = {key: value for key, value in raw_config.items() if key != legacy_key}
    unified_value = folded.get("root_disk_size_gb")
    if unified_value is not None and unified_value != legacy_value:
        raise VpsConfigError(
            f"{legacy_key} ({legacy_value}) and root_disk_size_gb ({unified_value}) disagree; "
            f"{legacy_key} is a deprecated alias of root_disk_size_gb, so set only root_disk_size_gb"
        )
    folded["root_disk_size_gb"] = legacy_value
    return folded


class PublicIpVpsProviderConfig(OfflineCapableVpsProviderConfig):
    """Config base for the AWS / Azure providers (which share ``associate_public_ip``).

    GCP deliberately does not inherit this: its equivalent field is named
    ``associate_external_ip`` (GCE terminology), so collapsing the two would change
    the TOML key GCP accepts. GCP extends ``OfflineCapableVpsProviderConfig``
    directly and declares ``associate_external_ip`` itself.
    """

    associate_public_ip: bool = Field(
        default=True,
        description=(
            "Assign a public IPv4 address to the instance. Required for the current "
            "mngr-from-developer-laptop SSH access model. For a more secure deployment, set "
            "to False and run mngr from a bastion inside the network."
        ),
    )
