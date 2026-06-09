"""Unit tests for antigravity_config helpers."""

import json
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_antigravity.antigravity_config import CAPTURE_CONVERSATION_ID_SCRIPT_NAME
from imbue.mngr_antigravity.antigravity_config import CLEAR_ACTIVE_MARKER_WHEN_IDLE_SCRIPT_NAME
from imbue.mngr_antigravity.antigravity_config import SET_ACTIVE_MARKER_SCRIPT_NAME
from imbue.mngr_antigravity.antigravity_config import build_antigravity_hooks_config
from imbue.mngr_antigravity.antigravity_config import build_isolated_settings
from imbue.mngr_antigravity.antigravity_config import build_onboarding_seed
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
# Hook config builder
# =============================================================================


def test_hooks_config_always_emits_active_marker_via_preinvocation_and_stop() -> None:
    """PreInvocation sets the marker (+ root); Stop clears it only on the root's fully-idle.

    The active marker drives BaseAgent's RUNNING/WAITING detection. agy runs
    PreInvocation before each model call (agent working) and the Stop hooks each
    time any conversation -- the root agent or a subagent -- goes idle. The first
    PreInvocation handler runs set_active_marker.sh (touch + record the turn's
    root) and the Stop handler runs clear_active_marker_when_idle.sh, which
    removes the marker only on the root's fully-idle Stop, so the pair flips the
    marker at the right boundaries even when subagents / background tasks finish
    first.
    """
    config = build_antigravity_hooks_config()

    mngr = config["mngr"]
    # PreInvocation/Stop use the flat handler-list shape (no matcher wrapper).
    pre = mngr["PreInvocation"]
    stop = mngr["Stop"]
    # The marker/root script is the first PreInvocation handler (a second handler
    # captures the conversation id; see the test below).
    assert pre[0] == {
        "type": "command",
        "command": f'bash "$MNGR_AGENT_STATE_DIR/commands/{SET_ACTIVE_MARKER_SCRIPT_NAME}"',
    }
    # Stop runs the root-gated clear script (not a bare `rm`), so the marker is
    # removed only on the root agent's fully-idle Stop.
    assert stop == [
        {
            "type": "command",
            "command": f'bash "$MNGR_AGENT_STATE_DIR/commands/{CLEAR_ACTIVE_MARKER_WHEN_IDLE_SCRIPT_NAME}"',
        }
    ]


def test_hooks_config_captures_conversation_id_via_second_preinvocation_handler() -> None:
    """A second PreInvocation handler runs capture_conversation_id.sh.

    agy delivers the hook-payload stdin to each handler independently (verified
    live against agy 1.0.4), so the capture handler runs alongside the
    active-marker touch without contending for stdin. The captured id drives
    both conversation resume (assemble_command) and transcript scoping
    (stream_transcript.sh).
    """
    config = build_antigravity_hooks_config()

    pre = config["mngr"]["PreInvocation"]
    assert len(pre) == 2
    assert pre[1] == {
        "type": "command",
        "command": f'bash "$MNGR_AGENT_STATE_DIR/commands/{CAPTURE_CONVERSATION_ID_SCRIPT_NAME}"',
    }


def test_hooks_config_never_emits_pretooluse() -> None:
    """No PreToolUse hook is generated: auto-approval uses the CLI flag, not a hook.

    agy's documented PreToolUse {"decision": "allow"} output does not actually
    gate the run_command confirmation dialog (verified live against agy 1.0.3),
    so permission auto-approval is wired through --dangerously-skip-permissions
    in assemble_command instead. The hooks file only carries lifecycle markers.
    """
    config = build_antigravity_hooks_config()
    assert "PreToolUse" not in config["mngr"]
    assert set(config["mngr"]) == {"PreInvocation", "Stop"}


def test_serialize_antigravity_hooks_round_trips() -> None:
    config = build_antigravity_hooks_config()
    serialized = serialize_antigravity_hooks(config)
    assert json.loads(serialized) == config
    assert "  " in serialized
