"""The collector and materializer of the key material that grants access to one host, per backend layout.

Every mngr provider keeps the material that opens a host somewhere under its
instance's state dir and publishes the shape as a ``HostAccessManifest``
(see ``specs/account-owned-providers/spec.md`` section 6.4). A workspace
record's encrypted secrets carry that material as a :class:`HostAccessMaterial`
so any device that unlocks the account can lay the same files down under its
own copy of the provider instance and reach the host. Only per-host material
is ever collected: a provider-wide client key would open every host of that
provider on the device that minted it.
"""

import stat
from collections.abc import Mapping
from enum import auto
from pathlib import Path
from typing import Final

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.connections.primitives import ConnectionBackend
from imbue.minds.desktop_client.connections.primitives import IMBUE_CLOUD_PROVIDER_NAME_PREFIX
from imbue.minds.desktop_client.connections.primitives import backend_for_connection_instance_name
from imbue.minds.desktop_client.connections.primitives import mngr_backend_name
from imbue.mngr.primitives import HostId
from imbue.mngr.providers.host_access_manifest import HostAccessManifest
from imbue.mngr.providers.host_access_manifest import host_access_per_host_dir
from imbue.mngr.providers.host_access_manifest import host_access_pin_store_paths
from imbue.mngr.providers.host_key_store import HostKeyOrigin
from imbue.mngr.providers.host_key_store import has_unpinned_bootstrap_drift
from imbue.mngr.providers.host_key_store import load_current_host_key_pins
from imbue.mngr.providers.host_key_store import load_host_key_record
from imbue.mngr.providers.host_key_store import pin_known_hosts_text
from imbue.mngr.providers.host_key_store import render_pins_as_known_hosts_text
from imbue.mngr.providers.ssh_utils import ensure_per_host_known_hosts_link
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr_docker.constants import DOCKER_BACKEND_NAME
from imbue.mngr_docker.constants import DOCKER_HOST_ACCESS_MANIFEST
from imbue.mngr_imbue_cloud.primitives import IMBUE_CLOUD_BACKEND_NAME
from imbue.mngr_imbue_cloud.primitives import IMBUE_CLOUD_HOST_ACCESS_MANIFEST
from imbue.mngr_lima.constants import LIMA_BACKEND_NAME
from imbue.mngr_lima.constants import LIMA_HOST_ACCESS_MANIFEST
from imbue.mngr_modal.constants import MODAL_HOST_ACCESS_MANIFEST
from imbue.mngr_vps.primitives import VPS_HOST_ACCESS_MANIFEST

# The device providers that are ordinary providers for every account: their
# instance name is the backend name, and their per-host material rides the
# record so a cross-device connection can be added later.
DOCKER_PROVIDER_NAME: Final[str] = str(DOCKER_BACKEND_NAME)
LIMA_PROVIDER_NAME: Final[str] = str(LIMA_BACKEND_NAME)
VULTR_PROVIDER_NAME: Final[str] = "vultr"

_PUBLIC_KEY_SUFFIX: Final[str] = ".pub"


class HostAccessLayout(LowerCaseStrEnum):
    """Which provider family's key layout a host's access material follows (the lowercase values are the wire strings)."""

    IMBUE_CLOUD = auto()
    VPS = auto()
    MODAL = auto()
    LIMA = auto()
    DOCKER = auto()


class HostAccessMaterial(FrozenModel):
    """The complete set of per-host files and host-key pins that grant access to one host."""

    # A newer client may add fields; this version must keep reading the rest.
    model_config = ConfigDict(extra="ignore")

    layout: str = Field(description="The HostAccessLayout the files and pins follow (unknown values are left alone)")
    files: dict[str, str] = Field(
        default_factory=dict, description="File name -> content, relative to the per-host dir"
    )
    pins: dict[str, str] = Field(
        default_factory=dict, description="Pin store name -> known_hosts text of this host's records in that store"
    )


# Each layout's shape comes from the provider that writes it; the vps manifest
# covers every mngr_vps backend (aws, gcp, azure, vultr).
_MANIFEST_BY_LAYOUT: Final[Mapping[HostAccessLayout, HostAccessManifest]] = {
    HostAccessLayout.IMBUE_CLOUD: IMBUE_CLOUD_HOST_ACCESS_MANIFEST,
    HostAccessLayout.VPS: VPS_HOST_ACCESS_MANIFEST,
    HostAccessLayout.MODAL: MODAL_HOST_ACCESS_MANIFEST,
    HostAccessLayout.LIMA: LIMA_HOST_ACCESS_MANIFEST,
    HostAccessLayout.DOCKER: DOCKER_HOST_ACCESS_MANIFEST,
}


class HostAccessPaths(FrozenModel):
    """Where one host's access material lives under a provider instance on this device."""

    layout: HostAccessLayout = Field(description="The layout the paths follow")
    host_id: HostId = Field(description="The host the material belongs to")
    per_host_dir: Path = Field(description="The host's own key dir")
    file_names: tuple[str, ...] = Field(description="The per-host file names the layout carries")
    agent_key_file_name: str = Field(
        description="The per-host private key whose presence and mtime drive the tile state (for vps the VPS-root key, "
        "always minted per host)"
    )
    pin_store_by_name: dict[str, Path] = Field(description="Pin store name -> the known_hosts path holding it")
    # Provider-wide stores hold every host's pins and are read by host record;
    # a per-host store holds only this host's and is read whole.
    is_pin_store_per_host: bool = Field(description="Whether the pin stores are per-host files")
    known_hosts_link_store_name: str | None = Field(
        description="The store the per-host dir's known_hosts symlink points at, or None when the layout has no link"
    )


@pure
def layout_for_provider_kind(provider_kind: str) -> HostAccessLayout | None:
    """The key layout of a provider instance name, or None for a provider whose material is not synced."""
    if provider_kind.startswith(IMBUE_CLOUD_PROVIDER_NAME_PREFIX):
        return HostAccessLayout.IMBUE_CLOUD
    connection_backend = backend_for_connection_instance_name(provider_kind)
    if connection_backend is ConnectionBackend.MODAL:
        return HostAccessLayout.MODAL
    if connection_backend is not None or provider_kind == VULTR_PROVIDER_NAME:
        return HostAccessLayout.VPS
    if provider_kind == LIMA_PROVIDER_NAME:
        return HostAccessLayout.LIMA
    if provider_kind == DOCKER_PROVIDER_NAME:
        return HostAccessLayout.DOCKER
    return None


@pure
def _backend_name_for_provider_kind(provider_kind: str, layout: HostAccessLayout) -> str:
    """The backend segment of the provider's state dir."""
    match layout:
        case HostAccessLayout.IMBUE_CLOUD:
            return IMBUE_CLOUD_BACKEND_NAME
        case HostAccessLayout.MODAL:
            return mngr_backend_name(ConnectionBackend.MODAL)
        case HostAccessLayout.LIMA:
            return LIMA_PROVIDER_NAME
        case HostAccessLayout.DOCKER:
            return DOCKER_PROVIDER_NAME
        case HostAccessLayout.VPS:
            connection_backend = backend_for_connection_instance_name(provider_kind)
            return mngr_backend_name(connection_backend) if connection_backend is not None else provider_kind


@pure
def host_access_paths(profile_dir: Path, instance_name: str, host_id: HostId) -> HostAccessPaths | None:
    """The paths of ``host_id``'s access material under ``instance_name`` in this profile, or None for an unsynced provider."""
    layout = layout_for_provider_kind(instance_name)
    if layout is None:
        return None
    manifest = _MANIFEST_BY_LAYOUT[layout]
    instance_dir = profile_dir / "providers" / _backend_name_for_provider_kind(instance_name, layout) / instance_name
    return HostAccessPaths(
        layout=layout,
        host_id=host_id,
        per_host_dir=host_access_per_host_dir(instance_dir, manifest, host_id),
        file_names=manifest.file_names,
        agent_key_file_name=manifest.agent_key_file_name,
        pin_store_by_name=host_access_pin_store_paths(instance_dir, manifest, host_id),
        is_pin_store_per_host=manifest.is_pin_store_per_host,
        known_hosts_link_store_name=manifest.known_hosts_link_store_name,
    )


def derive_openssh_public_key_line(private_key_text: str) -> str | None:
    """Derive the OpenSSH public-key line from a private key, or None when unparseable.

    The materializer must write ``ssh_key.pub`` alongside the private key:
    mngr's ``load_or_create_ssh_keypair`` regenerates the whole pair when
    either half is missing, which would silently clobber a materialized key.

    Both container formats found in mngr profiles are handled: traditional /
    PKCS#8 PEM (older mngr installs generated PEM RSA client keys,
    ``-----BEGIN RSA PRIVATE KEY-----``) and the OpenSSH format that current
    mngr Ed25519 client keys use (``-----BEGIN OPENSSH PRIVATE KEY-----``) --
    ``cryptography`` needs a different loader for each.
    """
    key_bytes = private_key_text.encode("utf-8")
    loader_errors: list[str] = []
    for load_private_key in (serialization.load_pem_private_key, serialization.load_ssh_private_key):
        try:
            private_key = load_private_key(key_bytes, password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm) as e:
            loader_errors.append(f"{load_private_key.__name__}: {e}")
            continue
        public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.OpenSSH, format=serialization.PublicFormat.OpenSSH
        )
        return public_bytes.decode("utf-8")
    logger.warning("Could not parse a synced SSH private key: {}", "; ".join(loader_errors))
    return None


def read_optional_text(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return path.read_text()
    except OSError as e:
        logger.warning("Could not read host access material at {}: {}", path, e)
        return None


def _render_host_pins(store_path: Path, host_id: HostId, is_per_host: bool) -> str | None:
    """This host's pins in one store as known_hosts text, or None when the store holds none for it."""
    if not store_path.exists():
        return None
    if is_per_host:
        pins = load_current_host_key_pins(store_path)
    else:
        record = load_host_key_record(store_path, host_id)
        pins = record.pins if record is not None else ()
    return render_pins_as_known_hosts_text(pins) if pins else None


def collect_host_access_material(paths: HostAccessPaths) -> HostAccessMaterial | None:
    """Read the per-host files and this host's pin records; None when this device holds nothing for the host."""
    files: dict[str, str] = {}
    for file_name in paths.file_names:
        path = paths.per_host_dir / file_name
        if path.is_symlink():
            # mngr_lima heals a legacy host by linking its per-host root key
            # slots to the provider-wide key, which must never ride a record.
            logger.debug("Not collecting {}: it links outside the host's own material", path)
            continue
        content = read_optional_text(path)
        if content is not None:
            files[file_name] = content
    # A missing public half is derived so every device sees the same
    # material (the materializer writes both halves).
    for file_name in paths.file_names:
        if file_name not in files:
            derived = _file_content_or_derived_public_half(files, file_name)
            if derived is not None:
                files[file_name] = derived
    pins: dict[str, str] = {}
    for store_name, store_path in paths.pin_store_by_name.items():
        rendered = _render_host_pins(store_path, paths.host_id, paths.is_pin_store_per_host)
        if rendered is not None:
            pins[store_name] = rendered
    if not files and not pins:
        return None
    return HostAccessMaterial(layout=paths.layout.value, files=files, pins=pins)


def _write_secret_text_file(path: Path, content: str, is_public: bool) -> None:
    atomic_write(path, content)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | (stat.S_IRGRP | stat.S_IROTH if is_public else 0))


def _file_content_or_derived_public_half(files: Mapping[str, str], file_name: str) -> str | None:
    """The content of one per-host file, deriving a missing ``.pub`` from its private half.

    mngr's load-or-create helpers regenerate a whole keypair when either half
    is missing, which would clobber a materialized private key.
    """
    content = files.get(file_name)
    if content is not None or not file_name.endswith(_PUBLIC_KEY_SUFFIX):
        return content
    private_key_text = files.get(file_name.removesuffix(_PUBLIC_KEY_SUFFIX))
    return derive_openssh_public_key_line(private_key_text) if private_key_text is not None else None


def is_host_access_material_missing_locally(paths: HostAccessPaths, material: HostAccessMaterial) -> bool:
    """Whether any file or pin the material carries is absent from (or unpinned in) this device's stores.

    The escape hatch that re-opens the revision gate: a swept and re-created
    host dir, or a store whose endpoints still hold only bootstrap pins.
    """
    for file_name in paths.file_names:
        if _file_content_or_derived_public_half(material.files, file_name) is None:
            continue
        if not (paths.per_host_dir / file_name).is_file():
            return True
    for store_name, known_hosts_text in material.pins.items():
        store_path = paths.pin_store_by_name.get(store_name)
        if store_path is None:
            continue
        if not store_path.exists() or has_unpinned_bootstrap_drift(store_path, known_hosts_text):
            return True
    return False


def materialize_host_access_material(paths: HostAccessPaths, material: HostAccessMaterial) -> bool:
    """Lay the material down under this device's provider instance; returns whether anything changed.

    Files are compare-and-write (0600, ``.pub`` files 0644); pins go through
    the host-key store as user-origin material attributed to the host, so a
    stale bootstrap pin can never shadow the live key. Raises ``OSError`` (or
    an ``MngrError`` from the store) on failure; the caller records it.
    """
    is_changed = False
    for file_name in paths.file_names:
        content = _file_content_or_derived_public_half(material.files, file_name)
        if content is None:
            continue
        target = paths.per_host_dir / file_name
        if target.is_symlink():
            logger.warning("Not materializing {}: it links outside the host's own material", target)
            continue
        if read_optional_text(target) == content:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_secret_text_file(target, content, is_public=file_name.endswith(_PUBLIC_KEY_SUFFIX))
        is_changed = True
    for store_name, known_hosts_text in material.pins.items():
        store_path = paths.pin_store_by_name.get(store_name)
        if store_path is None:
            logger.warning("Ignoring pins for unknown store {!r} in a {} layout", store_name, paths.layout.value)
            continue
        content_before = read_optional_text(store_path)
        pin_known_hosts_text(store_path, known_hosts_text, host_id=paths.host_id, origin=HostKeyOrigin.USER)
        is_changed = read_optional_text(store_path) != content_before or is_changed
    if paths.known_hosts_link_store_name is not None:
        ensure_per_host_known_hosts_link(
            paths.per_host_dir.parent.parent, paths.host_id, paths.known_hosts_link_store_name
        )
    return is_changed
