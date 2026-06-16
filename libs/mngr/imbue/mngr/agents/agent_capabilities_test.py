import pytest

from imbue.mngr.agents.agent_capabilities import AGENT_CAPABILITIES
from imbue.mngr.agents.agent_capabilities import AgentCapability
from imbue.mngr.agents.agent_capabilities import AgentCapabilityError
from imbue.mngr.agents.agent_capabilities import AgentClassInfo
from imbue.mngr.agents.agent_capabilities import CapabilityDetectionKind
from imbue.mngr.agents.agent_capabilities import CapabilityScope
from imbue.mngr.agents.agent_capabilities import is_capability_applicable
from imbue.mngr.agents.agent_capabilities import is_capability_present
from imbue.mngr.agents.agent_capabilities import render_capability_matrix
from imbue.mngr.interfaces.agent import CliBackedAgentMixin
from imbue.mngr.interfaces.agent import HasAutoInstallMixin
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.agent import HasSessionAdoptionMixin
from imbue.mngr.interfaces.agent import HasStreamingSnapshotMixin
from imbue.mngr.interfaces.agent import HasTranscriptMixin
from imbue.mngr.interfaces.agent import HasUnattendedModeMixin
from imbue.mngr.interfaces.agent import HeadlessAgentMixin
from imbue.mngr.interfaces.agent import StreamingHeadlessAgentMixin


# A CLI-backed agent that emits both transcript layers but is not headless (claude-style).
class _FakeTranscriptAgent(CliBackedAgentMixin, HasCommonTranscriptMixin): ...


# A CLI-backed headless agent (StreamingHeadlessAgentMixin extends HeadlessAgentMixin and
# SupportsLiveOutputMixin), so it is CLI-backed, headless, and has live output (headless_claude).
class _FakeStreamingHeadlessAgent(CliBackedAgentMixin, StreamingHeadlessAgentMixin): ...


# A CLI-backed agent that publishes a streaming snapshot (HasStreamingSnapshotMixin extends
# SupportsLiveOutputMixin), so it has live output and is not headless.
class _FakeTuiSnapshotAgent(CliBackedAgentMixin, HasStreamingSnapshotMixin): ...


# A CLI-backed agent that can adopt an existing session.
class _FakeAdoptingAgent(CliBackedAgentMixin, HasSessionAdoptionMixin): ...


# A bare command runner: not CLI-backed, unattended by construction.
class _FakeCommandAgent(HasUnattendedModeMixin): ...


# A non-CLI-backed agent that nonetheless structurally inherits a CLI-only transcript mixin:
# used to exercise that an out-of-scope CLASS_MIXIN capability renders n/a (not a raise), since
# a mixin can legitimately be inherited by a kind the capability does not apply to.
class _FakeCommandWithTranscript(HasCommonTranscriptMixin): ...


# A bare agent with none of the capability mixins.
class _FakeBareAgent: ...


def _info(
    agent_type_name: str,
    agent_class: type,
    field_generator_agent_type_names: frozenset[str] = frozenset(),
    plugin_hook_names: frozenset[str] = frozenset(),
    is_usage_source_claimed: bool = False,
) -> AgentClassInfo:
    # The kind traits are derived from the class exactly as build_agent_class_infos does,
    # so fakes behave like real agent classes.
    return AgentClassInfo(
        agent_type_name=agent_type_name,
        agent_class=agent_class,
        field_generator_agent_type_names=field_generator_agent_type_names,
        plugin_hook_names=plugin_hook_names,
        is_usage_source_claimed=is_usage_source_claimed,
        is_headless=issubclass(agent_class, HeadlessAgentMixin),
        is_cli_backed=issubclass(agent_class, CliBackedAgentMixin),
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


def test_capability_applicability_by_scope() -> None:
    interactive_only = AgentCapability(
        key="waiting_reason_field",
        description="x",
        detection_kind=CapabilityDetectionKind.FIELD_GENERATOR,
        scope=CapabilityScope.INTERACTIVE_ONLY,
    )
    cli_backed_only = AgentCapability(
        key="auto_install",
        description="x",
        detection_kind=CapabilityDetectionKind.CLASS_MIXIN,
        scope=CapabilityScope.CLI_BACKED_ONLY,
        mixin=HasAutoInstallMixin,
    )
    headless_only = AgentCapability(
        key="headless_output",
        description="x",
        detection_kind=CapabilityDetectionKind.CLASS_MIXIN,
        scope=CapabilityScope.HEADLESS_ONLY,
        mixin=HeadlessAgentMixin,
    )
    applies_to_all = AgentCapability(
        key="raw_transcript",
        description="x",
        detection_kind=CapabilityDetectionKind.CLASS_MIXIN,
        mixin=HasTranscriptMixin,
    )
    interactive = _info("claude", _FakeTuiSnapshotAgent)
    headless = _info("headless_claude", _FakeStreamingHeadlessAgent)
    command = _info("command", _FakeCommandAgent)

    # INTERACTIVE_ONLY: only the CLI-backed, non-headless agent prompts.
    assert is_capability_applicable(interactive_only, interactive) is True
    assert is_capability_applicable(interactive_only, headless) is False
    assert is_capability_applicable(interactive_only, command) is False
    # CLI_BACKED_ONLY: applies to the CLI-backed agents (interactive or headless), not the command runner.
    assert is_capability_applicable(cli_backed_only, interactive) is True
    assert is_capability_applicable(cli_backed_only, headless) is True
    assert is_capability_applicable(cli_backed_only, command) is False
    # HEADLESS_ONLY: only the headless agent.
    assert is_capability_applicable(headless_only, interactive) is False
    assert is_capability_applicable(headless_only, headless) is True
    assert is_capability_applicable(headless_only, command) is False
    # ALL: applies to every agent kind.
    assert is_capability_applicable(applies_to_all, command) is True


def test_render_capability_matrix_orders_columns_by_fixed_order() -> None:
    # Pass the infos out of order; rendering must reorder by _MATRIX_AGENT_TYPE_ORDER,
    # where claude precedes headless_claude.
    infos = [
        _info("headless_claude", _FakeStreamingHeadlessAgent),
        _info("claude", _FakeTranscriptAgent, field_generator_agent_type_names=frozenset({"claude"})),
    ]
    matrix = render_capability_matrix(AGENT_CAPABILITIES, infos)

    lines = matrix.splitlines()
    # Columns follow the fixed order (claude before headless_claude), not the input order.
    assert lines[0] == "| Capability | claude | headless_claude |"
    # claude has both transcript layers; headless_claude (a bare headless fake) has neither.
    raw_row = next(line for line in lines if line.startswith("| raw_transcript |"))
    assert raw_row == "| raw_transcript | Y | - |"
    # headless_output is headless-only: present for the headless agent, n/a for claude.
    headless_row = next(line for line in lines if line.startswith("| headless_output |"))
    assert headless_row == "| headless_output | n/a | Y |"
    # live_output is the unified streaming capability; the headless fake streams, the plain
    # transcript fake (no snapshot mixin) does not.
    live_row = next(line for line in lines if line.startswith("| live_output |"))
    assert live_row == "| live_output | - | Y |"
    # waiting_reason_field is interactive-only, so it is n/a for the headless agent;
    # claude has the field generator and prompts, so it is present.
    waiting_row = next(line for line in lines if line.startswith("| waiting_reason_field |"))
    assert waiting_row == "| waiting_reason_field | Y | n/a |"


def test_render_capability_matrix_marks_command_runner_cells_na() -> None:
    infos = [
        _info("claude", _FakeTranscriptAgent, field_generator_agent_type_names=frozenset({"claude"})),
        _info("command", _FakeCommandAgent),
    ]
    matrix = render_capability_matrix(AGENT_CAPABILITIES, infos)
    lines = matrix.splitlines()

    # CLI-only capability: n/a for the bare command runner, present for claude.
    common_row = next(line for line in lines if line.startswith("| common_transcript |"))
    assert common_row == "| common_transcript | Y | n/a |"
    # Interactive-only capability: n/a for the command runner.
    waiting_row = next(line for line in lines if line.startswith("| waiting_reason_field |"))
    assert waiting_row == "| waiting_reason_field | Y | n/a |"
    # Unattended applies to all kinds; the command runner is unattended by construction (Y),
    # while this CLI-backed fake does not declare auto-allow, so it is applicable-but-absent (-).
    unattended_row = next(line for line in lines if line.startswith("| unattended_operation |"))
    assert unattended_row == "| unattended_operation | - | Y |"
    # CLI-only session_resume: n/a for the command runner (claude here does not adopt, so -).
    resume_row = next(line for line in lines if line.startswith("| session_resume |"))
    assert resume_row == "| session_resume | - | n/a |"


def test_render_capability_matrix_renders_na_for_inherited_out_of_scope_mixin() -> None:
    # A command runner that structurally inherits a CLI-only transcript mixin renders n/a --
    # not a raise -- because a CLASS_MIXIN can legitimately be inherited by a kind for which
    # the capability does not apply.
    cli_only = AgentCapability(
        key="common_transcript",
        description="x",
        detection_kind=CapabilityDetectionKind.CLASS_MIXIN,
        scope=CapabilityScope.CLI_BACKED_ONLY,
        mixin=HasCommonTranscriptMixin,
    )
    infos = [
        _info("claude", _FakeTranscriptAgent),
        _info("command", _FakeCommandWithTranscript),
    ]
    matrix = render_capability_matrix([cli_only], infos)
    row = next(line for line in matrix.splitlines() if line.startswith("| common_transcript |"))
    # claude is CLI-backed and has the mixin -> Y; the command runner inherits the mixin but
    # is a bare command, so the CLI-only transcript is n/a.
    assert row == "| common_transcript | Y | n/a |"


def test_render_capability_matrix_raises_when_genuine_capability_is_out_of_scope() -> None:
    # Unlike an inherited mixin, a field generator is registered deliberately per agent type.
    # If such a genuine capability is present but the scope says n/a, the scope is wrong --
    # so rendering raises rather than silently hiding the contradiction.
    infos = [_info("command", _FakeCommandAgent, field_generator_agent_type_names=frozenset({"command"}))]
    with pytest.raises(AgentCapabilityError, match="waiting_reason_field"):
        render_capability_matrix(AGENT_CAPABILITIES, infos)


def test_render_capability_matrix_session_resume_tracks_adoption_mixin() -> None:
    # session_resume is CLI-backed-only and detected by HasSessionAdoptionMixin: Y for the
    # adopting CLI agent, - for a CLI agent that does not adopt, n/a for the command runner.
    infos = [
        _info("claude", _FakeAdoptingAgent),
        _info("codex", _FakeTranscriptAgent),
        _info("command", _FakeCommandAgent),
    ]
    matrix = render_capability_matrix(AGENT_CAPABILITIES, infos)
    resume_row = next(line for line in matrix.splitlines() if line.startswith("| session_resume |"))
    assert resume_row == "| session_resume | Y | - | n/a |"


def test_render_capability_matrix_rejects_unlisted_agent_type() -> None:
    # An agent type that is neither in the fixed order nor explicitly excluded must fail
    # loudly rather than be silently dropped from the table.
    infos = [_info("brand-new-agent", _FakeBareAgent)]
    with pytest.raises(AgentCapabilityError, match="brand-new-agent"):
        render_capability_matrix(AGENT_CAPABILITIES, infos)


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
