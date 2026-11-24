"""Unit tests for codex_config helpers."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_codex.codex_config import RECORD_SESSION_POINTERS_SCRIPT_NAME
from imbue.mngr_codex.codex_config import TURN_HOOK_SCRIPT_NAME
from imbue.mngr_codex.codex_config import build_codex_config
from imbue.mngr_codex.codex_config import build_codex_hooks_config
from imbue.mngr_codex.codex_config import extract_latest_codex_version
from imbue.mngr_codex.codex_config import get_codex_app_server_socket_path
from imbue.mngr_codex.codex_config import get_codex_auth_path
from imbue.mngr_codex.codex_config import get_codex_config_path
from imbue.mngr_codex.codex_config import get_codex_home
from imbue.mngr_codex.codex_config import get_codex_hooks_path
from imbue.mngr_codex.codex_config import get_codex_personality_migration_path
from imbue.mngr_codex.codex_config import get_codex_version_cache_path
from imbue.mngr_codex.codex_config import is_codex_update_available
from imbue.mngr_codex.codex_config import is_mngr_hook_entry
from imbue.mngr_codex.codex_config import is_project_trusted
from imbue.mngr_codex.codex_config import is_turn_hook_installed
from imbue.mngr_codex.codex_config import merge_project_trust
from imbue.mngr_codex.codex_config import parse_codex_cli_version
from imbue.mngr_codex.codex_config import read_codex_config
from imbue.mngr_codex.codex_config import rebuild_codex_hooks_keeping_other_entries
from imbue.mngr_codex.codex_config import rewrite_rollout_record_cwd
from imbue.mngr_codex.codex_config import serialize_codex_config
from imbue.mngr_codex.codex_config import serialize_codex_hooks

# Path helpers


def test_codex_home_is_under_the_agent_state_dir() -> None:
    state_dir = Path("/state/agents/abc")
    assert get_codex_home(state_dir) == state_dir / "plugin" / "codex" / "home"


def test_path_helpers_address_the_codex_home_tree() -> None:
    """All file helpers are rooted at a given CODEX_HOME, so the same builders serve
    both the user's real ~/.codex and each agent's isolated CODEX_HOME."""
    home = Path("/some/codex_home")
    assert get_codex_config_path(home) == home / "config.toml"
    assert get_codex_auth_path(home) == home / "auth.json"
    assert get_codex_hooks_path(home) == home / "hooks.json"
    assert get_codex_personality_migration_path(home) == home / ".personality_migration"
    assert get_codex_version_cache_path(home) == home / "version.json"


# Update-check helpers (codex's version.json)


def test_app_server_socket_sits_in_its_own_directory_under_tmp() -> None:
    """codex refuses a socket whose parent other users can write to, which a bare /tmp can be."""
    deep_home = Path("/tmp") / ("nested" * 20) / "agent" / "plugin" / "codex" / ".codex"
    socket_path = get_codex_app_server_socket_path(deep_home)

    assert socket_path.parent != Path("/tmp")
    assert socket_path.parent.parent == Path("/tmp")
    assert len(str(socket_path)) < 104
    assert get_codex_app_server_socket_path(deep_home) == socket_path
    assert get_codex_app_server_socket_path(deep_home / "other").parent != socket_path.parent


def test_parse_codex_cli_version_extracts_the_bare_semver() -> None:
    assert parse_codex_cli_version("codex-cli 0.138.0") == "0.138.0"
    assert parse_codex_cli_version("codex-cli 0.139.0\n") == "0.139.0"
    # A bare version (no leading program name) still parses.
    assert parse_codex_cli_version("1.2.3") == "1.2.3"


def test_parse_codex_cli_version_returns_none_when_absent_or_unclean() -> None:
    # Empty output (codex not installed -> the probe captured nothing).
    assert parse_codex_cli_version("") is None
    assert parse_codex_cli_version("   \n") is None
    # A pre-release / build-tagged version is treated as unparseable (conservative,
    # like codex's own is_newer), so we skip rather than risk a false notice.
    assert parse_codex_cli_version("codex-cli 0.139.0-rc.1") is None


def test_extract_latest_codex_version_reads_the_cache_field() -> None:
    cache = {"latest_version": "0.139.0", "last_checked_at": "2026-06-09T07:49:30Z", "dismissed_version": None}
    assert extract_latest_codex_version(cache) == "0.139.0"


def test_extract_latest_codex_version_returns_none_for_missing_or_unclean() -> None:
    assert extract_latest_codex_version({}) is None
    assert extract_latest_codex_version({"latest_version": None}) is None
    assert extract_latest_codex_version({"latest_version": 139}) is None
    assert extract_latest_codex_version({"latest_version": "0.139.0-rc.1"}) is None


def test_is_codex_update_available_compares_numeric_semver() -> None:
    assert is_codex_update_available("0.138.0", "0.139.0") is True
    assert is_codex_update_available("0.139.0", "0.139.0") is False
    assert is_codex_update_available("0.139.0", "0.138.0") is False
    # Compared as integer tuples, so 0.10.0 is newer than 0.9.0 (a string compare
    # would get this wrong).
    assert is_codex_update_available("0.9.0", "0.10.0") is True


def test_is_codex_update_available_is_false_for_unparseable_input() -> None:
    assert is_codex_update_available("not-a-version", "0.139.0") is False
    assert is_codex_update_available("0.138.0", "") is False


# build_codex_config


def test_build_codex_config_always_pins_the_file_credential_store() -> None:
    """The file store pin is load-bearing for shared auth and must always be present."""
    config = build_codex_config(
        model=None,
        model_reasoning_effort=None,
        sandbox_mode=None,
        approval_policy=None,
        trusted_projects=[],
        config_overrides={},
    )
    assert config["cli_auth_credentials_store"] == "file"
    # The blocking startup update prompt is always disabled so it can't intercept
    # the first message.
    assert config["check_for_update_on_startup"] is False
    assert config["notice"] == {
        "hide_full_access_warning": True,
        "hide_world_writable_warning": True,
        "hide_rate_limit_model_nudge": True,
    }
    # None-valued knobs are omitted entirely so codex's own defaults stand.
    assert "model" not in config
    assert "model_reasoning_effort" not in config
    assert "sandbox_mode" not in config
    assert "approval_policy" not in config
    assert "projects" not in config


def test_build_codex_config_writes_only_the_set_knobs() -> None:
    config = build_codex_config(
        model="gpt-5.5",
        model_reasoning_effort="high",
        sandbox_mode="workspace-write",
        approval_policy="never",
        trusted_projects=["/work/agent-1"],
        config_overrides={},
    )
    assert config["model"] == "gpt-5.5"
    assert config["model_reasoning_effort"] == "high"
    assert config["sandbox_mode"] == "workspace-write"
    assert config["approval_policy"] == "never"
    assert config["projects"] == {"/work/agent-1": {"trust_level": "trusted"}}


def test_build_codex_config_overrides_win_last() -> None:
    """config_overrides are merged last (shallow), so a key replaces the built-in value."""
    config = build_codex_config(
        model="gpt-5.5",
        model_reasoning_effort=None,
        sandbox_mode="workspace-write",
        approval_policy=None,
        trusted_projects=[],
        config_overrides={"model": "gpt-5.4", "model_provider": "openai"},
    )
    assert config["model"] == "gpt-5.4"
    assert config["model_provider"] == "openai"
    # The pin is still present (override didn't touch it).
    assert config["cli_auth_credentials_store"] == "file"


def test_build_codex_config_seeds_each_trusted_project() -> None:
    config = build_codex_config(
        model=None,
        model_reasoning_effort=None,
        sandbox_mode=None,
        approval_policy=None,
        trusted_projects=["/work/a", "/work/b"],
        config_overrides={},
    )
    assert config["projects"] == {
        "/work/a": {"trust_level": "trusted"},
        "/work/b": {"trust_level": "trusted"},
    }


# serialize_codex_config


def test_serialize_codex_config_round_trips_via_toml() -> None:
    config = build_codex_config(
        model="gpt-5.5",
        model_reasoning_effort=None,
        sandbox_mode="workspace-write",
        approval_policy="never",
        trusted_projects=["/private/tmp/work dir"],
        config_overrides={"model_provider": "openai"},
    )
    serialized = serialize_codex_config(config)
    parsed = tomllib.loads(serialized)
    assert parsed["model"] == "gpt-5.5"
    assert parsed["sandbox_mode"] == "workspace-write"
    assert parsed["approval_policy"] == "never"
    assert parsed["cli_auth_credentials_store"] == "file"
    assert parsed["model_provider"] == "openai"
    # The project-path table key (with a space) round-trips through TOML quoting.
    assert parsed["projects"]["/private/tmp/work dir"] == {"trust_level": "trusted"}


# merge_project_trust / is_project_trusted


def test_merge_project_trust_adds_to_empty_config() -> None:
    merged = merge_project_trust({}, "/work/agent-1")
    assert merged == {"projects": {"/work/agent-1": {"trust_level": "trusted"}}}


def test_merge_project_trust_preserves_other_projects_and_keys() -> None:
    base = {
        "model": "gpt-5.5",
        "projects": {"/work/other": {"trust_level": "untrusted"}},
    }
    merged = merge_project_trust(base, "/work/agent-1")
    assert merged is not None
    assert merged["model"] == "gpt-5.5"
    assert merged["projects"]["/work/other"] == {"trust_level": "untrusted"}
    assert merged["projects"]["/work/agent-1"] == {"trust_level": "trusted"}


def test_merge_project_trust_returns_none_when_already_trusted() -> None:
    base = {"projects": {"/work/agent-1": {"trust_level": "trusted"}}}
    assert merge_project_trust(base, "/work/agent-1") is None


def test_merge_project_trust_upgrades_an_untrusted_entry() -> None:
    """An existing untrusted entry is upgraded to trusted (other entry keys kept)."""
    base = {"projects": {"/work/agent-1": {"trust_level": "untrusted", "extra": 1}}}
    merged = merge_project_trust(base, "/work/agent-1")
    assert merged is not None
    assert merged["projects"]["/work/agent-1"] == {"trust_level": "trusted", "extra": 1}


def test_merge_project_trust_rejects_non_table_projects() -> None:
    with pytest.raises(UserInputError):
        merge_project_trust({"projects": "oops"}, "/work/agent-1")


def test_merge_project_trust_rejects_non_table_entry() -> None:
    with pytest.raises(UserInputError):
        merge_project_trust({"projects": {"/work/agent-1": "oops"}}, "/work/agent-1")


def test_is_project_trusted() -> None:
    trusted = {"projects": {"/work/a": {"trust_level": "trusted"}}}
    assert is_project_trusted(trusted, "/work/a")
    assert not is_project_trusted(trusted, "/work/missing")
    assert not is_project_trusted({"projects": {"/work/a": {"trust_level": "untrusted"}}}, "/work/a")
    assert not is_project_trusted({"projects": "oops"}, "/work/a")
    assert not is_project_trusted({}, "/work/a")


# hooks builders


def test_build_codex_hooks_config_runs_the_turn_hook_for_each_turn_event() -> None:
    """Guards belong to the repo the agent runs in, which codex reads from its own `.codex/` config layer."""
    hooks = build_codex_hooks_config()["hooks"]
    commands_by_event = {event: [hook["command"] for hook in groups[0]["hooks"]] for event, groups in hooks.items()}

    assert commands_by_event == {
        "UserPromptSubmit": [
            f'bash "$MNGR_AGENT_STATE_DIR/commands/{RECORD_SESSION_POINTERS_SCRIPT_NAME}"',
            f'bash "$MNGR_AGENT_STATE_DIR/commands/{TURN_HOOK_SCRIPT_NAME}" UserPromptSubmit',
        ],
        **{
            event: [f'bash "$MNGR_AGENT_STATE_DIR/commands/{TURN_HOOK_SCRIPT_NAME}" {event}']
            for event in ("Stop", "Interrupt", "PermissionRequest", "PreToolUse", "PostToolUse")
        },
    }


def test_is_turn_hook_installed_tells_current_hooks_from_those_written_before_the_turn_hook() -> None:
    hooks_written_before = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": 'bash "$MNGR_AGENT_STATE_DIR/commands/record_session_pointers.sh"',
                        }
                    ]
                }
            ]
        }
    }

    assert is_turn_hook_installed(serialize_codex_hooks(build_codex_hooks_config()))
    assert not is_turn_hook_installed(serialize_codex_hooks(hooks_written_before))
    assert not is_turn_hook_installed("")


def test_serialize_codex_hooks_round_trips_to_json() -> None:
    hooks = build_codex_hooks_config()
    serialized = serialize_codex_hooks(hooks)
    assert json.loads(serialized) == hooks
    # two-space indent
    assert "  " in serialized


def _command_entry(command: str) -> dict[str, str]:
    return {"type": "command", "command": command}


_RECORD_SESSION_POINTERS_ENTRY = _command_entry(
    f'bash "$MNGR_AGENT_STATE_DIR/commands/{RECORD_SESSION_POINTERS_SCRIPT_NAME}"'
)
# An entry an older mngr wrote, running a script this mngr no longer ships.
_RETIRED_MNGR_ENTRY = _command_entry('bash "$MNGR_AGENT_STATE_DIR/commands/set_active_marker.sh"')
_USER_ENTRY = _command_entry('bash "$MNGR_AGENT_WORK_DIR/scripts/guard-71402.sh"')
_USER_GROUP = {"hooks": [_USER_ENTRY]}


@pytest.mark.parametrize(
    ("hook_entry", "expected"),
    [
        pytest.param(_RECORD_SESSION_POINTERS_ENTRY, True, id="a_script_this_mngr_ships"),
        pytest.param(_RETIRED_MNGR_ENTRY, True, id="a_script_an_older_mngr_shipped"),
        pytest.param(_USER_ENTRY, False, id="a_work_dir_script"),
        pytest.param(_command_entry('python3 "$MNGR_AGENT_STATE_DIR/commands/x.py"'), False, id="not_run_by_bash"),
        pytest.param(
            _command_entry('cd /tmp && bash "$MNGR_AGENT_STATE_DIR/commands/x.sh"'),
            False,
            id="run_after_another_command",
        ),
        pytest.param({"type": "command"}, False, id="no_command"),
        pytest.param("not-an-entry", False, id="not_an_object"),
    ],
)
def test_is_mngr_hook_entry_recognizes_only_mngrs_commands_script_hooks(hook_entry: object, expected: bool) -> None:
    assert is_mngr_hook_entry(hook_entry) is expected


def test_rebuild_codex_hooks_keeping_other_entries_is_mngrs_hooks_for_an_agent_with_none() -> None:
    assert rebuild_codex_hooks_keeping_other_entries({}) == build_codex_hooks_config()["hooks"]


def test_rebuild_codex_hooks_keeping_other_entries_leaves_current_hooks_as_they_are() -> None:
    current_hooks = build_codex_hooks_config()["hooks"]

    assert rebuild_codex_hooks_keeping_other_entries(current_hooks) == current_hooks


def test_rebuild_codex_hooks_keeping_other_entries_puts_mngrs_groups_where_its_entries_were() -> None:
    mngr_hooks = build_codex_hooks_config()["hooks"]
    live_hooks = {
        "SessionStart": [_USER_GROUP],
        "UserPromptSubmit": [_USER_GROUP, {"matcher": "*", "hooks": [_RECORD_SESSION_POINTERS_ENTRY, _USER_ENTRY]}],
        "Stop": [{"hooks": [_RETIRED_MNGR_ENTRY]}],
        "SubagentStop": [{"hooks": [_RETIRED_MNGR_ENTRY]}],
        "Notification": [],
        "PreToolUse": [_USER_GROUP],
    }

    rebuilt_hooks = rebuild_codex_hooks_keeping_other_entries(live_hooks)

    assert rebuilt_hooks == {
        "SessionStart": [_USER_GROUP],
        "UserPromptSubmit": [_USER_GROUP, *mngr_hooks["UserPromptSubmit"], {"matcher": "*", "hooks": [_USER_ENTRY]}],
        "Stop": mngr_hooks["Stop"],
        "Notification": [],
        "PreToolUse": [_USER_GROUP, *mngr_hooks["PreToolUse"]],
        **{event: mngr_hooks[event] for event in ("Interrupt", "PermissionRequest", "PostToolUse")},
    }
    assert list(rebuilt_hooks)[:5] == ["SessionStart", "UserPromptSubmit", "Stop", "Notification", "PreToolUse"]


# read_codex_config (host-backed)


def test_read_codex_config_returns_empty_for_missing_file(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert read_codex_config(host, tmp_path / "config.toml") == {}


def test_read_codex_config_returns_empty_for_blank_file(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    config_path = tmp_path / "config.toml"
    config_path.write_text("   \n\t\n")
    assert read_codex_config(host, config_path) == {}


def test_read_codex_config_parses_nested_tables_into_plain_dicts(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'model = "gpt-5.5"\nsandbox_permissions = ["a", "b"]\n[projects."/work/a"]\ntrust_level = "trusted"\n'
    )
    parsed = read_codex_config(host, config_path)
    assert parsed["model"] == "gpt-5.5"
    assert parsed["sandbox_permissions"] == ["a", "b"]
    assert parsed["projects"] == {"/work/a": {"trust_level": "trusted"}}
    # Plain dicts, not tomlkit proxies, so is_project_trusted / merge work on them.
    assert isinstance(parsed["projects"], dict)
    assert is_project_trusted(parsed, "/work/a")


def test_read_codex_config_raises_on_malformed_toml(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    config_path = tmp_path / "config.toml"
    config_path.write_text("this is = = not valid toml [[[")
    with pytest.raises(UserInputError):
        read_codex_config(host, config_path)


# Rollout cwd rebind (session adoption)

_SESSION_ID = "019ae614-d626-70f1-a87d-31e6966231f5"
_OLD_CWD = "/private/tmp/old/workdir"
_NEW_CWD = "/private/tmp/new/workdir"


def test_rewrite_rollout_record_cwd_rebinds_session_meta_and_turn_context() -> None:
    session_meta = {"type": "session_meta", "payload": {"id": _SESSION_ID, "cwd": _OLD_CWD}}
    turn_context = {"type": "turn_context", "payload": {"cwd": _OLD_CWD, "model": "gpt-5.5"}}
    rewritten_meta = rewrite_rollout_record_cwd(session_meta, _NEW_CWD)
    rewritten_turn = rewrite_rollout_record_cwd(turn_context, _NEW_CWD)
    assert rewritten_meta["payload"]["cwd"] == _NEW_CWD
    assert rewritten_turn["payload"]["cwd"] == _NEW_CWD
    # The session id (and other payload fields) survive the rewrite.
    assert rewritten_meta["payload"]["id"] == _SESSION_ID
    assert rewritten_turn["payload"]["model"] == "gpt-5.5"
    # The input record is not mutated in place (a fresh dict is returned).
    assert session_meta["payload"]["cwd"] == _OLD_CWD


def test_rewrite_rollout_record_cwd_leaves_non_cwd_records_untouched() -> None:
    # A record type without a cwd is returned unchanged.
    response_item = {"type": "response_item", "payload": {"type": "message", "role": "user"}}
    assert rewrite_rollout_record_cwd(response_item, _NEW_CWD) == response_item
    # A cwd-bearing type whose payload happens to lack a cwd is also untouched.
    no_cwd = {"type": "turn_context", "payload": {"model": "gpt-5.5"}}
    assert rewrite_rollout_record_cwd(no_cwd, _NEW_CWD) == no_cwd
