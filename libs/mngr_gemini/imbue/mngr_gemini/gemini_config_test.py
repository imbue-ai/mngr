"""Unit tests for gemini_config.py."""

from __future__ import annotations

import json
from pathlib import Path

from imbue.mngr_gemini.gemini_config import HOOK_EVENT_BEFORE_TOOL
from imbue.mngr_gemini.gemini_config import HOOK_EVENT_SESSION_START
from imbue.mngr_gemini.gemini_config import build_permission_auto_allow_hooks_config
from imbue.mngr_gemini.gemini_config import build_readiness_hooks_config
from imbue.mngr_gemini.gemini_config import get_gemini_config_dir
from imbue.mngr_gemini.gemini_config import get_project_gemini_settings_path
from imbue.mngr_gemini.gemini_config import get_system_gemini_settings_path
from imbue.mngr_gemini.gemini_config import get_user_gemini_settings_path
from imbue.mngr_gemini.gemini_config import hook_already_exists
from imbue.mngr_gemini.gemini_config import interpolate_env_vars
from imbue.mngr_gemini.gemini_config import merge_hooks_config
from imbue.mngr_gemini.gemini_config import read_gemini_settings
from imbue.mngr_gemini.gemini_config import serialize_gemini_settings
from imbue.mngr_gemini.gemini_config import write_gemini_settings

# =============================================================================
# Path resolution
# =============================================================================


def test_get_gemini_config_dir_returns_home_dot_gemini() -> None:
    assert get_gemini_config_dir() == Path.home() / ".gemini"


def test_get_user_gemini_settings_path_returns_settings_json_in_config_dir() -> None:
    assert get_user_gemini_settings_path() == Path.home() / ".gemini" / "settings.json"


def test_get_project_gemini_settings_path_returns_dot_gemini_under_project() -> None:
    project = Path("/tmp/some-project")
    assert get_project_gemini_settings_path(project) == project / ".gemini" / "settings.json"


def test_get_system_gemini_settings_path_returns_etc_path() -> None:
    assert get_system_gemini_settings_path() == Path("/etc/gemini-cli/settings.json")


# =============================================================================
# Read
# =============================================================================


def test_read_gemini_settings_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_gemini_settings(tmp_path / "does-not-exist.json") == {}


def test_read_gemini_settings_empty_file_returns_empty(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("")
    assert read_gemini_settings(settings_path) == {}


def test_read_gemini_settings_parses_valid_json(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"general": {"approvalMode": "default"}}))
    assert read_gemini_settings(settings_path) == {"general": {"approvalMode": "default"}}


def test_read_gemini_settings_malformed_json_returns_empty(tmp_path: Path) -> None:
    """Malformed JSON should not raise -- a user typo must not break agent provisioning."""
    settings_path = tmp_path / "settings.json"
    # Missing the closing brace -> json.loads raises.
    settings_path.write_text('{"general": {"approvalMode": "default"')
    assert read_gemini_settings(settings_path) == {}


def test_read_gemini_settings_non_object_json_returns_empty(tmp_path: Path) -> None:
    """A top-level JSON list/string/number should be treated as empty, not crash."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("[1, 2, 3]")
    assert read_gemini_settings(settings_path) == {}


# =============================================================================
# Serializer
# =============================================================================


def test_serialize_gemini_settings_uses_two_space_indent_and_trailing_newline() -> None:
    output = serialize_gemini_settings({"a": 1, "b": [2, 3]})
    assert output.endswith("\n")
    # At least one indented line (two-space pretty-print).
    assert "\n  " in output


def test_serialize_gemini_settings_round_trips_through_json_loads() -> None:
    payload = {"hooks": {"SessionStart": [{"hooks": [{"command": "echo"}]}]}}
    assert json.loads(serialize_gemini_settings(payload)) == payload


# =============================================================================
# Atomic write
# =============================================================================


def test_write_gemini_settings_creates_parent_dirs(tmp_path: Path) -> None:
    settings_path = tmp_path / "nested" / "deeper" / "settings.json"
    write_gemini_settings(settings_path, {"key": "value"})
    assert settings_path.is_file()
    assert json.loads(settings_path.read_text()) == {"key": "value"}


def test_write_gemini_settings_writes_pretty_printed_json_with_trailing_newline(
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "settings.json"
    write_gemini_settings(settings_path, {"a": 1, "b": [2, 3]})
    contents = settings_path.read_text()
    assert contents.endswith("\n")
    # Indented 2-space pretty-print
    assert "  " in contents


def test_write_gemini_settings_creates_backup_when_overwriting(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"prev": True}))

    write_gemini_settings(settings_path, {"prev": False})

    backup_path = tmp_path / "settings.json.bak"
    assert backup_path.is_file()
    assert json.loads(backup_path.read_text()) == {"prev": True}
    assert json.loads(settings_path.read_text()) == {"prev": False}


def test_write_gemini_settings_no_backup_for_new_file(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    write_gemini_settings(settings_path, {"new": True})
    assert not (tmp_path / "settings.json.bak").exists()


def test_write_gemini_settings_round_trips_through_read(tmp_path: Path) -> None:
    """Atomic write + read should return the same data unchanged."""
    settings_path = tmp_path / "settings.json"
    payload: dict[str, object] = {
        "hooks": {"BeforeTool": [{"matcher": ".*", "hooks": [{"type": "command", "command": "echo"}]}]},
        "mcpServers": {"my-server": {"command": "node", "args": ["server.js"]}},
        "general": {"approvalMode": "default"},
    }
    write_gemini_settings(settings_path, payload)
    assert read_gemini_settings(settings_path) == payload


# =============================================================================
# Env-var interpolation
# =============================================================================


def test_interpolate_env_vars_substitutes_bare_var() -> None:
    assert interpolate_env_vars("$HOME/foo", {"HOME": "/u/me"}) == "/u/me/foo"


def test_interpolate_env_vars_substitutes_braced_var() -> None:
    assert interpolate_env_vars("${HOME}/foo", {"HOME": "/u/me"}) == "/u/me/foo"


def test_interpolate_env_vars_uses_default_when_var_unset() -> None:
    assert interpolate_env_vars("${MISSING:-fallback}", {}) == "fallback"


def test_interpolate_env_vars_prefers_value_over_default_when_var_is_set() -> None:
    assert interpolate_env_vars("${X:-fallback}", {"X": "set"}) == "set"


def test_interpolate_env_vars_leaves_undefined_bare_var_literal() -> None:
    """Undefined ``$VAR`` without a default is left as-is, not replaced with empty."""
    assert interpolate_env_vars("$UNDEFINED/foo", {}) == "$UNDEFINED/foo"


def test_interpolate_env_vars_leaves_undefined_braced_var_literal() -> None:
    assert interpolate_env_vars("${UNDEFINED}/foo", {}) == "${UNDEFINED}/foo"


def test_interpolate_env_vars_supports_empty_default() -> None:
    assert interpolate_env_vars("${MISSING:-}", {}) == ""


def test_interpolate_env_vars_handles_multiple_refs_in_one_string() -> None:
    env = {"A": "alpha", "B": "beta"}
    assert interpolate_env_vars("$A and ${B} and ${C:-gamma}", env) == "alpha and beta and gamma"


def test_interpolate_env_vars_does_not_recurse_into_substituted_value() -> None:
    """Substitution is a single pass: ``${A}`` resolving to ``${B}`` does not then resolve ``${B}``."""
    env = {"A": "${B}", "B": "ignored"}
    assert interpolate_env_vars("${A}", env) == "${B}"


def test_interpolate_env_vars_no_refs_returns_input_unchanged() -> None:
    assert interpolate_env_vars("plain string with no vars", {}) == "plain string with no vars"


# =============================================================================
# Hook builders
# =============================================================================


def test_build_readiness_hooks_config_emits_session_start_touch_command() -> None:
    config = build_readiness_hooks_config()
    assert HOOK_EVENT_SESSION_START in config["hooks"]
    matcher_groups = config["hooks"][HOOK_EVENT_SESSION_START]
    assert len(matcher_groups) == 1
    inner = matcher_groups[0]["hooks"]
    assert len(inner) == 1
    assert inner[0]["type"] == "command"
    assert "session_started" in inner[0]["command"]
    assert "MNGR_AGENT_STATE_DIR" in inner[0]["command"]


def test_build_permission_auto_allow_hooks_config_uses_before_tool_with_wildcard() -> None:
    config = build_permission_auto_allow_hooks_config()
    assert HOOK_EVENT_BEFORE_TOOL in config["hooks"]
    matcher_groups = config["hooks"][HOOK_EVENT_BEFORE_TOOL]
    assert len(matcher_groups) == 1
    assert matcher_groups[0]["matcher"] == ".*"
    inner = matcher_groups[0]["hooks"]
    assert len(inner) == 1
    assert '"decision":"allow"' in inner[0]["command"]


# =============================================================================
# Merge
# =============================================================================


def test_hook_already_exists_matches_on_command_set() -> None:
    existing = [{"hooks": [{"command": "echo a"}, {"command": "echo b"}]}]
    new_same_commands = {"hooks": [{"command": "echo b"}, {"command": "echo a"}]}
    new_diff_commands = {"hooks": [{"command": "echo c"}]}
    assert hook_already_exists(existing, new_same_commands) is True
    assert hook_already_exists(existing, new_diff_commands) is False


def test_hook_already_exists_returns_false_for_empty_existing() -> None:
    assert hook_already_exists([], {"hooks": [{"command": "echo a"}]}) is False


def test_hook_already_exists_distinguishes_by_matcher() -> None:
    """Same inner commands under different matchers describe different runtime behavior."""
    existing = [{"matcher": ".*", "hooks": [{"command": "echo a"}]}]
    new_same_matcher = {"matcher": ".*", "hooks": [{"command": "echo a"}]}
    new_diff_matcher = {"matcher": "^bash$", "hooks": [{"command": "echo a"}]}
    assert hook_already_exists(existing, new_same_matcher) is True
    assert hook_already_exists(existing, new_diff_matcher) is False


def test_hook_already_exists_treats_missing_matcher_as_none() -> None:
    """Events that don't carry a matcher key (e.g. SessionStart) still dedupe."""
    existing = [{"hooks": [{"command": "echo a"}]}]
    duplicate = {"hooks": [{"command": "echo a"}]}
    matcher_present = {"matcher": ".*", "hooks": [{"command": "echo a"}]}
    assert hook_already_exists(existing, duplicate) is True
    assert hook_already_exists(existing, matcher_present) is False


def test_merge_hooks_config_adds_new_event() -> None:
    existing: dict[str, object] = {"general": {"approvalMode": "default"}}
    new_hooks = build_readiness_hooks_config()
    merged = merge_hooks_config(existing, new_hooks)
    assert merged is not None
    # Original keys preserved
    assert merged["general"] == {"approvalMode": "default"}
    # New event installed
    assert HOOK_EVENT_SESSION_START in merged["hooks"]


def test_merge_hooks_config_appends_to_existing_event_list() -> None:
    existing: dict[str, object] = {
        "hooks": {
            HOOK_EVENT_SESSION_START: [
                {"hooks": [{"type": "command", "command": "echo existing"}]},
            ]
        }
    }
    new_hooks = build_readiness_hooks_config()
    merged = merge_hooks_config(existing, new_hooks)
    assert merged is not None
    matcher_groups = merged["hooks"][HOOK_EVENT_SESSION_START]
    # One group from the existing settings, one appended by the merge.
    assert len(matcher_groups) == 2


def test_merge_hooks_config_returns_none_when_duplicate() -> None:
    existing = build_readiness_hooks_config()
    merged = merge_hooks_config(existing, build_readiness_hooks_config())
    assert merged is None


def test_merge_hooks_config_does_not_mutate_inputs() -> None:
    existing: dict[str, object] = {"hooks": {}}
    new_hooks = build_readiness_hooks_config()
    merge_hooks_config(existing, new_hooks)
    assert existing == {"hooks": {}}
    # new_hooks should still have its original shape
    assert HOOK_EVENT_SESSION_START in new_hooks["hooks"]


def test_merge_hooks_config_preserves_unrelated_event_lists() -> None:
    existing: dict[str, object] = {
        "hooks": {
            "AfterTool": [{"hooks": [{"type": "command", "command": "echo keep"}]}],
        }
    }
    merged = merge_hooks_config(existing, build_readiness_hooks_config())
    assert merged is not None
    assert merged["hooks"]["AfterTool"] == [{"hooks": [{"type": "command", "command": "echo keep"}]}]
    assert HOOK_EVENT_SESSION_START in merged["hooks"]


def test_merge_hooks_config_round_trips_through_disk(tmp_path: Path) -> None:
    """End-to-end: write existing settings, merge in hooks, write back, re-read."""
    settings_path = tmp_path / "settings.json"
    write_gemini_settings(settings_path, {"general": {"approvalMode": "default"}})

    existing = read_gemini_settings(settings_path)
    merged = merge_hooks_config(existing, build_readiness_hooks_config())
    assert merged is not None
    write_gemini_settings(settings_path, merged)

    reloaded = read_gemini_settings(settings_path)
    assert reloaded["general"] == {"approvalMode": "default"}
    assert HOOK_EVENT_SESSION_START in reloaded["hooks"]
