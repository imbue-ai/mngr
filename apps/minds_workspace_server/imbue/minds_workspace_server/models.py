from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class AgentCreationError(ValueError):
    """Raised when agent creation fails due to invalid input."""

    ...


class AgentListItem(FrozenModel):
    """An agent entry in the agent list response."""

    id: str = Field(description="The agent's unique identifier")
    name: str = Field(description="The agent's human-readable name")
    state: str = Field(description="The agent's lifecycle state")
    supports_interrupt: bool = Field(
        default=False,
        description="Whether the agent type supports interrupting the current turn",
    )


class InterruptAgentResponse(FrozenModel):
    """Response from the /api/agents/{id}/interrupt endpoint."""

    status: str = Field(description="Status of the interrupt operation")


class AgentListResponse(FrozenModel):
    """Response from the /api/agents endpoint."""

    agents: list[AgentListItem] = Field(description="List of discovered agents")


class SendMessageRequest(FrozenModel):
    """Request body for sending a message to an agent."""

    message: str = Field(description="The message text to send")


class SendMessageResponse(FrozenModel):
    """Response from the message endpoint."""

    status: str = Field(description="Status of the send operation")


class ErrorResponse(FrozenModel):
    """Error response body."""

    detail: str = Field(description="Human-readable error description")


class AgentStateItem(FrozenModel):
    """Agent state for the unified WebSocket stream."""

    id: str = Field(description="The agent's unique identifier")
    name: str = Field(description="The agent's human-readable name")
    state: str = Field(description="The agent's lifecycle state")
    labels: dict[str, str] = Field(description="Agent labels (e.g., user_created, chat_parent_id)")
    work_dir: str | None = Field(description="The agent's working directory path")
    supports_interrupt: bool = Field(
        default=False,
        description="Whether the agent type supports interrupting the current turn",
    )


class ApplicationEntry(FrozenModel):
    """An application registered in runtime/applications.toml."""

    name: str = Field(description="Application name (e.g., 'web', 'terminal')")
    url: str = Field(description="Local URL where the application is accessible")


class CreateWorktreeRequest(FrozenModel):
    """Request body for creating a worktree agent."""

    name: str = Field(description="Name for the new worktree agent")
    selected_agent_id: str = Field(
        default="",
        description="ID of the agent whose work dir to create the worktree from",
    )


class CreateChatRequest(FrozenModel):
    """Request body for creating a chat agent."""

    name: str = Field(description="Name for the new chat agent")


class CreateAgentResponse(FrozenModel):
    """Response from agent creation endpoints."""

    agent_id: str = Field(description="The pre-generated agent ID")


class RandomNameResponse(FrozenModel):
    """Response from the random name endpoint."""

    name: str = Field(description="A random agent name")


class DestroyAgentResponse(FrozenModel):
    """Response from the agent destroy endpoint."""

    status: str = Field(description="Result of the destroy operation")
