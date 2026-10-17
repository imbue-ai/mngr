from enum import auto
from pathlib import Path
from typing import Final
from typing import Self
from typing import assert_never

from pydantic import Field
from pydantic import model_validator

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import HostId
from imbue.mngr.providers.ssh_utils import per_host_key_dir

# The per-host subdir of the providers that key a host's dir by its full id
# (``hosts/<host id>/``); the others use ``per_host_key_dir``'s ``host_keys/<uuid hex>/``.
PER_HOST_HOSTS_SUBDIR: Final[str] = "hosts"


class HostAccessManifestError(MngrError, ValueError):
    """Raised when a provider's manifest names a key or store it does not carry."""


class PerHostDirScheme(UpperCaseStrEnum):
    """How a provider names a host's own dir under its key dir."""

    # ``host_keys/<host uuid hex>/`` (``per_host_key_dir``): the vps, modal and docker providers.
    HOST_KEYS_UUID_HEX = auto()
    # ``hosts/<host id>/``: the imbue_cloud and lima providers.
    HOSTS_HOST_ID = auto()


class HostAccessManifest(FrozenModel):
    """The per-host access material a provider keeps under one of its instance dirs.

    A provider publishes one so a consumer that carries a host's access to
    another machine collects and lays down exactly the provider's own files
    and pins, without restating the layout. It describes the provider's
    default placement; a provider with a second placement (the vps providers'
    bare placement) links its per-host dir differently and does not publish
    that variant.
    """

    key_dir_name: str = Field(
        description="The key dir under the provider instance dir; '' when the per-host dirs sit directly under it"
    )
    per_host_dir_scheme: PerHostDirScheme = Field(description="How the host's own dir is named under the key dir")
    file_names: tuple[str, ...] = Field(
        description="The files in a host's own dir: each private key and its .pub half, host keypairs included"
    )
    agent_key_file_name: str = Field(
        description="The private client key that opens the agent endpoint under the provider's default placement, "
        "always minted per host"
    )
    pin_store_names: tuple[str, ...] = Field(
        description="The known_hosts stores holding the host's pins: provider-wide files under the key dir, "
        "or files in the host's own dir"
    )
    is_pin_store_per_host: bool = Field(
        description="Whether the pin stores are per-host files (read whole) rather than provider-wide stores "
        "(read by the host's record)"
    )
    known_hosts_link_store_name: str | None = Field(
        description="The provider-wide store the per-host dir's known_hosts symlink points at under the provider's "
        "default placement, or None when the layout has no link"
    )

    @model_validator(mode="after")
    def _check_names_are_carried(self) -> Self:
        if self.agent_key_file_name not in self.file_names:
            raise HostAccessManifestError(f"agent key {self.agent_key_file_name!r} is not among the per-host files")
        if self.known_hosts_link_store_name is not None and (
            self.is_pin_store_per_host or self.known_hosts_link_store_name not in self.pin_store_names
        ):
            raise HostAccessManifestError(
                f"known_hosts link target {self.known_hosts_link_store_name!r} is not a provider-wide pin store"
            )
        return self


@pure
def keypair_file_names(*private_key_names: str) -> tuple[str, ...]:
    """Each private key name followed by its ``.pub`` half, in the order given."""
    return tuple(
        name for private_key_name in private_key_names for name in (private_key_name, f"{private_key_name}.pub")
    )


@pure
def host_access_key_dir(instance_dir: Path, manifest: HostAccessManifest) -> Path:
    """The key dir of a provider instance whose dir is ``instance_dir``."""
    return instance_dir / manifest.key_dir_name if manifest.key_dir_name else instance_dir


@pure
def host_access_per_host_dir(instance_dir: Path, manifest: HostAccessManifest, host_id: HostId) -> Path:
    """The dir holding ``host_id``'s own access material under the instance at ``instance_dir``."""
    key_dir = host_access_key_dir(instance_dir, manifest)
    match manifest.per_host_dir_scheme:
        case PerHostDirScheme.HOST_KEYS_UUID_HEX:
            return per_host_key_dir(key_dir, host_id)
        case PerHostDirScheme.HOSTS_HOST_ID:
            return key_dir / PER_HOST_HOSTS_SUBDIR / str(host_id)
        case _ as unreachable:
            assert_never(unreachable)


@pure
def host_access_pin_store_paths(instance_dir: Path, manifest: HostAccessManifest, host_id: HostId) -> dict[str, Path]:
    """Store name -> the known_hosts path holding ``host_id``'s pins under the instance at ``instance_dir``."""
    store_dir = (
        host_access_per_host_dir(instance_dir, manifest, host_id)
        if manifest.is_pin_store_per_host
        else host_access_key_dir(instance_dir, manifest)
    )
    return {store_name: store_dir / store_name for store_name in manifest.pin_store_names}
