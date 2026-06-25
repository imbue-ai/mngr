"""Concrete, typed mock host implementations for interface-layer unit tests.

These are real ``OnlineHostInterface`` implementations (not ``unittest.mock``
doubles), so they catch interface drift -- a renamed or re-typed abstract
method makes the mock fail to instantiate -- and they expose observable state
(e.g. ``disconnect_count``) instead of requiring call-count assertions.

Only the handful of methods exercised by the provider-instance default methods
(``get_host_and_agent_details``, ``discover_hosts_and_agents``) carry real
behavior; the remaining abstract methods raise ``NotImplementedError`` (the
same convention used by ``providers/mock_provider_test.MockProviderInstance``).
"""

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Iterator
from typing import Mapping
from typing import Sequence

from pydantic import Field

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.hosts.outer_host import create_local_pyinfra_host
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import ActivityConfig
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.data_types import CpuResources
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.interfaces.data_types import SnapshotInfo
from imbue.mngr.interfaces.data_types import VolumeFile
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import CreateWorkDirResult
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState


def _default_connector() -> PyinfraConnector:
    return PyinfraConnector(create_local_pyinfra_host())


def _default_resources() -> HostResources:
    return HostResources(cpu=CpuResources(count=1), memory_gb=1.0, disk_gb=10.0)


class MockOnlineHost(OnlineHostInterface):
    """A concrete ``OnlineHostInterface`` whose behavior is set via fields.

    Configure ``agents`` / ``discovered_agents`` for the happy path, or set
    ``raise_connection_error_on_get_agents`` /
    ``raise_connection_error_on_discover_agents`` to simulate a host whose
    sshd has died after it was discovered. ``disconnect`` increments
    ``disconnect_count`` so tests can assert cleanup happened without
    coupling to a mock's call record.
    """

    connector: PyinfraConnector = Field(default_factory=_default_connector, frozen=True)
    host_name: HostName = Field(default=HostName("test-host"))
    state: HostState = Field(default=HostState.RUNNING)
    certified_data: CertifiedHostData = Field()
    activity_config: ActivityConfig = Field(default_factory=lambda: ActivityConfig(idle_timeout_seconds=3600))
    agents: list[AgentInterface] = Field(default_factory=list)
    discovered_agents: list[DiscoveredAgent] = Field(default_factory=list)
    ssh_connection_info: tuple[str, str, int, Path] | None = Field(default=None)
    is_locked: bool = Field(default=False)
    provider_resources: HostResources = Field(default_factory=_default_resources)
    snapshots: list[SnapshotInfo] = Field(default_factory=list)
    raise_connection_error_on_get_agents: bool = Field(default=False)
    raise_connection_error_on_discover_agents: bool = Field(default=False)
    disconnect_count: int = Field(default=0)

    # --- HostInterface ---

    @property
    def is_local(self) -> bool:
        return False

    @property
    def host_dir(self) -> Path:
        return Path("/mngr")

    def get_name(self) -> HostName:
        return self.host_name

    def get_activity_config(self) -> ActivityConfig:
        return self.activity_config

    def set_activity_config(self, config: ActivityConfig) -> None:
        self.activity_config = config

    def get_certified_data(self) -> CertifiedHostData:
        return self.certified_data

    def set_certified_data(self, data: CertifiedHostData) -> None:
        self.certified_data = data

    def get_plugin_data(self, plugin_name: str) -> dict[str, Any]:
        return dict(self.certified_data.plugin.get(plugin_name, {}))

    def get_seconds_since_stopped(self) -> float | None:
        return None

    def get_stop_time(self) -> datetime | None:
        return None

    def get_snapshots(self) -> list[SnapshotInfo]:
        return list(self.snapshots)

    def get_image(self) -> str | None:
        return self.certified_data.image

    def get_tags(self) -> dict[str, str]:
        return dict(self.certified_data.user_tags)

    def discover_agents(self) -> list[DiscoveredAgent]:
        if self.raise_connection_error_on_discover_agents:
            raise HostConnectionError("SSH error (Error reading SSH protocol banner)")
        return list(self.discovered_agents)

    def rename_agent(
        self,
        agent_ref: DiscoveredAgent,
        new_name: AgentName,
        labels_to_merge: Mapping[str, str] | None = None,
    ) -> DiscoveredAgent:
        raise NotImplementedError()

    def get_state(self) -> HostState:
        return self.state

    def get_failure_reason(self) -> str | None:
        return self.certified_data.failure_reason

    def get_build_log(self) -> str | None:
        return None

    def disconnect(self) -> None:
        self.disconnect_count += 1

    # --- OuterHostInterface ---

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        raise NotImplementedError()

    def execute_stateful_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        raise NotImplementedError()

    def execute_streaming_command(
        self,
        command: str,
        on_line: Callable[[str], None],
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        raise NotImplementedError()

    def read_file(self, path: Path) -> bytes:
        raise NotImplementedError()

    def write_file(self, path: Path, content: bytes, mode: str | None = None, is_atomic: bool = False) -> None:
        raise NotImplementedError()

    def read_text_file(self, path: Path, encoding: str = "utf-8") -> str:
        raise NotImplementedError()

    def write_text_file(self, path: Path, content: str, encoding: str = "utf-8", mode: str | None = None) -> None:
        raise NotImplementedError()

    def get_file_mtime(self, path: Path) -> datetime | None:
        raise NotImplementedError()

    def list_directory(self, path: Path, *, recursive: bool = False) -> list[VolumeFile]:
        raise NotImplementedError()

    def get_ssh_connection_info(self) -> tuple[str, str, int, Path] | None:
        return self.ssh_connection_info

    # --- OnlineHostInterface ---

    def get_reported_activity_time(self, activity_type: ActivitySource) -> datetime | None:
        return None

    def record_activity(self, activity_type: ActivitySource) -> None:
        raise NotImplementedError()

    def get_reported_activity_content(self, activity_type: ActivitySource) -> str | None:
        return None

    @contextmanager
    def lock_cooperatively(self, timeout_seconds: float = 30.0) -> Iterator[None]:
        raise NotImplementedError()
        yield

    def get_reported_lock_time(self) -> datetime | None:
        return None

    def is_lock_held(self) -> bool:
        return self.is_locked

    def set_plugin_data(self, plugin_name: str, data: dict[str, Any]) -> None:
        raise NotImplementedError()

    def to_offline_host(self) -> HostInterface:
        raise NotImplementedError()

    def get_idle_seconds(self) -> float:
        raise NotImplementedError()

    def get_reported_plugin_state_file_data(self, plugin_name: str, filename: str) -> str:
        raise NotImplementedError()

    def set_reported_plugin_state_file_data(self, plugin_name: str, filename: str, data: str) -> None:
        raise NotImplementedError()

    def get_reported_plugin_state_files(self, plugin_name: str) -> list[str]:
        raise NotImplementedError()

    def get_host_env_path(self) -> Path:
        raise NotImplementedError()

    def get_env_vars(self) -> dict[str, str]:
        raise NotImplementedError()

    def set_env_vars(self, env: Mapping[str, str]) -> None:
        raise NotImplementedError()

    def get_env_var(self, key: str) -> str | None:
        raise NotImplementedError()

    def set_env_var(self, key: str, value: str) -> None:
        raise NotImplementedError()

    def build_source_env_prefix(self, agent: AgentInterface) -> str:
        raise NotImplementedError()

    def get_boot_time(self) -> datetime | None:
        return None

    def get_uptime_seconds(self) -> float:
        return 0.0

    def get_provider_resources(self) -> HostResources:
        return self.provider_resources

    def set_tags(self, tags: Mapping[str, str]) -> None:
        raise NotImplementedError()

    def add_tags(self, tags: Mapping[str, str]) -> None:
        raise NotImplementedError()

    def remove_tags(self, keys: Sequence[str]) -> None:
        raise NotImplementedError()

    def get_agent_env_path(self, agent: AgentInterface) -> Path:
        raise NotImplementedError()

    def get_agents(self) -> list[AgentInterface]:
        if self.raise_connection_error_on_get_agents:
            raise HostConnectionError("SSH error")
        return list(self.agents)

    def create_agent_work_dir(
        self,
        host: OnlineHostInterface,
        path: Path,
        options: CreateAgentOptions,
    ) -> CreateWorkDirResult:
        raise NotImplementedError()

    def create_agent_state(
        self,
        work_dir_path: Path,
        options: CreateAgentOptions,
        created_branch_name: str | None = None,
    ) -> AgentInterface:
        raise NotImplementedError()

    def provision_agent(self, agent: AgentInterface, options: CreateAgentOptions, mngr_ctx: MngrContext) -> None:
        raise NotImplementedError()

    def destroy_agent(self, agent: AgentInterface) -> None:
        raise NotImplementedError()

    def start_agents(self, agent_ids: Sequence[AgentId]) -> None:
        raise NotImplementedError()

    def stop_agents(self, agent_ids: Sequence[AgentId], timeout_seconds: float = 5.0) -> None:
        raise NotImplementedError()

    def copy_directory(
        self,
        source_host: OnlineHostInterface,
        source_path: Path,
        target_path: Path,
        extra_args: str | None = None,
        exclude_git: bool = False,
    ) -> None:
        raise NotImplementedError()

    def copy_local_directory(self, source_path: Path, target_path: Path, extra_args: str | None) -> None:
        raise NotImplementedError()

    def save_agent_data(self, agent_id: AgentId, agent_data: Mapping[str, object]) -> None:
        raise NotImplementedError()
