from collections.abc import Sequence
from typing import Any
from typing import Protocol
from typing import runtime_checkable

from pydantic import Field

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

    def env_vars(self, key: str) -> dict[str, str]:
        """Return env var name -> value pairs for shell command injection.

        The default implementation exposes display text as MNGR_FIELD_{KEY}.
        Subclasses may override to provide more structured env vars (e.g. PR number, URL).
        """
        return {f"MNGR_FIELD_{key.upper()}": self.display().text}


class StringField(FieldValue):
    """Simple string field for shell data sources and similar."""

    value: str = Field(description="The string value")

    def display(self) -> CellDisplay:
        return CellDisplay(text=self.value)

    def env_vars(self, key: str) -> dict[str, str]:
        return {f"MNGR_FIELD_{key.upper()}": self.value}


class BoolField(FieldValue):
    """Boolean field (e.g. muted state)."""

    value: bool = Field(description="The boolean value")

    def display(self) -> CellDisplay:
        return CellDisplay(text="yes" if self.value else "no")


@runtime_checkable
class KanpanDataSource(Protocol):
    """Protocol for kanpan data sources.

    Each data source produces typed fields for agents on the board.
    Cached fields from the previous cycle are passed in-memory via the TUI state.
    """

    @property
    def name(self) -> str:
        """Unique identifier for this data source."""
        ...

    @property
    def is_remote(self) -> bool:
        """Whether this data source requires network access (e.g. GitHub API).

        Local-only refreshes skip remote data sources for speed.
        Defaults to False (local).
        """
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
