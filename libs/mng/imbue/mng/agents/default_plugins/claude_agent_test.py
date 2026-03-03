import json
import subprocess
from datetime import datetime
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch
from uuid import UUID

import pluggy
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessSetupError
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.mng.agents.default_plugins.claude_agent import ClaudeAgent
from imbue.mng.agents.default_plugins.claude_agent import ClaudeAgentConfig
from imbue.mng.agents.default_plugins.claude_agent import _build_install_command_hint
from imbue.mng.agents.default_plugins.claude_agent import _claude_json_has_primary_api_key
from imbue.mng.agents.default_plugins.claude_agent import _get_claude_version
from imbue.mng.agents.default_plugins.claude_agent import _has_api_credentials_available
from imbue.mng.agents.default_plugins.claude_agent import _install_claude
from imbue.mng.agents.default_plugins.claude_agent import _parse_claude_version_output
from imbue.mng.agents.default_plugins.claude_agent import _read_macos_keychain_credential
from imbue.mng.agents.default_plugins.claude_agent import get_files_for_deploy
from imbue.mng.agents.default_plugins.claude_config import ClaudeDirectoryNotTrustedError
from imbue.mng.agents.default_plugins.claude_config import ClaudeEffortCalloutNotDismissedError
from imbue.mng.agents.default_plugins.claude_config import build_readiness_hooks_config
from imbue.mng.api.test_fixtures import FakeHost
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.config.data_types import EnvVar
from imbue.mng.config.data_types import MngConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.conftest import make_mng_ctx
from imbue.mng.errors import NoCommandDefinedError
from imbue.mng.errors import PluginMngError
from imbue.mng.hosts.host import Host
from imbue.mng.interfaces.host import AgentEnvironmentOptions
from imbue.mng.interfaces.host import AgentGitOptions
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import HostName
from imbue.mng.primitives import WorkDirCopyMode
from imbue.mng.providers.local.instance import LocalProviderInstance
from imbue.mng.utils.testing import init_git_repo

# =============================================================================
# Test Helpers
# =============================================================================


def make_claude_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    mng_ctx: MngContext,
    agent_config: ClaudeAgentConfig | AgentTypeConfig | None = None,
    agent_type: AgentTypeName | None = None,
    work_dir: Path | None = None,
) -> tuple[ClaudeAgent, Host]:
    """Create a ClaudeAgent with a real local host for testing."""
    host = local_provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)
    if work_dir is None:
        work_dir = tmp_path / f"work-{str(AgentId.generate().get_uuid())[:8]}"
        work_dir.mkdir()

    if agent_config is None:
        agent_config = ClaudeAgentConfig(check_installation=False)
    if agent_type is None:
        agent_type = AgentTypeName("claude")

    agent = ClaudeAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=agent_type,
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mng_ctx=mng_ctx,
        agent_config=agent_config,
        host=host,
    )
    return agent, host


def _sid_export_for(uuid: UUID) -> str:
    """Build the expected MAIN_CLAUDE_SESSION_ID export string for a given agent UUID."""
    return (
        f'_MNG_READ_SID=$(cat "$MNG_AGENT_STATE_DIR/claude_session_id" 2>/dev/null || true);'
        f' export MAIN_CLAUDE_SESSION_ID="${{_MNG_READ_SID:-{uuid}}}"'
    )


def _init_git_with_gitignore(work_dir: Path) -> None:
    """Initialize a git repo in work_dir with .claude/settings.local.json gitignored."""
    init_git_repo(work_dir, initial_commit=False)
    (work_dir / ".gitignore").write_text(".claude/settings.local.json\n")


def _setup_git_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """Set up a git repo and worktree for trust extension testing.

    Creates a source repo with .gitignore (for readiness hooks) and a worktree
    branched from it. Requires setup_git_config fixture for git user config.

    Returns (source_path, worktree_path).
    """
    source = tmp_path / "source"
    source.mkdir()
    init_git_repo(source, initial_commit=True)

    # Add .gitignore (needed by _configure_readiness_hooks in provision)
    (source / ".gitignore").write_text(".claude/settings.local.json\n")
    subprocess.run(["git", "-C", str(source), "add", ".gitignore"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(source), "commit", "-m", "add gitignore"],
        check=True,
        capture_output=True,
    )

    # Create worktree from the source repo
    worktree = tmp_path / "worktree"
    subprocess.run(
        ["git", "-C", str(source), "worktree", "add", str(worktree), "-b", "test-branch"],
        check=True,
        capture_output=True,
    )

    return source, worktree


def _write_claude_trust(source_path: Path) -> None:
    """Write ~/.claude.json with trust entry for source_path."""
    config_path = Path.home() / ".claude.json"
    config = {
        "effortCalloutDismissed": True,
        "projects": {
            str(source_path.resolve()): {
                "hasTrustDialogAccepted": True,
                "allowedTools": [],
            }
        },
    }
    config_path.write_text(json.dumps(config))


def _write_mng_trust_entry(path: Path) -> None:
    """Write ~/.claude.json with a mng-created trust entry for path."""
    config_path = Path.home() / ".claude.json"
    config = {
        "effortCalloutDismissed": True,
        "projects": {
            str(path.resolve()): {
                "hasTrustDialogAccepted": True,
                "allowedTools": [],
                "_mngCreated": True,
                "_mngSourcePath": "/some/source",
            }
        },
    }
    config_path.write_text(json.dumps(config))


_WORKTREE_OPTIONS = CreateAgentOptions(
    agent_type=AgentTypeName("claude"),
    git=AgentGitOptions(copy_mode=WorkDirCopyMode.WORKTREE),
)


def _setup_worktree_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    mng_ctx: MngContext,
    *,
    is_source_trusted: bool = False,
) -> tuple[Path, Path, ClaudeAgent, Host]:
    """Set up a git worktree with an agent for trust testing.

    Requires the setup_git_config fixture. Creates a source repo and worktree,
    optionally writes trust for the source, and creates an agent at the worktree.

    Returns (source_path, worktree_path, agent, host).
    """
    source_path, worktree_path = _setup_git_worktree(tmp_path)
    if is_source_trusted:
        _write_claude_trust(source_path)
    agent, host = make_claude_agent(local_provider, tmp_path, mng_ctx, work_dir=worktree_path)
    return source_path, worktree_path, agent, host


# =============================================================================
# ClaudeAgentConfig Tests
# =============================================================================


def test_claude_agent_config_has_default_command() -> None:
    """Claude agent config should have a default command."""
    config = ClaudeAgentConfig()
    assert config.command == CommandString("claude")


def test_claude_agent_config_merge_overrides_command() -> None:
    """Merging should override command field."""
    base = ClaudeAgentConfig()
    override = ClaudeAgentConfig(command=CommandString("custom-claude"))

    merged = base.merge_with(override)

    assert merged.command == CommandString("custom-claude")


def test_claude_agent_config_merge_concatenates_cli_args() -> None:
    """Claude agent config should concatenate cli_args."""
    base = ClaudeAgentConfig(cli_args=("--verbose",))
    override = ClaudeAgentConfig(cli_args=("--model", "sonnet"))

    merged = base.merge_with(override)

    assert merged.cli_args == ("--verbose", "--model", "sonnet")


def test_claude_agent_config_merge_uses_override_cli_args_when_base_empty() -> None:
    """ClaudeAgentConfig merge should use override cli_args when base is empty."""
    base = ClaudeAgentConfig()
    override = ClaudeAgentConfig(cli_args=("--verbose",))

    merged = base.merge_with(override)

    assert merged.cli_args == ("--verbose",)


# =============================================================================
# assemble_command Tests
# =============================================================================


def test_claude_agent_assemble_command_with_no_args(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """ClaudeAgent should generate resume/session-id command format with no args."""
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mng_ctx)

    command = agent.assemble_command(host=host, agent_args=(), command_override=None)

    uuid = agent.id.get_uuid()
    prefix = temp_mng_ctx.config.prefix
    session_name = f"{prefix}test-agent"
    background_cmd = agent._build_background_tasks_command(session_name)
    sid_export = _sid_export_for(uuid)
    # Local hosts should NOT have IS_SANDBOX set
    assert command == CommandString(
        f'{background_cmd} {sid_export} && rm -rf $MNG_AGENT_STATE_DIR/session_started && ( ( find ~/.claude/ -name "$MAIN_CLAUDE_SESSION_ID" | grep . ) && claude --resume "$MAIN_CLAUDE_SESSION_ID" ) || claude --session-id {uuid}'
    )


def test_claude_agent_assemble_command_with_agent_args(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """ClaudeAgent should append agent args to both command variants."""
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mng_ctx)

    command = agent.assemble_command(host=host, agent_args=("--model", "opus"), command_override=None)

    uuid = agent.id.get_uuid()
    prefix = temp_mng_ctx.config.prefix
    session_name = f"{prefix}test-agent"
    background_cmd = agent._build_background_tasks_command(session_name)
    sid_export = _sid_export_for(uuid)
    assert command == CommandString(
        f'{background_cmd} {sid_export} && rm -rf $MNG_AGENT_STATE_DIR/session_started && ( ( find ~/.claude/ -name "$MAIN_CLAUDE_SESSION_ID" | grep . ) && claude --resume "$MAIN_CLAUDE_SESSION_ID" --model opus ) || claude --session-id {uuid} --model opus'
    )


def test_claude_agent_assemble_command_with_cli_args_and_agent_args(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """ClaudeAgent should append both cli_args and agent_args to both command variants."""
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mng_ctx,
        agent_config=ClaudeAgentConfig(cli_args=("--verbose",), check_installation=False),
    )

    command = agent.assemble_command(host=host, agent_args=("--model", "opus"), command_override=None)

    uuid = agent.id.get_uuid()
    prefix = temp_mng_ctx.config.prefix
    session_name = f"{prefix}test-agent"
    background_cmd = agent._build_background_tasks_command(session_name)
    sid_export = _sid_export_for(uuid)
    assert command == CommandString(
        f'{background_cmd} {sid_export} && rm -rf $MNG_AGENT_STATE_DIR/session_started && ( ( find ~/.claude/ -name "$MAIN_CLAUDE_SESSION_ID" | grep . ) && claude --resume "$MAIN_CLAUDE_SESSION_ID" --verbose --model opus ) || claude --session-id {uuid} --verbose --model opus'
    )


def test_claude_agent_assemble_command_with_command_override(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """ClaudeAgent should use command override when provided."""
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mng_ctx)

    command = agent.assemble_command(
        host=host,
        agent_args=("--model", "opus"),
        command_override=CommandString("custom-claude"),
    )

    uuid = agent.id.get_uuid()
    prefix = temp_mng_ctx.config.prefix
    session_name = f"{prefix}test-agent"
    background_cmd = agent._build_background_tasks_command(session_name)
    sid_export = _sid_export_for(uuid)
    assert command == CommandString(
        f'{background_cmd} {sid_export} && rm -rf $MNG_AGENT_STATE_DIR/session_started && ( ( find ~/.claude/ -name "$MAIN_CLAUDE_SESSION_ID" | grep . ) && custom-claude --resume "$MAIN_CLAUDE_SESSION_ID" --model opus ) || custom-claude --session-id {uuid} --model opus'
    )


def test_claude_agent_assemble_command_raises_when_no_command(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """ClaudeAgent should raise NoCommandDefinedError when no command defined."""
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mng_ctx,
        agent_config=AgentTypeConfig(),
        agent_type=AgentTypeName("custom"),
    )

    with pytest.raises(NoCommandDefinedError, match="No command defined"):
        agent.assemble_command(host=host, agent_args=(), command_override=None)


def test_claude_agent_assemble_command_sets_is_sandbox_for_remote_host(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """ClaudeAgent should set IS_SANDBOX=1 only for remote (non-local) hosts."""
    agent, _ = make_claude_agent(local_provider, tmp_path, temp_mng_ctx)

    # Use SimpleNamespace to simulate a non-local host. Creating a real remote host
    # requires SSH infrastructure that is not available in unit tests. The assemble_command
    # method only reads host.is_local to decide whether to set IS_SANDBOX.
    non_local_host = cast(OnlineHostInterface, SimpleNamespace(is_local=False))

    command = agent.assemble_command(host=non_local_host, agent_args=(), command_override=None)

    uuid = agent.id.get_uuid()
    prefix = temp_mng_ctx.config.prefix
    session_name = f"{prefix}test-agent"
    background_cmd = agent._build_background_tasks_command(session_name)
    sid_export = _sid_export_for(uuid)
    # Remote hosts SHOULD have IS_SANDBOX set
    assert command == CommandString(
        f'{background_cmd} export IS_SANDBOX=1 && {sid_export} && rm -rf $MNG_AGENT_STATE_DIR/session_started && ( ( find ~/.claude/ -name "$MAIN_CLAUDE_SESSION_ID" | grep . ) && claude --resume "$MAIN_CLAUDE_SESSION_ID" ) || claude --session-id {uuid}'
    )


# =============================================================================
# Activity Updater Tests
# =============================================================================


def test_build_background_tasks_command(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """_build_background_tasks_command should launch the provisioned background script."""
    agent, _ = make_claude_agent(local_provider, tmp_path, temp_mng_ctx)

    prefix = temp_mng_ctx.config.prefix
    session_name = f"{prefix}test-agent"
    cmd = agent._build_background_tasks_command(session_name)

    # Should be a background subshell
    assert cmd.startswith("(")
    assert cmd.endswith(") &")

    # Should reference the provisioned script
    assert "claude_background_tasks.sh" in cmd

    # Should pass the session name as argument
    assert session_name in cmd


# =============================================================================
# _get_claude_config Tests
# =============================================================================


def test_get_claude_config_returns_config_when_claude_agent_config(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """_get_claude_config should return the config when it is a ClaudeAgentConfig."""
    config = ClaudeAgentConfig(cli_args=("--verbose",), check_installation=False)
    agent, _ = make_claude_agent(local_provider, tmp_path, temp_mng_ctx, agent_config=config)

    result = agent._get_claude_config()

    assert result is config
    assert result.cli_args == ("--verbose",)


def test_get_claude_config_returns_default_when_not_claude_agent_config(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """_get_claude_config should return default ClaudeAgentConfig when config is not ClaudeAgentConfig."""
    agent, _ = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mng_ctx,
        agent_config=AgentTypeConfig(),
    )

    result = agent._get_claude_config()

    assert isinstance(result, ClaudeAgentConfig)
    assert result.command == CommandString("claude")


# =============================================================================
# Provisioning Lifecycle Tests
# =============================================================================


def test_on_before_provisioning_skips_check_when_disabled(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """on_before_provisioning should skip installation check when check_installation=False."""
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mng_ctx)

    options = CreateAgentOptions(agent_type=AgentTypeName("claude"))

    # Should not raise and should complete without error
    agent.on_before_provisioning(host=host, options=options, mng_ctx=temp_mng_ctx)


def test_get_provision_file_transfers_returns_empty_when_no_local_settings(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """get_provision_file_transfers should return empty list when no .claude/ settings exist."""
    # Create agent with sync_repo_settings=True but no .claude/ directory exists
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mng_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, sync_repo_settings=True),
    )

    options = CreateAgentOptions(agent_type=AgentTypeName("claude"))

    transfers = agent.get_provision_file_transfers(host=host, options=options, mng_ctx=temp_mng_ctx)

    assert list(transfers) == []


def test_get_provision_file_transfers_returns_override_folder_files(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """get_provision_file_transfers should return files from override_settings_folder."""
    # Create override folder with a test file
    override_folder = tmp_path / "override_settings"
    override_folder.mkdir()
    test_file = override_folder / "test_config.json"
    test_file.write_text('{"test": true}')

    # Disable sync_repo_settings to test override folder only
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mng_ctx,
        agent_config=ClaudeAgentConfig(
            check_installation=False,
            sync_repo_settings=False,
            override_settings_folder=override_folder,
        ),
    )

    options = CreateAgentOptions(agent_type=AgentTypeName("claude"))

    transfers = list(agent.get_provision_file_transfers(host=host, options=options, mng_ctx=temp_mng_ctx))

    assert len(transfers) == 1
    assert transfers[0].local_path == test_file
    assert str(transfers[0].agent_path) == ".claude/test_config.json"
    assert transfers[0].is_required is False


def test_get_provision_file_transfers_with_sync_repo_settings_disabled(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """get_provision_file_transfers should skip repo settings when sync_repo_settings=False."""
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mng_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, sync_repo_settings=False),
    )

    options = CreateAgentOptions(agent_type=AgentTypeName("claude"))

    transfers = list(agent.get_provision_file_transfers(host=host, options=options, mng_ctx=temp_mng_ctx))

    # Should return empty since sync_repo_settings=False and no override folder
    assert transfers == []


# =============================================================================
# Readiness Hooks Tests
# =============================================================================


def test_build_readiness_hooks_config_has_session_start_hook() -> None:
    """build_readiness_hooks_config should include SessionStart hooks for readiness and session tracking."""
    config = build_readiness_hooks_config()

    assert "hooks" in config
    assert "SessionStart" in config["hooks"]
    assert len(config["hooks"]["SessionStart"]) == 1
    hooks = config["hooks"]["SessionStart"][0]["hooks"]
    assert len(hooks) == 2

    # First hook: creates session_started file for polling-based detection
    assert hooks[0]["type"] == "command"
    assert "touch" in hooks[0]["command"]
    assert "session_started" in hooks[0]["command"]

    # Second hook: tracks current session ID for session replacement detection
    session_id_hook = hooks[1]["command"]
    assert hooks[1]["type"] == "command"
    assert "claude_session_id" in session_id_hook
    assert "session_id" in session_id_hook
    assert "MNG_AGENT_STATE_DIR" in session_id_hook
    # Should fail loudly on missing session_id, not silently swallow
    assert "exit 1" in session_id_hook
    assert ">&2" in session_id_hook
    # Should extract source from hook payload
    assert "source" in session_id_hook
    assert "_MNG_SOURCE" in session_id_hook
    # Should append to history file for tracking old session IDs (with source)
    assert "claude_session_id_history" in session_id_hook
    # Should use atomic write (write to .tmp then mv) to prevent torn reads
    assert "claude_session_id.tmp" in session_id_hook
    assert "mv" in session_id_hook


def test_build_readiness_hooks_config_has_user_prompt_submit_hook() -> None:
    """build_readiness_hooks_config should include UserPromptSubmit hook that creates active file."""
    config = build_readiness_hooks_config()

    assert "UserPromptSubmit" in config["hooks"]
    assert len(config["hooks"]["UserPromptSubmit"]) == 1
    hook = config["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert hook["type"] == "command"
    assert "touch" in hook["command"]
    assert "MNG_AGENT_STATE_DIR" in hook["command"]
    assert "active" in hook["command"]


def test_build_readiness_hooks_config_has_notification_idle_hook() -> None:
    """build_readiness_hooks_config should include Notification idle_prompt hook that removes active file."""
    config = build_readiness_hooks_config()

    assert "Notification" in config["hooks"]
    assert len(config["hooks"]["Notification"]) == 1
    hook_group = config["hooks"]["Notification"][0]
    assert hook_group["matcher"] == "idle_prompt"
    hook = hook_group["hooks"][0]
    assert hook["type"] == "command"
    assert "rm" in hook["command"]
    assert "MNG_AGENT_STATE_DIR" in hook["command"]
    assert "active" in hook["command"]


def test_get_expected_process_name_returns_claude(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """ClaudeAgent.get_expected_process_name should return 'claude'."""
    agent, _ = make_claude_agent(local_provider, tmp_path, temp_mng_ctx)
    assert agent.get_expected_process_name() == "claude"


def test_uses_marker_based_send_message_returns_true(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """ClaudeAgent.uses_marker_based_send_message should return True."""
    agent, _ = make_claude_agent(local_provider, tmp_path, temp_mng_ctx)
    assert agent.uses_marker_based_send_message() is True


def test_configure_readiness_hooks_raises_when_not_gitignored(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """_configure_readiness_hooks should raise when .claude/settings.local.json is not gitignored."""
    host = local_provider.create_host(HostName("localhost"))
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    # Init git but do NOT add .gitignore entry
    init_git_repo(work_dir, initial_commit=False)

    agent = ClaudeAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("claude"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mng_ctx=temp_mng_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False),
        host=host,
    )

    with pytest.raises(PluginMngError, match="not gitignored"):
        agent._configure_readiness_hooks(host)


def test_configure_readiness_hooks_skips_gitignore_check_when_not_a_git_repo(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """_configure_readiness_hooks should skip gitignore check when the work_dir is not a git repo."""
    host = local_provider.create_host(HostName("localhost"))
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    # Do NOT init a git repo -- work_dir is just a plain directory
    agent = ClaudeAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("claude"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mng_ctx=temp_mng_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False),
        host=host,
    )

    # Should succeed without raising (no gitignore check needed for non-git dirs)
    agent._configure_readiness_hooks(host)

    # Verify the hooks file was still created
    settings_path = work_dir / ".claude" / "settings.local.json"
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text())
    assert "hooks" in settings
    assert "SessionStart" in settings["hooks"]


def test_configure_readiness_hooks_creates_settings_file(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """_configure_readiness_hooks should create .claude/settings.local.json."""
    host = local_provider.create_host(HostName("localhost"))
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _init_git_with_gitignore(work_dir)

    agent = ClaudeAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("claude"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mng_ctx=temp_mng_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False),
        host=host,
    )

    agent._configure_readiness_hooks(host)

    # Verify the file was actually created
    settings_path = work_dir / ".claude" / "settings.local.json"
    assert settings_path.exists()

    # Verify the content has the expected hooks
    settings = json.loads(settings_path.read_text())
    assert "hooks" in settings
    assert "SessionStart" in settings["hooks"]
    assert "UserPromptSubmit" in settings["hooks"]
    assert "Notification" in settings["hooks"]


def test_configure_readiness_hooks_merges_with_existing_settings(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """_configure_readiness_hooks should merge with existing settings."""
    host = local_provider.create_host(HostName("localhost"))
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _init_git_with_gitignore(work_dir)

    # Create existing settings file
    claude_dir = work_dir / ".claude"
    claude_dir.mkdir()
    existing_settings = {"model": "opus", "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]}}
    (claude_dir / "settings.local.json").write_text(json.dumps(existing_settings))

    agent = ClaudeAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("claude"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mng_ctx=temp_mng_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False),
        host=host,
    )

    agent._configure_readiness_hooks(host)

    # Read the file and verify it was merged
    settings_path = work_dir / ".claude" / "settings.local.json"
    settings = json.loads(settings_path.read_text())

    # Should preserve existing settings
    assert settings["model"] == "opus"
    assert "PreToolUse" in settings["hooks"]

    # Should add new hooks
    assert "SessionStart" in settings["hooks"]
    assert "UserPromptSubmit" in settings["hooks"]
    assert "Notification" in settings["hooks"]


def test_provision_configures_readiness_hooks(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """provision should configure readiness hooks."""
    # check_installation=False avoids running `claude --version` which would fail in test env
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mng_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False),
    )
    _init_git_with_gitignore(agent.work_dir)

    options = CreateAgentOptions(agent_type=AgentTypeName("claude"))
    agent.provision(host=host, options=options, mng_ctx=temp_mng_ctx)

    # Verify the hooks file was actually created
    settings_path = agent.work_dir / ".claude" / "settings.local.json"
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text())
    assert "hooks" in settings
    assert "SessionStart" in settings["hooks"]


def test_provision_raises_when_remote_installation_disabled(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_host_dir: Path,
    temp_profile_dir: Path,
    plugin_manager: "pluggy.PluginManager",
    mng_test_prefix: str,
) -> None:
    """provision should raise when claude is not installed on remote host and is_remote_agent_installation_allowed is False."""
    config = MngConfig(
        prefix=mng_test_prefix,
        default_host_dir=temp_host_dir,
        is_remote_agent_installation_allowed=False,
    )
    with ConcurrencyGroup(name="test-remote-install") as cg:
        ctx = make_mng_ctx(config, plugin_manager, temp_profile_dir, concurrency_group=cg)
        agent, _ = make_claude_agent(
            local_provider,
            tmp_path,
            ctx,
            agent_config=ClaudeAgentConfig(check_installation=True),
        )

        # Simulate a non-local host where claude is not installed.
        # execute_command returns a failed result to simulate 'command -v claude' failing.
        non_local_host = cast(
            OnlineHostInterface,
            SimpleNamespace(
                is_local=False,
                execute_command=lambda *args, **kwargs: SimpleNamespace(success=False),
            ),
        )

        options = CreateAgentOptions(agent_type=AgentTypeName("claude"))

        with pytest.raises(PluginMngError, match="automatic remote installation is disabled"):
            agent.provision(host=non_local_host, options=options, mng_ctx=ctx)


# =============================================================================
# Trust Extension / Cleanup Tests
# =============================================================================


def test_provision_extends_trust_for_worktree(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mng_ctx: MngContext,
    setup_git_config: None,
) -> None:
    """provision should extend Claude trust when using worktree mode."""
    source_path, worktree_path, agent, host = _setup_worktree_agent(
        local_provider,
        tmp_path,
        temp_mng_ctx,
        is_source_trusted=True,
    )

    agent.provision(host=host, options=_WORKTREE_OPTIONS, mng_ctx=temp_mng_ctx)

    # Verify trust was extended to the worktree
    config_path = Path.home() / ".claude.json"
    config = json.loads(config_path.read_text())
    assert str(worktree_path.resolve()) in config["projects"]
    worktree_entry = config["projects"][str(worktree_path.resolve())]
    assert worktree_entry["hasTrustDialogAccepted"] is True
    assert worktree_entry["_mngCreated"] is True


def test_provision_does_not_extend_trust_for_non_worktree(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """provision should not extend trust when not using worktree mode."""
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mng_ctx)
    _init_git_with_gitignore(agent.work_dir)

    options = CreateAgentOptions(
        agent_type=AgentTypeName("claude"),
        git=AgentGitOptions(copy_mode=WorkDirCopyMode.COPY),
    )

    agent.provision(host=host, options=options, mng_ctx=temp_mng_ctx)

    # Trust should NOT have been extended since we're using COPY mode
    config_path = Path.home() / ".claude.json"
    assert not config_path.exists()


def test_provision_does_not_extend_trust_when_no_git_options(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """provision should not extend trust when git options are None."""
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mng_ctx)
    _init_git_with_gitignore(agent.work_dir)

    options = CreateAgentOptions(agent_type=AgentTypeName("claude"))

    agent.provision(host=host, options=options, mng_ctx=temp_mng_ctx)

    # Trust should NOT have been extended since no git options provided
    config_path = Path.home() / ".claude.json"
    assert not config_path.exists()


def test_provision_skips_trust_when_git_common_dir_is_none(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """provision should skip trust extension when find_git_common_dir returns None."""
    # Create agent with work_dir that is NOT a git repo
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mng_ctx)
    # Don't init git - work_dir is not a git repo

    agent.provision(host=host, options=_WORKTREE_OPTIONS, mng_ctx=temp_mng_ctx)

    # Trust should NOT have been extended since there's no git common dir
    config_path = Path.home() / ".claude.json"
    assert not config_path.exists()


def test_provision_trusts_working_directory_when_enabled(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """provision should add trust for work_dir when trust_working_directory is True."""
    config = ClaudeAgentConfig(check_installation=False, trust_working_directory=True)
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mng_ctx, agent_config=config)

    options = CreateAgentOptions(agent_type=AgentTypeName("claude"))

    agent.provision(host=host, options=options, mng_ctx=temp_mng_ctx)

    config_path = Path.home() / ".claude.json"
    claude_config = json.loads(config_path.read_text())
    assert str(agent.work_dir.resolve()) in claude_config["projects"]
    assert claude_config["projects"][str(agent.work_dir.resolve())]["hasTrustDialogAccepted"] is True
    assert claude_config["effortCalloutDismissed"] is True


def test_provision_does_not_trust_working_directory_when_disabled(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """provision should not add trust when trust_working_directory is False (default)."""
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mng_ctx)

    options = CreateAgentOptions(agent_type=AgentTypeName("claude"))

    agent.provision(host=host, options=options, mng_ctx=temp_mng_ctx)

    config_path = Path.home() / ".claude.json"
    assert not config_path.exists()


def test_trust_working_directory_defaults_to_false() -> None:
    """Verify that trust_working_directory defaults to False for ClaudeAgentConfig."""
    config = ClaudeAgentConfig()
    assert config.trust_working_directory is False


def test_on_before_provisioning_raises_for_worktree_on_remote_host(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """on_before_provisioning should raise PluginMngError for worktree mode on remote hosts."""
    agent, _ = make_claude_agent(local_provider, tmp_path, temp_mng_ctx)

    # Use SimpleNamespace to simulate a non-local host. Creating a real remote host
    # requires SSH infrastructure not available in unit tests. The method only reads
    # host.is_local before raising.
    non_local_host = cast(OnlineHostInterface, SimpleNamespace(is_local=False))

    options = CreateAgentOptions(
        agent_type=AgentTypeName("claude"),
        git=AgentGitOptions(copy_mode=WorkDirCopyMode.WORKTREE),
    )

    with pytest.raises(PluginMngError, match="Worktree mode is not supported on remote hosts"):
        agent.on_before_provisioning(host=non_local_host, options=options, mng_ctx=temp_mng_ctx)


def test_on_before_provisioning_validates_trust_for_worktree(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mng_ctx: MngContext,
    setup_git_config: None,
) -> None:
    """on_before_provisioning should validate source directory is trusted for worktree mode."""
    source_path, worktree_path, agent, host = _setup_worktree_agent(
        local_provider,
        tmp_path,
        temp_mng_ctx,
        is_source_trusted=True,
    )

    # Should succeed without error because the source directory is trusted
    agent.on_before_provisioning(host=host, options=_WORKTREE_OPTIONS, mng_ctx=temp_mng_ctx)


def test_on_before_provisioning_skips_dialog_check_when_interactive(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    interactive_mng_ctx: MngContext,
    setup_git_config: None,
) -> None:
    """on_before_provisioning should skip dialog check for interactive runs (provision() handles it)."""
    source_path, worktree_path, agent, host = _setup_worktree_agent(
        local_provider,
        tmp_path,
        interactive_mng_ctx,
    )

    # Should NOT raise even though dialogs are not dismissed -- interactive defers to provision()
    agent.on_before_provisioning(host=host, options=_WORKTREE_OPTIONS, mng_ctx=interactive_mng_ctx)


def test_on_before_provisioning_skips_trust_check_when_git_common_dir_is_none(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """on_before_provisioning should skip trust check when find_git_common_dir returns None."""
    # Create agent with work_dir that is NOT a git repo
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mng_ctx)

    # Should succeed without error because find_git_common_dir returns None
    agent.on_before_provisioning(host=host, options=_WORKTREE_OPTIONS, mng_ctx=temp_mng_ctx)


def test_on_destroy_removes_trust(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """on_destroy should remove the Claude trust entry for the agent's work_dir."""
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mng_ctx)

    # Write a mng-created trust entry for the agent's work_dir
    _write_mng_trust_entry(agent.work_dir)

    # Verify the entry exists before destroy
    config_path = Path.home() / ".claude.json"
    config_before = json.loads(config_path.read_text())
    assert str(agent.work_dir.resolve()) in config_before["projects"]

    agent.on_destroy(host)

    # Verify the trust entry was removed
    config_after = json.loads(config_path.read_text())
    assert str(agent.work_dir.resolve()) not in config_after.get("projects", {})


def test_provision_prompts_for_all_dialogs_when_interactive(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    interactive_mng_ctx: MngContext,
    setup_git_config: None,
) -> None:
    """provision should prompt for both trust and effort callout when neither is set."""
    source_path, worktree_path, agent, host = _setup_worktree_agent(
        local_provider,
        tmp_path,
        interactive_mng_ctx,
    )

    with (
        patch(
            "imbue.mng.agents.default_plugins.claude_agent._prompt_user_for_trust",
            return_value=True,
        ) as mock_trust_prompt,
        patch(
            "imbue.mng.agents.default_plugins.claude_agent._prompt_user_for_effort_callout_dismissal",
            return_value=True,
        ) as mock_effort_prompt,
    ):
        agent.provision(host=host, options=_WORKTREE_OPTIONS, mng_ctx=interactive_mng_ctx)

    # Verify both prompts fired
    mock_trust_prompt.assert_called_once_with(source_path)
    mock_effort_prompt.assert_called_once()

    # Verify both dialogs were resolved in the config
    config_path = Path.home() / ".claude.json"
    config = json.loads(config_path.read_text())
    assert str(source_path.resolve()) in config["projects"]
    assert str(worktree_path.resolve()) in config["projects"]
    worktree_entry = config["projects"][str(worktree_path.resolve())]
    assert worktree_entry["hasTrustDialogAccepted"] is True
    assert worktree_entry["_mngCreated"] is True
    assert config["effortCalloutDismissed"] is True


def test_provision_raises_when_non_interactive_and_untrusted(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mng_ctx: MngContext,
    setup_git_config: None,
) -> None:
    """provision should raise when non-interactive and source is untrusted."""
    source_path, worktree_path, agent, host = _setup_worktree_agent(
        local_provider,
        tmp_path,
        temp_mng_ctx,
    )

    with pytest.raises(ClaudeDirectoryNotTrustedError):
        agent.provision(host=host, options=_WORKTREE_OPTIONS, mng_ctx=temp_mng_ctx)


def test_provision_raises_when_user_declines_trust(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    interactive_mng_ctx: MngContext,
    setup_git_config: None,
) -> None:
    """provision should raise when user declines the trust prompt."""
    source_path, worktree_path, agent, host = _setup_worktree_agent(
        local_provider,
        tmp_path,
        interactive_mng_ctx,
    )

    with patch(
        "imbue.mng.agents.default_plugins.claude_agent._prompt_user_for_trust",
        return_value=False,
    ):
        with pytest.raises(ClaudeDirectoryNotTrustedError):
            agent.provision(host=host, options=_WORKTREE_OPTIONS, mng_ctx=interactive_mng_ctx)


# =============================================================================
# API Credential Check Tests
# =============================================================================

_DEFAULT_CREDENTIAL_CHECK_OPTIONS = CreateAgentOptions(agent_type=AgentTypeName("claude"))


@pytest.fixture()
def _no_api_key_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure ANTHROPIC_API_KEY is not in os.environ."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture()
def credential_check_host(local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext) -> Host:
    """Create a local host for credential check tests."""
    _, host = make_claude_agent(local_provider, tmp_path, temp_mng_ctx)
    return host


@pytest.fixture()
def credential_check_cg(temp_mng_ctx: MngContext) -> ConcurrencyGroup:
    """Provide the concurrency group for credential check tests."""
    return temp_mng_ctx.concurrency_group


@pytest.fixture()
def _local_credentials_file() -> None:
    """Create a ~/.claude/.credentials.json file for testing."""
    credentials_dir = Path.home() / ".claude"
    credentials_dir.mkdir(parents=True, exist_ok=True)
    (credentials_dir / ".credentials.json").write_text('{"token": "test"}')


def _make_non_local_host() -> OnlineHostInterface:
    """Create a simulated non-local host for credential check tests."""
    return cast(
        OnlineHostInterface,
        SimpleNamespace(is_local=False, get_env_var=lambda key: None),
    )


def test_has_api_credentials_detects_env_var_on_local_host(
    credential_check_host: Host, credential_check_cg: ConcurrencyGroup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_has_api_credentials_available returns True when ANTHROPIC_API_KEY is in os.environ on local host."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    config = ClaudeAgentConfig(check_installation=False)

    assert (
        _has_api_credentials_available(
            credential_check_host, _DEFAULT_CREDENTIAL_CHECK_OPTIONS, config, credential_check_cg
        )
        is True
    )


def test_has_api_credentials_ignores_env_var_on_remote_host(
    credential_check_cg: ConcurrencyGroup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_has_api_credentials_available ignores os.environ ANTHROPIC_API_KEY for remote hosts."""
    config = ClaudeAgentConfig(check_installation=False)

    # Set the key locally -- remote hosts should still return False because they don't inherit os.environ
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    assert (
        _has_api_credentials_available(
            _make_non_local_host(), _DEFAULT_CREDENTIAL_CHECK_OPTIONS, config, credential_check_cg
        )
        is False
    )


@pytest.mark.usefixtures("_no_api_key_in_env")
def test_has_api_credentials_detects_agent_env_var(
    credential_check_host: Host, credential_check_cg: ConcurrencyGroup
) -> None:
    """_has_api_credentials_available returns True when ANTHROPIC_API_KEY is in agent env vars."""
    config = ClaudeAgentConfig(check_installation=False)
    options = CreateAgentOptions(
        agent_type=AgentTypeName("claude"),
        environment=AgentEnvironmentOptions(
            env_vars=(EnvVar(key="ANTHROPIC_API_KEY", value="sk-test-key"),),
        ),
    )

    assert _has_api_credentials_available(credential_check_host, options, config, credential_check_cg) is True


@pytest.mark.usefixtures("_no_api_key_in_env")
def test_has_api_credentials_detects_host_env_var(
    credential_check_host: Host, credential_check_cg: ConcurrencyGroup
) -> None:
    """_has_api_credentials_available returns True when ANTHROPIC_API_KEY is in host env vars."""
    config = ClaudeAgentConfig(check_installation=False)
    credential_check_host.set_env_var("ANTHROPIC_API_KEY", "sk-test-key")

    assert (
        _has_api_credentials_available(
            credential_check_host, _DEFAULT_CREDENTIAL_CHECK_OPTIONS, config, credential_check_cg
        )
        is True
    )


@pytest.mark.usefixtures("_no_api_key_in_env", "_local_credentials_file")
def test_has_api_credentials_detects_credentials_file_local(
    credential_check_host: Host, credential_check_cg: ConcurrencyGroup
) -> None:
    """_has_api_credentials_available returns True when credentials file exists on local host."""
    config = ClaudeAgentConfig(check_installation=False)

    assert (
        _has_api_credentials_available(
            credential_check_host, _DEFAULT_CREDENTIAL_CHECK_OPTIONS, config, credential_check_cg
        )
        is True
    )


@pytest.mark.usefixtures("_no_api_key_in_env", "_local_credentials_file")
def test_has_api_credentials_detects_credentials_file_remote_with_sync(credential_check_cg: ConcurrencyGroup) -> None:
    """_has_api_credentials_available returns True when credentials file exists and sync is enabled for remote."""
    config = ClaudeAgentConfig(check_installation=False, sync_claude_credentials=True)

    assert (
        _has_api_credentials_available(
            _make_non_local_host(), _DEFAULT_CREDENTIAL_CHECK_OPTIONS, config, credential_check_cg
        )
        is True
    )


@pytest.mark.usefixtures("_no_api_key_in_env")
def test_has_api_credentials_returns_false_when_no_credentials(
    credential_check_host: Host, credential_check_cg: ConcurrencyGroup
) -> None:
    """_has_api_credentials_available returns False when no credential source is available."""
    config = ClaudeAgentConfig(check_installation=False)

    assert (
        _has_api_credentials_available(
            credential_check_host, _DEFAULT_CREDENTIAL_CHECK_OPTIONS, config, credential_check_cg
        )
        is False
    )


@pytest.mark.usefixtures("_no_api_key_in_env", "_local_credentials_file")
def test_has_api_credentials_returns_false_remote_no_sync(credential_check_cg: ConcurrencyGroup) -> None:
    """_has_api_credentials_available returns False for remote host when credentials exist but sync is disabled."""
    config = ClaudeAgentConfig(check_installation=False, sync_claude_credentials=False)

    assert (
        _has_api_credentials_available(
            _make_non_local_host(), _DEFAULT_CREDENTIAL_CHECK_OPTIONS, config, credential_check_cg
        )
        is False
    )


# =============================================================================
# primaryApiKey in ~/.claude.json Tests
# =============================================================================


def _write_claude_json_with_primary_api_key(api_key: str = "sk-ant-test-key") -> None:
    """Write ~/.claude.json with a primaryApiKey entry."""
    claude_json_path = Path.home() / ".claude.json"
    config = {"primaryApiKey": api_key}
    claude_json_path.write_text(json.dumps(config))


def test_claude_json_has_primary_api_key_returns_true_when_key_exists() -> None:
    """_claude_json_has_primary_api_key returns True when primaryApiKey is set."""
    _write_claude_json_with_primary_api_key()

    assert _claude_json_has_primary_api_key() is True


def test_claude_json_has_primary_api_key_returns_false_when_no_file() -> None:
    """_claude_json_has_primary_api_key returns False when ~/.claude.json does not exist."""
    assert _claude_json_has_primary_api_key() is False


def test_claude_json_has_primary_api_key_returns_false_when_key_missing() -> None:
    """_claude_json_has_primary_api_key returns False when primaryApiKey is not in the config."""
    claude_json_path = Path.home() / ".claude.json"
    claude_json_path.write_text(json.dumps({"projects": {}}))

    assert _claude_json_has_primary_api_key() is False


def test_claude_json_has_primary_api_key_returns_false_when_key_empty() -> None:
    """_claude_json_has_primary_api_key returns False when primaryApiKey is empty string."""
    claude_json_path = Path.home() / ".claude.json"
    claude_json_path.write_text(json.dumps({"primaryApiKey": ""}))

    assert _claude_json_has_primary_api_key() is False


def test_claude_json_has_primary_api_key_returns_false_when_invalid_json() -> None:
    """_claude_json_has_primary_api_key returns False when ~/.claude.json contains invalid JSON."""
    claude_json_path = Path.home() / ".claude.json"
    claude_json_path.write_text("not valid json {{{")

    assert _claude_json_has_primary_api_key() is False


@pytest.mark.usefixtures("_no_api_key_in_env")
def test_has_api_credentials_detects_primary_api_key_local(
    credential_check_host: Host, credential_check_cg: ConcurrencyGroup
) -> None:
    """_has_api_credentials_available returns True when primaryApiKey exists in ~/.claude.json on local host."""
    _write_claude_json_with_primary_api_key()
    config = ClaudeAgentConfig(check_installation=False)

    assert (
        _has_api_credentials_available(
            credential_check_host, _DEFAULT_CREDENTIAL_CHECK_OPTIONS, config, credential_check_cg
        )
        is True
    )


@pytest.mark.usefixtures("_no_api_key_in_env")
def test_has_api_credentials_detects_primary_api_key_remote_with_sync(credential_check_cg: ConcurrencyGroup) -> None:
    """_has_api_credentials_available returns True when primaryApiKey exists and sync_claude_json is enabled."""
    _write_claude_json_with_primary_api_key()
    config = ClaudeAgentConfig(check_installation=False, sync_claude_json=True)

    assert (
        _has_api_credentials_available(
            _make_non_local_host(), _DEFAULT_CREDENTIAL_CHECK_OPTIONS, config, credential_check_cg
        )
        is True
    )


@pytest.mark.usefixtures("_no_api_key_in_env")
def test_has_api_credentials_returns_false_primary_api_key_remote_no_sync(
    credential_check_cg: ConcurrencyGroup,
) -> None:
    """_has_api_credentials_available returns False when primaryApiKey exists but sync_claude_json is disabled."""
    _write_claude_json_with_primary_api_key()
    config = ClaudeAgentConfig(check_installation=False, sync_claude_json=False)

    assert (
        _has_api_credentials_available(
            _make_non_local_host(), _DEFAULT_CREDENTIAL_CHECK_OPTIONS, config, credential_check_cg
        )
        is False
    )


@pytest.mark.usefixtures("_no_api_key_in_env")
def test_on_before_provisioning_does_not_raise_when_no_credentials(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mng_ctx: MngContext,
) -> None:
    """on_before_provisioning should not raise when no API credentials are detected."""
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mng_ctx,
        agent_config=ClaudeAgentConfig(check_installation=True),
    )

    # Should complete without raising (logs a warning instead)
    agent.on_before_provisioning(host=host, options=_DEFAULT_CREDENTIAL_CHECK_OPTIONS, mng_ctx=temp_mng_ctx)


def test_on_before_provisioning_succeeds_with_credentials(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mng_ctx: MngContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """on_before_provisioning should succeed without warning when credentials are available."""
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mng_ctx,
        agent_config=ClaudeAgentConfig(check_installation=True),
    )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")

    agent.on_before_provisioning(host=host, options=_DEFAULT_CREDENTIAL_CHECK_OPTIONS, mng_ctx=temp_mng_ctx)


# =============================================================================
# Dialog Dismissal Tests
# =============================================================================


def _write_claude_trust_without_dialog_dismissed(source_path: Path) -> None:
    """Write ~/.claude.json with trust but WITHOUT effortCalloutDismissed."""
    config_path = Path.home() / ".claude.json"
    config = {
        "projects": {
            str(source_path.resolve()): {
                "hasTrustDialogAccepted": True,
                "allowedTools": [],
            }
        },
    }
    config_path.write_text(json.dumps(config))


def test_on_before_provisioning_raises_when_dialogs_not_dismissed(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mng_ctx: MngContext,
    setup_git_config: None,
) -> None:
    """on_before_provisioning should raise when effortCalloutDismissed is not set."""
    source_path, worktree_path, agent, host = _setup_worktree_agent(
        local_provider,
        tmp_path,
        temp_mng_ctx,
    )

    # Write trust but without effortCalloutDismissed
    _write_claude_trust_without_dialog_dismissed(source_path)

    with pytest.raises(ClaudeEffortCalloutNotDismissedError):
        agent.on_before_provisioning(host=host, options=_WORKTREE_OPTIONS, mng_ctx=temp_mng_ctx)


def test_provision_dismisses_dialogs_when_auto_approve(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_config: MngConfig,
    temp_profile_dir: Path,
    plugin_manager: "pluggy.PluginManager",
    setup_git_config: None,
) -> None:
    """provision should auto-dismiss dialogs when auto_approve is enabled."""
    with ConcurrencyGroup(name="test-auto-approve-dialogs") as cg:
        auto_approve_ctx = make_mng_ctx(
            temp_config, plugin_manager, temp_profile_dir, is_auto_approve=True, concurrency_group=cg
        )
        source_path, worktree_path, agent, host = _setup_worktree_agent(
            local_provider,
            tmp_path,
            auto_approve_ctx,
        )

        # Write trust but without effortCalloutDismissed
        _write_claude_trust_without_dialog_dismissed(source_path)

        agent.provision(host=host, options=_WORKTREE_OPTIONS, mng_ctx=auto_approve_ctx)

        # Verify effortCalloutDismissed was set
        config_path = Path.home() / ".claude.json"
        config = json.loads(config_path.read_text())
        assert config["effortCalloutDismissed"] is True


def test_provision_prompts_for_dialog_dismissal_when_interactive(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    interactive_mng_ctx: MngContext,
    setup_git_config: None,
) -> None:
    """provision should prompt and dismiss dialogs when interactive and not yet dismissed."""
    source_path, worktree_path, agent, host = _setup_worktree_agent(
        local_provider,
        tmp_path,
        interactive_mng_ctx,
    )

    # Write trust but without effortCalloutDismissed
    _write_claude_trust_without_dialog_dismissed(source_path)

    with patch(
        "imbue.mng.agents.default_plugins.claude_agent._prompt_user_for_effort_callout_dismissal",
        return_value=True,
    ) as mock_prompt:
        agent.provision(host=host, options=_WORKTREE_OPTIONS, mng_ctx=interactive_mng_ctx)

    # Verify user was prompted
    mock_prompt.assert_called_once()

    # Verify effortCalloutDismissed was set
    config_path = Path.home() / ".claude.json"
    config = json.loads(config_path.read_text())
    assert config["effortCalloutDismissed"] is True


def test_provision_raises_when_user_declines_dialog_dismissal(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    interactive_mng_ctx: MngContext,
    setup_git_config: None,
) -> None:
    """provision should raise when user declines dialog dismissal prompt."""
    source_path, worktree_path, agent, host = _setup_worktree_agent(
        local_provider,
        tmp_path,
        interactive_mng_ctx,
    )

    # Write trust but without effortCalloutDismissed
    _write_claude_trust_without_dialog_dismissed(source_path)

    with patch(
        "imbue.mng.agents.default_plugins.claude_agent._prompt_user_for_effort_callout_dismissal",
        return_value=False,
    ):
        with pytest.raises(ClaudeEffortCalloutNotDismissedError):
            agent.provision(host=host, options=_WORKTREE_OPTIONS, mng_ctx=interactive_mng_ctx)


def test_provision_raises_when_non_interactive_and_dialogs_not_dismissed(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mng_ctx: MngContext,
    setup_git_config: None,
) -> None:
    """provision should raise when non-interactive and dialogs are not dismissed."""
    source_path, worktree_path, agent, host = _setup_worktree_agent(
        local_provider,
        tmp_path,
        temp_mng_ctx,
    )

    # Write trust but without effortCalloutDismissed
    _write_claude_trust_without_dialog_dismissed(source_path)

    with pytest.raises(ClaudeEffortCalloutNotDismissedError):
        agent.provision(host=host, options=_WORKTREE_OPTIONS, mng_ctx=temp_mng_ctx)


# =============================================================================
# Remote Trust Tests
# =============================================================================


def test_provision_adds_trust_for_remote_work_dir(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provision should add hasTrustDialogAccepted for work_dir in the claude.json synced to remote hosts."""
    monkeypatch.chdir(tmp_path)

    agent, _ = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mng_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, sync_claude_json=True),
        work_dir=temp_work_dir,
    )

    _write_claude_trust(temp_work_dir)

    host = cast(OnlineHostInterface, FakeHost(is_local=False, host_dir=tmp_path / "host_dir"))
    agent.provision(host=host, options=CreateAgentOptions(agent_type=AgentTypeName("claude")), mng_ctx=temp_mng_ctx)

    transferred_config = json.loads((tmp_path / ".claude.json").read_text())
    assert transferred_config["projects"][str(temp_work_dir)]["hasTrustDialogAccepted"] is True


def test_provision_preserves_existing_remote_project_config(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provision should preserve existing project config when adding trust for remote work_dir."""
    monkeypatch.chdir(tmp_path)

    agent, _ = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mng_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, sync_claude_json=True),
        work_dir=temp_work_dir,
    )

    # Write trust with extra fields that should be preserved
    _write_claude_trust(temp_work_dir)

    host = cast(OnlineHostInterface, FakeHost(is_local=False, host_dir=tmp_path / "host_dir"))
    agent.provision(host=host, options=CreateAgentOptions(agent_type=AgentTypeName("claude")), mng_ctx=temp_mng_ctx)

    transferred_config = json.loads((tmp_path / ".claude.json").read_text())
    project_entry = transferred_config["projects"][str(temp_work_dir)]
    assert project_entry["hasTrustDialogAccepted"] is True
    # Existing fields from _write_claude_trust should be preserved
    assert project_entry["allowedTools"] == []


# =============================================================================
# macOS Keychain Credential Tests
# =============================================================================


def _make_mock_cg_with_result(result: FinishedProcess | Exception) -> ConcurrencyGroup:
    """Create a mock ConcurrencyGroup that returns the given result from run_process_to_completion."""

    def _run(*args: object, **kwargs: object) -> FinishedProcess:
        if isinstance(result, Exception):
            raise result
        return result

    return cast(ConcurrencyGroup, SimpleNamespace(run_process_to_completion=_run))


def test_read_macos_keychain_credential_returns_value_on_success() -> None:
    """_read_macos_keychain_credential returns the stripped stdout on success."""
    mock_cg = _make_mock_cg_with_result(
        FinishedProcess(
            command=("security",),
            returncode=0,
            stdout="test-credential-value\n",
            stderr="",
            is_output_already_logged=False,
        )
    )

    result = _read_macos_keychain_credential("some-label", mock_cg)

    assert result == "test-credential-value"


def test_read_macos_keychain_credential_returns_none_on_nonzero_exit() -> None:
    """_read_macos_keychain_credential returns None when security returns non-zero exit code."""
    mock_cg = _make_mock_cg_with_result(
        FinishedProcess(
            command=("security",), returncode=44, stdout="", stderr="not found", is_output_already_logged=False
        )
    )

    result = _read_macos_keychain_credential("nonexistent-label", mock_cg)

    assert result is None


def test_read_macos_keychain_credential_returns_none_on_process_setup_error() -> None:
    """_read_macos_keychain_credential returns None when security binary is not found."""
    mock_cg = _make_mock_cg_with_result(
        ProcessSetupError(command=("security",), stdout="", stderr="", is_output_already_logged=False)
    )

    result = _read_macos_keychain_credential("some-label", mock_cg)

    assert result is None


@pytest.mark.usefixtures("_no_api_key_in_env", "_local_credentials_file")
def test_has_api_credentials_detects_credentials_file_on_local(
    credential_check_host: Host,
    credential_check_cg: ConcurrencyGroup,
) -> None:
    """_has_api_credentials_available returns True on local host when credentials file exists."""
    config = ClaudeAgentConfig(check_installation=False)

    assert (
        _has_api_credentials_available(
            credential_check_host, _DEFAULT_CREDENTIAL_CHECK_OPTIONS, config, credential_check_cg
        )
        is True
    )


@pytest.mark.usefixtures("_no_api_key_in_env", "_local_credentials_file")
def test_has_api_credentials_detects_credentials_file_on_remote_with_sync_enabled(
    credential_check_cg: ConcurrencyGroup,
) -> None:
    """_has_api_credentials_available returns True on remote host when credentials file exists and sync is enabled."""
    config = ClaudeAgentConfig(check_installation=False, sync_claude_credentials=True)

    assert (
        _has_api_credentials_available(
            _make_non_local_host(), _DEFAULT_CREDENTIAL_CHECK_OPTIONS, config, credential_check_cg
        )
        is True
    )


@pytest.mark.usefixtures("_no_api_key_in_env", "_local_credentials_file")
def test_has_api_credentials_ignores_credentials_file_on_remote_with_sync_disabled(
    credential_check_cg: ConcurrencyGroup,
) -> None:
    """_has_api_credentials_available returns False on remote host when sync is disabled even with credentials file."""
    config = ClaudeAgentConfig(
        check_installation=False,
        sync_claude_credentials=False,
        sync_claude_json=False,
    )

    assert (
        _has_api_credentials_available(
            _make_non_local_host(), _DEFAULT_CREDENTIAL_CHECK_OPTIONS, config, credential_check_cg
        )
        is False
    )


# =============================================================================
# get_files_for_deploy Tests
# =============================================================================


def test_get_files_for_deploy_returns_generated_defaults_when_no_claude_files(
    temp_mng_ctx: MngContext, tmp_path: Path
) -> None:
    """get_files_for_deploy returns generated defaults when no local claude config files exist."""
    # Exclude project settings since the test repo_root may contain .claude/ files
    result = get_files_for_deploy(
        mng_ctx=temp_mng_ctx, include_user_settings=True, include_project_settings=False, repo_root=tmp_path
    )

    # Always ships generated defaults for settings.json and claude.json
    assert Path("~/.claude/settings.json") in result
    assert Path("~/.claude.json") in result
    settings_content = result[Path("~/.claude/settings.json")]
    assert isinstance(settings_content, str)
    settings_data = json.loads(settings_content)
    assert settings_data["skipDangerousModePermissionPrompt"] is True
    claude_json_content = result[Path("~/.claude.json")]
    assert isinstance(claude_json_content, str)
    claude_json_data = json.loads(claude_json_content)
    assert claude_json_data["hasCompletedOnboarding"] is True


def test_get_files_for_deploy_includes_claude_json(temp_mng_ctx: MngContext, tmp_path: Path) -> None:
    """get_files_for_deploy always includes ~/.claude.json with generated defaults (not local content).

    The deploy uses generated defaults with a fixed timestamp for better Docker
    layer caching, rather than syncing the user's local ~/.claude.json content.
    """
    claude_json = Path.home() / ".claude.json"
    claude_json.write_text('{"test": true}')

    result = get_files_for_deploy(
        mng_ctx=temp_mng_ctx, include_user_settings=True, include_project_settings=False, repo_root=tmp_path
    )

    assert Path("~/.claude.json") in result
    claude_json_content = result[Path("~/.claude.json")]
    assert isinstance(claude_json_content, str)
    claude_json_data = json.loads(claude_json_content)
    # Local content is NOT preserved (generated defaults used for caching)
    assert "test" not in claude_json_data
    # Dialog-suppression fields are always present in the generated defaults
    assert claude_json_data["bypassPermissionsModeAccepted"] is True
    assert claude_json_data["effortCalloutDismissed"] is True


def test_get_files_for_deploy_includes_claude_settings(temp_mng_ctx: MngContext, tmp_path: Path) -> None:
    """get_files_for_deploy includes ~/.claude/settings.json with skipDangerousModePermissionPrompt when it exists."""
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings = claude_dir / "settings.json"
    settings.write_text('{"settings": true}')

    result = get_files_for_deploy(
        mng_ctx=temp_mng_ctx, include_user_settings=True, include_project_settings=False, repo_root=tmp_path
    )

    assert Path("~/.claude/settings.json") in result
    settings_content = result[Path("~/.claude/settings.json")]
    assert isinstance(settings_content, str)
    settings_data = json.loads(settings_content)
    assert settings_data["settings"] is True
    assert settings_data["skipDangerousModePermissionPrompt"] is True


def test_get_files_for_deploy_includes_claude_json_and_settings(temp_mng_ctx: MngContext, tmp_path: Path) -> None:
    """get_files_for_deploy includes both claude.json and settings.json when both exist."""
    claude_json = Path.home() / ".claude.json"
    claude_json.write_text('{"test": true}')

    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings = claude_dir / "settings.json"
    settings.write_text('{"settings": true}')

    # Exclude project settings to avoid picking up .claude/*.local.* from the repo_root
    result = get_files_for_deploy(
        mng_ctx=temp_mng_ctx, include_user_settings=True, include_project_settings=False, repo_root=tmp_path
    )

    assert Path("~/.claude.json") in result
    assert Path("~/.claude/settings.json") in result


def test_get_files_for_deploy_ships_defaults_when_user_settings_excluded(
    temp_mng_ctx: MngContext,
    tmp_path: Path,
) -> None:
    """get_files_for_deploy ships generated defaults even when include_user_settings is False."""
    claude_json = Path.home() / ".claude.json"
    claude_json.write_text('{"test": true}')

    result = get_files_for_deploy(
        mng_ctx=temp_mng_ctx, include_user_settings=False, include_project_settings=True, repo_root=tmp_path
    )

    # Generated defaults are always shipped
    assert Path("~/.claude/settings.json") in result
    assert Path("~/.claude.json") in result
    # But the local ~/.claude.json should NOT be used (generated defaults instead)
    claude_json_content = result[Path("~/.claude.json")]
    assert isinstance(claude_json_content, str)
    claude_json_data = json.loads(claude_json_content)
    assert claude_json_data.get("test") is None
    assert claude_json_data["hasCompletedOnboarding"] is True


def test_get_files_for_deploy_includes_project_local_settings(
    temp_mng_ctx: MngContext,
    tmp_path: Path,
) -> None:
    """get_files_for_deploy includes .claude/settings.local.json from the repo root."""
    project_claude_dir = tmp_path / ".claude"
    project_claude_dir.mkdir(parents=True, exist_ok=True)
    local_settings = project_claude_dir / "settings.local.json"
    local_settings.write_text('{"local": true}')

    result = get_files_for_deploy(
        mng_ctx=temp_mng_ctx, include_user_settings=False, include_project_settings=True, repo_root=tmp_path
    )

    assert Path(".claude/settings.local.json") in result
    assert result[Path(".claude/settings.local.json")] == local_settings


def test_get_files_for_deploy_excludes_project_settings_when_flag_false(
    temp_mng_ctx: MngContext,
    tmp_path: Path,
) -> None:
    """get_files_for_deploy skips project local files when include_project_settings is False, but always ships defaults."""
    project_claude_dir = tmp_path / ".claude"
    project_claude_dir.mkdir(parents=True, exist_ok=True)
    local_settings = project_claude_dir / "settings.local.json"
    local_settings.write_text('{"local": true}')

    result = get_files_for_deploy(
        mng_ctx=temp_mng_ctx, include_user_settings=False, include_project_settings=False, repo_root=tmp_path
    )

    # Generated defaults are always shipped
    assert Path("~/.claude/settings.json") in result
    assert Path("~/.claude.json") in result
    # But project local files should NOT be included
    assert Path(".claude/settings.local.json") not in result


def test_get_files_for_deploy_includes_skills_directory(temp_mng_ctx: MngContext, tmp_path: Path) -> None:
    """get_files_for_deploy includes files from ~/.claude/skills/ recursively."""
    claude_dir = Path.home() / ".claude"
    skills_dir = claude_dir / "skills" / "my-skill"
    skills_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skills_dir / "SKILL.md"
    skill_file.write_text("# My Skill")

    result = get_files_for_deploy(
        mng_ctx=temp_mng_ctx, include_user_settings=True, include_project_settings=False, repo_root=tmp_path
    )

    assert Path("~/.claude/skills/my-skill/SKILL.md") in result
    assert result[Path("~/.claude/skills/my-skill/SKILL.md")] == skill_file


def test_get_files_for_deploy_includes_commands_directory(temp_mng_ctx: MngContext, tmp_path: Path) -> None:
    """get_files_for_deploy includes files from ~/.claude/commands/ recursively."""
    claude_dir = Path.home() / ".claude"
    commands_dir = claude_dir / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    cmd_file = commands_dir / "my-command.md"
    cmd_file.write_text("# Command")

    result = get_files_for_deploy(
        mng_ctx=temp_mng_ctx, include_user_settings=True, include_project_settings=False, repo_root=tmp_path
    )

    assert Path("~/.claude/commands/my-command.md") in result
    assert result[Path("~/.claude/commands/my-command.md")] == cmd_file


def test_get_files_for_deploy_includes_agents_directory(temp_mng_ctx: MngContext, tmp_path: Path) -> None:
    """get_files_for_deploy includes files from ~/.claude/agents/ recursively."""
    claude_dir = Path.home() / ".claude"
    agents_dir = claude_dir / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    agent_file = agents_dir / "my-agent.json"
    agent_file.write_text('{"agent": true}')

    result = get_files_for_deploy(
        mng_ctx=temp_mng_ctx, include_user_settings=True, include_project_settings=False, repo_root=tmp_path
    )

    assert Path("~/.claude/agents/my-agent.json") in result
    assert result[Path("~/.claude/agents/my-agent.json")] == agent_file


def test_get_files_for_deploy_includes_credentials(temp_mng_ctx: MngContext, tmp_path: Path) -> None:
    """get_files_for_deploy includes ~/.claude/.credentials.json when it exists."""
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    credentials = claude_dir / ".credentials.json"
    credentials.write_text('{"oauth_token": "test"}')

    result = get_files_for_deploy(
        mng_ctx=temp_mng_ctx, include_user_settings=True, include_project_settings=False, repo_root=tmp_path
    )

    assert Path("~/.claude/.credentials.json") in result
    assert result[Path("~/.claude/.credentials.json")] == credentials


# =============================================================================
# Version Pinning Tests
# =============================================================================


def test_claude_agent_config_version_defaults_to_none() -> None:
    """ClaudeAgentConfig.version should default to None."""
    config = ClaudeAgentConfig()
    assert config.version is None


def test_claude_agent_config_version_can_be_set() -> None:
    """ClaudeAgentConfig.version should accept a version string."""
    config = ClaudeAgentConfig(version="2.1.50")
    assert config.version == "2.1.50"


def test_parse_claude_version_output_normal() -> None:
    """_parse_claude_version_output should extract the version from standard output."""
    assert _parse_claude_version_output("2.1.50 (Claude Code)") == "2.1.50"


def test_parse_claude_version_output_version_only() -> None:
    """_parse_claude_version_output should handle version-only output."""
    assert _parse_claude_version_output("2.1.50") == "2.1.50"


def test_parse_claude_version_output_with_whitespace() -> None:
    """_parse_claude_version_output should handle leading/trailing whitespace."""
    assert _parse_claude_version_output("  2.1.50 (Claude Code)\n") == "2.1.50"


def test_parse_claude_version_output_empty() -> None:
    """_parse_claude_version_output should return None for empty output."""
    assert _parse_claude_version_output("") is None
    assert _parse_claude_version_output("   ") is None


def test_build_install_command_hint_no_version() -> None:
    """_build_install_command_hint should return standard install command without version."""
    assert _build_install_command_hint() == "curl -fsSL https://claude.ai/install.sh | bash"
    assert _build_install_command_hint(None) == "curl -fsSL https://claude.ai/install.sh | bash"


def test_build_install_command_hint_with_version() -> None:
    """_build_install_command_hint should include version in install command."""
    assert _build_install_command_hint("2.1.50") == "curl -fsSL https://claude.ai/install.sh | bash -s 2.1.50"


def _make_command_tracking_host() -> tuple[OnlineHostInterface, list[str]]:
    """Create a mock host that tracks executed commands.

    Returns (host, executed_commands) where executed_commands is a list that
    accumulates command strings passed to execute_command.
    """
    executed_commands: list[str] = []

    def mock_execute_command(cmd: str, *args: object, **kwargs: object) -> SimpleNamespace:
        executed_commands.append(cmd)
        return SimpleNamespace(success=True, stdout="", stderr="")

    host = cast(
        OnlineHostInterface,
        SimpleNamespace(
            execute_command=mock_execute_command,
        ),
    )
    return host, executed_commands


def test_get_claude_version_returns_version_on_success() -> None:
    """_get_claude_version should return the version string when claude --version succeeds."""
    host = cast(
        OnlineHostInterface,
        SimpleNamespace(
            execute_command=lambda cmd, *args, **kwargs: SimpleNamespace(
                success=True,
                stdout="2.1.50 (Claude Code)\n",
                stderr="",
            ),
        ),
    )

    assert _get_claude_version(host) == "2.1.50"


def test_get_claude_version_returns_none_on_failure() -> None:
    """_get_claude_version should return None when claude --version fails."""
    host = cast(
        OnlineHostInterface,
        SimpleNamespace(
            execute_command=lambda cmd, *args, **kwargs: SimpleNamespace(
                success=False,
                stdout="",
                stderr="command not found",
            ),
        ),
    )

    assert _get_claude_version(host) is None


def test_provision_raises_on_version_mismatch(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_host_dir: Path,
    temp_profile_dir: Path,
    plugin_manager: "pluggy.PluginManager",
    mng_test_prefix: str,
) -> None:
    """provision should raise when installed claude version does not match pinned version."""
    config = MngConfig(
        prefix=mng_test_prefix,
        default_host_dir=temp_host_dir,
    )
    with ConcurrencyGroup(name="test-version-mismatch") as cg:
        ctx = make_mng_ctx(config, plugin_manager, temp_profile_dir, concurrency_group=cg)
        agent, _ = make_claude_agent(
            local_provider,
            tmp_path,
            ctx,
            agent_config=ClaudeAgentConfig(check_installation=True, version="99.99.99"),
        )

        # Simulate a host where claude is installed but at a different version.
        host_with_wrong_version = cast(
            OnlineHostInterface,
            SimpleNamespace(
                is_local=True,
                execute_command=lambda cmd, *args, **kwargs: SimpleNamespace(
                    success=True,
                    stdout="2.1.50 (Claude Code)\n",
                    stderr="",
                ),
            ),
        )

        options = CreateAgentOptions(agent_type=AgentTypeName("claude"))

        with pytest.raises(PluginMngError, match="Claude version mismatch"):
            agent.provision(host=host_with_wrong_version, options=options, mng_ctx=ctx)


def test_install_claude_passes_version_to_command() -> None:
    """_install_claude should pass the version to the install script via bash -s."""
    host, executed_commands = _make_command_tracking_host()

    _install_claude(host, version="2.1.50")

    assert len(executed_commands) == 1
    assert "bash -s 2.1.50" in executed_commands[0]


def test_install_claude_without_version() -> None:
    """_install_claude should not pass -s flag when no version is specified."""
    host, executed_commands = _make_command_tracking_host()

    _install_claude(host, version=None)

    assert len(executed_commands) == 1
    assert "bash -s" not in executed_commands[0]
    assert "install.sh | bash" in executed_commands[0]
