from collections.abc import Sequence
from enum import auto
from pathlib import Path
from typing import Annotated
from typing import Any
from typing import Literal

from pydantic import Field
from pydantic import SerializeAsAny

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.config.data_types import PluginConfig
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_kanpan.data_source import CellDisplay
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.errors import KanpanError


class KanpanConfigError(KanpanError, ValueError):
    """Raised when kanpan's plugin config holds a value the board cannot run with."""

    ...


class BoardSection(UpperCaseStrEnum):
    """Sections for grouping agents on the board, based on PR state."""

    STILL_COOKING = auto()
    PR_DRAFT = auto()
    PRS_FAILED = auto()
    PR_BEING_REVIEWED = auto()
    PR_MERGED = auto()
    PR_CLOSED = auto()
    MUTED = auto()


# Section labels split into a leading phrase and a clarifying suffix. The TUI
# heading renderer colors the prefix; the JSON output path joins them into a
# plain human label.
SECTION_PREFIX: dict[BoardSection, str] = {
    BoardSection.PR_MERGED: "Done",
    BoardSection.PR_CLOSED: "Cancelled",
    BoardSection.PR_BEING_REVIEWED: "In review",
    BoardSection.PR_DRAFT: "In progress",
    BoardSection.STILL_COOKING: "In progress",
    BoardSection.PRS_FAILED: "In progress",
    BoardSection.MUTED: "Muted",
}

SECTION_SUFFIX: dict[BoardSection, str] = {
    BoardSection.PR_MERGED: "PR merged",
    BoardSection.PR_CLOSED: "PR closed",
    BoardSection.PR_BEING_REVIEWED: "PR pending",
    BoardSection.PR_DRAFT: "draft PR",
    BoardSection.STILL_COOKING: "no PR yet",
    BoardSection.PRS_FAILED: "PRs not loaded",
    BoardSection.MUTED: "",
}


def section_label(section: BoardSection) -> str:
    """Human-readable label for a board section, e.g. ``Done - PR merged``.

    Mirrors the text the TUI heading shows (minus the agent count). Sections
    with no suffix (e.g. MUTED) return just the prefix.
    """
    prefix = SECTION_PREFIX[section]
    suffix = SECTION_SUFFIX[section]
    return f"{prefix} - {suffix}" if suffix else prefix


class AgentBoardEntry(FrozenModel):
    """A single agent entry on the kanpan board."""

    name: AgentName = Field(description="Agent name")
    state: AgentLifecycleState = Field(description="Agent lifecycle state")
    provider_name: ProviderInstanceName = Field(description="Provider instance name")
    work_dir: Path | None = Field(default=None, description="Local work directory (None for remote agents)")
    branch: str | None = Field(default=None, description="Git branch for this agent")
    is_muted: bool = Field(default=False, description="Whether the agent is muted (relegated to bottom)")
    fields: dict[str, SerializeAsAny[FieldValue]] = Field(
        default_factory=dict,
        description="Field values from data sources. SerializeAsAny so model_dump emits each "
        "FieldValue subclass's full payload (incl. its `kind` discriminator) rather than only "
        "the FieldValue base fields.",
    )
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


@pure
def group_entries_by_section(
    snapshot: BoardSnapshot,
    section_order: Sequence[BoardSection],
) -> list[tuple[BoardSection, list[AgentBoardEntry]]]:
    """Group entries by section in display order.

    Sections are returned in ``section_order``; empty sections are omitted, and
    entries within a section keep their snapshot order. Entries whose section is
    not in ``section_order`` are dropped, so the result is what the board shows.
    """
    by_section: dict[BoardSection, list[AgentBoardEntry]] = {}
    for entry in snapshot.entries:
        by_section.setdefault(entry.section, []).append(entry)
    return [(section, by_section[section]) for section in section_order if by_section.get(section)]


@pure
def entries_shown_on_board(
    snapshot: BoardSnapshot,
    section_order: Sequence[BoardSection],
) -> tuple[AgentBoardEntry, ...]:
    """The entries the board renders, in board order (by section, then snapshot order)."""
    return tuple(entry for _section, entries in group_entries_by_section(snapshot, section_order) for entry in entries)


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
        description="Shell command to run. MNGR_AGENT_NAME env var is set to the focused agent's name, and "
        "MNGR_INPUT to the text typed at the prompt (empty when `prompt` is unset).",
    )
    prompt: str = Field(
        default="",
        description="When non-empty, running the command first opens a one-line input using this text as "
        "the caption; the submitted text is passed to the command as the MNGR_INPUT env var. Combined with "
        "`markable`, the input is asked once when x executes and the answer applies to every marked agent.",
    )
    refresh_afterwards: bool = Field(default=False, description="Whether to trigger a board refresh after completion")
    enabled: bool = Field(default=True, description="Whether this command is active")
    markable: bool | str = Field(
        default=False,
        description="If set to anything other than false, pressing the key marks agents for batch execution with x"
        " instead of running immediately."
        " Set to a color name (e.g. 'light red') to customize the mark indicator color.",
    )

    @property
    def is_markable(self) -> bool:
        """Whether pressing the key toggles a mark instead of running the command.

        Any ``markable`` other than ``False`` marks; an empty color string marks too,
        with an empty mark-indicator color.
        """
        return self.markable is not False


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
    SEARCH = auto()


class MarkableBuiltinRole(UpperCaseStrEnum):
    """Identifies a markable builtin whose key press toggles a mark.

    Batch dispatch in ``_submit_batch_item`` uses ``match`` over this enum
    with ``assert_never`` so the type checker flags any missing branch when
    a new markable role is added.
    """

    PUSH = auto()
    DELETE = auto()


class ActionBuiltinCommand(FrozenModel):
    """A non-markable kanpan builtin (refresh, mute, unmark, execute, search).

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

# When `staleness_threshold_seconds` is unset, use this fraction of
# `refresh_interval_seconds` so values that weren't updated in the last cycle
# show as stale, but values that were just refreshed within their cycle don't
# briefly grey out near the cycle boundary.
STALENESS_FRACTION_OF_REFRESH_INTERVAL = 0.9

# Short enough that a board left open describes the fleet as it is rather than as the last
# full refresh found it, long enough that the read it costs stays a small share of the time
# the board spends idle.
DEFAULT_LOCAL_REFRESH_INTERVAL_SECONDS = 30.0


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
    header_status: str | None = Field(
        default=None,
        description="Text shown at the right of the header, e.g. "
        "'{state == \"RUNNING\"} running / {total}'. Each braced CEL expression renders as the number "
        "of agents the board is showing that it holds for, counted against the same entry shape "
        "`--format json` emits (name, state, provider_name, work_dir, branch, is_muted, section, "
        "fields, cells). That shape is narrower than what --include sees, so an expression naming "
        "anything else (labels, host, age) matches no agent and stays at zero. '{total}' counts "
        "every agent; '{{' and '}}' are literal braces. Unset (default) shows nothing.",
    )
    batch_concurrency: Annotated[int, Field(ge=1)] = Field(
        default=4,
        description="How many marked operations `x` runs at once. Marked agents are independent, so "
        "they need not wait for each other -- a command that blocks (e.g. `mngr message` waiting on "
        "an agent to accept) otherwise makes a batch take the sum of its parts. Raise it for more "
        "overlap, or set 1 to run them strictly one at a time.",
    )
    refresh_interval_seconds: Annotated[float, Field(gt=0)] = Field(
        default=600.0,
        description="Seconds between periodic full refreshes (default 10 minutes)",
    )
    local_refresh_interval_seconds: Annotated[float, Field(ge=0)] = Field(
        default=DEFAULT_LOCAL_REFRESH_INTERVAL_SECONDS,
        description="Seconds between periodic local refreshes, which run every local data "
        "source, so `STATE`, `commits_ahead`, label columns and any header count over them stay "
        "current between full refreshes. Remote columns (PR, CI, shell) are carried forward and "
        "keep the full refresh's cadence. A tick that lands while the previous one is still "
        "running is skipped rather than queued, so the interval can be shorter than a refresh "
        "takes. Set 0 to run these only in response to an action. A refresh costs roughly a "
        "second per few dozen agents and is spent whether or not anything changed, so shortening "
        "the interval trades that against how soon the board shows what it did not cause.",
    )
    retry_cooldown_seconds: float = Field(
        default=60.0,
        description="Minimum seconds before retrying after a failed full refresh",
    )
    staleness_threshold_seconds: float | None = Field(
        default=None,
        description="Field values whose `created` timestamp is older than this many seconds "
        "are rendered greyed-out to indicate they may be out of date. "
        "When unset (default), resolves to 90% of `refresh_interval_seconds` so that anything "
        "that wasn't updated in the last refresh cycle shows as stale. Set explicitly to override.",
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

    def check_refresh_intervals(self) -> None:
        """Reject a periodic interval the board's alarm chains could never advance past.

        Plugin config is built with `model_construct`, which skips the bound declared on the
        field, so an out-of-range interval arrives here intact. Both chains re-arm on firing, so
        an alarm that is always due leaves urwid's loop no iteration in which it is idle -- and
        idle is when the screen repaints, so the board pegs a core and freezes. Zero arms no
        alarm at all, which is why the local refresh takes it as the way to ask for none and the
        full refresh, having no such off switch, does not.
        """
        if self.refresh_interval_seconds <= 0:
            raise KanpanConfigError(
                "plugins.kanpan.refresh_interval_seconds must be greater than zero, "
                f"but is {self.refresh_interval_seconds}"
            )
        if self.local_refresh_interval_seconds < 0:
            raise KanpanConfigError(
                "plugins.kanpan.local_refresh_interval_seconds cannot be negative, "
                f"but is {self.local_refresh_interval_seconds}; use 0 to run no periodic local refreshes"
            )

    def effective_staleness_threshold_seconds(self) -> float:
        """Resolved staleness threshold: explicit value, or
        ``STALENESS_FRACTION_OF_REFRESH_INTERVAL * refresh_interval_seconds``.
        """
        if self.staleness_threshold_seconds is not None:
            return self.staleness_threshold_seconds
        return STALENESS_FRACTION_OF_REFRESH_INTERVAL * self.refresh_interval_seconds
