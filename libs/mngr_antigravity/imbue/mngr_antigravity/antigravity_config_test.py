"""Unit tests for antigravity_config helpers."""

import json
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.errors import ConfigParseError
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_antigravity.antigravity_config import CAPTURE_CONVERSATION_ID_SCRIPT_NAME
from imbue.mngr_antigravity.antigravity_config import STATUSLINE_SCRIPT_NAME
from imbue.mngr_antigravity.antigravity_config import build_antigravity_hooks_config
from imbue.mngr_antigravity.antigravity_config import build_antigravity_statusline_settings
from imbue.mngr_antigravity.antigravity_config import build_isolated_settings
from imbue.mngr_antigravity.antigravity_config import build_onboarding_seed
from imbue.mngr_antigravity.antigravity_config import extract_statusline_command
from imbue.mngr_antigravity.antigravity_config import get_antigravity_cli_dir
from imbue.mngr_antigravity.antigravity_config import get_antigravity_hooks_config_path
from imbue.mngr_antigravity.antigravity_config import get_antigravity_oauth_token_path
from imbue.mngr_antigravity.antigravity_config import get_antigravity_onboarding_cache_path
from imbue.mngr_antigravity.antigravity_config import get_antigravity_settings_path
from imbue.mngr_antigravity.antigravity_config import merge_trusted_workspace
from imbue.mngr_antigravity.antigravity_config import read_antigravity_settings
from imbue.mngr_antigravity.antigravity_config import serialize_antigravity_hooks
from imbue.mngr_antigravity.antigravity_config import serialize_antigravity_settings


def test_path_helpers_address_the_gemini_tree_under_a_given_home() -> None:
    """All path helpers are rooted at a given ``$HOME``, so the same builders serve
    both the user's real home and each agent's relocated home (no env-var override
    exists in agy; a per-agent ``$HOME`` is the only lever)."""
    home = Path("/some/home")
    cli_dir = home / ".gemini" / "antigravity-cli"
    assert get_antigravity_cli_dir(home) == cli_dir
    assert get_antigravity_settings_path(home) == cli_dir / "settings.json"
    assert get_antigravity_oauth_token_path(home) == cli_dir / "antigravity-oauth-token"
    assert get_antigravity_onboarding_cache_path(home) == cli_dir / "cache" / "onboarding.json"
    assert get_antigravity_hooks_config_path(home) == home / ".gemini" / "config" / "hooks.json"


def test_serialize_round_trips_to_two_space_indented_json() -> None:
    settings = {"trustedWorkspaces": ["/tmp/a", "/tmp/b"], "colorScheme": "dark"}

    serialized = serialize_antigravity_settings(settings)

    assert json.loads(serialized) == settings
    # Two-space indent, no trailing newline -- mirrors what agy itself writes.
    # A top-level key must be indented by exactly two spaces (not four, which a
    # bare ``"  " in serialized`` check could not distinguish).
    color_scheme_line = next(line for line in serialized.splitlines() if '"colorScheme"' in line)
    assert color_scheme_line.startswith("  ") and not color_scheme_line.startswith("   ")
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


# =============================================================================
# Per-agent settings + onboarding builders
# =============================================================================


def test_build_isolated_settings_layers_base_trust_and_overrides() -> None:
    """Base (copy of user settings) is the floor; trust is merged; overrides win on top."""
    base = {"colorScheme": "dark", "model": "Base Model", "trustedWorkspaces": ["/repo"]}
    overrides = {"model": "Override Model", "permissions": {"allow": ["command(git)"]}}

    result = build_isolated_settings(base, overrides, ["/tmp/ws"])

    # Inherited base key survives.
    assert result["colorScheme"] == "dark"
    # Overrides win over the base value.
    assert result["model"] == "Override Model"
    assert result["permissions"] == {"allow": ["command(git)"]}
    # Workspace path appended to the inherited trust list (deduped, order preserved).
    assert result["trustedWorkspaces"] == ["/repo", "/tmp/ws"]


def test_build_isolated_settings_does_not_mutate_base() -> None:
    """The builder is @pure: the caller's base mapping is left untouched."""
    base = {"trustedWorkspaces": ["/repo"]}
    build_isolated_settings(base, {}, ["/tmp/ws"])
    assert base == {"trustedWorkspaces": ["/repo"]}


def test_build_isolated_settings_dedupes_already_trusted_workspace() -> None:
    base = {"trustedWorkspaces": ["/tmp/ws"]}
    result = build_isolated_settings(base, {}, ["/tmp/ws"])
    assert result["trustedWorkspaces"] == ["/tmp/ws"]


def test_build_isolated_settings_leaves_trust_untouched_for_empty_workspace_list() -> None:
    """An empty trusted_workspaces sequence must not change the inherited trust list."""
    base = {"trustedWorkspaces": ["/repo"]}
    result = build_isolated_settings(base, {}, [])
    assert result["trustedWorkspaces"] == ["/repo"]


def test_build_isolated_settings_omits_trust_key_for_empty_base_and_empty_workspaces() -> None:
    """No spurious empty trustedWorkspaces key when there's nothing to trust."""
    result = build_isolated_settings({}, {}, [])
    assert "trustedWorkspaces" not in result


def test_build_isolated_settings_seeds_trust_list_from_empty_base() -> None:
    """With an empty base (sync_home_settings=False) the workspace still gets trusted."""
    result = build_isolated_settings({}, {}, ["/tmp/ws"])
    assert result["trustedWorkspaces"] == ["/tmp/ws"]


def test_build_isolated_settings_mngr_merge_extends_onto_base() -> None:
    """A desugared ``__mngr_merge`` extend (the internal suffix form provision passes)
    merges the override onto the base list rather than replacing it -- parity with
    mngr_claude's fold."""
    base = {"permissions": {"allow": ["command(git)"]}}
    overrides = {"permissions__extend": {"allow__extend": ["command(npm)"]}}
    result = build_isolated_settings(base, overrides, [])
    assert result["permissions"]["allow"] == ["command(git)", "command(npm)"]


def test_build_isolated_settings_bare_override_narrows_raises_with_mngr_merge_remediation() -> None:
    """A bare override that narrows raises, surfacing the Claude-compatible ``__mngr_merge``
    remediation through antigravity's fold, never the internal suffix form. (The exact
    recursive patch is pinned in external_settings_test.)"""
    base = {"permissions": {"allow": ["command(git)"]}}
    overrides = {"permissions": {"allow": ["command(npm)"]}}
    with pytest.raises(ConfigParseError) as exc_info:
        build_isolated_settings(base, overrides, [])
    message = str(exc_info.value)
    assert "__mngr_merge" in message
    assert "allow__extend" not in message


def test_build_isolated_settings_narrowing_allowed_with_escape_hatch() -> None:
    """``allow_narrowing`` lets a bare override replace the base aggregate without erroring."""
    base = {"permissions": {"allow": ["command(git)"]}}
    overrides = {"permissions": {"allow": ["command(npm)"]}}
    result = build_isolated_settings(base, overrides, [], allow_narrowing=True)
    assert result["permissions"] == {"allow": ["command(npm)"]}


def test_build_isolated_settings_strips_mngr_merge_key_from_base() -> None:
    """A ``__mngr_merge`` key in the base (a no-op on the floor, ignored by agy) is dropped
    so it never leaks into the written settings.json."""
    base = {"model": "Base", "__mngr_merge": {"model": "extend"}}
    result = build_isolated_settings(base, {}, [])
    assert "__mngr_merge" not in result
    assert result["model"] == "Base"


def test_build_onboarding_seed_emits_the_three_nux_keys() -> None:
    """The NUX seed must carry exactly the keys agy checks to skip the first-run flow."""
    seed = build_onboarding_seed()
    assert seed == {
        "consumerOnboardingComplete": True,
        "enterpriseOnboardingComplete": True,
        "onboardingComplete": True,
    }


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
# statusLine settings builder
# =============================================================================


def test_statusline_settings_emits_command_block() -> None:
    """The statusLine block runs statusline.sh, agy's source of truth for lifecycle.

    agy invokes this command on every agent-state change; statusline.sh maintains
    the active marker (RUNNING/WAITING), records the root conversation, and fires
    the message-submission signal. The block must be a {"type":"command"} shape
    pointing at the provisioned script path.
    """
    settings = build_antigravity_statusline_settings()
    assert settings == {
        "statusLine": {
            "type": "command",
            "command": f'bash "$MNGR_AGENT_STATE_DIR/commands/{STATUSLINE_SCRIPT_NAME}"',
        }
    }


def test_extract_statusline_command_returns_command_for_command_block() -> None:
    """A runnable command-type statusLine yields its command string (for compose)."""
    assert extract_statusline_command({"type": "command", "command": "echo hi"}) == "echo hi"


# Each is a statusLine that cannot be run as a shell command, so it is not
# composable: None; a non-dict; a non-"command" type; a command-type block with a
# missing, blank, whitespace-only, or non-string command.
_NON_RUNNABLE_STATUSLINES = [
    None,
    "not-a-dict",
    {"type": "static", "text": "x"},
    {"type": "command"},
    {"type": "command", "command": ""},
    {"type": "command", "command": "   "},
    {"type": "command", "command": 123},
]


@pytest.mark.parametrize("statusline", _NON_RUNNABLE_STATUSLINES)
def test_extract_statusline_command_returns_none_for_non_runnable(statusline: Any) -> None:
    """Anything but a {"type":"command","command":<non-blank str>} block is not composable."""
    assert extract_statusline_command(statusline) is None


# =============================================================================
# Hook config builder
# =============================================================================


def test_hooks_config_emits_only_conversation_id_capture_preinvocation() -> None:
    """The lone hook is a single PreInvocation handler running capture_conversation_id.sh.

    Lifecycle (RUNNING/WAITING) and message submission are driven by the
    statusLine command, NOT hooks -- so the hooks config carries no marker
    handler and no Stop block. The capture hook exists because the statusLine
    payload only ever reports the root conversation, so subagent ids (needed for
    transcript scoping) are surfaced only here.
    """
    config = build_antigravity_hooks_config()

    mngr = config["mngr"]
    # Only PreInvocation remains -- no Stop block.
    assert set(mngr) == {"PreInvocation"}
    pre = mngr["PreInvocation"]
    assert pre == [
        {
            "type": "command",
            "command": f'bash "$MNGR_AGENT_STATE_DIR/commands/{CAPTURE_CONVERSATION_ID_SCRIPT_NAME}"',
        }
    ]


def test_hooks_config_emits_no_stop_handler() -> None:
    """No Stop handler: clearing the active marker is the statusLine command's job."""
    config = build_antigravity_hooks_config()
    assert "Stop" not in config["mngr"]


def test_hooks_config_never_emits_pretooluse() -> None:
    """No PreToolUse hook is generated: auto-approval uses the CLI flag, not a hook.

    agy's documented PreToolUse {"decision": "allow"} output does not actually
    gate the run_command confirmation dialog (verified live against agy 1.0.3),
    so permission auto-approval is wired through --dangerously-skip-permissions
    in assemble_command instead. The hooks file only carries the capture handler.
    """
    config = build_antigravity_hooks_config()
    assert "PreToolUse" not in config["mngr"]
    assert set(config["mngr"]) == {"PreInvocation"}


def test_serialize_antigravity_hooks_round_trips() -> None:
    config = build_antigravity_hooks_config()
    serialized = serialize_antigravity_hooks(config)
    assert json.loads(serialized) == config
    # Two-space indent: the top-level key must start with exactly two spaces,
    # which a bare ``"  " in serialized`` check could not distinguish from four.
    mngr_key_line = next(line for line in serialized.splitlines() if '"mngr"' in line)
    assert mngr_key_line.startswith("  ") and not mngr_key_line.startswith("   ")
