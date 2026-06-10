"""Concrete, typed mock agent implementation for interface-layer unit tests.

A real ``AgentInterface`` implementation (not a ``unittest.mock`` double) so
that the provider-instance default methods exercise a typed contract: a
renamed or re-typed abstract method makes the mock fail to instantiate rather
than silently fabricating a return value.

Only the methods reached while building ``AgentDetails`` from a live agent
carry real behavior; the rest raise ``NotImplementedError``.
"""

from datetime import datetime
from typing import Any
from typing import Mapping
from typing import Sequence

from pydantic import Field

from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import FileTransferSpec
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import CommandString


class MockAgent(AgentInterface[AgentTypeConfig]):
    """A concrete ``AgentInterface`` whose reported values are set via fields.

    Set ``raise_connection_error_on_reported_url`` to simulate the host's sshd
    dropping mid-way through ``_build_agent_details_from_online_agent`` (the
    error must surface *after* the earlier getters succeed), which the
    provider should recover from by falling back to offline data.
    """

    command: CommandString = Field(default=CommandString("sleep 999"))
    labels: dict[str, str] = Field(default_factory=dict)
    lifecycle_state: AgentLifecycleState = Field(default=AgentLifecycleState.RUNNING)
    is_start_on_boot: bool = Field(default=False)
    created_branch_name: str | None = Field(default=None)
    reported_url: str | None = Field(default=None)
    raise_connection_error_on_reported_url: bool = Field(default=False)

    # --- methods reached while building AgentDetails from a live agent ---

    def get_command(self) -> CommandString:
        return self.command

    def set_command(self, command: CommandString) -> None:
        self.command = command

    def get_labels(self) -> dict[str, str]:
        return dict(self.labels)

    def set_labels(self, labels: Mapping[str, str]) -> None:
        self.labels = dict(labels)

    def get_created_branch_name(self) -> str | None:
        return self.created_branch_name

    def get_is_start_on_boot(self) -> bool:
        return self.is_start_on_boot

    def set_is_start_on_boot(self, value: bool) -> None:
        self.is_start_on_boot = value

    def get_lifecycle_state(self) -> AgentLifecycleState:
        return self.lifecycle_state

    def get_reported_activity_time(self, activity_type: ActivitySource) -> datetime | None:
        return None

    def get_reported_url(self) -> str | None:
        if self.raise_connection_error_on_reported_url:
            raise HostConnectionError("SSH connection dropped")
        return self.reported_url

    @property
    def runtime_seconds(self) -> float | None:
        return None

    # --- unused abstract methods ---

    def get_host(self) -> OnlineHostInterface:
        raise NotImplementedError()

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
        initial_message: str | None = None,
    ) -> CommandString:
        raise NotImplementedError()

    def get_expected_process_name(self) -> str:
        raise NotImplementedError()

    def is_running(self) -> bool:
        raise NotImplementedError()

    def get_initial_message(self) -> str | None:
        raise NotImplementedError()

    def get_resume_message(self) -> str | None:
        raise NotImplementedError()

    def get_ready_timeout_seconds(self) -> float:
        raise NotImplementedError()

    def send_message(self, message: str) -> None:
        raise NotImplementedError()

    def capture_pane_content(self, include_scrollback: bool = False) -> str | None:
        raise NotImplementedError()

    def get_reported_start_time(self) -> datetime | None:
        raise NotImplementedError()

    def record_activity(self, activity_type: ActivitySource) -> None:
        raise NotImplementedError()

    def get_reported_activity_record(self, activity_type: ActivitySource) -> str | None:
        raise NotImplementedError()

    def get_plugin_data(self, plugin_name: str) -> dict[str, Any]:
        raise NotImplementedError()

    def set_plugin_data(self, plugin_name: str, data: dict[str, Any]) -> None:
        raise NotImplementedError()

    def get_reported_plugin_file(self, plugin_name: str, filename: str) -> str:
        raise NotImplementedError()

    def set_reported_plugin_file(self, plugin_name: str, filename: str, data: str) -> None:
        raise NotImplementedError()

    def list_reported_plugin_files(self, plugin_name: str) -> list[str]:
        raise NotImplementedError()

    def get_env_vars(self) -> dict[str, str]:
        raise NotImplementedError()

    def set_env_vars(self, env: Mapping[str, str]) -> None:
        raise NotImplementedError()

    def get_env_var(self, key: str) -> str | None:
        raise NotImplementedError()

    def set_env_var(self, key: str, value: str) -> None:
        raise NotImplementedError()

    def on_before_provisioning(
        self, host: OnlineHostInterface, options: CreateAgentOptions, mngr_ctx: MngrContext
    ) -> None:
        raise NotImplementedError()

    def get_provision_file_transfers(
        self, host: OnlineHostInterface, options: CreateAgentOptions, mngr_ctx: MngrContext
    ) -> Sequence[FileTransferSpec]:
        raise NotImplementedError()

    def provision(self, host: OnlineHostInterface, options: CreateAgentOptions, mngr_ctx: MngrContext) -> None:
        raise NotImplementedError()

    def on_after_provisioning(
        self, host: OnlineHostInterface, options: CreateAgentOptions, mngr_ctx: MngrContext
    ) -> None:
        raise NotImplementedError()

    def on_destroy(self, host: OnlineHostInterface) -> None:
        raise NotImplementedError()
