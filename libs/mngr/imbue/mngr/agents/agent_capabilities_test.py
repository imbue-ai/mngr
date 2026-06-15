from imbue.mngr.agents.agent_capabilities import AGENT_CAPABILITIES
from imbue.mngr.agents.agent_capabilities import AgentCapability
from imbue.mngr.agents.agent_capabilities import AgentClassInfo
from imbue.mngr.agents.agent_capabilities import CapabilityDetectionKind
from imbue.mngr.agents.agent_capabilities import is_capability_present
from imbue.mngr.agents.agent_capabilities import render_capability_matrix
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.agent import HasTranscriptMixin
from imbue.mngr.interfaces.agent import StreamingHeadlessAgentMixin


# A TUI-style agent that emits both transcript layers but is not headless.
class _FakeTranscriptAgent(HasCommonTranscriptMixin): ...


# A headless streaming agent (StreamingHeadlessAgentMixin extends HeadlessAgentMixin).
class _FakeStreamingHeadlessAgent(StreamingHeadlessAgentMixin): ...


# A bare agent with none of the capability mixins.
class _FakeBareAgent: ...


def _info(
    agent_type_name: str,
    agent_class: type,
    field_generator_agent_type_names: frozenset[str] = frozenset(),
    plugin_hook_names: frozenset[str] = frozenset(),
    is_usage_source_claimed: bool = False,
) -> AgentClassInfo:
    return AgentClassInfo(
        agent_type_name=agent_type_name,
        agent_class=agent_class,
        field_generator_agent_type_names=field_generator_agent_type_names,
        plugin_hook_names=plugin_hook_names,
        is_usage_source_claimed=is_usage_source_claimed,
    )


def test_class_mixin_detection_follows_inheritance() -> None:
    raw = AgentCapability(
        key="raw_transcript",
        description="x",
        detection_kind=CapabilityDetectionKind.CLASS_MIXIN,
        mixin=HasTranscriptMixin,
    )
    common = AgentCapability(
        key="common_transcript",
        description="x",
        detection_kind=CapabilityDetectionKind.CLASS_MIXIN,
        mixin=HasCommonTranscriptMixin,
    )
    transcript_info = _info("fake-tui", _FakeTranscriptAgent)
    bare_info = _info("fake-bare", _FakeBareAgent)

    # HasCommonTranscriptMixin extends HasTranscriptMixin, so both are present.
    assert is_capability_present(raw, transcript_info) is True
    assert is_capability_present(common, transcript_info) is True
    assert is_capability_present(raw, bare_info) is False
    assert is_capability_present(common, bare_info) is False


def test_field_generator_detection_matches_agent_type_name() -> None:
    capability = AgentCapability(
        key="waiting_reason_field",
        description="x",
        detection_kind=CapabilityDetectionKind.FIELD_GENERATOR,
    )
    with_field = _info("opencode", _FakeBareAgent, field_generator_agent_type_names=frozenset({"opencode"}))
    without_field = _info("pi-coding", _FakeBareAgent, field_generator_agent_type_names=frozenset({"opencode"}))

    assert is_capability_present(capability, with_field) is True
    assert is_capability_present(capability, without_field) is False


def test_plugin_hookimpl_detection_matches_hook_name() -> None:
    capability = AgentCapability(
        key="deploy_contributions",
        description="x",
        detection_kind=CapabilityDetectionKind.PLUGIN_HOOKIMPL,
        hook_name="get_files_for_deploy",
    )
    with_hook = _info("claude", _FakeBareAgent, plugin_hook_names=frozenset({"get_files_for_deploy"}))
    without_hook = _info("codex", _FakeBareAgent, plugin_hook_names=frozenset())

    assert is_capability_present(capability, with_hook) is True
    assert is_capability_present(capability, without_hook) is False


def test_usage_source_detection_reads_claim_flag() -> None:
    capability = AgentCapability(
        key="usage_tracking",
        description="x",
        detection_kind=CapabilityDetectionKind.USAGE_SOURCE,
    )
    assert is_capability_present(capability, _info("claude", _FakeBareAgent, is_usage_source_claimed=True)) is True
    assert (
        is_capability_present(capability, _info("antigravity", _FakeBareAgent, is_usage_source_claimed=False)) is False
    )


def test_render_capability_matrix_produces_sorted_grid() -> None:
    infos = [
        _info("zeta", _FakeStreamingHeadlessAgent),
        _info("alpha", _FakeTranscriptAgent, field_generator_agent_type_names=frozenset({"alpha"})),
    ]
    matrix = render_capability_matrix(AGENT_CAPABILITIES, infos)

    lines = matrix.splitlines()
    # Columns are sorted by agent type name.
    assert lines[0] == "| Capability | alpha | zeta |"
    # alpha has both transcript layers; zeta (headless) has neither.
    raw_row = next(line for line in lines if line.startswith("| raw_transcript |"))
    assert raw_row == "| raw_transcript | Y | - |"
    # zeta is the streaming-headless one; alpha is not headless.
    streaming_row = next(line for line in lines if line.startswith("| streaming_headless_output |"))
    assert streaming_row == "| streaming_headless_output | - | Y |"
    # alpha is registered as having a field generator; zeta is not.
    waiting_row = next(line for line in lines if line.startswith("| waiting_reason_field |"))
    assert waiting_row == "| waiting_reason_field | Y | - |"


def test_registry_capabilities_are_well_formed() -> None:
    # Every CLASS_MIXIN capability must name a mixin; every PLUGIN_HOOKIMPL must name a hook.
    for capability in AGENT_CAPABILITIES:
        if capability.detection_kind == CapabilityDetectionKind.CLASS_MIXIN:
            assert capability.mixin is not None, capability.key
        if capability.detection_kind == CapabilityDetectionKind.PLUGIN_HOOKIMPL:
            assert capability.hook_name is not None, capability.key
    # Keys are unique.
    keys = [c.key for c in AGENT_CAPABILITIES]
    assert len(keys) == len(set(keys))
