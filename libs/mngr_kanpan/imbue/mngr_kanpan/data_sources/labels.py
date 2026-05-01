from collections.abc import Sequence

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentName
from imbue.mngr_kanpan.data_source import CellDisplay
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_source import now_utc


class LabelColumnConfig(FrozenModel):
    """Configuration for a single label-backed column."""

    header: str = Field(description="Column header text")
    label_key: str = Field(description="Agent label key to read from agent labels dict")
    colors: dict[str, str] = Field(default_factory=dict, description="Mapping from label value to urwid color name")


class _ColoredStringField(FieldValue):
    """String field with an optional color from a color map."""

    value: str = Field(description="The string value")
    color: str | None = Field(default=None, description="Optional urwid color name")

    def display(self) -> CellDisplay:
        return CellDisplay(text=self.value, color=self.color)


class LabelsDataSource(FrozenModel):
    """Reads agent labels and produces colored string fields.

    Each configured label key becomes a column. Values are colored
    according to the color map in the config.
    """

    field_key: str = Field(description="Field key for this label column")
    config: LabelColumnConfig = Field(description="Label column configuration")

    @property
    def name(self) -> str:
        return f"label_{self.field_key}"

    @property
    def is_remote(self) -> bool:
        return False

    @property
    def columns(self) -> dict[str, str]:
        return {self.field_key: self.config.header}

    @property
    def field_types(self) -> dict[str, type[FieldValue]]:
        return {self.field_key: _ColoredStringField}

    def compute(
        self,
        agents: tuple[AgentDetails, ...],
        cached_fields: dict[AgentName, dict[str, FieldValue]],
        mngr_ctx: MngrContext,
    ) -> tuple[dict[AgentName, dict[str, FieldValue]], Sequence[str]]:
        label_key = self.config.label_key
        now = now_utc()
        fields: dict[AgentName, dict[str, FieldValue]] = {}
        for agent in agents:
            value = agent.labels.get(label_key, "")
            if value:
                color = self.config.colors.get(value)
                fields[agent.name] = {
                    self.field_key: _ColoredStringField(value=value, color=color, created=now),
                }
        return fields, []
