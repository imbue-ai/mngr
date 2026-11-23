import json
from pathlib import Path
from typing import Any
from typing import Final

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.testing import make_current_outcome
from imbue.mngr.api.testing import make_prepare_failed_outcome
from imbue.mngr.api.testing import make_unsupported_until_recreated_outcome
from imbue.mngr.api.testing import make_upgraded_while_stopped_outcome
from imbue.mngr.api.testing import run_upgrade_as_cli
from imbue.mngr.api.upgrade import UPGRADE_STAGING_DIR_NAME
from imbue.mngr.api.upgrade import upgrade_agent
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.host import write_shared_shell_libs
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import make_local_host_of_class
from imbue.mngr_claude.claude_config import MANAGED_SETTINGS_RELATIVE_PATH
from imbue.mngr_claude.claude_config import SESSION_GUARD
from imbue.mngr_claude.claude_config import build_credential_sync_hooks_config
from imbue.mngr_claude.claude_config import build_readiness_hooks_config
from imbue.mngr_claude.claude_config import get_agent_hook_settings_path
from imbue.mngr_claude.claude_config import get_managed_settings_path
from imbue.mngr_claude.claude_config import has_credential_sync_hook
from imbue.mngr_claude.claude_config import has_permission_auto_allow_hook
from imbue.mngr_claude.claude_config import merge_hooks_config
from imbue.mngr_claude.plugin import ClaudeAgent
from imbue.mngr_claude.plugin import ClaudeAgentConfig
from imbue.mngr_claude.plugin_test import make_claude_agent

_COMMON_TRANSCRIPT_SCRIPT_NAMES: Final[tuple[str, ...]] = ("common_transcript.sh", "common_transcript_convert.py")
_STREAM_SNAPSHOT_SCRIPT_NAMES: Final[tuple[str, ...]] = ("stream_snapshot.py",)

_MANAGED_SETTINGS_PATH: Final[Path] = Path(*MANAGED_SETTINGS_RELATIVE_PATH)
_CONFIG_DIR_SETTINGS_PATH: Final[Path] = get_agent_hook_settings_path(Path(), use_env_config_dir=False)

_USER_HOOK_GROUP: Final[dict[str, Any]] = {"hooks": [{"type": "command", "command": "echo user-hook-62017"}]}
_STALE_MNGR_HOOK_ENTRY: Final[dict[str, Any]] = {
    "type": "command",
    "command": SESSION_GUARD + "echo stale-mngr-hook-62017",
}
_STALE_MNGR_HOOK_GROUP: Final[dict[str, Any]] = {"hooks": [_STALE_MNGR_HOOK_ENTRY]}

# The user's own hooks, on events mngr also uses and on one it does not.
_HOME_HOOKS: Final[dict[str, list[dict[str, Any]]]] = {
    "SubagentStop": [{"hooks": [{"type": "command", "command": "echo home-subagent-stop-62017"}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "echo home-stop-62017"}]}],
    "Notification": [
        {"matcher": "idle_prompt", "hooks": [{"type": "command", "command": "echo home-notification-62017"}]}
    ],
    "PermissionRequest": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo home-permission-62017"}]}
    ],
}
_HOME_SETTINGS: Final[dict[str, Any]] = {
    "statusLine": {"type": "command", "command": "echo home-status-line-62017"},
    "hooks": _HOME_HOOKS,
}

_SHARED_CONFIG: Final[ClaudeAgentConfig] = ClaudeAgentConfig(
    check_installation=False,
    isolate_local_config_dir=False,
    auto_dismiss_dialogs_at_startup=True,
    settings_overrides={"model": "opus[1m]"},
)
_ISOLATED_CONFIG: Final[ClaudeAgentConfig] = ClaudeAgentConfig(
    check_installation=False,
    isolate_local_config_dir=True,
    auto_dismiss_dialogs_at_startup=True,
    settings_overrides={"model": "opus[1m]", "hooks__extend": {"Stop__extend": [_USER_HOOK_GROUP]}},
)

_EITHER_CONFIG_DIR_MODE: Final[pytest.MarkDecorator] = pytest.mark.parametrize(
    "base_config",
    [pytest.param(_SHARED_CONFIG, id="shared_config_dir"), pytest.param(_ISOLATED_CONFIG, id="isolated_config_dir")],
)


_STAGING_UPLOAD_ERROR_MESSAGE: Final[str] = "rsync failed (local): No space left on device 62017"


class _StagingUploadFailingHost(Host):
    """Local Host whose upload of a local directory into an upgrade's staging dir fails."""

    def copy_local_directory(self, source_path: Path, target_path: Path, extra_args: str | None) -> None:
        if target_path.name == UPGRADE_STAGING_DIR_NAME:
            raise MngrError(_STAGING_UPLOAD_ERROR_MESSAGE)
        super().copy_local_directory(source_path, target_path, extra_args)


def _provision_as_create_does(agent: ClaudeAgent, host: Host, mngr_ctx: MngrContext) -> None:
    init_git_repo(agent.work_dir, initial_commit=False)
    agent.set_command(agent.assemble_command(host, (), None))
    write_shared_shell_libs(host, agent._get_agent_dir() / "commands")
    agent.provision(host=host, options=CreateAgentOptions(agent_type=AgentTypeName("claude")), mngr_ctx=mngr_ctx)


def _create_shared_config_claude_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
    agent_config: ClaudeAgentConfig,
) -> ClaudeAgent:
    """A stopped claude agent provisioned as `mngr create` provisions one that shares the Claude config dir."""
    shared_config_dir = tmp_path / "shared-claude-config"
    shared_config_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(shared_config_dir))
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx, agent_config=agent_config)
    _provision_as_create_does(agent, host, temp_mngr_ctx)
    return agent


def _create_isolated_config_claude_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    agent_config: ClaudeAgentConfig,
) -> ClaudeAgent:
    """A stopped claude agent provisioned as `mngr create` provisions one with its own Claude config dir, from home settings holding the user's hooks."""
    home_settings_path = Path.home() / ".claude" / "settings.json"
    home_settings_path.parent.mkdir(exist_ok=True)
    home_settings_path.write_text(json.dumps(_HOME_SETTINGS, indent=2) + "\n")
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx, agent_config=agent_config)
    _provision_as_create_does(agent, host, temp_mngr_ctx)
    return agent


def _create_claude_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
    agent_config: ClaudeAgentConfig,
) -> ClaudeAgent:
    """A stopped claude agent provisioned as `mngr create` provisions one under ``agent_config``."""
    if agent_config.isolate_local_config_dir:
        return _create_isolated_config_claude_agent(local_provider, tmp_path, temp_mngr_ctx, agent_config)
    return _create_shared_config_claude_agent(local_provider, tmp_path, temp_mngr_ctx, monkeypatch, agent_config)


def _as_loaded_under_config(agent: ClaudeAgent, agent_config: ClaudeAgentConfig) -> ClaudeAgent:
    """The agent as a `mngr upgrade` run from a directory with another claude config loads it."""
    return agent.model_copy_update(to_update(agent.field_ref().agent_config, agent_config))


def _get_hook_settings_relative_path(agent: ClaudeAgent) -> Path:
    """Where the settings file holding the agent's hooks is under its state dir, by the config it was created with."""
    return _CONFIG_DIR_SETTINGS_PATH if agent.agent_config.isolate_local_config_dir else _MANAGED_SETTINGS_PATH


def _get_hook_settings_path(agent: ClaudeAgent) -> Path:
    return agent._get_agent_dir() / _get_hook_settings_relative_path(agent)


def _add_credential_sync_hook(agent: ClaudeAgent) -> None:
    """Give the agent the credential-sync hook where provisioning writes it, which it does only on macOS."""
    settings_path = _get_hook_settings_path(agent)
    settings = merge_hooks_config(json.loads(settings_path.read_text()), build_credential_sync_hooks_config())
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")


def _read_live_hooks(agent: ClaudeAgent) -> dict[str, Any]:
    return json.loads(_get_hook_settings_path(agent).read_text())["hooks"]


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_rewrites_only_the_generated_files_of_a_stopped_claude_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _create_shared_config_claude_agent(local_provider, tmp_path, temp_mngr_ctx, monkeypatch, _SHARED_CONFIG)
    state_dir = agent._get_agent_dir()
    commands_dir = state_dir / "commands"
    managed_settings_path = get_managed_settings_path(state_dir)
    created_stop_hook = (commands_dir / "wait_for_stop_hook.sh").read_text()
    created_settings = json.loads(managed_settings_path.read_text())

    # Make it look like an older mngr created it from another config, beside files other plugins and Studio wrote.
    (commands_dir / "wait_for_stop_hook.sh").write_text("echo old-stop-hook-48213\n")
    (commands_dir / "mngr_background_task_lib.sh").unlink()
    (commands_dir / "user_statusline_cmd").write_text("echo usage-plugin-48213\n")
    credential_sync_groups = [
        hook_group
        for hook_group in created_settings["hooks"]["Notification"]
        if has_credential_sync_hook({"Notification": [hook_group]})
    ]
    old_hooks = {"Stop": [{"hooks": [_STALE_MNGR_HOOK_ENTRY]}], "Notification": credential_sync_groups}
    assert created_settings["model"] == "opus[1m]"
    managed_settings_path.write_text(
        json.dumps({**created_settings, "hooks": old_hooks, "model": "sonnet", "fastMode": True}, indent=2) + "\n"
    )
    (state_dir / "env").write_text("MNGR_AGENT_NAME=test-agent\n")
    (state_dir / "session_started").touch()
    (state_dir / "claude_main_pid").write_text("48213\n")
    data_before = (state_dir / "data.json").read_bytes()

    outcome = run_upgrade_as_cli(agent)

    assert outcome == make_upgraded_while_stopped_outcome(
        agent,
        (
            Path("commands/mngr_background_task_lib.sh"),
            Path("commands/wait_for_stop_hook.sh"),
            _MANAGED_SETTINGS_PATH,
        ),
    )
    assert (commands_dir / "wait_for_stop_hook.sh").read_text() == created_stop_hook
    assert (commands_dir / "mngr_background_task_lib.sh").is_file()
    assert (commands_dir / "user_statusline_cmd").read_text() == "echo usage-plugin-48213\n"
    assert json.loads(managed_settings_path.read_text()) == {**created_settings, "model": "sonnet", "fastMode": True}
    assert (state_dir / "data.json").read_bytes() == data_before
    assert (state_dir / "env").read_text() == "MNGR_AGENT_NAME=test-agent\n"
    assert not (state_dir / "session_started").exists()
    assert not (state_dir / "claude_main_pid").exists()
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()
    assert agent.get_lifecycle_state() == AgentLifecycleState.STOPPED


@pytest.mark.rsync
def test_upgrade_finds_a_just_created_claude_agent_current(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _create_shared_config_claude_agent(local_provider, tmp_path, temp_mngr_ctx, monkeypatch, _SHARED_CONFIG)

    outcome = run_upgrade_as_cli(agent)

    assert outcome == make_current_outcome(agent)


@pytest.mark.rsync
def test_upgrade_finds_a_just_created_agent_with_its_own_config_dir_and_the_users_hooks_current(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    agent = _create_isolated_config_claude_agent(local_provider, tmp_path, temp_mngr_ctx, _ISOLATED_CONFIG)
    settings_path = _get_hook_settings_path(agent)
    created_settings_bytes = settings_path.read_bytes()
    created_hooks = _read_live_hooks(agent)

    outcome = run_upgrade_as_cli(agent)

    assert outcome == make_current_outcome(agent)
    assert settings_path.read_bytes() == created_settings_bytes
    # The file holds the user's hooks first, then mngr's, then the config's own.
    readiness_hooks = build_readiness_hooks_config()["hooks"]
    assert created_hooks["SubagentStop"] == _HOME_HOOKS["SubagentStop"]
    assert created_hooks["Stop"] == [*_HOME_HOOKS["Stop"], *readiness_hooks["Stop"], _USER_HOOK_GROUP]
    # On macOS the credential-sync group follows these.
    notification_groups = [*_HOME_HOOKS["Notification"], *readiness_hooks["Notification"]]
    assert created_hooks["Notification"][: len(notification_groups)] == notification_groups
    assert created_hooks["PermissionRequest"] == [
        *_HOME_HOOKS["PermissionRequest"],
        *readiness_hooks["PermissionRequest"],
    ]


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_restores_mngrs_script_and_its_hooks_among_the_users_in_an_agents_own_config_dir(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    agent = _create_isolated_config_claude_agent(local_provider, tmp_path, temp_mngr_ctx, _ISOLATED_CONFIG)
    state_dir = agent._get_agent_dir()
    stop_hook_path = state_dir / "commands" / "wait_for_stop_hook.sh"
    created_stop_hook = stop_hook_path.read_text()
    stop_hook_path.write_text("echo old-stop-hook-48213\n")
    settings_path = _get_hook_settings_path(agent)
    created_settings = json.loads(settings_path.read_text())
    credential_sync_groups = [
        hook_group
        for hook_group in created_settings["hooks"]["Notification"]
        if has_credential_sync_hook({"Notification": [hook_group]})
    ]
    stale_hooks = {
        "SubagentStop": _HOME_HOOKS["SubagentStop"],
        "Stop": [*_HOME_HOOKS["Stop"], _STALE_MNGR_HOOK_GROUP, _USER_HOOK_GROUP],
        "Notification": [*_HOME_HOOKS["Notification"], _STALE_MNGR_HOOK_GROUP, *credential_sync_groups],
        "PermissionRequest": [*_HOME_HOOKS["PermissionRequest"], _STALE_MNGR_HOOK_GROUP],
        "SubagentStart": [_STALE_MNGR_HOOK_GROUP],
    }
    settings_path.write_text(json.dumps({**created_settings, "hooks": stale_hooks, "fastMode": True}, indent=2) + "\n")

    first_outcome = run_upgrade_as_cli(agent)
    second_outcome = run_upgrade_as_cli(agent)

    assert first_outcome == make_upgraded_while_stopped_outcome(
        agent, (Path("commands/wait_for_stop_hook.sh"), _CONFIG_DIR_SETTINGS_PATH)
    )
    assert second_outcome == make_current_outcome(agent)
    assert stop_hook_path.read_text() == created_stop_hook
    upgraded_settings = json.loads(settings_path.read_text())
    assert upgraded_settings == {**created_settings, "fastMode": True}
    assert upgraded_settings["statusLine"] == _HOME_SETTINGS["statusLine"]
    assert list(upgraded_settings["hooks"])[: len(_HOME_HOOKS)] == list(_HOME_HOOKS)
    assert not get_managed_settings_path(state_dir).exists()
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()


@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.parametrize(
    ("created_config", "running_config"),
    [
        pytest.param(_SHARED_CONFIG, _ISOLATED_CONFIG, id="created_sharing_the_config_dir"),
        pytest.param(_ISOLATED_CONFIG, _SHARED_CONFIG, id="created_with_its_own_config_dir"),
    ],
)
def test_upgrade_rewrites_the_settings_file_the_agent_loads_under_a_config_of_the_other_mode(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
    created_config: ClaudeAgentConfig,
    running_config: ClaudeAgentConfig,
) -> None:
    agent = _create_claude_agent(local_provider, tmp_path, temp_mngr_ctx, monkeypatch, created_config)
    state_dir = agent._get_agent_dir()
    settings_path = _get_hook_settings_path(agent)
    created_settings = json.loads(settings_path.read_text())
    settings_path.write_text(json.dumps({**created_settings, "hooks": {"Stop": [_STALE_MNGR_HOOK_GROUP]}}) + "\n")
    agent_under_running_config = _as_loaded_under_config(agent, running_config)

    first_outcome = run_upgrade_as_cli(agent_under_running_config)
    second_outcome = run_upgrade_as_cli(agent_under_running_config)

    assert first_outcome == make_upgraded_while_stopped_outcome(agent, (_get_hook_settings_relative_path(agent),))
    assert second_outcome == make_current_outcome(agent)
    assert json.loads(settings_path.read_text()) == {
        **created_settings,
        "hooks": build_readiness_hooks_config()["hooks"],
    }
    settings_paths_under_state_dir = {state_dir / _MANAGED_SETTINGS_PATH, state_dir / _CONFIG_DIR_SETTINGS_PATH}
    assert {path for path in settings_paths_under_state_dir if path.exists()} == {settings_path}


@pytest.mark.parametrize(
    "settings_text",
    [
        pytest.param(None, id="missing"),
        pytest.param(json.dumps(_HOME_SETTINGS), id="no_mngr_hook"),
    ],
)
def test_upgrade_leaves_an_agent_whose_own_config_dir_holds_no_hook_of_mngrs_unsupported_until_recreated(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    settings_text: str | None,
) -> None:
    agent = _create_isolated_config_claude_agent(local_provider, tmp_path, temp_mngr_ctx, _ISOLATED_CONFIG)
    state_dir = agent._get_agent_dir()
    settings_path = _get_hook_settings_path(agent)
    if settings_text is None:
        settings_path.unlink()
    else:
        settings_path.write_text(settings_text)
    (state_dir / "commands" / "wait_for_stop_hook.sh").write_text("echo old-stop-hook-48213\n")

    outcome = run_upgrade_as_cli(agent)

    assert outcome == make_unsupported_until_recreated_outcome(
        agent,
        "created before mngr kept its hooks in a file of its own; first remove the hooks mngr left in .claude/settings.local.json in its work dir",
    )
    assert (state_dir / "commands" / "wait_for_stop_hook.sh").read_text() == "echo old-stop-hook-48213\n"
    assert (settings_path.read_text() if settings_path.exists() else None) == settings_text
    assert not get_managed_settings_path(state_dir).exists()


@pytest.mark.rsync
@pytest.mark.parametrize("is_config_dir_isolated", [False, True])
def test_upgrade_finds_an_agent_created_with_every_option_current(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
    is_config_dir_isolated: bool,
) -> None:
    pre_tool_use_group = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo user-pre-tool-62017"}]}
    every_option_config = ClaudeAgentConfig(
        check_installation=False,
        isolate_local_config_dir=is_config_dir_isolated,
        auto_dismiss_dialogs_at_startup=True,
        auto_allow_permissions=True,
        sync_credentials_on_login=True,
        emit_common_transcript=True,
        streaming_snapshot_interval_seconds=0.25,
        settings_overrides={
            "model": "opus[1m]",
            "hooks__extend": {"Stop__extend": [_USER_HOOK_GROUP], "PreToolUse": [pre_tool_use_group]},
        },
    )
    agent = _create_claude_agent(local_provider, tmp_path, temp_mngr_ctx, monkeypatch, every_option_config)
    commands_dir = agent._get_agent_dir() / "commands"
    created_hooks = _read_live_hooks(agent)

    outcome = run_upgrade_as_cli(agent)

    assert outcome == make_current_outcome(agent)
    assert has_permission_auto_allow_hook(created_hooks)
    assert created_hooks["Stop"][-1] == _USER_HOOK_GROUP
    assert created_hooks["PreToolUse"] == [pre_tool_use_group]
    optional_script_names = (*_COMMON_TRANSCRIPT_SCRIPT_NAMES, *_STREAM_SNAPSHOT_SCRIPT_NAMES)
    assert [name for name in optional_script_names if not (commands_dir / name).is_file()] == []


@pytest.mark.rsync
@_EITHER_CONFIG_DIR_MODE
@pytest.mark.parametrize("is_created_with_auto_allow", [True, False])
def test_upgrade_leaves_permission_auto_allow_as_the_agent_was_created_with_it(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
    base_config: ClaudeAgentConfig,
    is_created_with_auto_allow: bool,
) -> None:
    created_config = base_config.model_copy_update(
        to_update(base_config.field_ref().auto_allow_permissions, is_created_with_auto_allow)
    )
    running_config = base_config.model_copy_update(
        to_update(base_config.field_ref().auto_allow_permissions, not is_created_with_auto_allow)
    )
    agent = _create_claude_agent(local_provider, tmp_path, temp_mngr_ctx, monkeypatch, created_config)
    settings_path = _get_hook_settings_path(agent)
    created_settings_bytes = settings_path.read_bytes()
    agent_under_running_config = _as_loaded_under_config(agent, running_config)

    first_outcome = run_upgrade_as_cli(agent_under_running_config)
    second_outcome = run_upgrade_as_cli(agent_under_running_config)

    assert first_outcome == make_current_outcome(agent)
    assert second_outcome == make_current_outcome(agent)
    assert settings_path.read_bytes() == created_settings_bytes
    assert has_permission_auto_allow_hook(_read_live_hooks(agent)) is is_created_with_auto_allow


@pytest.mark.rsync
@_EITHER_CONFIG_DIR_MODE
@pytest.mark.parametrize("is_created_with_credential_sync", [True, False])
def test_upgrade_leaves_credential_sync_as_the_agent_was_created_with_it(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
    base_config: ClaudeAgentConfig,
    is_created_with_credential_sync: bool,
) -> None:
    created_config = base_config.model_copy_update(
        to_update(base_config.field_ref().sync_credentials_on_login, is_created_with_credential_sync)
    )
    running_config = base_config.model_copy_update(
        to_update(base_config.field_ref().sync_credentials_on_login, not is_created_with_credential_sync)
    )
    agent = _create_claude_agent(local_provider, tmp_path, temp_mngr_ctx, monkeypatch, created_config)
    if is_created_with_credential_sync:
        _add_credential_sync_hook(agent)
    settings_path = _get_hook_settings_path(agent)
    created_settings_bytes = settings_path.read_bytes()
    agent_under_running_config = _as_loaded_under_config(agent, running_config)

    first_outcome = run_upgrade_as_cli(agent_under_running_config)
    second_outcome = run_upgrade_as_cli(agent_under_running_config)

    assert first_outcome == make_current_outcome(agent)
    assert second_outcome == make_current_outcome(agent)
    assert settings_path.read_bytes() == created_settings_bytes
    assert has_credential_sync_hook(_read_live_hooks(agent)) is is_created_with_credential_sync


@pytest.mark.rsync
def test_upgrade_adds_no_settings_overrides_hook_of_the_running_config(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _create_shared_config_claude_agent(local_provider, tmp_path, temp_mngr_ctx, monkeypatch, _SHARED_CONFIG)
    managed_settings_path = get_managed_settings_path(agent._get_agent_dir())
    created_settings_bytes = managed_settings_path.read_bytes()
    running_config = _SHARED_CONFIG.model_copy_update(
        to_update(
            _SHARED_CONFIG.field_ref().settings_overrides,
            {"model": "opus[1m]", "hooks__extend": {"Stop__extend": [_USER_HOOK_GROUP]}},
        )
    )

    outcome = run_upgrade_as_cli(_as_loaded_under_config(agent, running_config))

    assert outcome == make_current_outcome(agent)
    assert managed_settings_path.read_bytes() == created_settings_bytes


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_keeps_the_hooks_mngr_did_not_write_and_drops_a_stale_mngr_hook(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _create_shared_config_claude_agent(local_provider, tmp_path, temp_mngr_ctx, monkeypatch, _SHARED_CONFIG)
    managed_settings_path = get_managed_settings_path(agent._get_agent_dir())
    created_settings = json.loads(managed_settings_path.read_text())
    created_hooks = created_settings["hooks"]
    user_entry_of_mixed_group = {"type": "command", "command": "echo user-hook-of-mixed-group-62017"}
    mixed_group = {"matcher": "startup", "hooks": [_STALE_MNGR_HOOK_ENTRY, user_entry_of_mixed_group]}
    live_hooks = {
        **created_hooks,
        "SessionStart": [*created_hooks["SessionStart"], mixed_group],
        "Stop": [*created_hooks["Stop"], _USER_HOOK_GROUP, {"hooks": [_STALE_MNGR_HOOK_ENTRY]}],
    }
    managed_settings_path.write_text(json.dumps({**created_settings, "hooks": live_hooks}, indent=2) + "\n")

    first_outcome = run_upgrade_as_cli(agent)
    second_outcome = run_upgrade_as_cli(agent)

    assert first_outcome == make_upgraded_while_stopped_outcome(agent, (_MANAGED_SETTINGS_PATH,))
    assert second_outcome == make_current_outcome(agent)
    assert _read_live_hooks(agent) == {
        **created_hooks,
        "SessionStart": [
            *created_hooks["SessionStart"],
            {"matcher": "startup", "hooks": [user_entry_of_mixed_group]},
        ],
        "Stop": [*created_hooks["Stop"], _USER_HOOK_GROUP],
    }


@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.parametrize(
    ("is_created_with_common_transcript", "script_names_agent_has", "script_names_agent_lacks"),
    [
        pytest.param(True, _COMMON_TRANSCRIPT_SCRIPT_NAMES, _STREAM_SNAPSHOT_SCRIPT_NAMES, id="common_transcript"),
        pytest.param(False, _STREAM_SNAPSHOT_SCRIPT_NAMES, _COMMON_TRANSCRIPT_SCRIPT_NAMES, id="stream_snapshot"),
    ],
)
def test_upgrade_rewrites_the_optional_scripts_an_agent_has_and_adds_none_it_lacks(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
    is_created_with_common_transcript: bool,
    script_names_agent_has: tuple[str, ...],
    script_names_agent_lacks: tuple[str, ...],
) -> None:
    created_config = _SHARED_CONFIG.model_copy_update(
        to_update(_SHARED_CONFIG.field_ref().emit_common_transcript, is_created_with_common_transcript),
        to_update(
            _SHARED_CONFIG.field_ref().streaming_snapshot_interval_seconds,
            0.0 if is_created_with_common_transcript else 0.25,
        ),
    )
    # The running config enables exactly the option the agent was created without.
    running_config = _SHARED_CONFIG.model_copy_update(
        to_update(_SHARED_CONFIG.field_ref().emit_common_transcript, not is_created_with_common_transcript),
        to_update(
            _SHARED_CONFIG.field_ref().streaming_snapshot_interval_seconds,
            0.25 if is_created_with_common_transcript else 0.0,
        ),
    )
    agent = _create_shared_config_claude_agent(local_provider, tmp_path, temp_mngr_ctx, monkeypatch, created_config)
    commands_dir = agent._get_agent_dir() / "commands"
    created_scripts = {name: (commands_dir / name).read_text() for name in script_names_agent_has}
    for name in script_names_agent_has:
        (commands_dir / name).write_text("echo old-script-39581\n")
    agent_under_running_config = _as_loaded_under_config(agent, running_config)

    first_outcome = run_upgrade_as_cli(agent_under_running_config)
    second_outcome = run_upgrade_as_cli(agent_under_running_config)

    assert first_outcome == make_upgraded_while_stopped_outcome(
        agent, tuple(Path("commands") / name for name in script_names_agent_has)
    )
    assert second_outcome == make_current_outcome(agent)
    assert {name: (commands_dir / name).read_text() for name in script_names_agent_has} == created_scripts
    assert [name for name in script_names_agent_lacks if (commands_dir / name).exists()] == []


@pytest.mark.rsync
def test_upgrade_finds_a_settings_file_another_writer_reformatted_current(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _create_shared_config_claude_agent(local_provider, tmp_path, temp_mngr_ctx, monkeypatch, _SHARED_CONFIG)
    managed_settings_path = get_managed_settings_path(agent._get_agent_dir())
    created_settings = json.loads(managed_settings_path.read_text())
    reformatted_settings_text = json.dumps({**created_settings, "fastMode": True})
    managed_settings_path.write_text(reformatted_settings_text)

    outcome = run_upgrade_as_cli(agent)

    assert outcome == make_current_outcome(agent)
    assert managed_settings_path.read_text() == reformatted_settings_text


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_writes_a_missing_managed_settings_file_fresh(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _create_shared_config_claude_agent(local_provider, tmp_path, temp_mngr_ctx, monkeypatch, _SHARED_CONFIG)
    managed_settings_path = get_managed_settings_path(agent._get_agent_dir())
    managed_settings_path.unlink()

    first_outcome = run_upgrade_as_cli(agent)
    second_outcome = run_upgrade_as_cli(agent)

    assert first_outcome == make_upgraded_while_stopped_outcome(agent, (_MANAGED_SETTINGS_PATH,))
    assert second_outcome == make_current_outcome(agent)
    assert json.loads(managed_settings_path.read_text()) == {"hooks": build_readiness_hooks_config()["hooks"]}


@_EITHER_CONFIG_DIR_MODE
@pytest.mark.parametrize(
    ("settings_text", "expected_problem"),
    [
        pytest.param(
            "{not json",
            "is not valid JSON: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
            id="not_json",
        ),
        pytest.param("[]", "does not hold a JSON object", id="list"),
        pytest.param(
            '{"hooks": []}',
            "has a `hooks` value that does not map each event to a list of hook groups",
            id="hooks_list",
        ),
        pytest.param(
            '{"hooks": {"Stop": {}}}',
            "has a `hooks` value that does not map each event to a list of hook groups",
            id="event_object",
        ),
    ],
)
def test_upgrade_reports_a_malformed_settings_file_as_a_prepare_failure_in_either_config_dir_mode(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
    base_config: ClaudeAgentConfig,
    settings_text: str,
    expected_problem: str,
) -> None:
    agent = _create_claude_agent(local_provider, tmp_path, temp_mngr_ctx, monkeypatch, base_config)
    state_dir = agent._get_agent_dir()
    settings_path = _get_hook_settings_path(agent)
    settings_path.write_text(settings_text)

    outcome = run_upgrade_as_cli(agent)

    assert outcome == make_prepare_failed_outcome(
        agent, f"{_get_hook_settings_relative_path(agent)} {expected_problem}"
    )
    assert settings_path.read_text() == settings_text
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()


def test_upgrade_reports_files_it_cannot_stage_as_a_prepare_failure(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _create_shared_config_claude_agent(local_provider, tmp_path, temp_mngr_ctx, monkeypatch, _SHARED_CONFIG)
    state_dir = agent._get_agent_dir()
    (state_dir / "commands" / "wait_for_stop_hook.sh").write_text("echo old-stop-hook-48213\n")
    upload_failing_host = make_local_host_of_class(local_provider, _StagingUploadFailingHost)

    outcome = upgrade_agent(
        agent, upload_failing_host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx
    )

    assert outcome == make_prepare_failed_outcome(agent, _STAGING_UPLOAD_ERROR_MESSAGE)
    assert (state_dir / "commands" / "wait_for_stop_hook.sh").read_text() == "echo old-stop-hook-48213\n"
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()
