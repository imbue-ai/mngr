from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Literal
from typing import Protocol
from typing import runtime_checkable

from loguru import logger
from pydantic import Field
from pydantic import TypeAdapter
from pydantic import ValidationError

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


class OldestCreatedNoInputsError(KanpanDataSourceError, ValueError):
    """Raised when oldest_created is called with no non-None inputs."""

    ...


class CellDisplay(FrozenModel):
    """Everything the column renderer needs for one cell."""

    text: str = Field(description="Display text for the cell")
    url: str | None = Field(default=None, description="Optional hyperlink URL")
    color: str | None = Field(default=None, description="Optional urwid color attribute name")


class FieldValue(FrozenModel):
    """Base for all field values. Subclass per data type."""

    # Required (no default): forgetting to propagate `created` from cached
    # inputs to derived values would silently mark stale data as fresh.
    # Making this required means pydantic raises ValidationError at construction
    # if a code path forgets it.
    created: datetime = Field(
        description="Timezone-aware UTC timestamp of when this value was computed. "
        "For values derived from cached fields, must be the min of the inputs' created.",
    )

    def display(self) -> CellDisplay:
        return CellDisplay(text=str(self))

    def env_vars(self, key: str) -> dict[str, str]:
        """Return env var name -> value pairs for shell command injection.

        The default implementation exposes display text as MNGR_FIELD_{KEY}.
        Subclasses may override to provide more structured env vars (e.g. PR number, URL).
        """
        return {f"MNGR_FIELD_{key.upper()}": self.display().text}


def now_utc() -> datetime:
    """Current UTC timestamp. Helper to keep call sites short."""
    return datetime.now(timezone.utc)


def oldest_created(*fields: FieldValue | None) -> datetime:
    """Minimum 'created' across non-None inputs.

    Raises OldestCreatedNoInputsError if all inputs are None -- callers
    should pass now_utc() explicitly when there are no cached inputs to
    inherit from, rather than relying on a silent fallback.
    """
    timestamps = [f.created for f in fields if f is not None]
    if not timestamps:
        raise OldestCreatedNoInputsError("oldest_created requires at least one non-None FieldValue input")
    return min(timestamps)


class StringField(FieldValue):
    """Simple string field for shell data sources and similar."""

    kind: Literal["string"] = Field(default="string", description="Discriminator tag")
    value: str = Field(description="The string value")

    def display(self) -> CellDisplay:
        return CellDisplay(text=self.value)

    def env_vars(self, key: str) -> dict[str, str]:
        return {f"MNGR_FIELD_{key.upper()}": self.value}


class BoolField(FieldValue):
    """Boolean field (e.g. muted state)."""

    kind: Literal["bool"] = Field(default="bool", description="Discriminator tag")
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
    def field_types(self) -> dict[str, TypeAdapter[FieldValue]]:
        """Field key -> TypeAdapter that validates raw payloads for this slot.

        A "slot" (e.g. FIELD_PR) can be polymorphic: it may hold a real PrField,
        or a sentinel like CreatePrUrlField / PrFetchFailedField. Build a
        discriminated union for the slot using pydantic's standard pattern --
        every FieldValue subclass declares ``kind: Literal["..."]`` and the
        adapter is constructed as::

            TypeAdapter(Annotated[
                PrField | CreatePrUrlField | PrFetchFailedField,
                Field(discriminator="kind"),
            ])

        Single-class slots use ``TypeAdapter(SomeField)`` directly.
        """
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
    field_types: dict[str, TypeAdapter[FieldValue]],
) -> dict[str, FieldValue]:
    """Deserialize a dict of raw JSON dicts into typed FieldValue objects.

    ``field_types`` maps each slot to a pydantic ``TypeAdapter``. For
    polymorphic slots the adapter wraps a discriminated union keyed on the
    ``kind`` field; for single-class slots it wraps the class directly.
    Pydantic picks the right concrete class via the discriminator (no
    order-sensitive trial validation). Keys not present in field_types are
    skipped; payloads that fail validation are logged and dropped.
    """
    result: dict[str, FieldValue] = {}
    for key, value in raw.items():
        adapter = field_types.get(key)
        if adapter is None:
            continue
        try:
            result[key] = adapter.validate_python(value)
        except ValidationError as e:
            logger.debug("deserialize_fields: validation failed for key {!r}: {}", key, e)
    return result
