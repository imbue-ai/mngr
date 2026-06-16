"""Forcing function: every agent capability must be exercised by a real test.

The capability registry (``AGENT_CAPABILITIES``) is the single source of truth for
"what each agent can do". This module makes it also drive *test coverage*: every
capability key must appear in ``_EXERCISE_BY_CAPABILITY`` pointing at the test(s)
that actually exercise it against a real agent. Adding a capability without an
exercise fails ``test_every_capability_has_an_exercise`` -- so coverage cannot
silently lag the matrix.

The exercises themselves live where they can use real agents: the per-plugin unit
tests for contract methods that read config (permission policy, version policy,
unattended, install command), and the per-plugin release/e2e tests for behavior
that needs a live CLI (transcripts, waiting_reason markers, auto-install on a host).
"""

import importlib.metadata

import pluggy
import pytest

from imbue.mngr.agents.agent_capabilities import AGENT_CAPABILITIES
from imbue.mngr.agents.agent_capabilities import build_agent_class_infos
from imbue.mngr.agents.agent_capabilities import is_capability_present

# Each capability key -> where its behavior is exercised against a real agent.
# Keep in sync with AGENT_CAPABILITIES; the coverage test below enforces it.
_EXERCISE_BY_CAPABILITY: dict[str, str] = {
    "raw_transcript": "per-plugin release e2e (test_*_agent_e2e.py) assert the raw transcript is written",
    "common_transcript": "per-plugin transcript tests + `mngr transcript` release e2e",
    "headless_output": "mngr_claude headless_claude_agent tests (output() from stdout.jsonl)",
    "streaming_headless_output": "mngr_claude headless_claude_agent stream_output() tests",
    "session_preservation": "per-plugin plugin_test.py test_on_destroy_preserves_transcripts (rsync-marked)",
    "auto_install": "installation_test.py (helper) + per-plugin provision tests; live install via modal release e2e",
    "streaming_snapshot": "mngr_claude resources/stream_snapshot tests + claude release e2e",
    "unattended_operation": "per-plugin config tests for auto_allow_permissions (pi: degenerate-pin tests)",
    "permission_policy": "per-plugin get_permission_policy via config_overrides/settings_overrides tests",
    "version_management": "claude version-pin tests + codex update_policy tests",
    "waiting_reason_field": "per-plugin _waiting_reason marker tests + opencode/codex release e2e",
    "deploy_contributions": "mngr_claude get_files_for_deploy tests + mngr schedule release e2e",
    "usage_tracking": "mngr_<harness>_usage plugin tests + usage release e2e on modal",
}


@pytest.fixture
def enabled_plugins() -> frozenset[str]:
    return frozenset(ep.name for ep in importlib.metadata.entry_points(group="mngr"))


def test_every_capability_has_an_exercise() -> None:
    capability_keys = {capability.key for capability in AGENT_CAPABILITIES}
    exercised_keys = set(_EXERCISE_BY_CAPABILITY)
    missing = capability_keys - exercised_keys
    extra = exercised_keys - capability_keys
    assert not missing, f"capabilities with no registered exercise: {sorted(missing)}"
    assert not extra, f"exercises for unknown capabilities: {sorted(extra)}"


def test_every_registered_agent_has_at_least_one_capability(plugin_manager: pluggy.PluginManager) -> None:
    """Every real agent declares at least one capability; only the bare command shells have none."""
    infos = build_agent_class_infos(plugin_manager)
    bare_shell_types = {"command", "headless_command"}
    for info in infos:
        present = [c.key for c in AGENT_CAPABILITIES if is_capability_present(c, info)]
        if info.agent_type_name in bare_shell_types:
            continue
        assert present, f"agent type {info.agent_type_name!r} declares no capabilities"
