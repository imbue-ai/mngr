"""Unit tests for antigravity_config helpers."""

import json
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_antigravity.antigravity_config import ACTIVE_MARKER_FILENAME
from imbue.mngr_antigravity.antigravity_config import build_antigravity_hooks_config
from imbue.mngr_antigravity.antigravity_config import get_antigravity_user_settings_path
from imbue.mngr_antigravity.antigravity_config import merge_trusted_workspace
from imbue.mngr_antigravity.antigravity_config import read_antigravity_settings
from imbue.mngr_antigravity.antigravity_config import serialize_antigravity_hooks
from imbue.mngr_antigravity.antigravity_config import serialize_antigravity_settings


def test_user_settings_path_lives_under_gemini_antigravity_cli() -> None:
    """Path is fixed by agy; no env-var override exists in the binary."""
    path = get_antigravity_user_settings_path()
    assert path == Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


def test_serialize_round_trips_to_two_space_indented_json() -> None:
    settings = {"trustedWorkspaces": ["/tmp/a", "/tmp/b"], "colorScheme": "dark"}

    serialized = serialize_antigravity_settings(settings)

    assert json.loads(serialized) == settings
    # Two-space indent, no trailing newline -- mirrors what agy itself writes.
    assert "  " in serialized
    assert not serialized.endswith("\n")


def test_merge_trusted_workspace_appends_to_empty_settings() -> None:
    merged = merge_trusted_workspace({}, "/work/agent-1")
    assert merged == {"trustedWorkspaces": ["/work/agent-1"]}


def test_merge_trusted_workspace_appends_to_existing_array() -> None:
    """Existing paths and non-trust keys must be preserved verbatim."""
    base = {"trustedWorkspaces": ["/work/agent-0"], "colorScheme": "dark"}
    merged = merge_trusted_workspace(base, "/work/agent-1")

    assert merged is not None
    assert merged["trustedWorkspaces"] == ["/work/agent-0", "/work/agent-1"]
    assert merged["colorScheme"] == "dark"


def test_merge_trusted_workspace_returns_none_when_already_trusted() -> None:
    """No-op idempotency: a second provision must not duplicate the entry."""
    base = {"trustedWorkspaces": ["/work/agent-1"]}
    assert merge_trusted_workspace(base, "/work/agent-1") is None


def test_merge_trusted_workspace_promotes_non_list_value_to_fresh_array() -> None:
    """If a future agy version stores a different shape under the key, we don't crash."""
    base = {"trustedWorkspaces": "unexpected"}
    merged = merge_trusted_workspace(base, "/work/agent-1")

    assert merged is not None
    assert merged["trustedWorkspaces"] == ["/work/agent-1"]


def test_read_antigravity_settings_returns_empty_dict_for_missing_file(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    settings_path = tmp_path / "does-not-exist.json"
    assert read_antigravity_settings(host, settings_path) == {}


def test_read_antigravity_settings_returns_empty_dict_for_empty_file(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    settings_path = tmp_path / "empty.json"
    settings_path.write_text("")
    assert read_antigravity_settings(host, settings_path) == {}


def test_read_antigravity_settings_raises_for_malformed_json(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """Malformed JSON in user-authored settings is surfaced; we refuse to overwrite it."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    settings_path = tmp_path / "bad.json"
    settings_path.write_text("{ not really json")
    with pytest.raises(UserInputError) as excinfo:
        read_antigravity_settings(host, settings_path)
    assert "malformed JSON" in str(excinfo.value)
    assert str(settings_path) in str(excinfo.value)


def test_read_antigravity_settings_raises_for_non_object_top_level(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """A non-object top-level value (e.g. a JSON array) means an unknown schema; refuse to overwrite."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    settings_path = tmp_path / "array.json"
    settings_path.write_text("[1, 2, 3]")
    with pytest.raises(UserInputError) as excinfo:
        read_antigravity_settings(host, settings_path)
    assert "non-object top-level value" in str(excinfo.value)
    assert "list" in str(excinfo.value)


@pytest.fixture
def settings_with_existing_trust(tmp_path: Path) -> Path:
    settings_path = tmp_path / "settings.json"
    payload: dict[str, Any] = {
        "trustedWorkspaces": ["/work/prior"],
        "colorScheme": "dark",
        "enableTelemetry": False,
    }
    settings_path.write_text(json.dumps(payload, indent=2))
    return settings_path


def test_read_antigravity_settings_returns_parsed_dict(
    local_provider: LocalProviderInstance, settings_with_existing_trust: Path
) -> None:
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    parsed = read_antigravity_settings(host, settings_with_existing_trust)
    assert parsed["trustedWorkspaces"] == ["/work/prior"]
    assert parsed["enableTelemetry"] is False


# =============================================================================
# Hook config builder
# =============================================================================


def test_hooks_config_always_emits_active_marker_via_preinvocation_and_stop() -> None:
    """PreInvocation touches the active marker; Stop removes it.

    The active marker drives BaseAgent's RUNNING/WAITING detection. agy fires
    PreInvocation before each model call (agent working) and Stop when the
    loop terminates (agent idle), so this pair flips the marker at the right
    boundaries.
    """
    config = build_antigravity_hooks_config(auto_allow_permissions=False)

    mngr = config["mngr"]
    # PreInvocation/Stop use the flat handler-list shape (no matcher wrapper).
    pre = mngr["PreInvocation"]
    stop = mngr["Stop"]
    assert pre == [{"type": "command", "command": f'touch "$MNGR_AGENT_STATE_DIR/{ACTIVE_MARKER_FILENAME}"'}]
    assert stop == [{"type": "command", "command": f'rm -f "$MNGR_AGENT_STATE_DIR/{ACTIVE_MARKER_FILENAME}"'}]


def test_hooks_config_omits_pretooluse_when_auto_allow_disabled() -> None:
    """Without auto-allow there is no PreToolUse hook -- tool calls prompt normally."""
    config = build_antigravity_hooks_config(auto_allow_permissions=False)
    assert "PreToolUse" not in config["mngr"]


def test_hooks_config_auto_allow_emits_pretooluse_allow_decision() -> None:
    """auto_allow_permissions adds a match-all PreToolUse hook returning decision=allow.

    PreToolUse uses the matcher-group shape; the command echoes the JSON
    decision agy reads from stdout to suppress the permission dialog.
    """
    config = build_antigravity_hooks_config(auto_allow_permissions=True)

    pre_tool_use = config["mngr"]["PreToolUse"]
    assert len(pre_tool_use) == 1
    group = pre_tool_use[0]
    assert group["matcher"] == "*"
    assert group["hooks"] == [{"type": "command", "command": 'echo \'{"decision":"allow"}\''}]


def test_hooks_config_auto_allow_decision_is_valid_json_allow() -> None:
    """The echoed payload must parse to exactly {"decision": "allow"} so agy honors it."""
    config = build_antigravity_hooks_config(auto_allow_permissions=True)
    command = config["mngr"]["PreToolUse"][0]["hooks"][0]["command"]
    # Strip the surrounding `echo '...'` to recover the JSON payload.
    payload = command[len("echo '") : -len("'")]
    assert json.loads(payload) == {"decision": "allow"}


def test_serialize_antigravity_hooks_round_trips() -> None:
    config = build_antigravity_hooks_config(auto_allow_permissions=True)
    serialized = serialize_antigravity_hooks(config)
    assert json.loads(serialized) == config
    assert "  " in serialized
