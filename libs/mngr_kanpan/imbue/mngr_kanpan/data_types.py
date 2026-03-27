from enum import auto
from pathlib import Path
from typing import Any

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.config.data_types import PluginConfig
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import ProviderInstanceName


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


class ColumnData(FrozenModel):
    """Data sources available to custom columns for a single agent."""

    labels: dict[str, str] = Field(default_factory=dict, description="Agent labels (key-value pairs)")
    plugin_data: dict[str, Any] = Field(default_factory=dict, description="Plugin fields from AgentDetails.plugin")


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
    column_data: ColumnData = Field(default_factory=ColumnData, description="Data sources for custom columns")


class BoardSnapshot(FrozenModel):
    """A complete snapshot of the pankan board state."""

    entries: tuple[AgentBoardEntry, ...] = Field(description="All agent board entries")
    errors: tuple[str, ...] = Field(default=(), description="Errors encountered during fetch")
    prs_loaded: bool = Field(default=True, description="Whether PR data was successfully fetched from GitHub")
    fetch_time_seconds: float = Field(description="Time taken to fetch data")


class GitHubData(FrozenModel):
    """GitHub PR data fetched via the gh CLI, used to enrich agent snapshots."""

    pr_by_branch: dict[str, PrInfo] = Field(description="Mapping from branch name to the most relevant PR")
    repo_path: str | None = Field(default=None, description="GitHub owner/repo path (e.g. 'owner/repo')")
    prs_loaded: bool = Field(default=True, description="Whether PR data was successfully fetched")
    errors: tuple[str, ...] = Field(default=(), description="Errors encountered during remote fetch")


class CustomColumnConfig(FrozenModel):
    """Configuration for a single custom column on the kanpan board."""

    header: str = Field(description="Column header text")
    colors: dict[str, str] = Field(default_factory=dict, description="Mapping from value to urwid color name")
    source: str = Field(
        default="labels",
        description="Data source: 'labels' (agent labels) "
        "or 'agent' (AgentDetails.plugin, populated by agent_field_generators via AgentInterface)",
    )
    plugin_name: str | None = Field(default=None, description="Plugin name (required for 'agent' source)")
    field: str | None = Field(default=None, description="Field name within plugin data (required for 'agent' source)")


class RefreshHook(FrozenModel):
    """A hook command that runs during kanpan board refresh."""

    name: str = Field(description="Human-readable name")
    command: str = Field(description="Shell command to run per agent. Env vars provide agent context.")
    enabled: bool = Field(default=True)


class CustomCommand(FrozenModel):
    """A command definition for the kanpan board (builtin or user-defined)."""

    name: str = Field(description="Display name shown in the status bar")
    command: str = Field(
        default="",
        description="Shell command to run. MNGR_AGENT_NAME env var is set to the focused agent's name.",
    )
    refresh_afterwards: bool = Field(default=False, description="Whether to trigger a board refresh after completion")
    enabled: bool = Field(default=True, description="Whether this command is active")
    markable: bool | str = Field(
        default=False,
        description="If truthy, pressing the key marks agents for batch execution with x instead of running immediately."
        " Set to a color name (e.g. 'light red') to customize the mark indicator color.",
    )


class KanpanPluginConfig(PluginConfig):
    """Configuration for the kanpan plugin."""

    commands: dict[str, CustomCommand] = Field(
        default_factory=dict,
        description="Custom commands keyed by their trigger key",
    )
    columns: dict[str, CustomColumnConfig] = Field(
        default_factory=dict,
        description="Custom columns keyed by column identifier",
    )
    column_order: list[str] | None = Field(
        default=None,
        description="Display order for all columns (built-in and custom). "
        "Built-in names: name, state, git, pr, ci, link. "
        "If None, defaults to: name, state, git, pr, ci, [custom in config order], link.",
    )
    refresh_interval_seconds: float = Field(
        default=600.0,
        description="Seconds between periodic full refreshes (default 10 minutes)",
    )
    retry_cooldown_seconds: float = Field(
        default=60.0,
        description="Minimum seconds before retrying after a failed full refresh",
    )
    on_before_refresh: dict[str, RefreshHook] = Field(
        default_factory=dict,
        description="Hook commands to run before each full refresh, keyed by identifier",
    )
    on_after_refresh: dict[str, RefreshHook] = Field(
        default_factory=dict,
        description="Hook commands to run after each full refresh, keyed by identifier",
    )

    def merge_with(self, override: "PluginConfig") -> "KanpanPluginConfig":
        """Merge this config with an override config."""
        if not isinstance(override, KanpanPluginConfig):
            return self
        merged_enabled = override.enabled if override.enabled is not None else self.enabled
        merged_commands = {**self.commands, **override.commands}
        merged_columns = {**self.columns, **override.columns}
        merged_column_order = override.column_order if override.column_order is not None else self.column_order
        merged_refresh_interval = (
            override.refresh_interval_seconds
            if override.refresh_interval_seconds is not None
            else self.refresh_interval_seconds
        )
        merged_auto_cooldown = (
            override.retry_cooldown_seconds
            if override.retry_cooldown_seconds is not None
            else self.retry_cooldown_seconds
        )
        merged_on_before_refresh = {**self.on_before_refresh, **override.on_before_refresh}
        merged_on_after_refresh = {**self.on_after_refresh, **override.on_after_refresh}
        return KanpanPluginConfig(
            enabled=merged_enabled,
            commands=merged_commands,
            columns=merged_columns,
            column_order=merged_column_order,
            refresh_interval_seconds=merged_refresh_interval,
            retry_cooldown_seconds=merged_auto_cooldown,
            on_before_refresh=merged_on_before_refresh,
            on_after_refresh=merged_on_after_refresh,
        )
