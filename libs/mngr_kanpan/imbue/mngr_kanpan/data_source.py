from collections.abc import Sequence
from enum import auto
from typing import Any
from typing import Protocol
from typing import runtime_checkable

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentName


class KanpanDataSourceError(Exception):
    """Base exception for kanpan data source errors."""

    ...


class KanpanFieldTypeError(KanpanDataSourceError, TypeError):
    """Raised when a field has an unexpected type during section classification."""

    ...


class CellDisplay(FrozenModel):
    """Everything the column renderer needs for one cell."""

    text: str = Field(description="Display text for the cell")
    url: str | None = Field(default=None, description="Optional hyperlink URL")
    color: str | None = Field(default=None, description="Optional urwid color attribute name")


class FieldValue(FrozenModel):
    """Base for all field values. Subclass per data type."""

    def display(self) -> CellDisplay:
        return CellDisplay(text=str(self))


class StringField(FieldValue):
    """Simple string field for shell data sources and similar."""

    value: str = Field(description="The string value")

    def display(self) -> CellDisplay:
        return CellDisplay(text=self.value)


class BoolField(FieldValue):
    """Boolean field (e.g. muted state)."""

    value: bool = Field(description="The boolean value")

    def display(self) -> CellDisplay:
        return CellDisplay(text="yes" if self.value else "no")


class PrState(UpperCaseStrEnum):
    """State of a GitHub pull request."""

    OPEN = auto()
    CLOSED = auto()
    MERGED = auto()


class CiStatus(UpperCaseStrEnum):
    """Aggregate CI check status for a PR."""

    PASSING = auto()
    FAILING = auto()
    PENDING = auto()
    UNKNOWN = auto()

    @property
    def color(self) -> str | None:
        return {
            CiStatus.PASSING: "light green",
            CiStatus.FAILING: "light red",
            CiStatus.PENDING: "yellow",
        }.get(self)


class PrField(FieldValue):
    """GitHub pull request field value."""

    number: int = Field(description="PR number")
    url: str = Field(description="PR URL")
    is_draft: bool = Field(description="Whether the PR is a draft")
    title: str = Field(description="PR title")
    state: PrState = Field(description="PR state (open/closed/merged)")
    head_branch: str = Field(description="Head branch name of the PR")

    def display(self) -> CellDisplay:
        return CellDisplay(text=f"#{self.number}", url=self.url)


class CiField(FieldValue):
    """CI check status field value."""

    status: CiStatus = Field(description="Aggregate CI check status")

    def display(self) -> CellDisplay:
        if self.status == CiStatus.UNKNOWN:
            return CellDisplay(text="")
        return CellDisplay(text=self.status.lower(), color=self.status.color)


class CreatePrUrlField(FieldValue):
    """URL to create a new PR for a branch."""

    url: str = Field(description="URL to create a PR")

    def display(self) -> CellDisplay:
        return CellDisplay(text="+PR", url=self.url)


class RepoPathField(FieldValue):
    """GitHub repository path (owner/repo) for an agent."""

    path: str = Field(description="GitHub owner/repo path")

    def display(self) -> CellDisplay:
        return CellDisplay(text=self.path)


class CommitsAheadField(FieldValue):
    """Number of commits ahead of the remote tracking branch."""

    count: int | None = Field(description="Commits ahead count, None if unknown")
    has_work_dir: bool = Field(default=True, description="Whether the agent has a local work directory")

    def display(self) -> CellDisplay:
        if not self.has_work_dir:
            return CellDisplay(text="")
        if self.count is None:
            return CellDisplay(text="[not pushed]")
        if self.count == 0:
            return CellDisplay(text="[up to date]")
        return CellDisplay(text=f"[{self.count} unpushed]")


class ConflictsField(FieldValue):
    """Merge conflict status for a PR."""

    has_conflicts: bool = Field(description="Whether the PR has merge conflicts")

    def display(self) -> CellDisplay:
        if self.has_conflicts:
            return CellDisplay(text="YES", color="light red")
        return CellDisplay(text="no", color="light green")


class UnresolvedField(FieldValue):
    """Unresolved review comment status for a PR."""

    has_unresolved: bool = Field(description="Whether the PR has unresolved review comments")

    def display(self) -> CellDisplay:
        if self.has_unresolved:
            return CellDisplay(text="YES", color="light red")
        return CellDisplay(text="no", color="light green")


@runtime_checkable
class KanpanDataSource(Protocol):
    """Protocol for kanpan data sources.

    Each data source produces typed fields for agents on the board.
    Fields are persisted per-source and loaded as cached input for subsequent cycles.
    """

    @property
    def name(self) -> str:
        """Unique identifier for this data source."""
        ...

    @property
    def columns(self) -> dict[str, str]:
        """Field key -> column header. Each entry becomes a column."""
        ...

    @property
    def field_types(self) -> dict[str, type[FieldValue]]:
        """Field key -> FieldValue subclass, for deserialization via model_validate()."""
        ...

    def compute(
        self,
        agents: tuple[AgentDetails, ...],
        cached_fields: dict[AgentName, dict[str, FieldValue]],
        mngr_ctx: MngrContext,
    ) -> tuple[dict[AgentName, dict[str, FieldValue]], Sequence[str]]:
        """Compute field values for agents.

        Returns (fields_by_agent, errors).
        Data sources read cached fields from the *previous* refresh cycle.
        All data sources run in parallel; they do not see each other's current output.
        """
        ...


# Well-known field keys used by multiple components (section logic, TUI rendering, etc.)
FIELD_MUTED = "muted"
FIELD_PR = "pr"
FIELD_CI = "ci"
FIELD_CREATE_PR_URL = "create_pr_url"
FIELD_REPO_PATH = "repo_path"
FIELD_COMMITS_AHEAD = "commits_ahead"
FIELD_CONFLICTS = "conflicts"
FIELD_UNRESOLVED = "unresolved"


def deserialize_fields(
    raw: dict[str, Any],
    field_types: dict[str, type[FieldValue]],
) -> dict[str, FieldValue]:
    """Deserialize a dict of raw JSON dicts into typed FieldValue objects.

    Keys not present in field_types are skipped.
    """
    result: dict[str, FieldValue] = {}
    for key, value in raw.items():
        field_type = field_types.get(key)
        if field_type is None:
            continue
        result[key] = field_type.model_validate(value)
    return result
