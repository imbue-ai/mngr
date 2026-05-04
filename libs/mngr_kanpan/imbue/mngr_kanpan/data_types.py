from enum import auto
from pathlib import Path
from typing import Annotated
from typing import Any
from typing import Literal

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.config.data_types import PluginConfig
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_kanpan.data_source import CellDisplay
from imbue.mngr_kanpan.data_source import FieldValue


class BoardSection(UpperCaseStrEnum):
    """Sections for grouping agents on the board, based on PR state."""

    STILL_COOKING = auto()
    PR_DRAFT = auto()
    PRS_FAILED = auto()
    PR_BEING_REVIEWED = auto()
    PR_MERGED = auto()
    PR_CLOSED = auto()
    MUTED = auto()


class AgentBoardEntry(FrozenModel):
    """A single agent entry on the kanpan board."""

    name: AgentName = Field(description="Agent name")
    state: AgentLifecycleState = Field(description="Agent lifecycle state")
    provider_name: ProviderInstanceName = Field(description="Provider instance name")
    work_dir: Path | None = Field(default=None, description="Local work directory (None for remote agents)")
    branch: str | None = Field(default=None, description="Git branch for this agent")
    is_muted: bool = Field(default=False, description="Whether the agent is muted (relegated to bottom)")
    fields: dict[str, FieldValue] = Field(default_factory=dict, description="Field values from data sources")
    cells: dict[str, CellDisplay] = Field(
        default_factory=dict,
        description="Pre-computed cell displays from field.display(), keyed by field key",
    )
    section: BoardSection = Field(
        default=BoardSection.STILL_COOKING,
        description="Board section this agent belongs to",
    )


class BoardSnapshot(FrozenModel):
    """A complete snapshot of the kanpan board state."""

    entries: tuple[AgentBoardEntry, ...] = Field(description="All agent board entries")
    errors: tuple[str, ...] = Field(default=(), description="Errors encountered during fetch")
    fetch_time_seconds: float = Field(description="Time taken to fetch data")


class DataSourceConfig(FrozenModel):
    """Base configuration for a data source (enable/disable only).

    Used as the base class for source-specific configs (e.g. GitHubDataSourceConfig)
    that add their own fields. User-facing `KanpanPluginConfig.data_sources` stores
    raw dicts because the TOML loader uses ``model_construct`` and each source parses
    its own shape.
    """

    enabled: bool = Field(default=True, description="Whether this data source is enabled")


class CustomCommand(FrozenModel):
    """A user-defined command for the kanpan board.

    The ``kind`` discriminator distinguishes this from the builtin command
    shapes in ``KanpanCommand``; user TOML configs always parse as this
    shape and so cannot reach the builtin-specific dispatch paths
    (``mngr destroy`` for delete, ``git push`` for push).
    """

    kind: Literal["user"] = "user"
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


class ActionBuiltinRole(UpperCaseStrEnum):
    """Identifies a non-markable builtin action that runs immediately on key press.

    Dispatch in ``_dispatch_command`` uses ``match`` over this enum with
    ``assert_never`` so the type checker flags any missing branch when a
    new action role is added.
    """

    REFRESH = auto()
    MUTE = auto()
    UNMARK = auto()
    EXECUTE = auto()


class MarkableBuiltinRole(UpperCaseStrEnum):
    """Identifies a markable builtin whose key press toggles a mark.

    Batch dispatch in ``_submit_batch_item`` uses ``match`` over this enum
    with ``assert_never`` so the type checker flags any missing branch when
    a new markable role is added.
    """

    PUSH = auto()
    DELETE = auto()


class ActionBuiltinCommand(FrozenModel):
    """A non-markable kanpan builtin (refresh, mute, unmark, execute).

    Constructed only internally in ``tui._BUILTIN_COMMANDS``. The
    ``markable`` field is not modelled here: by construction these are
    never markable.
    """

    kind: Literal["action_builtin"] = "action_builtin"
    role: ActionBuiltinRole = Field(description="Which action this is; drives dispatch in tui._dispatch_command.")
    name: str = Field(description="Display name shown in the status bar")
    enabled: bool = Field(default=True, description="Whether this builtin is active")


class MarkableBuiltinCommand(FrozenModel):
    """A markable kanpan builtin (push, delete).

    Constructed only internally in ``tui._BUILTIN_COMMANDS``. Markable is a
    required color string by construction; key press toggles a mark, and
    later ``_submit_batch_item`` dispatches based on ``role``.
    """

    kind: Literal["markable_builtin"] = "markable_builtin"
    role: MarkableBuiltinRole = Field(description="Which markable builtin this is; drives batch dispatch.")
    name: str = Field(description="Display name shown in the status bar")
    enabled: bool = Field(default=True, description="Whether this builtin is active")
    markable: str = Field(description="Mark indicator color (e.g. 'light red').")


KanpanCommand = Annotated[CustomCommand | ActionBuiltinCommand | MarkableBuiltinCommand, Field(discriminator="kind")]


class KanpanPluginConfig(PluginConfig):
    """Configuration for the kanpan plugin."""

    commands: dict[str, CustomCommand] = Field(
        default_factory=dict,
        description="Custom commands keyed by their trigger key",
    )
    column_order: list[str] | None = Field(
        default=None,
        description="Display order for columns. Uses field keys from data sources. "
        "Built-in column names: name, state. "
        "Data source field keys: commits_ahead, pr, ci, conflicts, unresolved, repo_path. "
        "If None, uses the default column order plus any user-configured columns.",
    )
    section_order: list[BoardSection] | None = Field(
        default=None,
        description="Display order for board sections. "
        "Valid names: PR_MERGED, PR_CLOSED, PR_BEING_REVIEWED, STILL_COOKING, PRS_FAILED, MUTED. "
        "If None, defaults to: PR_MERGED, PR_CLOSED, PR_BEING_REVIEWED, STILL_COOKING, PRS_FAILED, MUTED. "
        "Sections not listed are omitted.",
    )
    refresh_interval_seconds: float = Field(
        default=600.0,
        description="Seconds between periodic full refreshes (default 10 minutes)",
    )
    retry_cooldown_seconds: float = Field(
        default=60.0,
        description="Minimum seconds before retrying after a failed full refresh",
    )
    data_sources: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Data source configurations keyed by source name (e.g. 'github', 'repo_paths'). "
        "Each entry is a raw dict -- source-specific fields are parsed by the matching data source.",
    )
    shell_commands: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Shell command data sources keyed by field key. "
        "Each entry should have 'name', 'header', and 'command' (all str).",
    )

    columns: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Label-backed columns keyed by field key. "
        "Each entry should have 'header' (str) and optionally 'colors' (dict[str, str]).",
    )
    on_before_refresh: dict[str, Any] = Field(
        default_factory=dict,
        description="[deprecated] Before-refresh hooks - use data sources instead",
    )
    on_after_refresh: dict[str, Any] = Field(
        default_factory=dict,
        description="[deprecated] After-refresh hooks - use data sources instead",
    )

    def merge_with(self, override: "PluginConfig") -> "KanpanPluginConfig":
        """Merge this config with an override config."""
        if not isinstance(override, KanpanPluginConfig):
            return self
        merged_enabled = override.enabled if override.enabled is not None else self.enabled
        merged_commands = {**self.commands, **override.commands}
        merged_column_order = override.column_order if override.column_order is not None else self.column_order
        merged_section_order = override.section_order if override.section_order is not None else self.section_order
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
        merged_data_sources = {**self.data_sources, **override.data_sources}
        merged_shell_commands = {**self.shell_commands, **override.shell_commands}
        merged_columns = {**self.columns, **override.columns}
        merged_on_before_refresh = {**self.on_before_refresh, **override.on_before_refresh}
        merged_on_after_refresh = {**self.on_after_refresh, **override.on_after_refresh}
        return KanpanPluginConfig(
            enabled=merged_enabled,
            commands=merged_commands,
            column_order=merged_column_order,
            section_order=merged_section_order,
            refresh_interval_seconds=merged_refresh_interval,
            retry_cooldown_seconds=merged_auto_cooldown,
            data_sources=merged_data_sources,
            shell_commands=merged_shell_commands,
            columns=merged_columns,
            on_before_refresh=merged_on_before_refresh,
            on_after_refresh=merged_on_after_refresh,
        )
