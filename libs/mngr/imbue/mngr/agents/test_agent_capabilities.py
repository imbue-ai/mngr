import importlib.metadata
import os
from pathlib import Path

import pluggy
import pytest

from imbue.mngr.agents.agent_capabilities import AGENT_CAPABILITIES
from imbue.mngr.agents.agent_capabilities import AgentClassInfo
from imbue.mngr.agents.agent_capabilities import build_agent_class_infos
from imbue.mngr.agents.agent_capabilities import generate_capability_matrix_doc
from imbue.mngr.agents.agent_capabilities import is_capability_present

# Set this env var to overwrite the committed matrix doc instead of asserting against it.
_REGEN_ENV_VAR = "MNGR_REGEN_CAPABILITY_DOC"


@pytest.fixture
def enabled_plugins() -> frozenset[str]:
    # Enable every installed mngr plugin so the matrix reflects the full agent set.
    return frozenset(ep.name for ep in importlib.metadata.entry_points(group="mngr"))


def _capability_doc_path() -> Path:
    # libs/mngr/imbue/mngr/agents/<this file> -> libs/mngr/docs/concepts/agent_capabilities.md
    libs_mngr_dir = Path(__file__).parents[3]
    return libs_mngr_dir / "docs" / "concepts" / "agent_capabilities.md"


def _present_keys(infos: tuple[AgentClassInfo, ...], agent_type_name: str) -> set[str]:
    info = next(i for i in infos if i.agent_type_name == agent_type_name)
    return {c.key for c in AGENT_CAPABILITIES if is_capability_present(c, info)}


def test_builder_detects_known_capabilities(plugin_manager: pluggy.PluginManager) -> None:
    infos = build_agent_class_infos(plugin_manager)
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


def test_capability_matrix_doc_is_current(plugin_manager: pluggy.PluginManager) -> None:
    """The committed matrix doc must equal the matrix derived from the code (drift guard)."""
    infos = build_agent_class_infos(plugin_manager)
    generated = generate_capability_matrix_doc(AGENT_CAPABILITIES, infos)
    doc_path = _capability_doc_path()

    if os.environ.get(_REGEN_ENV_VAR):
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text(generated)
        return

    assert doc_path.read_text() == generated, (
        f"{doc_path} is stale relative to the agent capability registry; "
        f"regenerate with `{_REGEN_ENV_VAR}=1 just test ...` (or the regenerate recipe)."
    )
