from collections.abc import Sequence
from enum import auto
from typing import Final
from typing import assert_never

from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.agent import HasTranscriptMixin
from imbue.mngr.interfaces.agent import HeadlessAgentMixin
from imbue.mngr.interfaces.agent import StreamingHeadlessAgentMixin


class CapabilityDetectionKind(UpperCaseStrEnum):
    """How the presence of a capability is determined from an agent's code."""

    # The agent class inherits a capability mixin (issubclass check).
    CLASS_MIXIN = auto()
    # The agent's plugin implements the `agent_field_generators` hookimpl,
    # which returns a `waiting_reason`-style field keyed by agent type name.
    FIELD_GENERATOR = auto()
    # The agent's own plugin module implements a named hookimpl
    # (e.g. `get_files_for_deploy`).
    PLUGIN_HOOKIMPL = auto()
    # A sibling `mngr_<harness>_usage` plugin claims this agent's usage source.
    USAGE_SOURCE = auto()


class AgentCapability(FrozenModel):
    """A discrete unit of agent functionality whose presence the matrix tracks."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    key: str = Field(description="Stable matrix-row name for this capability")
    description: str = Field(
        description="One line: what the capability does, and whether a new port normally wants it"
    )
    detection_kind: CapabilityDetectionKind = Field(description="How presence is determined from the code")
    # Required when detection_kind is CLASS_MIXIN; the mixin an agent class inherits to have this capability.
    mixin: type | None = Field(default=None, description="The capability mixin for CLASS_MIXIN detection")
    # Required when detection_kind is PLUGIN_HOOKIMPL; the hook the agent's plugin must implement.
    hook_name: str | None = Field(default=None, description="The pluggy hook name for PLUGIN_HOOKIMPL detection")


class AgentClassInfo(FrozenModel):
    """Everything a capability detector needs to judge one registered agent type."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    agent_type_name: str = Field(description="The registered agent type name (e.g. 'claude')")
    agent_class: type = Field(description="The agent class registered for this type")
    # Agent type names whose plugin exposes an `agent_field_generators` hookimpl.
    field_generator_agent_type_names: frozenset[str] = Field(
        description="Agent type names that have a waiting_reason field generator"
    )
    # Hook names the agent's own plugin module implements (for PLUGIN_HOOKIMPL detection).
    plugin_hook_names: frozenset[str] = Field(description="Hook names implemented by this agent's plugin module")
    # Whether a sibling usage plugin claims this agent's usage source.
    is_usage_source_claimed: bool = Field(description="Whether a mngr_<harness>_usage plugin covers this agent")


def is_capability_present(capability: AgentCapability, info: AgentClassInfo) -> bool:
    """Determine whether the given agent type has the given capability, from its code structure."""
    match capability.detection_kind:
        case CapabilityDetectionKind.CLASS_MIXIN:
            if capability.mixin is None:
                raise AgentCapabilityError(f"Capability '{capability.key}' is CLASS_MIXIN but has no mixin")
            return issubclass(info.agent_class, capability.mixin)
        case CapabilityDetectionKind.FIELD_GENERATOR:
            return info.agent_type_name in info.field_generator_agent_type_names
        case CapabilityDetectionKind.PLUGIN_HOOKIMPL:
            if capability.hook_name is None:
                raise AgentCapabilityError(f"Capability '{capability.key}' is PLUGIN_HOOKIMPL but has no hook_name")
            return capability.hook_name in info.plugin_hook_names
        case CapabilityDetectionKind.USAGE_SOURCE:
            return info.is_usage_source_claimed
        case _ as unreachable:
            assert_never(unreachable)


class AgentCapabilityError(Exception):
    """Raised when the agent capability registry is misconfigured."""

    ...


# The ordered capability registry. New capabilities are appended here; the
# generated matrix and its drift guard read directly from this list, so the
# matrix can never silently disagree with the code.
AGENT_CAPABILITIES: Final[tuple[AgentCapability, ...]] = (
    AgentCapability(
        key="raw_transcript",
        description="Copies the agent's native session JSONL verbatim into the agent state dir. Baseline; every port wants it.",
        detection_kind=CapabilityDetectionKind.CLASS_MIXIN,
        mixin=HasTranscriptMixin,
    ),
    AgentCapability(
        key="common_transcript",
        description="Emits the agent-agnostic common transcript that `mngr transcript` renders. Baseline; every port wants it.",
        detection_kind=CapabilityDetectionKind.CLASS_MIXIN,
        mixin=HasCommonTranscriptMixin,
    ),
    AgentCapability(
        key="headless_output",
        description="Runs non-interactively and exposes its output via output(). Only for headless agent variants.",
        detection_kind=CapabilityDetectionKind.CLASS_MIXIN,
        mixin=HeadlessAgentMixin,
    ),
    AgentCapability(
        key="streaming_headless_output",
        description="A headless agent that also streams output incrementally. Only for headless agent variants.",
        detection_kind=CapabilityDetectionKind.CLASS_MIXIN,
        mixin=StreamingHeadlessAgentMixin,
    ),
    AgentCapability(
        key="waiting_reason_field",
        description="Surfaces why a WAITING agent is blocked (PERMISSIONS vs END_OF_TURN) in `mngr list`. Wanted if the CLI prompts for tool approval.",
        detection_kind=CapabilityDetectionKind.FIELD_GENERATOR,
    ),
)


def render_capability_matrix(
    capabilities: Sequence[AgentCapability],
    infos: Sequence[AgentClassInfo],
) -> str:
    """Render a markdown matrix of capability (rows) x agent type (columns), with Y/- cells."""
    agent_type_names = [info.agent_type_name for info in sorted(infos, key=lambda i: i.agent_type_name)]
    sorted_infos = sorted(infos, key=lambda i: i.agent_type_name)

    header_row = "| Capability | " + " | ".join(agent_type_names) + " |"
    separator_row = "|" + "---|" * (len(agent_type_names) + 1)

    body_rows: list[str] = []
    for capability in capabilities:
        cells = ["Y" if is_capability_present(capability, info) else "-" for info in sorted_infos]
        body_rows.append(f"| {capability.key} | " + " | ".join(cells) + " |")

    return "\n".join([header_row, separator_row, *body_rows])
