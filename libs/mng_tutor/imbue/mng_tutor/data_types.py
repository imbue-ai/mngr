from typing import Annotated
from typing import Literal

from pydantic import Discriminator
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName


class AgentExistsCheck(FrozenModel):
    """Check that an agent with the given name exists."""

    check_type: Literal["agent_exists"] = "agent_exists"
    agent_name: AgentName = Field(description="Name of the agent to check for")


class AgentNotExistsCheck(FrozenModel):
    """Check that an agent with the given name does not exist."""

    check_type: Literal["agent_not_exists"] = "agent_not_exists"
    agent_name: AgentName = Field(description="Name of the agent to check for")


class AgentInStateCheck(FrozenModel):
    """Check that an agent is in one of the expected lifecycle states."""

    check_type: Literal["agent_in_state"] = "agent_in_state"
    agent_name: AgentName = Field(description="Name of the agent to check")
    expected_states: tuple[AgentLifecycleState, ...] = Field(description="Acceptable lifecycle states")


class FileExistsInAgentWorkDirCheck(FrozenModel):
    """Check that a file exists in an agent's working directory."""

    check_type: Literal["file_exists_in_work_dir"] = "file_exists_in_work_dir"
    agent_name: AgentName = Field(description="Name of the agent whose work_dir to check")
    file_path: str = Field(description="Relative path within the agent's work_dir")


class TmuxSessionHasClientsCheck(FrozenModel):
    """Check that an agent's tmux session has at least one attached client."""

    check_type: Literal["tmux_session_has_clients"] = "tmux_session_has_clients"
    agent_name: AgentName = Field(description="Name of the agent whose tmux session to check")


StepCheck = Annotated[
    AgentExistsCheck
    | AgentNotExistsCheck
    | AgentInStateCheck
    | FileExistsInAgentWorkDirCheck
    | TmuxSessionHasClientsCheck,
    Discriminator("check_type"),
]


class LessonStep(FrozenModel):
    """A single step in a tutorial lesson."""

    heading: str = Field(description="Short heading for the step")
    details: str = Field(description="Detailed instructions for completing the step")
    check: StepCheck = Field(description="How to verify the step is complete")


class Lesson(FrozenModel):
    """A complete tutorial lesson with ordered steps."""

    title: str = Field(description="Title of the lesson")
    description: str = Field(description="Brief description of what this lesson teaches")
    steps: tuple[LessonStep, ...] = Field(description="Ordered steps to complete")
