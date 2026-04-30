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


class IframeLogRecord(FrozenModel):
    """A single console-message record forwarded from an Electron renderer.

    The Electron main process captures renderer ``console.*`` output for
    frames whose URL path starts with ``/service/<name>/`` and POSTs them
    here in batches. Every field except ``client_timestamp`` is derived
    either from the ``console-message`` event payload or from parsing
    ``frame_url``, so the record is self-describing.
    """

    level: str = Field(description="Log level: one of 'info', 'warning', 'error', 'debug'")
    message: str = Field(description="The console message text")
    frame_url: str = Field(description="URL of the frame that produced the log")
    source_id: str = Field(default="", description="URL of the script that called console.log")
    line: int = Field(default=0, description="Line number in the source script")
    service_name: str = Field(description="Service name extracted from /service/<name>/ in frame_url")
    mind_id: str = Field(description="Agent id extracted from the <agent-id>.localhost subdomain")
    client_timestamp: str = Field(default="", description="Client-side timestamp when the log was captured")


class IframeLogsRequest(FrozenModel):
    """Batched payload for ``POST /api/iframe-logs``."""

    records: list[IframeLogRecord] = Field(description="Console-message records to persist")


class IframeLogsResponse(FrozenModel):
    """Response from ``POST /api/iframe-logs``."""

    written: int = Field(description="Number of records persisted")
