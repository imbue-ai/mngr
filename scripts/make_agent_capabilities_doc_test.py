from collections.abc import Iterator

import pluggy
import pytest

from imbue.mngr.agents.agent_registry import reset_agent_registry
from imbue.mngr.api.providers import reset_provider_instances
from imbue.mngr.interfaces.agent import CliBackedAgentMixin
from imbue.mngr.interfaces.agent import HasAutoInstallMixin
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.agent import HasSessionAdoptionMixin
from imbue.mngr.interfaces.agent import HasTranscriptMixin
from imbue.mngr.interfaces.agent import HasUnattendedModeMixin
from imbue.mngr.interfaces.agent import HeadlessAgentMixin
from imbue.mngr.interfaces.agent import StreamingHeadlessAgentMixin
from imbue.mngr.interfaces.agent import SupportsLiveOutputMixin
from imbue.mngr.providers.registry import reset_backend_registry
from scripts.make_agent_capabilities_doc import AGENT_CAPABILITIES
from scripts.make_agent_capabilities_doc import AgentCapability
from scripts.make_agent_capabilities_doc import AgentCapabilityError
from scripts.make_agent_capabilities_doc import AgentClassInfo
from scripts.make_agent_capabilities_doc import CapabilityDetectionKind
from scripts.make_agent_capabilities_doc import CapabilityScope
from scripts.make_agent_capabilities_doc import build_agent_class_infos
from scripts.make_agent_capabilities_doc import build_loaded_plugin_manager
from scripts.make_agent_capabilities_doc import doc_path
from scripts.make_agent_capabilities_doc import generate_capability_matrix_doc
from scripts.make_agent_capabilities_doc import is_capability_applicable
from scripts.make_agent_capabilities_doc import is_capability_present
from scripts.make_agent_capabilities_doc import render_capability_matrix


# A CLI-backed agent that emits both transcript layers but is not headless (claude-style).
class _FakeTranscriptAgent(CliBackedAgentMixin, HasCommonTranscriptMixin): ...


# A CLI-backed headless agent (StreamingHeadlessAgentMixin extends HeadlessAgentMixin and
# SupportsLiveOutputMixin), so it is CLI-backed, headless, and has live output (headless_claude).
class _FakeStreamingHeadlessAgent(CliBackedAgentMixin, StreamingHeadlessAgentMixin): ...


# A CLI-backed TUI agent that publishes a streaming snapshot (it supports live output via the
# snapshot surface of SupportsLiveOutputMixin), so it has live output and is not headless.
class _FakeTuiSnapshotAgent(CliBackedAgentMixin, SupportsLiveOutputMixin): ...


# A CLI-backed agent that can adopt an existing session.
class _FakeAdoptingAgent(CliBackedAgentMixin, HasSessionAdoptionMixin): ...


# A headless CLI agent that structurally inherits the adoption mixin: session_resume is
# interactive-only, so it renders n/a here rather than raising. Exercises that an inherited
# but out-of-scope CLASS_MIXIN capability is tolerated on a kind the capability does not apply
# to (the real headless_claude no longer inherits the mixin; this fixture forces the case).
class _FakeHeadlessAdoptingAgent(CliBackedAgentMixin, StreamingHeadlessAgentMixin, HasSessionAdoptionMixin): ...


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
    # session_resume is interactive-only and detected by HasSessionAdoptionMixin: Y for the
    # adopting interactive agent, - for an interactive CLI agent that does not adopt, n/a for
    # the headless variant (inherits the mixin but is out of scope) and the command runner.
    infos = [
        _info("claude", _FakeAdoptingAgent),
        _info("headless_claude", _FakeHeadlessAdoptingAgent),
        _info("codex", _FakeTranscriptAgent),
        _info("command", _FakeCommandAgent),
    ]
    matrix = render_capability_matrix(AGENT_CAPABILITIES, infos)
    resume_row = next(line for line in matrix.splitlines() if line.startswith("| session_resume |"))
    assert resume_row == "| session_resume | Y | n/a | - | n/a |"


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


@pytest.fixture
def loaded_plugin_manager() -> Iterator[pluggy.PluginManager]:
    """A plugin manager with every installed mngr plugin loaded, reset on teardown.

    Uses the same builder the generator script uses (load all entry points, local backend
    only). Resets the global agent/backend/provider registries afterward so building the
    full plugin set here does not leak into other scripts/ tests sharing the xdist worker.
    """
    pm = build_loaded_plugin_manager()
    try:
        yield pm
    finally:
        reset_agent_registry()
        reset_backend_registry()
        reset_provider_instances()


def _present_keys(infos: tuple[AgentClassInfo, ...], agent_type_name: str) -> set[str]:
    info = next(i for i in infos if i.agent_type_name == agent_type_name)
    return {c.key for c in AGENT_CAPABILITIES if is_capability_present(c, info)}


def test_builder_detects_known_capabilities(loaded_plugin_manager: pluggy.PluginManager) -> None:
    infos = build_agent_class_infos(loaded_plugin_manager)
    agent_type_names = {i.agent_type_name for i in infos}
    assert {"claude", "codex", "opencode", "pi-coding", "antigravity"} <= agent_type_names

    # Every real agent emits both transcript layers; the bare config shells do not.
    for agent_type_name in ("claude", "codex", "opencode", "pi-coding", "antigravity"):
        keys = _present_keys(infos, agent_type_name)
        assert "common_transcript" in keys
        assert "raw_transcript" in keys
    assert "common_transcript" not in _present_keys(infos, "command")

    # waiting_reason field generator: claude/codex/opencode/pi yes (pi degenerately,
    # a single-value END_OF_TURN); antigravity no (blocked on an upstream signal).
    assert "waiting_reason_field" in _present_keys(infos, "claude")
    assert "waiting_reason_field" in _present_keys(infos, "codex")
    assert "waiting_reason_field" in _present_keys(infos, "opencode")
    assert "waiting_reason_field" in _present_keys(infos, "pi-coding")
    assert "waiting_reason_field" not in _present_keys(infos, "antigravity")

    # live_output: claude publishes a streaming snapshot; codex does not.
    assert "live_output" in _present_keys(infos, "claude")
    assert "live_output" not in _present_keys(infos, "codex")
    # session_resume (--adopt-session / --from carry-forward): every interactive agent.
    for agent_type_name in ("claude", "codex", "opencode", "pi-coding", "antigravity"):
        assert "session_resume" in _present_keys(infos, agent_type_name)
    for agent_type_name in ("claude", "codex", "opencode", "pi-coding", "antigravity"):
        assert "session_preservation" in _present_keys(infos, agent_type_name)
        assert "unattended_operation" in _present_keys(infos, agent_type_name)
    # Per-resource permission policy: agy/opencode/codex yes; claude/pi no.
    for agent_type_name in ("antigravity", "opencode", "codex"):
        assert "permission_policy" in _present_keys(infos, agent_type_name)
    assert "permission_policy" not in _present_keys(infos, "claude")
    assert "permission_policy" not in _present_keys(infos, "pi-coding")
    # Version management: claude/codex yes; the rest no.
    assert "version_management" in _present_keys(infos, "claude")
    assert "version_management" in _present_keys(infos, "codex")
    assert "version_management" not in _present_keys(infos, "opencode")

    # Module-level capabilities (detected by package, not class).
    assert "deploy_contributions" in _present_keys(infos, "claude")
    assert "deploy_contributions" not in _present_keys(infos, "codex")
    for agent_type_name in ("claude", "codex", "opencode", "pi-coding"):
        assert "usage_tracking" in _present_keys(infos, agent_type_name)
    assert "usage_tracking" not in _present_keys(infos, "antigravity")


def test_capability_matrix_doc_is_current(loaded_plugin_manager: pluggy.PluginManager) -> None:
    """The committed matrix doc must equal the matrix derived from the code (drift guard)."""
    infos = build_agent_class_infos(loaded_plugin_manager)
    generated = generate_capability_matrix_doc(AGENT_CAPABILITIES, infos)
    path = doc_path()
    assert path.read_text() == generated, (
        f"{path} is stale relative to the agent capability registry; "
        "regenerate with `just regenerate-agent-capabilities-doc`."
    )
