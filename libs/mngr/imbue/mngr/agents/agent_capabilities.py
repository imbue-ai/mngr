from collections.abc import Sequence
from enum import auto
from typing import Final
from typing import assert_never

import pluggy
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.config.agent_class_registry import get_agent_class
from imbue.mngr.config.agent_class_registry import list_registered_agent_class_types
from imbue.mngr.config.agent_plugin_registry import get_agent_type_owner
from imbue.mngr.interfaces.agent import HasAutoInstallMixin
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.agent import HasPermissionPolicyMixin
from imbue.mngr.interfaces.agent import HasSessionPreservationMixin
from imbue.mngr.interfaces.agent import HasStreamingSnapshotMixin
from imbue.mngr.interfaces.agent import HasTranscriptMixin
from imbue.mngr.interfaces.agent import HasUnattendedModeMixin
from imbue.mngr.interfaces.agent import HasVersionManagementMixin
from imbue.mngr.interfaces.agent import HeadlessAgentMixin
from imbue.mngr.interfaces.agent import StreamingHeadlessAgentMixin

# The key that an agent_field_generators hookimpl uses for the waiting-reason field;
# a plugin that exposes a *different* field (e.g. kanpan's `muted`) does not count.
_WAITING_REASON_FIELD_KEY: Final[str] = "waiting_reason"
# A sibling usage plugin registers under the agent plugin's entry-point name + this suffix
# (e.g. the `claude` agent plugin is paired with the `claude_usage` plugin).
_USAGE_PLUGIN_SUFFIX: Final[str] = "_usage"
# The hookspec a usage plugin implements to claim an agent's usage source. Defined in
# the optional `mngr_usage` package, so it may be absent when usage is not installed.
_USAGE_SOURCE_HOOK: Final[str] = "aggregate_usage_source"

# Agent types excluded from the matrix: task-specialized skill variants that reuse a
# parent agent's class wholesale (only injecting a SKILL.md), plus mngr-proxy-child (an
# internal proxy, not a user-facing port). They are not distinct enough to warrant their
# own column -- a reader wants the parent's row. (headless_claude is deliberately NOT here:
# it runs `claude --print` with genuinely different logic, so its capabilities can
# legitimately diverge from claude's and are worth showing.)
_NON_MATRIX_AGENT_TYPES: Final[frozenset[str]] = frozenset({"code-guardian", "fixme-fairy", "mngr-proxy-child"})

# The fixed left-to-right column order for the matrix: the primary Claude ports first,
# then the other CLI ports, with the thin shell-command runners last. Every registered
# agent type must appear here or in _NON_MATRIX_AGENT_TYPES, or rendering raises -- so a
# newly added agent can never be silently dropped from the table.
_MATRIX_AGENT_TYPE_ORDER: Final[tuple[str, ...]] = (
    "claude",
    "headless_claude",
    "antigravity",
    "codex",
    "opencode",
    "pi-coding",
    "command",
    "headless_command",
)


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

    @model_validator(mode="after")
    def _require_detection_field(self) -> "AgentCapability":
        # Fail at construction (not at detection time) if the kind's required field is missing.
        if self.detection_kind == CapabilityDetectionKind.CLASS_MIXIN and self.mixin is None:
            raise AgentCapabilityError(f"Capability '{self.key}' is CLASS_MIXIN but has no mixin")
        if self.detection_kind == CapabilityDetectionKind.PLUGIN_HOOKIMPL and self.hook_name is None:
            raise AgentCapabilityError(f"Capability '{self.key}' is PLUGIN_HOOKIMPL but has no hook_name")
        return self


class AgentClassInfo(FrozenModel):
    """Everything a capability detector needs to judge one registered agent type."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    agent_type_name: str = Field(description="The registered agent type name (e.g. 'claude')")
    agent_class: type = Field(description="The agent class registered for this type")
    # Agent type names whose plugin exposes an `agent_field_generators` hookimpl.
    field_generator_agent_type_names: frozenset[str] = Field(
        description="Agent type names that have a waiting_reason field generator"
    )
    # Hook names that the agent's owning plugin (entry point) implements (for PLUGIN_HOOKIMPL detection).
    plugin_hook_names: frozenset[str] = Field(description="Hook names implemented by this agent's owning plugin")
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


# The ordered capability registry, in the row order the generated matrix uses. New
# capabilities are appended here, except that the two headless-output rows are kept last
# (they apply only to headless agent variants). The generated matrix and its drift guard
# read directly from this list, so the matrix can never silently disagree with the code.
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
        key="waiting_reason_field",
        description="Surfaces why a WAITING agent is blocked (PERMISSIONS vs END_OF_TURN) in `mngr list`. Wanted if the CLI prompts for tool approval.",
        detection_kind=CapabilityDetectionKind.FIELD_GENERATOR,
    ),
    AgentCapability(
        key="streaming_snapshot",
        description="Publishes a live, in-progress view of the agent's assistant text. Lowest-priority; only needed if a consuming UI wants live streaming.",
        detection_kind=CapabilityDetectionKind.CLASS_MIXIN,
        mixin=HasStreamingSnapshotMixin,
    ),
    AgentCapability(
        key="session_preservation",
        description="Preserves session/transcript files when the agent is destroyed, so the conversation is not lost. Baseline; every port wants it.",
        detection_kind=CapabilityDetectionKind.CLASS_MIXIN,
        mixin=HasSessionPreservationMixin,
    ),
    AgentCapability(
        key="auto_install",
        description="Installs its CLI binary at provision time if missing (gated by consent locally, a config flag remotely). Baseline; every real agent wants it.",
        detection_kind=CapabilityDetectionKind.CLASS_MIXIN,
        mixin=HasAutoInstallMixin,
    ),
    AgentCapability(
        key="unattended_operation",
        description="Can complete a run with no human by auto-allowing in-run tool prompts. The load-bearing capability for remote / scheduled / headless agents.",
        detection_kind=CapabilityDetectionKind.CLASS_MIXIN,
        mixin=HasUnattendedModeMixin,
    ),
    AgentCapability(
        key="permission_policy",
        description="Supports a per-resource allow/deny/ask permission policy (a refinement on plain auto-allow). Only some CLIs expose per-tool config.",
        detection_kind=CapabilityDetectionKind.CLASS_MIXIN,
        mixin=HasPermissionPolicyMixin,
    ),
    AgentCapability(
        key="version_management",
        description="Controls which version of its binary runs, by pinning a version or following an update policy. Absent for CLIs that just use whatever is on PATH.",
        detection_kind=CapabilityDetectionKind.CLASS_MIXIN,
        mixin=HasVersionManagementMixin,
    ),
    AgentCapability(
        key="deploy_contributions",
        description="Bakes config/cred files + env vars into a `mngr schedule` image (via the get_files_for_deploy hookimpl). Only needed if the agent runs under `mngr schedule`.",
        detection_kind=CapabilityDetectionKind.PLUGIN_HOOKIMPL,
        hook_name="get_files_for_deploy",
    ),
    AgentCapability(
        key="usage_tracking",
        description="Emits token/cost usage that `mngr usage` aggregates (via a sibling `mngr_<harness>_usage` plugin). Wanted so the agent's spend is visible.",
        detection_kind=CapabilityDetectionKind.USAGE_SOURCE,
    ),
    # Headless-output rows are kept last: they apply only to headless agent variants.
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
)


def render_capability_matrix(
    capabilities: Sequence[AgentCapability],
    infos: Sequence[AgentClassInfo],
) -> str:
    """Render a markdown matrix of capability (rows) x agent type (columns), with Y/- cells."""
    unordered = sorted(i.agent_type_name for i in infos if i.agent_type_name not in _MATRIX_AGENT_TYPE_ORDER)
    if unordered:
        raise AgentCapabilityError(
            f"Agent type(s) {unordered} are not in _MATRIX_AGENT_TYPE_ORDER and not excluded "
            "via _NON_MATRIX_AGENT_TYPES; add each to one or the other."
        )
    sorted_infos = sorted(infos, key=lambda i: _MATRIX_AGENT_TYPE_ORDER.index(i.agent_type_name))
    agent_type_names = [info.agent_type_name for info in sorted_infos]

    header_row = "| Capability | " + " | ".join(agent_type_names) + " |"
    separator_row = "|" + "---|" * (len(agent_type_names) + 1)

    body_rows: list[str] = []
    for capability in capabilities:
        cells = ["Y" if is_capability_present(capability, info) else "-" for info in sorted_infos]
        body_rows.append(f"| {capability.key} | " + " | ".join(cells) + " |")

    return "\n".join([header_row, separator_row, *body_rows])


def _hook_implementer_plugin_names(pm: pluggy.PluginManager, hook_name: str) -> frozenset[str]:
    """Return the entry-point names of the plugins that implement ``hook_name``.

    Returns empty if the hook is not registered at all -- the usage hookspec lives
    in the optional ``mngr_usage`` package and may be absent.
    """
    hook_relay = pm.hook
    if not hasattr(hook_relay, hook_name):
        return frozenset()
    hook_caller = pm.subset_hook_caller(hook_name, remove_plugins=[])
    return frozenset(impl.plugin_name for impl in hook_caller.get_hookimpls())


def build_agent_class_infos(
    pm: pluggy.PluginManager,
    capabilities: Sequence[AgentCapability] = AGENT_CAPABILITIES,
) -> tuple[AgentClassInfo, ...]:
    """Build one AgentClassInfo per registered agent type from a loaded plugin manager.

    Module-level capabilities are keyed by the agent's owning plugin entry-point name
    (e.g. ``claude``), matched against the entry-point names that implement the hook
    (deploy) or that pair a ``<owner>_usage`` sibling plugin (usage). This stays at the
    entry-point granularity that the matrix's columns are themselves keyed by.
    """
    # Agent type names whose plugin exposes a waiting_reason field generator (not some other field).
    field_generator_agent_type_names = frozenset(
        result[0]
        for result in pm.hook.agent_field_generators()
        if result is not None and _WAITING_REASON_FIELD_KEY in result[1]
    )

    # Entry-point names implementing each PLUGIN_HOOKIMPL hook, and each usage source.
    hook_names = frozenset(c.hook_name for c in capabilities if c.hook_name is not None)
    plugin_names_by_hook = {name: _hook_implementer_plugin_names(pm, name) for name in hook_names}
    usage_plugin_names = _hook_implementer_plugin_names(pm, _USAGE_SOURCE_HOOK)

    infos: list[AgentClassInfo] = []
    for agent_type_name in list_registered_agent_class_types():
        if agent_type_name in _NON_MATRIX_AGENT_TYPES:
            continue
        owner = get_agent_type_owner(agent_type_name)
        # Only plugin-registered agent types appear in the matrix. A type registered
        # directly (no owner) -- e.g. a test placeholder -- is not a shipped agent.
        if owner is None:
            continue
        plugin_hook_names = frozenset(
            hook_name for hook_name in hook_names if owner in plugin_names_by_hook[hook_name]
        )
        infos.append(
            AgentClassInfo(
                agent_type_name=agent_type_name,
                agent_class=get_agent_class(agent_type_name),
                field_generator_agent_type_names=field_generator_agent_type_names,
                plugin_hook_names=plugin_hook_names,
                is_usage_source_claimed=(owner + _USAGE_PLUGIN_SUFFIX) in usage_plugin_names,
            )
        )
    return tuple(infos)


_GENERATED_DOC_HEADER: Final[str] = """# Agent capabilities

<!-- GENERATED FILE -- do not edit by hand.
     Regenerate with `just regenerate-agent-capabilities-doc` (see `mngr.agents.agent_capabilities`). -->

Which agent types implement which capabilities, **derived from the code** (the agent classes
and their plugins), not maintained by hand. A `Y` means the capability is present; `-` means
absent. See `specs/agent-plugin-parity/capability-mixins.md` for the design.
"""


def generate_capability_matrix_doc(
    capabilities: Sequence[AgentCapability],
    infos: Sequence[AgentClassInfo],
) -> str:
    """Render the full generated `agent_capabilities.md` document (header + matrix + descriptions)."""
    matrix = render_capability_matrix(capabilities, infos)
    description_lines = [f"- **{capability.key}** -- {capability.description}" for capability in capabilities]
    return "\n".join([_GENERATED_DOC_HEADER, matrix, "", "## Capabilities", "", *description_lines, ""])
