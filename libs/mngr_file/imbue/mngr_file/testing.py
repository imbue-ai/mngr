"""Test utilities for mngr-file: a stopped host reachable through the real CLI.

The local provider's host is always running, so the stopped-host paths of
``mngr file`` (volume-backed reads and writes, the work-directory refusal, the
unreachable-storage refusal) cannot be driven through it. The provider below is
a local provider over its own host directory that reports its host as stopped,
serving that directory as the host's persisted storage until the host is
started. It is registered as a backend and configured through a project
settings file, so CLI invocations discover and resolve it exactly as they would
a real provider.

The persisted storage comes in the kinds real providers have: a filesystem that
fails the way the OS does (as local and Docker volumes do), a storage service
that fails with errors of its own kind (as a Modal volume does), or none that
can be reached.
"""

import json
import shutil
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from enum import auto
from functools import cached_property
from io import BytesIO
from pathlib import Path
from typing import Any
from typing import assert_never
from uuid import uuid4

import pytest
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.config.provider_config_registry import _provider_config_registry
from imbue.mngr.config.provider_config_registry import register_provider_config
from imbue.mngr.errors import ConfigStructureError
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import ProviderEmptyError
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.hosts.offline_host import make_readable_offline_host
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.data_types import VolumeFile
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.interfaces.volume import BaseVolume
from imbue.mngr.interfaces.volume import HostVolume
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostNameStyle
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.providers.local.config import LocalProviderConfig
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.providers.local.instance import get_or_create_local_host_id
from imbue.mngr.providers.registry import _backend_registry
from imbue.mngr.utils.plugin_testing import PLACEHOLDER_AGENT_TYPE

STOPPED_HOST_BACKEND_NAME = ProviderBackendName("mngr-file-stopped-host")


class StoppedHostStorage(UpperCaseStrEnum):
    """What a stopped host's persisted storage is, and so how it fails."""

    FILESYSTEM = auto()
    SERVICE = auto()
    UNREACHABLE = auto()


class StorageServiceError(Exception):
    """An error in a storage service's own terms, deliberately neither an ``OSError`` nor a ``MngrError``."""


class StorageServiceNotFoundError(StorageServiceError):
    """The storage service holds nothing at the requested path."""


class StorageServiceVolume(BaseVolume):
    """A volume over a local directory that fails the way a storage service does.

    Mirrors ``ModalVolume`` over a Modal volume: a missing path raises
    ``StorageServiceNotFoundError`` and a path of the wrong kind raises
    ``StorageServiceError``, where a filesystem volume would return nothing or
    raise ``FileNotFoundError`` / ``IsADirectoryError``.
    """

    root_path: Path = Field(frozen=True, description="Local directory holding the service's files")

    def _resolve(self, path: str) -> Path:
        resolved = (self.root_path / path.lstrip("/")).resolve()
        if not resolved.is_relative_to(self.root_path.resolve()):
            raise StorageServiceError(f"Path escapes volume root: {path}")
        return resolved

    def listdir(self, path: str) -> list[VolumeFile]:
        target = self._resolve(path)
        if not target.exists():
            raise StorageServiceNotFoundError(f"Path not found: {path}")
        if not target.is_dir():
            raise StorageServiceError(f"Not a directory: {path}")
        root = self.root_path.resolve()
        return [
            VolumeFile(
                path=str(child.relative_to(root)),
                file_type=FileType.DIRECTORY if child.is_dir() else FileType.FILE,
                mtime=int(child.stat().st_mtime),
                size=child.stat().st_size if child.is_file() else 0,
            )
            for child in sorted(target.iterdir())
        ]

    def path_exists(self, path: str) -> bool:
        return self._resolve(path).exists()

    def read_file(self, path: str) -> bytes:
        target = self._resolve(path)
        if not target.exists():
            raise StorageServiceNotFoundError(f"File not found: {path}")
        if not target.is_file():
            raise StorageServiceError(f"Not a file: {path}")
        return target.read_bytes()

    def remove_file(self, path: str, *, recursive: bool = False) -> None:
        target = self._resolve(path)
        if not target.exists():
            raise StorageServiceNotFoundError(f"Path not found: {path}")
        if target.is_dir():
            if not recursive:
                raise StorageServiceError(f"Cannot remove directory without recursive=True: {path}")
            shutil.rmtree(target)
        else:
            target.unlink()

    def remove_directory(self, path: str) -> None:
        self.remove_file(path, recursive=True)

    def write_files(self, file_contents_by_path: Mapping[str, bytes]) -> None:
        for path, data in file_contents_by_path.items():
            target = self._resolve(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)


class InteractiveStdin(BytesIO):
    """An empty input stream that reports itself as a terminal.

    Pass it as ``input=`` to ``CliRunner.invoke`` to simulate a user who pipes
    nothing into a command: the runner's ``sys.stdin`` delegates ``isatty`` to it.
    """

    def isatty(self) -> bool:
        return True


class StoppedHostProviderConfig(LocalProviderConfig):
    """Configuration for the stopped-host test backend."""

    backend: ProviderBackendName = Field(
        default=STOPPED_HOST_BACKEND_NAME,
        description="Provider backend (always the stopped-host test backend)",
    )
    host_name: str | None = Field(default=None, description="Name the provider's single host is discovered under")
    storage: StoppedHostStorage = Field(
        default=StoppedHostStorage.FILESYSTEM, description="What the host's persisted storage is"
    )
    running_marker_path: Path | None = Field(
        default=None, description="File whose existence means the host is running"
    )


class StoppedHostProviderInstance(LocalProviderInstance):
    """A local provider over its own host directory whose host is stopped until started.

    While stopped, the host resolves to an offline host whose persisted storage is
    the provider's host directory, served as ``storage`` says. ``start_host``
    brings it up as an ordinary local host over that same directory.
    """

    configured_host_name: HostName = Field(frozen=True, description="Name of the provider's single host")
    storage: StoppedHostStorage = Field(frozen=True, description="What the host's persisted storage is")
    running_marker_path: Path = Field(frozen=True, description="File whose existence means the host is running")

    @cached_property
    def host_id(self) -> HostId:
        return get_or_create_local_host_id(self.host_dir)

    def is_host_running(self) -> bool:
        return self.running_marker_path.exists()

    def get_host_name(self, style: HostNameStyle) -> HostName:
        return self.configured_host_name

    def get_host(self, host: HostId | HostName):
        """Return the running local host, or the offline host while stopped.

        No return annotation: while stopped this returns an ``OfflineHost``, which
        is not the ``Host`` the parent declares.
        """
        if host != self.host_id and host != self.configured_host_name:
            raise HostNotFoundError(self.name, host)
        if self.is_host_running():
            return self._create_host(self.configured_host_name)
        return self.to_offline_host(self.host_id)

    def to_offline_host(self, host_id: HostId) -> OfflineHost:
        if host_id != self.host_id:
            raise HostNotFoundError(self.name, host_id)
        now = datetime.now(timezone.utc)
        offline = OfflineHost(
            id=host_id,
            certified_host_data=CertifiedHostData(
                host_id=str(host_id),
                host_name=str(self.configured_host_name),
                created_at=now,
                updated_at=now,
            ),
            provider_instance=self,
            mngr_ctx=self.mngr_ctx,
        )
        return make_readable_offline_host(offline)

    def get_volume_for_host(self, host: HostInterface | HostId) -> HostVolume | None:
        match self.storage:
            case StoppedHostStorage.FILESYSTEM:
                return super().get_volume_for_host(host)
            case StoppedHostStorage.SERVICE:
                return HostVolume(volume=StorageServiceVolume(root_path=self.host_dir))
            case StoppedHostStorage.UNREACHABLE:
                return None
            case _ as unreachable:
                assert_never(unreachable)

    def list_persisted_agent_data_for_host(self, host_id: HostId) -> list[dict[str, Any]]:
        if host_id != self.host_id:
            return []
        return [json.loads(path.read_text()) for path in sorted(self.host_dir.glob("agents/*/data.json"))]

    def discover_hosts(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> list[DiscoveredHost]:
        return [
            DiscoveredHost(
                host_id=self.host_id,
                host_name=self.configured_host_name,
                provider_name=self.name,
                host_state=HostState.RUNNING if self.is_host_running() else HostState.STOPPED,
            )
        ]

    def start_host(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId | None = None,
    ) -> Host:
        self.running_marker_path.write_text("running")
        return self._create_host(self.configured_host_name)

    def stop_host(
        self,
        host: HostInterface | HostId,
        create_snapshot: bool = True,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.running_marker_path.unlink(missing_ok=True)


class StoppedHostProviderBackend(ProviderBackendInterface):
    """Backend that builds a ``StoppedHostProviderInstance`` from its settings."""

    @staticmethod
    def get_name() -> ProviderBackendName:
        return STOPPED_HOST_BACKEND_NAME

    @staticmethod
    def get_description() -> str:
        return "Test backend whose single local host is stopped until started"

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return StoppedHostProviderConfig

    @staticmethod
    def get_build_args_help() -> str:
        return "No arguments supported."

    @staticmethod
    def get_start_args_help() -> str:
        return "No arguments supported."

    @staticmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
    ) -> ProviderInstanceInterface:
        if not isinstance(config, StoppedHostProviderConfig):
            raise ConfigStructureError(f"Expected StoppedHostProviderConfig, got {type(config).__name__}")
        # Discovery also builds a default instance of every registered backend; only
        # the instances a settings file declares have a host.
        if config.host_dir is None or config.host_name is None or config.running_marker_path is None:
            raise ProviderEmptyError(provider_name=name, reason="no stopped host is configured")
        return StoppedHostProviderInstance(
            name=name,
            host_dir=config.host_dir,
            mngr_ctx=mngr_ctx,
            configured_host_name=HostName(config.host_name),
            storage=config.storage,
            running_marker_path=config.running_marker_path,
        )


@contextmanager
def registered_stopped_host_backend() -> Iterator[None]:
    """Make the stopped-host backend available to provider configuration while active."""
    _backend_registry[STOPPED_HOST_BACKEND_NAME] = StoppedHostProviderBackend
    register_provider_config(str(STOPPED_HOST_BACKEND_NAME), StoppedHostProviderConfig)
    try:
        yield
    finally:
        _backend_registry.pop(STOPPED_HOST_BACKEND_NAME, None)
        _provider_config_registry.pop(STOPPED_HOST_BACKEND_NAME, None)


class StoppedHost(FrozenModel):
    """A host served by the stopped-host backend, as a test sees it."""

    host_name: str = Field(description="The host's name")
    provider_name: str = Field(description="Name of the provider instance serving the host")
    host_dir: Path = Field(description="The host directory, which is also the host's persisted storage")
    running_marker_path: Path = Field(description="File whose existence means the host is running")
    storage: StoppedHostStorage = Field(description="What the host's persisted storage is")

    @property
    def address(self) -> str:
        """The TARGET text naming this host on the command line."""
        return f"@{self.host_name}"

    def is_running(self) -> bool:
        return self.running_marker_path.exists()

    def start(self) -> None:
        """Bring the host up over the same storage, as the provider's ``start_host`` does."""
        self.running_marker_path.write_text("running")


class StoppedHostFactory(MutableModel):
    """Creates stopped hosts and keeps the project settings file listing all of them."""

    root_dir: Path = Field(frozen=True, description="Directory under which each host's files are created")
    settings_path: Path = Field(frozen=True, description="Project settings file the CLI loads")
    hosts: list[StoppedHost] = Field(default_factory=list, description="Every host created so far")

    def create(self, storage: StoppedHostStorage, host_name: str | None = None) -> StoppedHost:
        """Create a stopped host; hosts given the same ``host_name`` make ``@host_name`` ambiguous."""
        unique = uuid4().hex
        host_root = self.root_dir / unique
        host = StoppedHost(
            host_name=host_name if host_name is not None else f"stopped-{unique}",
            provider_name=f"stopped-{unique}",
            host_dir=host_root / "host_dir",
            running_marker_path=host_root / "running",
            storage=storage,
        )
        host.host_dir.mkdir(parents=True)
        self.hosts.append(host)
        self.settings_path.write_text(self._render_settings())
        return host

    def _render_settings(self) -> str:
        sections = ["is_allowed_in_pytest = true\n"]
        for host in self.hosts:
            sections.append(
                f"[providers.{host.provider_name}]\n"
                f'backend = "{STOPPED_HOST_BACKEND_NAME}"\n'
                f"host_dir = {json.dumps(str(host.host_dir))}\n"
                f"host_name = {json.dumps(host.host_name)}\n"
                f'storage = "{host.storage}"\n'
                f"running_marker_path = {json.dumps(str(host.running_marker_path))}\n"
            )
        return "\n".join(sections)


class AddressedMachine(FrozenModel):
    """A machine a command can address, and the directory whose files it serves."""

    address: str = Field(description="The TARGET text naming the machine on the command line")
    host_dir: Path = Field(description="The host directory, where a test arranges files and reads them back")


class AddressedMachineFactory(FrozenModel):
    """Creates the machines a command can address, reached as a running host or through a stopped host's storage."""

    local_host_dir: Path = Field(description="Host directory of the running local host")
    stopped_host_factory: StoppedHostFactory = Field(description="Creates the stopped hosts")

    def create(self, storage: StoppedHostStorage | None) -> AddressedMachine:
        """The running local host when ``storage`` is ``None``, or a fresh stopped host with that storage."""
        if storage is None:
            return AddressedMachine(address="@localhost", host_dir=self.local_host_dir)
        host = self.stopped_host_factory.create(storage)
        return AddressedMachine(address=host.address, host_dir=host.host_dir)


def make_stopped_host_factory(root_dir: Path, monkeypatch: pytest.MonkeyPatch) -> StoppedHostFactory:
    """Point the CLI's project settings at a fresh directory and return a factory writing into it."""
    config_dir = root_dir / "project_config"
    config_dir.mkdir(parents=True)
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(config_dir))
    return StoppedHostFactory(root_dir=root_dir, settings_path=config_dir / "settings.toml")


def write_agent_record(host_dir: Path, agent_name: str, work_dir: Path) -> AgentId:
    """Record an agent in ``host_dir`` the way a host persists it, without starting anything."""
    agent_id = AgentId.generate()
    agent_dir = get_agent_state_dir_path(host_dir, agent_id)
    agent_dir.mkdir(parents=True)
    data = {
        "id": str(agent_id),
        "name": agent_name,
        "type": PLACEHOLDER_AGENT_TYPE,
        "command": "sleep 83917",
        "work_dir": str(work_dir),
        "create_time": datetime.now(timezone.utc).isoformat(),
    }
    (agent_dir / "data.json").write_text(json.dumps(data))
    return agent_id


def read_tree(root: Path) -> dict[str, bytes | None]:
    """Map every path under ``root`` (relative) to its bytes, or ``None`` for a directory.

    Compare two readings to show a command left a machine's files exactly as they were.
    """
    return {
        str(path.relative_to(root)): None if path.is_dir() else path.read_bytes() for path in sorted(root.rglob("*"))
    }
