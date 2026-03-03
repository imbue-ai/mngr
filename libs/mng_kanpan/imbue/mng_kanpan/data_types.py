from enum import auto
from pathlib import Path

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mng.config.data_types import PluginConfig
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import ProviderInstanceName


class PrState(UpperCaseStrEnum):
    """State of a GitHub pull request."""

    OPEN = auto()
    CLOSED = auto()
    MERGED = auto()


class CheckStatus(UpperCaseStrEnum):
    """Aggregate CI check status for a PR."""

    PASSING = auto()
    FAILING = auto()
    PENDING = auto()
    UNKNOWN = auto()


class BoardSection(UpperCaseStrEnum):
    """Sections for grouping agents on the board, based on PR state."""

    STILL_COOKING = auto()
    PR_BEING_REVIEWED = auto()
    PR_MERGED = auto()
    PR_CLOSED = auto()
    MUTED = auto()


class PrInfo(FrozenModel):
    """GitHub pull request information associated with an agent."""

    number: int = Field(description="PR number")
    title: str = Field(description="PR title")
    state: PrState = Field(description="PR state (open/closed/merged)")
    url: str = Field(description="PR URL")
    head_branch: str = Field(description="Head branch name of the PR")
    check_status: CheckStatus = Field(description="Aggregate CI check status")
    is_draft: bool = Field(description="Whether the PR is a draft")


class AgentBoardEntry(FrozenModel):
    """A single agent entry on the pankan board."""

    name: AgentName = Field(description="Agent name")
    state: AgentLifecycleState = Field(description="Agent lifecycle state")
    provider_name: ProviderInstanceName = Field(description="Provider instance name")
    work_dir: Path | None = Field(default=None, description="Local work directory (None for remote agents)")
    branch: str | None = Field(default=None, description="Git branch for this agent")
    pr: PrInfo | None = Field(default=None, description="Associated GitHub PR, if any")
    commits_ahead: int | None = Field(
        default=None, description="Commits ahead of remote tracking branch (None if unknown/no upstream)"
    )
    create_pr_url: str | None = Field(default=None, description="URL to create a new PR for this branch")
    is_muted: bool = Field(default=False, description="Whether the agent is muted (relegated to bottom)")


class BoardSnapshot(FrozenModel):
    """A complete snapshot of the pankan board state."""

    entries: tuple[AgentBoardEntry, ...] = Field(description="All agent board entries")
    errors: tuple[str, ...] = Field(default=(), description="Errors encountered during fetch")
    fetch_time_seconds: float = Field(description="Time taken to fetch data")


class CustomCommand(FrozenModel):
    """A command definition for the kanpan board (builtin or user-defined)."""

    name: str = Field(description="Display name shown in the status bar")
    command: str = Field(
        default="",
        description="Shell command to run. MNG_AGENT_NAME env var is set to the focused agent's name.",
    )
    refresh_afterwards: bool = Field(default=False, description="Whether to trigger a board refresh after completion")
    enabled: bool = Field(default=True, description="Whether this command is active")


class KanpanPluginConfig(PluginConfig):
    """Configuration for the kanpan plugin."""

    commands: dict[str, CustomCommand] = Field(
        default_factory=dict,
        description="Custom commands keyed by their trigger key",
    )

    def merge_with(self, override: "PluginConfig") -> "KanpanPluginConfig":
        """Merge this config with an override config."""
        if not isinstance(override, KanpanPluginConfig):
            return self
        merged_enabled = override.enabled if override.enabled is not None else self.enabled
        merged_commands = {**self.commands, **override.commands}
        return KanpanPluginConfig(enabled=merged_enabled, commands=merged_commands)
