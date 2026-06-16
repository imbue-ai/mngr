"""Forcing function: every agent capability must name where its behavior is tested.

This is a **bookkeeping** check, not a behavioral one. The registry
(``AGENT_CAPABILITIES``) already gives the matrix and the drift guard for free via
detection (``issubclass`` / hookimpl) -- but detection only proves a capability is
*declared*, not that it *works*. So every capability key must appear in
``_EXERCISE_BY_CAPABILITY`` pointing at the test(s) that exercise its behavior. Adding
a capability without registering such a pointer fails
``test_every_capability_has_an_exercise``, so a new capability cannot ship with zero
behavioral test on record.

What this test does **not** do: it does not run the exercises, nor verify the named
tests exist. The pointers are prose. The actual behavioral exercises live in the
per-plugin unit tests (for contract methods that read config) and the per-plugin
release/e2e tests (for behavior needing a live CLI -- transcripts, waiting_reason
markers, auto-install on a host). A registry-driven release harness that walks each
agent's declared capabilities and runs each exercise against a real agent is the
intended stronger form (see the e2e section of specs/agent-plugin-parity/capability-mixins.md).
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
