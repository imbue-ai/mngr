from pathlib import Path

from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.agent_creator import AgentCreateAttemptInfo
from imbue.minds.desktop_client.agent_creator import AgentCreateAttemptStatus
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.primitives import CreateAttemptId
from imbue.minds.primitives import LaunchMode
from imbue.mngr.primitives import AgentId


class InFlightCreateAttempt(FrozenModel):
    """One workspace create the fake agent creator reports as still running."""

    provider_instance_name: str = Field(description="The provider instance the create targets")
    host_name: str = Field(description="The host name the create reserved")
    agent_id: AgentId | None = Field(
        default=None, description="The canonical agent id once mngr create returned; None while still creating"
    )
    account_id: str = Field(default="", description="The account the workspace will associate with; empty for private")


class InFlightAgentCreator(AgentCreator):
    """An agent creator that reports fixed live create attempts and never runs a create.

    Drives the gates that must wait for a create to finish (connection
    removal, signout, dead-session retirement) and the providers panel's
    in-use count, which only read what is in flight.
    """

    in_flight_attempts: tuple[InFlightCreateAttempt, ...] = Field(
        default=(), description="The live attempts every in-flight query answers from"
    )

    def live_in_flight_host_names(self, provider_instance_name: str | None = None) -> set[str]:
        return {
            attempt.host_name.casefold()
            for attempt in self.in_flight_attempts
            if provider_instance_name is None or attempt.provider_instance_name == provider_instance_name
        }

    def live_in_flight_provider_instance_names(self) -> set[str]:
        return {attempt.provider_instance_name for attempt in self.in_flight_attempts}

    def live_in_flight_account_ids(self) -> set[str]:
        return {attempt.account_id for attempt in self.in_flight_attempts if attempt.account_id}

    def list_create_attempt_infos(self) -> list[AgentCreateAttemptInfo]:
        return [
            AgentCreateAttemptInfo(
                create_attempt_id=CreateAttemptId.generate(),
                agent_id=attempt.agent_id,
                status=AgentCreateAttemptStatus.CREATING_WORKSPACE,
                launch_mode=LaunchMode.MODAL,
                host_name=attempt.host_name,
                provider_instance_name=attempt.provider_instance_name,
            )
            for attempt in self.in_flight_attempts
        ]


def make_in_flight_agent_creator(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, attempts: tuple[InFlightCreateAttempt, ...]
) -> InFlightAgentCreator:
    return InFlightAgentCreator(
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        root_concurrency_group=root_concurrency_group,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
        in_flight_attempts=attempts,
    )
