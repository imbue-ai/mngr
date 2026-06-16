from collections.abc import Sequence
from enum import auto
from types import ModuleType
from typing import Final
from typing import assert_never
from typing import cast

import pluggy
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
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
# A sibling usage plugin lives in the agent plugin's package + this suffix.
_USAGE_PACKAGE_SUFFIX: Final[str] = "_usage"


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
    # Hook names the agent's own plugin package implements (for PLUGIN_HOOKIMPL detection).
    plugin_hook_names: frozenset[str] = Field(description="Hook names implemented by this agent's plugin package")
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


def _module_package(module: ModuleType) -> str:
    """Return a plugin module's package (e.g. 'imbue.mngr_claude' for '...mngr_claude.plugin')."""
    module_name = getattr(module, "__name__", "")
    return module_name.rsplit(".", 1)[0] if "." in module_name else module_name


def build_agent_class_infos(
    pm: pluggy.PluginManager,
    capabilities: Sequence[AgentCapability] = AGENT_CAPABILITIES,
) -> tuple[AgentClassInfo, ...]:
    """Build one AgentClassInfo per registered agent type from a loaded plugin manager."""
    # Map each registered agent type to its class and the plugin module that registered it.
    # pluggy types hookimpl.function() as returning `object`, so re-apply the declared shape.
    class_and_plugin_by_type: dict[str, tuple[type, ModuleType]] = {}
    for hookimpl in pm.hook.register_agent_type.get_hookimpls():
        registration = cast("tuple[str, type | None, type | None] | None", hookimpl.function())
        if registration is None:
            continue
        agent_type_name, agent_class, _config_class = registration
        if agent_class is None:
            continue
        class_and_plugin_by_type[agent_type_name] = (agent_class, cast(ModuleType, hookimpl.plugin))

    # Agent type names whose plugin exposes a waiting_reason field generator (not some other field).
    field_generator_agent_type_names = frozenset(
        result[0]
        for result in pm.hook.agent_field_generators()
        if result is not None and _WAITING_REASON_FIELD_KEY in result[1]
    )

    # For each hook referenced by a PLUGIN_HOOKIMPL capability, the plugin *packages*
    # that implement it. Package-level (not exact-module) because a hookimpl like
    # get_files_for_deploy is one global contribution for the whole agent package,
    # while sibling agent subtypes register from submodules of that same package --
    # consistent with usage-source detection below.
    hook_names = frozenset(c.hook_name for c in capabilities if c.hook_name is not None)
    packages_by_hook_name: dict[str, frozenset[str]] = {}
    for hook_name in hook_names:
        hook_caller = getattr(pm.hook, hook_name, None)
        impls = hook_caller.get_hookimpls() if hook_caller is not None else ()
        packages_by_hook_name[hook_name] = frozenset(_module_package(impl.plugin) for impl in impls)

    # Packages of plugins that claim a usage source (e.g. 'imbue.mngr_claude_usage').
    usage_hook_caller = getattr(pm.hook, "aggregate_usage_source", None)
    usage_impls = usage_hook_caller.get_hookimpls() if usage_hook_caller is not None else ()
    usage_plugin_packages = frozenset(_module_package(impl.plugin) for impl in usage_impls)

    infos: list[AgentClassInfo] = []
    for agent_type_name, (agent_class, plugin_module) in class_and_plugin_by_type.items():
        plugin_package = _module_package(plugin_module)
        plugin_hook_names = frozenset(
            hook_name for hook_name in hook_names if plugin_package in packages_by_hook_name[hook_name]
        )
        usage_package = plugin_package + _USAGE_PACKAGE_SUFFIX
        infos.append(
            AgentClassInfo(
                agent_type_name=agent_type_name,
                agent_class=agent_class,
                field_generator_agent_type_names=field_generator_agent_type_names,
                plugin_hook_names=plugin_hook_names,
                is_usage_source_claimed=usage_package in usage_plugin_packages,
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
