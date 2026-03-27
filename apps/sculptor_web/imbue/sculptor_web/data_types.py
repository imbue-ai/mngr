from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class AgentHostInfo(FrozenModel):
    """Host information for an agent."""

    id: str = Field(description="Host ID")
    name: str = Field(description="Host name")
    provider_name: str = Field(description="Provider name")


class AgentStatusInfo(FrozenModel):
    """Status information for an agent."""

    line: str = Field(description="Status line text")


class AgentDisplayInfo(FrozenModel):
    """Information about an agent for display purposes."""

    id: str = Field(description="Agent ID")
    name: str = Field(description="Agent name")
    type: str = Field(description="Agent type (claude, codex, etc.)")
    command: str = Field(description="Command used to start the agent")
    work_dir: Path = Field(description="Working directory")
    create_time: datetime = Field(description="Creation timestamp")
    start_on_boot: bool = Field(description="Whether agent starts on host boot")

    state: str = Field(description="Agent lifecycle state (stopped/running/waiting/replaced/done)")
    status: AgentStatusInfo | None = Field(default=None, description="Current status")
    url: str | None = Field(default=None, description="Agent URL")
    start_time: datetime | None = Field(default=None, description="Last start time")
    runtime_seconds: float | None = Field(default=None, description="Runtime in seconds")

    host: AgentHostInfo = Field(description="Host information")

    plugin: dict[str, Any] = Field(default_factory=dict, description="Plugin-specific fields")


class AgentListResult(FrozenModel):
    """Result of listing agents from mngr."""

    agents: tuple[AgentDisplayInfo, ...] = Field(default=(), description="List of agents")
    errors: tuple[str, ...] = Field(default=(), description="Errors encountered")
