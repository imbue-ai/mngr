import json
import os
import shlex
import shutil
import subprocess
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch
from uuid import UUID

import pluggy
import pytest
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessSetupError
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.imbue_common.model_update import to_update
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.update_policy import AgentUpdatePolicy
from imbue.mngr.api.preservation import get_local_preserved_agent_dir
from imbue.mngr.api.preservation import preserve_agent_data
from imbue.mngr.api.testing import FakeHost
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import EnvVar
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import split_cli_args_string
from imbue.mngr.config.overlay_merge import merge_models_via_overlay
from imbue.mngr.errors import AgentInstallationError
from imbue.mngr.errors import AgentStartError
from imbue.mngr.errors import ConfigError
from imbue.mngr.errors import ConfigParseError
from imbue.mngr.errors import NoCommandDefinedError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.hosts.offline_host import OfflineHostWithVolume
from imbue.mngr.hosts.offline_host import make_readable_offline_host
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.host import AgentEnvironmentOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.interfaces.host import NewHostOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.plugins.hookspecs import OnBeforeCreateArgs
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import TransferMode
from imbue.mngr.primitives import WaitingReason
from imbue.mngr.providers.docker.host_store import HostRecord
from imbue.mngr.providers.docker.instance import DockerProviderInstance
from imbue.mngr.providers.docker.testing import make_docker_provider_with_local_volume
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.testing import capture_loguru
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import make_mngr_ctx
from imbue.mngr_claude.claude_config import ClaudeDirectoryNotTrustedError
from imbue.mngr_claude.claude_config import ClaudeEffortCalloutNotDismissedError
from imbue.mngr_claude.claude_config import build_credential_sync_hooks_config
from imbue.mngr_claude.claude_config import build_readiness_hooks_config
from imbue.mngr_claude.claude_config import encode_claude_project_dir_name
from imbue.mngr_claude.claude_config import get_managed_settings_path
from imbue.mngr_claude.plugin import CLAUDE_INSTALL_PATH
from imbue.mngr_claude.plugin import ClaudeAgent
from imbue.mngr_claude.plugin import ClaudeAgentConfig
from imbue.mngr_claude.plugin import CostThresholdDialogIndicator
from imbue.mngr_claude.plugin import MANAGED_SETTINGS_LAUNCH_ARG
from imbue.mngr_claude.plugin import ProvisioningContext
from imbue.mngr_claude.plugin import _build_claude_install_command
from imbue.mngr_claude.plugin import _build_install_command_hint
from imbue.mngr_claude.plugin import _build_settings_json
from imbue.mngr_claude.plugin import _claude_json_has_primary_api_key
from imbue.mngr_claude.plugin import _claude_preserved_items
from imbue.mngr_claude.plugin import _compute_keychain_label_suffix
from imbue.mngr_claude.plugin import _generate_claude_json
from imbue.mngr_claude.plugin import _generate_installed_plugins_content
from imbue.mngr_claude.plugin import _generate_known_marketplaces_content
from imbue.mngr_claude.plugin import _get_claude_version
from imbue.mngr_claude.plugin import _has_api_credentials_available
from imbue.mngr_claude.plugin import _install_claude
from imbue.mngr_claude.plugin import _is_using_claude_oauth_subscription
from imbue.mngr_claude.plugin import _parse_claude_version_output
from imbue.mngr_claude.plugin import _provision_local_credentials
from imbue.mngr_claude.plugin import _read_macos_keychain_credential
from imbue.mngr_claude.plugin import _rewrite_installed_plugins_paths
from imbue.mngr_claude.plugin import _rewrite_known_marketplaces_paths
from imbue.mngr_claude.plugin import _should_preserve_sessions
from imbue.mngr_claude.plugin import _sync_user_resources
from imbue.mngr_claude.plugin import _write_generated_files
from imbue.mngr_claude.plugin import agent_field_generators
from imbue.mngr_claude.plugin import approve_api_key_for_claude
from imbue.mngr_claude.plugin import compute_claude_json_flags
from imbue.mngr_claude.plugin import compute_settings_json_flags
from imbue.mngr_claude.plugin import get_files_for_deploy
from imbue.mngr_claude.plugin import on_before_create
from imbue.mngr_claude.plugin import on_before_host_destroy
from imbue.mngr_claude.plugin import should_trust_work_dir
from imbue.overlay.markers import StaticList

# =============================================================================
# Test Helpers
# =============================================================================


def make_claude_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    mngr_ctx: MngrContext,
    agent_config: ClaudeAgentConfig | None = None,
    agent_type: AgentTypeName | None = None,
    work_dir: Path | None = None,
) -> tuple[ClaudeAgent, Host]:
    """Create a ClaudeAgent with a real local host for testing."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)
    if work_dir is None:
        work_dir = tmp_path / f"work-{str(AgentId.generate().get_uuid())[:8]}"
        work_dir.mkdir()

    if agent_config is None:
        agent_config = ClaudeAgentConfig(check_installation=False, preserve_sessions_on_destroy=False)
    if agent_type is None:
        agent_type = AgentTypeName("claude")

    agent = ClaudeAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=agent_type,
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=mngr_ctx,
        agent_config=agent_config,
        host=host,
    )
    return agent, host


def _sid_export_for(uuid: UUID) -> str:
    """Build the expected MAIN_CLAUDE_SESSION_ID export string for a given agent UUID."""
    return (
        f'_MNGR_READ_SID=$(cat "$MNGR_AGENT_STATE_DIR/claude_session_id" 2>/dev/null || true);'
        f' export MAIN_CLAUDE_SESSION_ID="${{_MNGR_READ_SID:-{uuid}}}"'
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

    # Add .gitignore (needed by _configure_agent_hooks in provision)
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


_ALL_DIALOGS_DISMISSED = {
    "effortCalloutDismissed": True,
    "hasCompletedOnboarding": True,
    "bypassPermissionsModeAccepted": True,
    "hasAcknowledgedCostThreshold": True,
}


def _write_claude_trust(source_path: Path) -> None:
    """Write ~/.claude.json with trust entry for source_path and all dialogs dismissed."""
    config_path = Path.home() / ".claude.json"
    config = {
        **_ALL_DIALOGS_DISMISSED,
        "projects": {
            str(source_path.resolve()): {
                "hasTrustDialogAccepted": True,
                "allowedTools": [],
            }
        },
    }
    config_path.write_text(json.dumps(config))


def _write_dialogs_dismissed_at(config_path: Path, trust_path: Path) -> None:
    """Write a .claude.json at ``config_path`` with all dialogs dismissed and trust for ``trust_path``.

    Used for shared-mode tests where mngr dismisses dialogs against the config file the
    agent's claude reads ($CLAUDE_CONFIG_DIR/.claude.json), which is not ~/.claude.json.
    """
    config = {
        **_ALL_DIALOGS_DISMISSED,
        "projects": {str(trust_path.resolve()): {"hasTrustDialogAccepted": True}},
    }
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config))


def _write_mngr_trust_entry(path: Path) -> None:
    """Write ~/.claude.json with a mngr-created trust entry for path and all dialogs dismissed."""
    config_path = Path.home() / ".claude.json"
    config = {
        **_ALL_DIALOGS_DISMISSED,
        "projects": {
            str(path.resolve()): {
                "hasTrustDialogAccepted": True,
                "allowedTools": [],
                "_mngrCreated": True,
                "_mngrSourcePath": "/some/source",
            }
        },
    }
    config_path.write_text(json.dumps(config))


def _write_all_dialogs_dismissed(work_dir: Path) -> None:
    """Write ~/.claude.json with all dialogs dismissed and trust for work_dir."""
    config_path = Path.home() / ".claude.json"
    config = {
        **_ALL_DIALOGS_DISMISSED,
        "projects": {
            str(work_dir.resolve()): {
                "hasTrustDialogAccepted": True,
            }
        },
    }
    config_path.write_text(json.dumps(config))


_CLAUDE_AGENT_MODULE = "imbue.mngr_claude.plugin"


@contextmanager
def _mock_all_dialog_prompts(
    trust_accepted: bool = True,
    effort_accepted: bool = True,
    onboarding_accepted: bool = True,
):
    """Mock all interactive dialog prompts with the given return values.

    Yields a dict of mock names to mock objects for assertion.
    """
    with (
        patch(f"{_CLAUDE_AGENT_MODULE}._prompt_user_for_trust", return_value=trust_accepted) as mock_trust,
        patch(
            f"{_CLAUDE_AGENT_MODULE}._prompt_user_for_effort_callout_dismissal", return_value=effort_accepted
        ) as mock_effort,
        patch(
            f"{_CLAUDE_AGENT_MODULE}._prompt_user_for_onboarding_completion", return_value=onboarding_accepted
        ) as mock_onboarding,
    ):
        yield {"trust": mock_trust, "effort": mock_effort, "onboarding": mock_onboarding}


_WORKTREE_OPTIONS = CreateAgentOptions(
    agent_type=AgentTypeName("claude"),
    transfer_mode=TransferMode.GIT_WORKTREE,
)


def _setup_worktree_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    mngr_ctx: MngrContext,
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
    agent, host = make_claude_agent(local_provider, tmp_path, mngr_ctx, work_dir=worktree_path)
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

    merged, _ = merge_models_via_overlay(base, override)

    assert merged.command == CommandString("custom-claude")


def test_claude_agent_config_merge_replaces_cli_args() -> None:
    """ClaudeAgentConfig assigns cli_args from override (no concat under assign-by-default)."""
    base = ClaudeAgentConfig(cli_args=("--verbose",))
    override = ClaudeAgentConfig(cli_args=("--model", "sonnet"))

    merged, _ = merge_models_via_overlay(base, override)

    assert merged.cli_args == ("--model", "sonnet")


def test_claude_agent_config_merge_uses_override_cli_args_when_base_empty() -> None:
    """ClaudeAgentConfig merge should use override cli_args when base is empty."""
    base = ClaudeAgentConfig()
    override = ClaudeAgentConfig(cli_args=("--verbose",))

    merged, _ = merge_models_via_overlay(base, override)

    assert merged.cli_args == ("--verbose",)


# =============================================================================
# assemble_command Tests
# =============================================================================


class _ParsedAssembleCommand:
    """Structural view of an assembled claude command, parsed via shlex.

    The assembled command has the shape (normal mode, no mngr --settings)::

        <bg> <exports> && rm -rf .../session_started
            && ( ( find ... | grep . ) && <base> --resume "$SID" <args> )
            || <base> --session-id <uuid> <args>

    In use_env_config_dir mode, mngr injects its own ``--settings <path>``
    immediately after ``<base>`` in both branches.

    shlex.split tokenizes shell operators (``&&``, ``||``) as their own tokens,
    so we split on the single top-level ``||`` to separate the resume branch from
    the create branch, then read the base command, the (optional) managed-settings
    path, and the trailing args from each. ``mngr_injects_settings`` selects the
    layout; in normal mode ``resume_settings`` / ``create_settings`` are None.
    """

    def __init__(self, command: str, mngr_injects_settings: bool = False) -> None:
        self.tokens = shlex.split(command)
        # The resume/create split is the LAST top-level `||`. (An earlier `||`
        # appears inside the SID export's `... 2>/dev/null || true` fallback, so
        # splitting on the first `||` would be wrong.)
        split_idx = len(self.tokens) - 1 - self.tokens[::-1].index("||")
        resume_tokens = self.tokens[:split_idx]
        create_tokens = self.tokens[split_idx + 1 :]

        # Resume branch: <base> [--settings <path>] --resume $MAIN_CLAUDE_SESSION_ID <args...>.
        # With mngr --settings, the launch arg sits immediately before --resume, so
        # the base is two tokens ahead of it; without it, the base is the token right
        # before --resume. The resume command is wrapped in a `( ... )` subshell, so a
        # trailing `)` group token follows the args; drop it before reading the args.
        resume_idx = resume_tokens.index("--resume")
        if mngr_injects_settings:
            assert resume_tokens[resume_idx - 2] == "--settings"
            self.resume_settings: str | None = resume_tokens[resume_idx - 1]
            self.resume_base = resume_tokens[resume_idx - 3]
        else:
            self.resume_settings = None
            self.resume_base = resume_tokens[resume_idx - 1]
        assert resume_tokens[resume_idx + 1] == "$MAIN_CLAUDE_SESSION_ID"
        resume_arg_tokens = resume_tokens[resume_idx + 2 :]
        if resume_arg_tokens and resume_arg_tokens[-1] == ")":
            resume_arg_tokens = resume_arg_tokens[:-1]
        self.resume_args = resume_arg_tokens

        # Create branch: <base> [--settings <path>] --session-id <uuid> <args...>
        session_idx = create_tokens.index("--session-id")
        if mngr_injects_settings:
            assert create_tokens[session_idx - 2] == "--settings"
            self.create_settings: str | None = create_tokens[session_idx - 1]
            self.create_base = create_tokens[session_idx - 3]
        else:
            self.create_settings = None
            self.create_base = create_tokens[session_idx - 1]
        self.create_session_id = create_tokens[session_idx + 1]
        self.create_args = create_tokens[session_idx + 2 :]

    @property
    def has_is_sandbox(self) -> bool:
        return "IS_SANDBOX=1" in self.tokens

    @property
    def has_background_script(self) -> bool:
        return any("claude_background_tasks.sh" in token for token in self.tokens)


def test_claude_agent_assemble_command_with_no_args(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """ClaudeAgent should generate resume/session-id command format with no args."""
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)

    command = agent.assemble_command(host=host, agent_args=(), command_override=None)

    uuid = agent.id.get_uuid()
    parsed = _ParsedAssembleCommand(str(command))
    assert parsed.has_background_script
    assert parsed.resume_base == "claude"
    assert parsed.resume_args == []
    assert parsed.create_base == "claude"
    assert parsed.create_session_id == str(uuid)
    assert parsed.create_args == []
    # Local hosts should NOT have IS_SANDBOX set
    assert not parsed.has_is_sandbox
    # In normal mode mngr injects no --settings of its own (its hooks live in the
    # config-dir settings.json), so no --settings token appears at all.
    assert "--settings" not in parsed.tokens


def test_claude_agent_assemble_command_with_agent_args(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """ClaudeAgent should append agent args to both command variants."""
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)

    command = agent.assemble_command(host=host, agent_args=("--model", "opus"), command_override=None)

    uuid = agent.id.get_uuid()
    parsed = _ParsedAssembleCommand(str(command))
    # agent_args appended to both variants, in order.
    assert parsed.resume_base == "claude"
    assert parsed.resume_args == ["--model", "opus"]
    assert parsed.create_base == "claude"
    assert parsed.create_session_id == str(uuid)
    assert parsed.create_args == ["--model", "opus"]


def test_claude_agent_assemble_command_with_cli_args_and_agent_args(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """ClaudeAgent should append both cli_args and agent_args to both command variants."""
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(cli_args=("--verbose",), check_installation=False),
    )

    command = agent.assemble_command(host=host, agent_args=("--model", "opus"), command_override=None)

    uuid = agent.id.get_uuid()
    parsed = _ParsedAssembleCommand(str(command))
    # cli_args precede agent_args in both variants.
    assert parsed.resume_args == ["--verbose", "--model", "opus"]
    assert parsed.create_session_id == str(uuid)
    assert parsed.create_args == ["--verbose", "--model", "opus"]


def test_claude_agent_assemble_command_passes_user_settings_through(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """In normal mode a user ``--settings`` (cli_args or agent_args) passes through verbatim.

    mngr injects no ``--settings`` of its own (its hooks live in the config-dir
    settings.json, which Claude layers under the user's command-line ``--settings``),
    so the user's flag reaches claude unmodified and there is nothing to collide.
    """
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(
            cli_args=split_cli_args_string('--settings \'{"model": "opus"}\' --verbose'), check_installation=False
        ),
    )

    command = agent.assemble_command(
        host=host, agent_args=("--settings", '{"hooks": {}}', "--model", "opus"), command_override=None
    )

    parsed = _ParsedAssembleCommand(str(command))
    # mngr added no --settings of its own.
    assert parsed.resume_settings is None
    assert parsed.create_settings is None
    # Both the cli_args and agent_args user --settings appear verbatim in the args.
    assert parsed.resume_args == [
        "--settings",
        '{"model": "opus"}',
        "--verbose",
        "--settings",
        '{"hooks": {}}',
        "--model",
        "opus",
    ]
    assert parsed.create_args == parsed.resume_args


def test_claude_agent_assemble_command_injects_mngr_settings_in_env_config_dir_mode(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In use_env_config_dir mode, mngr injects its own --settings, alongside the user's.

    There is no per-agent config dir, so mngr loads its hooks via --settings (the
    managed file). The user's own --settings still passes through, so both appear
    (the documented collision: claude is last-wins).
    """
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(shared_dir))
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(
            cli_args=split_cli_args_string('--settings \'{"model": "opus"}\''),
            check_installation=False,
            use_env_config_dir=True,
        ),
    )

    command = agent.assemble_command(host=host, agent_args=(), command_override=None)

    parsed = _ParsedAssembleCommand(str(command), mngr_injects_settings=True)
    # mngr's managed-settings launch arg is present...
    expected_settings = shlex.split(MANAGED_SETTINGS_LAUNCH_ARG)[1]
    assert parsed.resume_settings == expected_settings
    assert parsed.create_settings == expected_settings
    # ...and the user's own --settings also passes through (the collision).
    assert parsed.resume_args == ["--settings", '{"model": "opus"}']
    assert parsed.create_args == ["--settings", '{"model": "opus"}']


def test_claude_agent_assemble_command_injects_mngr_settings_for_isolate_false_flag(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared mode set via the current isolate_local_config_dir=False flag (not the deprecated
    use_env_config_dir alias) must still inject mngr's managed --settings.

    The hooks-write gate and the hooks-load gate both key off the resolved predicate, so the
    managed hooks file written at provision time is actually loaded by claude at launch.
    """
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(shared_dir))
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, isolate_local_config_dir=False),
    )

    command = agent.assemble_command(host=host, agent_args=(), command_override=None)

    parsed = _ParsedAssembleCommand(str(command), mngr_injects_settings=True)
    expected_settings = shlex.split(MANAGED_SETTINGS_LAUNCH_ARG)[1]
    assert parsed.resume_settings == expected_settings
    assert parsed.create_settings == expected_settings


def test_claude_agent_assemble_command_with_command_override(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """ClaudeAgent should use command override when provided."""
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)

    command = agent.assemble_command(
        host=host,
        agent_args=("--model", "opus"),
        command_override=CommandString("custom-claude"),
    )

    uuid = agent.id.get_uuid()
    parsed = _ParsedAssembleCommand(str(command))
    # The override replaces the base command in both variants.
    assert parsed.resume_base == "custom-claude"
    assert parsed.resume_args == ["--model", "opus"]
    assert parsed.create_base == "custom-claude"
    assert parsed.create_session_id == str(uuid)
    assert parsed.create_args == ["--model", "opus"]


def test_claude_agent_assemble_command_raises_when_no_command(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """ClaudeAgent should raise NoCommandDefinedError when no command defined."""
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig.model_construct(command=None, check_installation=False),
        agent_type=AgentTypeName("custom"),
    )

    with pytest.raises(NoCommandDefinedError, match="No command defined"):
        agent.assemble_command(host=host, agent_args=(), command_override=None)


def test_claude_agent_assemble_command_sets_is_sandbox_for_remote_host(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """ClaudeAgent should set IS_SANDBOX=1 only for remote (non-local) hosts."""
    agent, _ = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)

    # Use SimpleNamespace to simulate a non-local host. Creating a real remote host
    # requires SSH infrastructure that is not available in unit tests. The assemble_command
    # method only reads host.is_local to decide whether to set IS_SANDBOX.
    non_local_host = cast(OnlineHostInterface, SimpleNamespace(is_local=False))

    command = agent.assemble_command(host=non_local_host, agent_args=(), command_override=None)

    uuid = agent.id.get_uuid()
    prefix = temp_mngr_ctx.config.prefix
    session_name = f"{prefix}test-agent"
    background_cmd = agent._build_background_tasks_command(session_name, temp_mngr_ctx.config.tmux.primary_window_name)
    sid_export = _sid_export_for(uuid)
    # Remote hosts SHOULD have IS_SANDBOX set
    assert command == CommandString(
        f'{background_cmd} export IS_SANDBOX=1 && {sid_export} && rm -rf $MNGR_AGENT_STATE_DIR/session_started && ( ( find "${{CLAUDE_CONFIG_DIR:-$HOME/.claude}}" -name "$MAIN_CLAUDE_SESSION_ID.jsonl" | grep . ) && claude --resume "$MAIN_CLAUDE_SESSION_ID" ) || claude --session-id {uuid}'
    )


def test_claude_agent_assemble_command_quotes_agent_args_with_shell_metacharacters(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """Agent args containing shell metacharacters must survive shell parsing as single tokens."""
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)
    prompt = "respond with only the word HELLO; echo pwned & rm -rf $HOME"

    command = agent.assemble_command(host=host, agent_args=("--model", "opus", prompt), command_override=None)

    # The command ends with "|| {create_cmd}" where create_cmd is the last shell statement.
    # Split on "||" and shlex-parse the tail to confirm the prompt arrived intact.
    create_cmd_segment = str(command).rsplit("||", 1)[1]
    tokens = shlex.split(create_cmd_segment)
    assert prompt in tokens, f"prompt should be a single token after shell parsing, got tokens={tokens!r}"


def test_claude_agent_assemble_command_resume_branch_runs_when_session_jsonl_exists(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """Regression: the resume guard must actually find an adopted session JSONL on disk.

    The original bug was a ``find`` invocation without the ``.jsonl`` suffix
    (``-name "$MAIN_CLAUDE_SESSION_ID"`` instead of ``-name "$MAIN_CLAUDE_SESSION_ID.jsonl"``).
    Files on disk are named ``<session_id>.jsonl``, so the guard returned no
    matches, the ``&&`` short-circuited, and the silent ``||`` fallback ran
    ``claude --session-id <fresh agent uuid>`` instead of ``claude --resume <adopted_id>``.
    The end-user symptom was that ``--adopt`` appeared to do nothing
    and a brand-new session opened with no error.

    This test executes the assembled shell pipeline against a stub ``claude``
    binary that records its argv, with a real session ``.jsonl`` planted at
    the location the resume path expects to find it. The argv recorded by
    the stub must contain ``--resume <session_id>`` -- if it contains
    ``--session-id <agent_uuid>`` instead, the regression is back.
    """
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)

    # Plant a real session file at the location the resume guard inspects.
    config_dir = tmp_path / "claude-config"
    project_dir = config_dir / "projects" / "some-encoded-project"
    project_dir.mkdir(parents=True)
    target_session_id = "adopted-sid-deadbeef"
    (project_dir / f"{target_session_id}.jsonl").write_text('{"type":"message"}\n')

    # Provide the session-id tracking file so $MAIN_CLAUDE_SESSION_ID resolves
    # to the adopted id rather than the agent's UUID fallback.
    state_dir = tmp_path / "agent-state"
    (state_dir / "commands").mkdir(parents=True)
    (state_dir / "claude_session_id").write_text(target_session_id)

    # Stub the background-tasks script (the assembled command runs it
    # backgrounded with &; we just need the path to exist and exit cleanly).
    bg_script = state_dir / "commands" / "claude_background_tasks.sh"
    bg_script.write_text("#!/bin/bash\nexit 0\n")
    bg_script.chmod(0o755)

    # Stub claude: write argv to a log and exit 0. Putting the stub on PATH
    # ahead of the real claude (if any) ensures the assembled command's
    # bare `claude` invocation hits our stub.
    stub_dir = tmp_path / "stub_bin"
    stub_dir.mkdir()
    invocation_log = tmp_path / "claude_invocation.log"
    stub_claude = stub_dir / "claude"
    stub_claude.write_text(f"#!/bin/bash\nprintf '%s\\n' \"$@\" > {shlex.quote(str(invocation_log))}\nexit 0\n")
    stub_claude.chmod(0o755)

    command = agent.assemble_command(host=host, agent_args=("--print", "hi"), command_override=None)

    env = {
        "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
        "CLAUDE_CONFIG_DIR": str(config_dir),
        "MNGR_AGENT_STATE_DIR": str(state_dir),
        "HOME": str(tmp_path),
    }
    result = subprocess.run(
        ["bash", "-c", str(command)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"Assembled pipeline failed with exit {result.returncode}.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    assert invocation_log.exists(), (
        f"Stub claude was never invoked. Pipeline output:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    invocation_args = invocation_log.read_text().splitlines()
    assert "--resume" in invocation_args, (
        "Resume branch did not fire. Stub claude was invoked with "
        f"{invocation_args!r}, indicating the resume guard's `find` returned no "
        "matches and the silent `||` fallback ran `claude --session-id <agent_uuid>` "
        "instead. The adopted session is effectively lost."
    )
    assert target_session_id in invocation_args, (
        f"Expected adopted session id {target_session_id!r} in claude argv, got {invocation_args!r}."
    )


# =============================================================================
# Activity Updater Tests
# =============================================================================


def test_build_background_tasks_command(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """_build_background_tasks_command should launch the provisioned background script."""
    agent, _ = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)

    prefix = temp_mngr_ctx.config.prefix
    session_name = f"{prefix}test-agent"
    cmd = agent._build_background_tasks_command(session_name, "agent")

    # Should be a background subshell
    assert cmd.startswith("(")
    assert cmd.endswith(") &")

    # Should reference the provisioned script
    assert "claude_background_tasks.sh" in cmd

    # Should pass the session name and the primary window name as arguments so the
    # response-streaming watcher captures the agent pane by window name (not :0).
    assert f"claude_background_tasks.sh {session_name} agent " in cmd


def test_build_background_tasks_command_passes_custom_primary_window_name(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """A custom primary window name is forwarded to claude_background_tasks.sh."""
    agent, _ = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)

    cmd = agent._build_background_tasks_command("mngr-test-agent", "primary")

    assert "claude_background_tasks.sh mngr-test-agent primary " in cmd


# =============================================================================
# Provisioning Lifecycle Tests
# =============================================================================


def test_on_before_provisioning_skips_check_when_disabled(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """on_before_provisioning should skip installation check when check_installation=False."""
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)
    _write_all_dialogs_dismissed(agent.work_dir)

    options = CreateAgentOptions(agent_type=AgentTypeName("claude"))

    # on_before_provisioning is a read-only preflight: it must not mutate the
    # user's ~/.claude.json. Snapshot the config and assert it is byte-for-byte
    # unchanged after the call (this is the strongest cleanly-observable effect
    # for the disabled-check path; with check_installation=False the function
    # returns before any install/version probe, leaving config untouched).
    config_path = Path.home() / ".claude.json"
    config_before = config_path.read_text()

    agent.on_before_provisioning(host=host, options=options, mngr_ctx=temp_mngr_ctx)

    assert config_path.read_text() == config_before


def test_on_before_provisioning_rejects_user_settings_flag_in_cli_args_for_env_config_dir(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """In use_env_config_dir mode a user `--settings` in cli_args is rejected (mngr passes
    its own `--settings` and can't reliably merge a second one)."""
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(
            check_installation=False,
            use_env_config_dir=True,
            cli_args=("--settings", "/tmp/custom.json"),
        ),
    )
    options = CreateAgentOptions(agent_type=AgentTypeName("claude"))

    with pytest.raises(UserInputError, match="settings_overrides"):
        agent.on_before_provisioning(host=host, options=options, mngr_ctx=temp_mngr_ctx)


def test_on_before_provisioning_rejects_user_settings_flag_in_agent_args_for_env_config_dir(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """The same rejection covers a `-- --settings=...` passed on the create command line
    (carried as agent_args)."""
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, use_env_config_dir=True),
    )
    options = CreateAgentOptions(agent_type=AgentTypeName("claude"), agent_args=("--settings=/tmp/custom.json",))

    with pytest.raises(UserInputError, match="isolate_local_config_dir"):
        agent.on_before_provisioning(host=host, options=options, mngr_ctx=temp_mngr_ctx)


def test_get_provision_file_transfers_returns_empty_when_no_local_settings(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """get_provision_file_transfers should return empty list when no .claude/ settings exist."""
    # Create agent with sync_repo_settings=True but no .claude/ directory exists
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, sync_repo_settings=True),
    )

    options = CreateAgentOptions(agent_type=AgentTypeName("claude"))

    transfers = agent.get_provision_file_transfers(host=host, options=options, mngr_ctx=temp_mngr_ctx)

    assert list(transfers) == []


def test_get_provision_file_transfers_returns_override_folder_files(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
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
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(
            check_installation=False,
            sync_repo_settings=False,
            override_settings_folder=override_folder,
        ),
    )

    options = CreateAgentOptions(agent_type=AgentTypeName("claude"))

    transfers = list(agent.get_provision_file_transfers(host=host, options=options, mngr_ctx=temp_mngr_ctx))

    assert len(transfers) == 1
    assert transfers[0].local_path == test_file
    assert str(transfers[0].agent_path) == ".claude/test_config.json"
    assert transfers[0].is_required is False


def test_get_provision_file_transfers_with_sync_repo_settings_disabled(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """get_provision_file_transfers should skip repo settings when sync_repo_settings=False."""
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, sync_repo_settings=False),
    )

    options = CreateAgentOptions(agent_type=AgentTypeName("claude"))

    transfers = list(agent.get_provision_file_transfers(host=host, options=options, mngr_ctx=temp_mngr_ctx))

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
    assert len(hooks) == 5

    # First hook: creates session_started file for polling-based detection
    assert hooks[0]["type"] == "command"
    assert "touch" in hooks[0]["command"]
    assert "session_started" in hooks[0]["command"]

    # Second hook: echoes the base branch for the agent's context
    assert hooks[1]["type"] == "command"
    assert "MNGR_GIT_BASE_BRANCH" in hooks[1]["command"]

    # Third hook: tracks current session ID for session replacement detection
    session_id_hook = hooks[2]["command"]
    assert hooks[2]["type"] == "command"
    assert "claude_session_id" in session_id_hook
    assert "session_id" in session_id_hook
    assert "MNGR_AGENT_STATE_DIR" in session_id_hook
    # Should fail loudly on missing session_id, not silently swallow
    assert "exit 1" in session_id_hook
    assert ">&2" in session_id_hook
    # Should extract source from hook payload
    assert "source" in session_id_hook
    assert "_MNGR_SOURCE" in session_id_hook
    # Should append to history file for tracking old session IDs (with source)
    assert "claude_session_id_history" in session_id_hook
    # Should use atomic write (write to .tmp then mv) to prevent torn reads
    assert "claude_session_id.tmp" in session_id_hook
    assert "mv" in session_id_hook

    # Fourth hook: signals tmux wait-for on /clear and /compact so that
    # `mngr message agent -m /clear` does not time out. /clear and /compact
    # are TUI-local slash commands that do not trigger UserPromptSubmit, so
    # we mirror that hook's tmux wait-for signal here, filtered by source.
    submit_signal_hook = hooks[3]["command"]
    assert hooks[3]["type"] == "command"
    assert "tmux wait-for -S" in submit_signal_hook
    assert "mngr-submit-" in submit_signal_hook
    # Should filter on source so normal startup/resume do not fire the signal
    assert "clear" in submit_signal_hook
    assert "compact" in submit_signal_hook
    assert "_MNGR_SOURCE" in submit_signal_hook

    # Fifth hook: on startup/resume, resets the stale 'active'/'permissions_waiting'
    # markers left over from a turn abandoned by an abnormal exit, so the agent does
    # not report RUNNING forever after a restart. compact is excluded (mid-turn).
    reset_markers_hook = hooks[4]["command"]
    assert hooks[4]["type"] == "command"
    assert "_MNGR_SOURCE" in reset_markers_hook
    assert "startup|resume" in reset_markers_hook
    assert "compact" not in reset_markers_hook
    assert 'rm -f "$MNGR_AGENT_STATE_DIR/active"' in reset_markers_hook
    assert "permissions_waiting" in reset_markers_hook


@pytest.mark.parametrize(
    "hook_name, expected_substrings",
    [
        ("UserPromptSubmit", ["touch", "active", "permissions_waiting"]),
        ("PermissionRequest", ["touch", "permissions_waiting"]),
        ("PostToolUse", ["rm", "permissions_waiting"]),
        ("PostToolUseFailure", ["rm", "permissions_waiting"]),
        ("Stop", ["wait_for_stop_hook.sh"]),
    ],
)
def test_build_readiness_hooks_config_has_hook(hook_name: str, expected_substrings: list[str]) -> None:
    """build_readiness_hooks_config should include the expected hook with correct command."""
    config = build_readiness_hooks_config()

    assert hook_name in config["hooks"]
    assert len(config["hooks"][hook_name]) == 1
    hook = config["hooks"][hook_name][0]["hooks"][0]
    assert hook["type"] == "command"
    assert "MNGR_AGENT_STATE_DIR" in hook["command"]
    for substring in expected_substrings:
        assert substring in hook["command"], f"Expected '{substring}' in {hook_name} hook command"


def test_build_readiness_hooks_config_has_notification_idle_hook() -> None:
    """build_readiness_hooks_config should include Notification idle_prompt hook that removes active and permissions_waiting files."""
    config = build_readiness_hooks_config()

    assert "Notification" in config["hooks"]
    assert len(config["hooks"]["Notification"]) == 1
    hook_group = config["hooks"]["Notification"][0]
    assert hook_group["matcher"] == "idle_prompt"
    hook = hook_group["hooks"][0]
    assert hook["type"] == "command"
    assert "rm" in hook["command"]
    assert "MNGR_AGENT_STATE_DIR" in hook["command"]
    assert "active" in hook["command"]
    assert "permissions_waiting" in hook["command"]


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required to run the SessionStart hook command")
@pytest.mark.parametrize(
    "source, should_clear",
    [
        ("startup", True),
        ("resume", True),
        ("compact", False),
        ("clear", False),
    ],
)
def test_session_start_hook_resets_stale_active_marker(tmp_path: Path, source: str, should_clear: bool) -> None:
    """The SessionStart hook clears a stale 'active' marker on startup/resume only.

    A turn abandoned by an abnormal exit (e.g. a container restart) leaves the
    'active' marker set, because the Stop hook never ran to remove it. On the
    next startup/resume -- where a fresh Claude process is provably not mid-turn
    -- the marker must be reset so get_lifecycle_state stops reporting RUNNING
    forever. ``compact`` and ``clear`` fire mid-conversation and must leave the
    marker untouched (compact in particular happens while Claude is active).
    """
    state_dir = tmp_path / "state"
    host_dir = tmp_path / "host"
    state_dir.mkdir()
    host_dir.mkdir()
    active_marker = state_dir / "active"
    active_marker.touch()
    (state_dir / "permissions_waiting").touch()

    config = build_readiness_hooks_config()
    # hooks[4] is the marker-reset hook (asserted in
    # test_build_readiness_hooks_config_has_session_start_hook).
    command = config["hooks"]["SessionStart"][0]["hooks"][4]["command"]

    result = subprocess.run(
        ["bash", "-c", command],
        input=json.dumps({"session_id": "sess-1", "source": source}),
        env={
            "MAIN_CLAUDE_SESSION_ID": "sess-1",
            "MNGR_AGENT_STATE_DIR": str(state_dir),
            "MNGR_HOST_DIR": str(host_dir),
            "PATH": os.environ.get("PATH", ""),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"hook failed: stdout={result.stdout!r} stderr={result.stderr!r}"

    marker_was_cleared = not active_marker.exists()
    assert marker_was_cleared == should_clear, (
        f"source={source!r}: expected active marker "
        f"{'cleared' if should_clear else 'kept'}, but it was "
        f"{'absent' if marker_was_cleared else 'present'}"
    )

    activity_log = host_dir / "events" / "mngr" / "activity" / "events.jsonl"
    # An activity event is emitted only when the markers are actually reset, so
    # `mngr observe` re-fetches the now-WAITING state promptly.
    assert activity_log.exists() == should_clear

    # The restart-boundary marker is recorded only on a fresh (startup/resume)
    # process, never on a mid-turn compaction/clear.
    process_started_marker = state_dir / "claude_process_started"
    assert process_started_marker.exists() == should_clear


def test_build_readiness_hooks_config_all_commands_guard_on_main_session() -> None:
    """Every command in readiness hooks should exit early when MAIN_CLAUDE_SESSION_ID is unset."""
    config = build_readiness_hooks_config()
    guard = '[ -z "$MAIN_CLAUDE_SESSION_ID" ] && exit 0; '

    for event_name, event_hooks in config["hooks"].items():
        for hook_group in event_hooks:
            for hook in hook_group["hooks"]:
                assert hook["command"].startswith(guard), (
                    f"{event_name} hook command does not start with session guard: {hook['command'][:80]}"
                )


def test_build_credential_sync_hooks_config_structure() -> None:
    """build_credential_sync_hooks_config should return a Notification:auth_success hook."""
    config = build_credential_sync_hooks_config()

    assert "hooks" in config
    assert "Notification" in config["hooks"]
    assert len(config["hooks"]["Notification"]) == 1
    hook_group = config["hooks"]["Notification"][0]
    assert hook_group["matcher"] == "auth_success"
    hook = hook_group["hooks"][0]
    assert hook["type"] == "command"
    assert "sync_keychain_credentials.py" in hook["command"]
    assert "MNGR_AGENT_STATE_DIR" in hook["command"]


def test_get_lifecycle_state_returns_waiting_when_permissions_waiting(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """ClaudeAgent.get_lifecycle_state downgrades RUNNING to WAITING when permissions_waiting exists."""
    agent, _ = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)
    agent._get_agent_dir().mkdir(parents=True, exist_ok=True)

    with patch.object(BaseAgent, "get_lifecycle_state", return_value=AgentLifecycleState.RUNNING):
        assert agent.get_lifecycle_state() == AgentLifecycleState.RUNNING

        (agent._get_agent_dir() / "permissions_waiting").touch()
        assert agent.get_lifecycle_state() == AgentLifecycleState.WAITING

    # Non-RUNNING states should pass through unchanged
    (agent._get_agent_dir() / "permissions_waiting").touch()
    for state in (
        AgentLifecycleState.STOPPED,
        AgentLifecycleState.WAITING,
        AgentLifecycleState.REPLACED,
        AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE,
        AgentLifecycleState.DONE,
    ):
        with patch.object(BaseAgent, "get_lifecycle_state", return_value=state):
            assert agent.get_lifecycle_state() == state


def test_agent_field_generators_returns_correct_structure() -> None:
    """agent_field_generators returns ('claude', {waiting_reason: <callable>})."""
    result = agent_field_generators()
    assert result is not None
    plugin_name, generators = result
    assert plugin_name == "claude"
    assert "waiting_reason" in generators
    assert callable(generators["waiting_reason"])


def test_agent_field_generators_waiting_reason_returns_permissions(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """waiting_reason returns PERMISSIONS when blocked mid-turn (active present and
    permissions_waiting present)."""
    result = agent_field_generators()
    assert result is not None
    _, generators = result
    waiting_reason = generators["waiting_reason"]

    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)

    agent_dir = host.host_dir / "agents" / str(agent.id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "active").touch()
    (agent_dir / "permissions_waiting").touch()

    assert waiting_reason(agent, host) == WaitingReason.PERMISSIONS


def test_agent_field_generators_waiting_reason_ignores_stranded_permissions_marker(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """A stranded permissions_waiting marker (active absent -> turn over) reports
    END_OF_TURN, not PERMISSIONS: the PERMISSIONS verdict is gated on the active
    marker so a marker that outlived its turn cannot mislabel an idle agent."""
    result = agent_field_generators()
    assert result is not None
    _, generators = result
    waiting_reason = generators["waiting_reason"]

    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)

    agent_dir = host.host_dir / "agents" / str(agent.id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "permissions_waiting").touch()

    assert waiting_reason(agent, host) == WaitingReason.END_OF_TURN


def test_agent_field_generators_waiting_reason_returns_end_of_turn(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """waiting_reason returns END_OF_TURN when no active file and no permissions_waiting."""
    result = agent_field_generators()
    assert result is not None
    _, generators = result
    waiting_reason = generators["waiting_reason"]

    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)

    agent_dir = host.host_dir / "agents" / str(agent.id)
    agent_dir.mkdir(parents=True, exist_ok=True)

    assert waiting_reason(agent, host) == WaitingReason.END_OF_TURN


def test_agent_field_generators_waiting_reason_returns_none_when_active(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """waiting_reason returns None when active file exists (agent is running)."""
    result = agent_field_generators()
    assert result is not None
    _, generators = result
    waiting_reason = generators["waiting_reason"]

    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)

    agent_dir = host.host_dir / "agents" / str(agent.id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "active").touch()

    assert waiting_reason(agent, host) is None


def test_get_expected_process_name_returns_claude(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """ClaudeAgent.get_expected_process_name should return 'claude'."""
    agent, _ = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)
    assert agent.get_expected_process_name() == "claude"


def test_tui_ready_indicator_is_input_prompt_glyph(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """ClaudeAgent uses the input-prompt glyph, which renders on both fresh start and resume."""
    agent, _ = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)
    assert agent.get_tui_ready_indicator() == "❯"


@pytest.mark.skipif(
    shutil.which("jq") is None, reason="jq not installed; required by the Claude acceptance-marker probe"
)
def test_build_accept_marker_command_extracts_latest_enqueue_timestamp(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """The acceptance-marker probe returns the most recent enqueue event's timestamp.

    This is the agent-specific behavior that ``tui_utils`` deliberately does not
    hold: read the transcript event log at ``$MNGR_AGENT_STATE_DIR/logs/claude_transcript/events.jsonl``,
    select ``enqueue`` events, and print the last (most recently appended) one's
    timestamp -- the monotonic token ``send_enter_via_tmux_wait_for_hook``
    watches. We run the actual probe against a fixture transcript that
    interleaves multiple enqueue events with non-enqueue events, and assert it
    skips the non-enqueue events and the earlier enqueue, printing the timestamp
    of the last enqueue line.
    """
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)
    state_dir = tmp_path / "agent-state"
    log_path = state_dir / "logs" / "claude_transcript" / "events.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        '{"operation":"enqueue","timestamp":"2026-06-09T10:00:00Z"}\n'
        '{"operation":"dequeue","timestamp":"2026-06-09T10:00:05Z"}\n'
        '{"operation":"enqueue","timestamp":"2026-06-09T10:01:00Z"}\n'
        '{"type":"assistant","timestamp":"2026-06-09T10:02:00Z"}\n'
    )

    result = host.execute_stateful_command(
        agent._build_accept_marker_command(),
        env={"MNGR_AGENT_STATE_DIR": str(state_dir)},
    )

    assert result.success
    assert result.stdout.strip() == "2026-06-09T10:01:00Z"


def test_build_accept_marker_command_emits_empty_token_when_no_enqueue_event(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """With no transcript log yet, the probe prints nothing -- the "no marker" baseline.

    ``send_enter_via_tmux_wait_for_hook`` relies on an empty token sorting before
    any real timestamp, so a missing log must not error or emit a stray value.
    """
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)
    # Point at a state dir whose transcript log does not exist.
    result = host.execute_stateful_command(
        agent._build_accept_marker_command(),
        env={"MNGR_AGENT_STATE_DIR": str(tmp_path / "missing-state")},
    )
    assert result.stdout.strip() == ""


def _make_hooks_test_agent(
    host: OnlineHostInterface, temp_mngr_ctx: MngrContext, work_dir: Path, agent_config: ClaudeAgentConfig
) -> ClaudeAgent:
    """Build a minimal ClaudeAgent for exercising _configure_agent_hooks."""
    return ClaudeAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("claude"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=temp_mngr_ctx,
        agent_config=agent_config,
        host=host,
    )


def test_configure_agent_hooks_writes_managed_file_not_settings_local(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """Hooks go to the per-agent managed file, never the project settings.local.json.

    The managed file is loaded via ``claude --settings`` and is private to the
    agent, so mngr no longer needs settings.local.json to be gitignored: even a
    plain (un-gitignored) git repo is left untouched.
    """
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    # Init git but do NOT add a .gitignore entry: this used to raise.
    init_git_repo(work_dir, initial_commit=False)

    agent = _make_hooks_test_agent(
        host, temp_mngr_ctx, work_dir, ClaudeAgentConfig(check_installation=False, use_env_config_dir=True)
    )

    agent._configure_agent_hooks(host, temp_mngr_ctx)

    # The managed settings file holds the hooks.
    managed_path = get_managed_settings_path(agent._get_agent_dir())
    assert managed_path.exists()
    settings = json.loads(managed_path.read_text())
    assert "SessionStart" in settings["hooks"]

    # The project settings.local.json is never written.
    assert not (work_dir / ".claude" / "settings.local.json").exists()


def test_configure_agent_hooks_works_without_a_git_repo(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """_configure_agent_hooks works in a plain (non-git) work_dir."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    agent = _make_hooks_test_agent(
        host, temp_mngr_ctx, work_dir, ClaudeAgentConfig(check_installation=False, use_env_config_dir=True)
    )

    agent._configure_agent_hooks(host, temp_mngr_ctx)

    managed_path = get_managed_settings_path(agent._get_agent_dir())
    assert managed_path.exists()
    settings = json.loads(managed_path.read_text())
    assert "hooks" in settings
    assert "SessionStart" in settings["hooks"]


def test_configure_agent_hooks_creates_managed_settings_file(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """_configure_agent_hooks should create the managed settings file with the readiness hooks."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    agent = _make_hooks_test_agent(
        host, temp_mngr_ctx, work_dir, ClaudeAgentConfig(check_installation=False, use_env_config_dir=True)
    )

    agent._configure_agent_hooks(host, temp_mngr_ctx)

    managed_path = get_managed_settings_path(agent._get_agent_dir())
    assert managed_path.exists()
    settings = json.loads(managed_path.read_text())
    assert "hooks" in settings
    assert "SessionStart" in settings["hooks"]
    assert "UserPromptSubmit" in settings["hooks"]
    assert "Notification" in settings["hooks"]


def test_configure_agent_hooks_applies_settings_overrides_in_managed_file(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """In use_env_config_dir mode the settings_overrides patch is folded into the managed
    --settings file alongside mngr's hooks (resolved against the hooks base, markers gone)."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    agent = _make_hooks_test_agent(
        host,
        temp_mngr_ctx,
        work_dir,
        ClaudeAgentConfig(
            check_installation=False,
            use_env_config_dir=True,
            settings_overrides={"model": "opus[1m]", "permissions__extend": {"allow__extend": ["Bash(npm *)"]}},
        ),
    )

    agent._configure_agent_hooks(host, temp_mngr_ctx)

    content = get_managed_settings_path(agent._get_agent_dir()).read_text()
    settings = json.loads(content)
    assert settings["model"] == "opus[1m]"
    assert settings["permissions"]["allow"] == ["Bash(npm *)"]
    assert "SessionStart" in settings["hooks"]
    assert "__extend" not in content


def test_configure_agent_hooks_does_not_touch_existing_settings_local(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """A pre-existing project settings.local.json is left completely untouched.

    mngr owns its managed file and writes it fresh; it neither reads nor
    rewrites the user's settings.local.json (which plain claude also reads).
    """
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    claude_dir = work_dir / ".claude"
    claude_dir.mkdir()
    user_settings = {"model": "opus", "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]}}
    settings_local = claude_dir / "settings.local.json"
    original_text = json.dumps(user_settings)
    settings_local.write_text(original_text)

    agent = _make_hooks_test_agent(
        host, temp_mngr_ctx, work_dir, ClaudeAgentConfig(check_installation=False, use_env_config_dir=True)
    )

    agent._configure_agent_hooks(host, temp_mngr_ctx)

    # settings.local.json is byte-for-byte unchanged.
    assert settings_local.read_text() == original_text

    # The mngr hooks live in the managed file instead.
    managed_path = get_managed_settings_path(agent._get_agent_dir())
    managed = json.loads(managed_path.read_text())
    assert "SessionStart" in managed["hooks"]
    # And mngr's hooks did not leak into the user's file.
    assert "SessionStart" not in user_settings["hooks"]


def test_configure_agent_hooks_overwrites_managed_file_fresh(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """Re-running overwrites the managed file fresh -- no cross-generation accumulation.

    A stale hook left in the managed file from a prior (different) mngr version
    must not survive: mngr owns the whole file and rewrites its deterministic
    build each time.
    """
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    agent = _make_hooks_test_agent(
        host, temp_mngr_ctx, work_dir, ClaudeAgentConfig(check_installation=False, use_env_config_dir=True)
    )

    managed_path = get_managed_settings_path(agent._get_agent_dir())
    managed_path.parent.mkdir(parents=True, exist_ok=True)
    stale = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mkdir -p /events/stale"}]}]}}
    managed_path.write_text(json.dumps(stale))

    agent._configure_agent_hooks(host, temp_mngr_ctx)

    settings = json.loads(managed_path.read_text())
    stop_hooks = settings["hooks"].get("Stop", [])
    commands = [c["command"] for entry in stop_hooks for c in entry.get("hooks", [])]
    assert not any("/events/stale" in cmd for cmd in commands)


def test_configure_agent_hooks_adds_credential_sync_on_macos(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """_configure_agent_hooks should add credential sync hooks on macOS when sync_credentials_on_login is True."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    agent = _make_hooks_test_agent(
        host,
        temp_mngr_ctx,
        work_dir,
        ClaudeAgentConfig(check_installation=False, sync_credentials_on_login=True, use_env_config_dir=True),
    )

    with patch(f"{_CLAUDE_AGENT_MODULE}.is_macos", return_value=True):
        agent._configure_agent_hooks(host, temp_mngr_ctx)

    settings = json.loads(get_managed_settings_path(agent._get_agent_dir()).read_text())

    # Should have readiness hooks
    assert "SessionStart" in settings["hooks"]

    # Should have credential sync hook under Notification with auth_success matcher
    notification_hooks = settings["hooks"]["Notification"]
    auth_hooks = [h for h in notification_hooks if h.get("matcher") == "auth_success"]
    assert len(auth_hooks) == 1
    assert "sync_keychain_credentials.py" in auth_hooks[0]["hooks"][0]["command"]


def test_configure_agent_hooks_skips_credential_sync_when_disabled(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """_configure_agent_hooks should not add credential sync hooks when sync_credentials_on_login is False."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    agent = _make_hooks_test_agent(
        host,
        temp_mngr_ctx,
        work_dir,
        ClaudeAgentConfig(check_installation=False, sync_credentials_on_login=False, use_env_config_dir=True),
    )

    with patch(f"{_CLAUDE_AGENT_MODULE}.is_macos", return_value=True):
        agent._configure_agent_hooks(host, temp_mngr_ctx)

    settings = json.loads(get_managed_settings_path(agent._get_agent_dir()).read_text())

    # Should have readiness hooks
    assert "SessionStart" in settings["hooks"]

    # Should NOT have credential sync hook
    notification_hooks = settings["hooks"]["Notification"]
    auth_hooks = [h for h in notification_hooks if h.get("matcher") == "auth_success"]
    assert len(auth_hooks) == 0


def test_provision_local_credentials_symlink(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """_provision_local_credentials with symlink=True should create a symlink to the source credentials."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    source_dir = tmp_path / "source_claude"
    source_dir.mkdir()
    (source_dir / ".credentials.json").write_text('{"token": "test"}')

    config_dir = tmp_path / "agent_config"
    config_dir.mkdir()

    with patch(f"{_CLAUDE_AGENT_MODULE}.get_user_claude_config_dir", return_value=source_dir):
        _provision_local_credentials(host, config_dir, symlink=True)

    dest = config_dir / ".credentials.json"
    assert dest.is_symlink()
    assert dest.resolve() == (source_dir / ".credentials.json").resolve()


def test_provision_local_credentials_copy(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """_provision_local_credentials with symlink=False should copy the credentials file."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    source_dir = tmp_path / "source_claude"
    source_dir.mkdir()
    (source_dir / ".credentials.json").write_text('{"token": "test"}')

    config_dir = tmp_path / "agent_config"
    config_dir.mkdir()

    with patch(f"{_CLAUDE_AGENT_MODULE}.get_user_claude_config_dir", return_value=source_dir):
        _provision_local_credentials(host, config_dir, symlink=False)

    dest = config_dir / ".credentials.json"
    assert dest.exists()
    assert not dest.is_symlink()
    assert dest.read_text() == '{"token": "test"}'
    assert oct(dest.stat().st_mode & 0o777) == oct(0o600)


def test_provision_local_credentials_missing_source(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """_provision_local_credentials should be a no-op when the source credentials file does not exist."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    source_dir = tmp_path / "source_claude"
    source_dir.mkdir()
    # Deliberately do NOT create .credentials.json

    config_dir = tmp_path / "agent_config"
    config_dir.mkdir()

    with patch(f"{_CLAUDE_AGENT_MODULE}.get_user_claude_config_dir", return_value=source_dir):
        _provision_local_credentials(host, config_dir, symlink=True)

    assert not (config_dir / ".credentials.json").exists()


def test_configure_agent_hooks_adds_permission_auto_allow_when_enabled(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """_configure_agent_hooks should add permission auto-allow hook when auto_allow_permissions is True."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _init_git_with_gitignore(work_dir)

    agent = _make_hooks_test_agent(
        host,
        temp_mngr_ctx,
        work_dir,
        ClaudeAgentConfig(check_installation=False, auto_allow_permissions=True, use_env_config_dir=True),
    )

    agent._configure_agent_hooks(host, temp_mngr_ctx)

    managed_path = get_managed_settings_path(agent._get_agent_dir())
    assert managed_path.exists()
    settings = json.loads(managed_path.read_text())

    # Should have readiness hooks
    assert "SessionStart" in settings["hooks"]
    assert "UserPromptSubmit" in settings["hooks"]

    # Should have the permission auto-allow hook
    permission_hooks = settings["hooks"]["PermissionRequest"]
    auto_allow_hooks = [h for h in permission_hooks if h.get("matcher") == "*"]
    assert len(auto_allow_hooks) == 1
    inner = auto_allow_hooks[0]["hooks"][0]
    assert "allow" in inner["command"]
    assert inner["timeout"] == 5


def test_configure_agent_hooks_does_not_add_permission_auto_allow_by_default(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """_configure_agent_hooks should not add permission auto-allow hook when auto_allow_permissions is False."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    agent = _make_hooks_test_agent(
        host, temp_mngr_ctx, work_dir, ClaudeAgentConfig(check_installation=False, use_env_config_dir=True)
    )

    agent._configure_agent_hooks(host, temp_mngr_ctx)

    settings = json.loads(get_managed_settings_path(agent._get_agent_dir()).read_text())

    # The readiness PermissionRequest hook (no matcher) should exist
    permission_hooks = settings["hooks"]["PermissionRequest"]
    # No hook with matcher="*" should exist
    auto_allow_hooks = [h for h in permission_hooks if h.get("matcher") == "*"]
    assert len(auto_allow_hooks) == 0


def test_provision_configures_readiness_hooks(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """Normal mode bakes mngr's hooks into the per-agent config-dir settings.json.

    No managed --settings file and no project settings.local.json are written.
    """
    # check_installation=False avoids running `claude --version` which would fail in test env
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False),
    )
    _init_git_with_gitignore(agent.work_dir)
    _write_all_dialogs_dismissed(agent.work_dir)

    options = CreateAgentOptions(agent_type=AgentTypeName("claude"))
    agent.provision(host=host, options=options, mngr_ctx=temp_mngr_ctx)

    # The hooks live in the per-agent config-dir settings.json.
    settings_path = agent.get_claude_config_dir() / "settings.json"
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text())
    assert "hooks" in settings
    assert "SessionStart" in settings["hooks"]

    # The managed --settings file is NOT written in normal mode, and neither is
    # the project settings.local.json.
    assert not get_managed_settings_path(agent._get_agent_dir()).exists()
    assert not (agent.work_dir / ".claude" / "settings.local.json").exists()


def test_provision_configures_readiness_hooks_in_env_config_dir_mode(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In use_env_config_dir mode, provision writes the managed --settings file.

    There is no per-agent config dir to bake hooks into, so the managed file is
    the only channel.
    """
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(shared_dir))
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, use_env_config_dir=True),
    )
    _init_git_with_gitignore(agent.work_dir)
    # Shared mode dismisses dialogs against the shared config; pre-dismiss them so the
    # non-interactive provision does not raise on the trust dialog.
    _write_dialogs_dismissed_at(shared_dir / ".claude.json", agent.work_dir)

    options = CreateAgentOptions(agent_type=AgentTypeName("claude"))
    agent.provision(host=host, options=options, mngr_ctx=temp_mngr_ctx)

    # The managed --settings file holds the hooks in this mode.
    managed_path = get_managed_settings_path(agent._get_agent_dir())
    assert managed_path.exists()
    settings = json.loads(managed_path.read_text())
    assert "hooks" in settings
    assert "SessionStart" in settings["hooks"]


def test_provision_shared_mode_dismisses_dialogs_in_global_config_but_not_permissions(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared mode mutates the user's global config to dismiss the cosmetic startup dialogs
    (trust, onboarding, effort callout, cost threshold) -- but never the bypass-permissions
    grant, which must not be silently accepted via the global config."""
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(shared_dir))
    # auto_dismiss_dialogs lets the non-interactive provision silently dismiss dialogs.
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(
            check_installation=False, isolate_local_config_dir=False, auto_dismiss_dialogs=True
        ),
    )
    _init_git_with_gitignore(agent.work_dir)

    options = CreateAgentOptions(agent_type=AgentTypeName("claude"))
    agent.provision(host=host, options=options, mngr_ctx=temp_mngr_ctx)

    # The dismissals land in the shared config file (the one the agent's claude reads),
    # NOT in ~/.claude.json.
    shared_config = json.loads((shared_dir / ".claude.json").read_text())
    assert shared_config["hasCompletedOnboarding"] is True
    assert shared_config["effortCalloutDismissed"] is True
    assert shared_config["hasAcknowledgedCostThreshold"] is True
    assert shared_config["projects"][str(agent.work_dir.resolve())]["hasTrustDialogAccepted"] is True

    # The bypass-permissions grant is never written to the global config.
    assert "bypassPermissionsModeAccepted" not in shared_config
    assert not (Path.home() / ".claude.json").exists()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_provision_raises_when_remote_installation_disabled(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_host_dir: Path,
    temp_profile_dir: Path,
    plugin_manager: "pluggy.PluginManager",
    mngr_test_prefix: str,
) -> None:
    """provision should raise when claude is not installed on remote host and is_remote_agent_installation_allowed is False."""
    config = MngrConfig(
        prefix=mngr_test_prefix,
        default_host_dir=temp_host_dir,
        is_remote_agent_installation_allowed=False,
    )
    with ConcurrencyGroup(name="test-remote-install") as cg:
        ctx = make_mngr_ctx(config, plugin_manager, temp_profile_dir, concurrency_group=cg)
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
                execute_idempotent_command=lambda *args, **kwargs: SimpleNamespace(success=False),
                write_file=lambda *args, **kwargs: None,
            ),
        )

        options = CreateAgentOptions(agent_type=AgentTypeName("claude"))

        with pytest.raises(ConcurrencyExceptionGroup) as exc_info:
            agent.provision(host=non_local_host, options=options, mngr_ctx=ctx)
        assert isinstance(exc_info.value.main_exception, AgentInstallationError)
        assert "automatic remote installation is disabled" in str(exc_info.value.main_exception)


# =============================================================================
# Trust Extension / Cleanup Tests
# =============================================================================


def test_provision_extends_trust_for_worktree(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    setup_git_config: None,
) -> None:
    """provision should create per-agent config with trust for worktree."""
    source_path, worktree_path, agent, host = _setup_worktree_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        is_source_trusted=True,
    )

    agent.provision(host=host, options=_WORKTREE_OPTIONS, mngr_ctx=temp_mngr_ctx)

    # Verify trust was added to the per-agent config (not global)
    per_agent_config_path = agent.get_claude_config_dir() / ".claude.json"
    per_agent_config = json.loads(per_agent_config_path.read_text())
    assert str(worktree_path.resolve()) in per_agent_config["projects"]
    worktree_entry = per_agent_config["projects"][str(worktree_path.resolve())]
    assert worktree_entry["hasTrustDialogAccepted"] is True
    # Source project config should also be present (copied from global config)
    assert str(source_path.resolve()) in per_agent_config["projects"]


def test_provision_does_not_extend_trust_for_non_worktree(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """provision should not extend trust when the git source path cannot be found.

    GIT_MIRROR mode attempts trust extension, but _find_git_source_path returns
    None here because the work_dir is not a git worktree (it's an ordinary git
    repo), so no source path is available to extend trust from.
    """
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)
    _init_git_with_gitignore(agent.work_dir)
    _write_all_dialogs_dismissed(agent.work_dir)

    options = CreateAgentOptions(
        agent_type=AgentTypeName("claude"),
        transfer_mode=TransferMode.GIT_MIRROR,
    )

    agent.provision(host=host, options=options, mngr_ctx=temp_mngr_ctx)

    # Trust was written by _write_all_dialogs_dismissed, but the provision could
    # not extend trust from a source directory because _find_git_source_path
    # returns None (work_dir is not a git worktree). Assert the negative: the
    # global config's projects must contain ONLY the pre-existing work_dir entry,
    # so a regression that erroneously extended trust (adding the source path or
    # other entries) would fail here.
    config_path = Path.home() / ".claude.json"
    config = json.loads(config_path.read_text())
    assert set(config["projects"].keys()) == {str(agent.work_dir.resolve())}


def test_provision_does_not_extend_trust_when_no_git_options(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """provision should not extend trust when git options are None."""
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)
    _init_git_with_gitignore(agent.work_dir)
    _write_all_dialogs_dismissed(agent.work_dir)

    options = CreateAgentOptions(agent_type=AgentTypeName("claude"))

    agent.provision(host=host, options=options, mngr_ctx=temp_mngr_ctx)

    # Trust should NOT have been extended since no git options were provided.
    # The projects map must contain ONLY the pre-existing work_dir entry; an extra
    # key would mean provision wrongly extended trust.
    config_path = Path.home() / ".claude.json"
    config = json.loads(config_path.read_text())
    assert set(config["projects"].keys()) == {str(agent.work_dir.resolve())}


def test_provision_skips_trust_when_git_common_dir_is_none(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """provision should skip trust extension when find_git_common_dir returns None."""
    # Create agent with work_dir that is NOT a git repo
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)
    _write_all_dialogs_dismissed(agent.work_dir)
    # Don't init git - work_dir is not a git repo

    agent.provision(host=host, options=_WORKTREE_OPTIONS, mngr_ctx=temp_mngr_ctx)

    # Trust should NOT have been extended from a source since there's no git common dir.
    # The projects map must contain ONLY the pre-existing work_dir entry.
    config_path = Path.home() / ".claude.json"
    config = json.loads(config_path.read_text())
    assert set(config["projects"].keys()) == {str(agent.work_dir.resolve())}


def test_provision_trusts_working_directory_when_enabled(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """provision should add trust for work_dir when auto_dismiss_dialogs is True."""
    config = ClaudeAgentConfig(check_installation=False, auto_dismiss_dialogs=True)
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx, agent_config=config)

    options = CreateAgentOptions(agent_type=AgentTypeName("claude"))

    agent.provision(host=host, options=options, mngr_ctx=temp_mngr_ctx)

    config_path = Path.home() / ".claude.json"
    claude_config = json.loads(config_path.read_text())
    assert str(agent.work_dir.resolve()) in claude_config["projects"]
    assert claude_config["projects"][str(agent.work_dir.resolve())]["hasTrustDialogAccepted"] is True
    assert claude_config["effortCalloutDismissed"] is True


def test_provision_does_not_auto_dismiss_dialogs_when_disabled(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """provision should not add trust when auto_dismiss_dialogs is False (default)."""
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)
    _write_all_dialogs_dismissed(agent.work_dir)

    options = CreateAgentOptions(agent_type=AgentTypeName("claude"))

    agent.provision(host=host, options=options, mngr_ctx=temp_mngr_ctx)

    # auto_dismiss_dialogs=False (default) means no additional trust was added.
    # The projects map must contain ONLY the pre-existing work_dir entry; an extra
    # key would mean a dialog/trust entry was auto-dismissed despite the flag.
    config_path = Path.home() / ".claude.json"
    config = json.loads(config_path.read_text())
    assert set(config["projects"].keys()) == {str(agent.work_dir.resolve())}


def test_auto_dismiss_dialogs_defaults_to_false() -> None:
    """Verify that auto_dismiss_dialogs defaults to False for ClaudeAgentConfig."""
    config = ClaudeAgentConfig()
    assert config.auto_dismiss_dialogs is False


def test_on_before_provisioning_validates_trust_for_worktree(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    setup_git_config: None,
) -> None:
    """on_before_provisioning should validate source directory is trusted for worktree mode."""
    source_path, worktree_path, agent, host = _setup_worktree_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        is_source_trusted=True,
    )

    # Should succeed without error because the source directory is trusted.
    # The trust entry must survive (and remain the work_dir's source trust) --
    # a non-interactive preflight that does not see trust would raise below.
    agent.on_before_provisioning(host=host, options=_WORKTREE_OPTIONS, mngr_ctx=temp_mngr_ctx)
    config = json.loads((Path.home() / ".claude.json").read_text())
    assert config["projects"][str(source_path.resolve())]["hasTrustDialogAccepted"] is True


def test_on_before_provisioning_rejects_untrusted_worktree(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    setup_git_config: None,
) -> None:
    """on_before_provisioning must reject an untrusted worktree in non-interactive mode.

    Sibling to test_on_before_provisioning_validates_trust_for_worktree: the
    trusted case proves the happy path doesn't raise, and this case proves the
    gate actually fires (raising ClaudeDirectoryNotTrustedError) when the source
    directory has no trust entry.
    """
    source_path, worktree_path, agent, host = _setup_worktree_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        is_source_trusted=False,
    )

    with pytest.raises(ClaudeDirectoryNotTrustedError) as exc_info:
        agent.on_before_provisioning(host=host, options=_WORKTREE_OPTIONS, mngr_ctx=temp_mngr_ctx)
    # The error must name the specific untrusted source directory (not the worktree).
    assert str(source_path.resolve()) in str(exc_info.value)


def test_on_before_provisioning_skips_dialog_check_when_interactive(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    interactive_mngr_ctx: MngrContext,
    setup_git_config: None,
) -> None:
    """on_before_provisioning should skip dialog check for interactive runs (provision() handles it)."""
    source_path, worktree_path, agent, host = _setup_worktree_agent(
        local_provider,
        tmp_path,
        interactive_mngr_ctx,
    )

    # No ~/.claude.json was written, so the source is NOT trusted and no dialogs
    # are dismissed. A non-interactive preflight would raise here; the interactive
    # path defers to provision() and must NOT raise.
    config_path = Path.home() / ".claude.json"
    assert not config_path.exists()

    agent.on_before_provisioning(host=host, options=_WORKTREE_OPTIONS, mngr_ctx=interactive_mngr_ctx)

    # The read-only preflight must not have written/dismissed anything in the
    # interactive path (provision() owns that). The user's config stays absent.
    assert not config_path.exists()


def test_on_before_provisioning_skips_trust_check_when_git_common_dir_is_none(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """on_before_provisioning should skip trust check when find_git_common_dir returns None."""
    # Create agent with work_dir that is NOT a git repo
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)
    _write_all_dialogs_dismissed(agent.work_dir)

    # find_git_common_dir returns None, so the trust check falls back to work_dir,
    # which _write_all_dialogs_dismissed already trusts -- so the preflight passes.
    # It is read-only, so the config must be byte-for-byte unchanged afterward.
    config_path = Path.home() / ".claude.json"
    config_before = config_path.read_text()

    agent.on_before_provisioning(host=host, options=_WORKTREE_OPTIONS, mngr_ctx=temp_mngr_ctx)

    assert config_path.read_text() == config_before


def test_on_before_provisioning_shared_mode_validates_dialogs_against_shared_config(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In shared mode, on_before_provisioning validates dialog dismissal against the shared
    config file (``$CLAUDE_CONFIG_DIR/.claude.json``), the file the agent's claude reads."""
    shared_dir = tmp_path / "shared"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(shared_dir))
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, isolate_local_config_dir=False),
    )
    options = CreateAgentOptions(agent_type=AgentTypeName("claude"))

    # With no dialogs dismissed in the shared config, the non-interactive check raises.
    with pytest.raises(ClaudeDirectoryNotTrustedError):
        agent.on_before_provisioning(host=host, options=options, mngr_ctx=temp_mngr_ctx)

    # Dismissing the dialogs in the shared config (not ~/.claude.json) makes it pass.
    _write_dialogs_dismissed_at(shared_dir / ".claude.json", agent.work_dir)
    agent.on_before_provisioning(host=host, options=options, mngr_ctx=temp_mngr_ctx)


def _make_remote_claude_agent(
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    agent_config: ClaudeAgentConfig,
) -> tuple[ClaudeAgent, OnlineHostInterface]:
    """Construct a ClaudeAgent bound to a (fake) remote host."""
    remote_host = cast(OnlineHostInterface, FakeHost(is_local=False, host_dir=tmp_path / "remote_host_dir"))
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    agent = ClaudeAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-remote-agent"),
        agent_type=AgentTypeName("claude"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=HostId("host-0000000000000000000000000000beef"),
        mngr_ctx=temp_mngr_ctx,
        agent_config=agent_config,
        host=remote_host,
    )
    return agent, remote_host


def test_on_before_provisioning_remote_ignores_shared_flag(
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """isolate_local_config_dir is local-only: a remote agent with the flag set to False
    must NOT raise (remote agents always use an isolated per-agent config dir)."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "shared"))
    agent, remote_host = _make_remote_claude_agent(
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, isolate_local_config_dir=False),
    )

    options = CreateAgentOptions(agent_type=AgentTypeName("claude"))

    # Must not raise -- the flag is simply ignored for remote hosts.
    agent.on_before_provisioning(host=remote_host, options=options, mngr_ctx=temp_mngr_ctx)

    # ...and the agent uses an isolated per-agent config dir, not the local shared dir.
    assert agent._is_isolated_config_dir() is True
    assert agent.get_claude_config_dir() == agent._get_agent_dir() / "plugin" / "claude" / "anthropic"


def test_remote_agent_modify_env_vars_uses_isolated_dir_despite_shared_flag(
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote agent with isolate_local_config_dir=False still gets CLAUDE_CONFIG_DIR and
    ORIGINAL_CLAUDE_CONFIG_DIR pointing at its isolated per-agent config dir."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "shared"))
    agent, remote_host = _make_remote_claude_agent(
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, isolate_local_config_dir=False),
    )
    env_vars: dict[str, str] = {}

    agent.modify_env_vars(remote_host, env_vars)

    per_agent_dir = agent._get_agent_dir() / "plugin" / "claude" / "anthropic"
    assert env_vars["CLAUDE_CONFIG_DIR"] == str(per_agent_dir)
    # Remote agents are isolated, so ORIGINAL_CLAUDE_CONFIG_DIR is set (unlike local shared mode).
    assert "ORIGINAL_CLAUDE_CONFIG_DIR" in env_vars


def test_on_before_provisioning_shared_mode_validates_default_config_when_env_unset(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With isolate_local_config_dir=False and $CLAUDE_CONFIG_DIR unset, on_before_provisioning
    validates dialogs against the default ``~/.claude.json`` (where claude reads them)."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, isolate_local_config_dir=False),
    )
    options = CreateAgentOptions(agent_type=AgentTypeName("claude"))

    # No ~/.claude.json present -> the non-interactive dialog check raises.
    config_path = Path.home() / ".claude.json"
    assert not config_path.exists()
    with pytest.raises(ClaudeDirectoryNotTrustedError):
        agent.on_before_provisioning(host=host, options=options, mngr_ctx=temp_mngr_ctx)

    # Dismissing the dialogs in ~/.claude.json (the default location) makes it pass.
    _write_all_dialogs_dismissed(agent.work_dir)
    agent.on_before_provisioning(host=host, options=options, mngr_ctx=temp_mngr_ctx)


def test_on_destroy_removes_trust(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """on_destroy should remove the Claude trust entry for the agent's work_dir."""
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)

    # Write a mngr-created trust entry for the agent's work_dir
    _write_mngr_trust_entry(agent.work_dir)

    # Verify the entry exists before destroy
    config_path = Path.home() / ".claude.json"
    config_before = json.loads(config_path.read_text())
    assert str(agent.work_dir.resolve()) in config_before["projects"]

    agent.on_destroy(host)

    # Verify the trust entry was removed
    config_after = json.loads(config_path.read_text())
    assert str(agent.work_dir.resolve()) not in config_after.get("projects", {})


def _populate_session_files(agent: ClaudeAgent) -> dict[str, Path]:
    """Create fake session files in the agent's state directory for testing preservation.

    Returns a dict mapping logical names to their paths.
    """
    agent_dir = agent._get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)

    # Create session JSONL files in the per-agent Claude config dir
    config_dir = agent.get_claude_config_dir()
    project_name = encode_claude_project_dir_name(agent.work_dir)
    projects_dir = config_dir / "projects" / project_name
    projects_dir.mkdir(parents=True, exist_ok=True)

    session_file = projects_dir / "abc123.jsonl"
    session_file.write_text('{"type":"assistant","uuid":"u1"}\n')

    # Create the raw transcript
    raw_transcript_dir = agent_dir / "logs" / "claude_transcript"
    raw_transcript_dir.mkdir(parents=True, exist_ok=True)
    raw_transcript_file = raw_transcript_dir / "events.jsonl"
    raw_transcript_file.write_text('{"type":"message"}\n')

    # Create the common transcript
    common_transcript_dir = agent_dir / "events" / "claude" / "common_transcript"
    common_transcript_dir.mkdir(parents=True, exist_ok=True)
    common_transcript_file = common_transcript_dir / "events.jsonl"
    common_transcript_file.write_text('{"type":"user_message","text":"hello"}\n')

    # Create the session history
    history_file = agent_dir / "claude_session_id_history"
    history_file.write_text("abc123 create\n")

    return {
        "session_file": session_file,
        "raw_transcript_file": raw_transcript_file,
        "common_transcript_file": common_transcript_file,
        "history_file": history_file,
    }


def _preserved_dir_for_agent(agent: ClaudeAgent, mngr_ctx: MngrContext) -> Path:
    """Return the local preserved-files dir for an agent under the new mirrored layout."""
    return get_local_preserved_agent_dir(mngr_ctx, agent.name, agent.id)


@pytest.mark.rsync
def test_on_destroy_preserves_session_files(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """on_destroy should preserve session files when preserve_sessions_on_destroy is True.

    Files land at the new mirrored layout under <local_host_dir>/preserved/<name>--<id>/,
    matching the agent state directory structure verbatim.
    """
    agent_config = ClaudeAgentConfig(check_installation=False, preserve_sessions_on_destroy=True)
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx, agent_config=agent_config)
    files = _populate_session_files(agent)
    _write_mngr_trust_entry(agent.work_dir)

    agent.on_destroy(host)

    dest_dir = _preserved_dir_for_agent(agent, temp_mngr_ctx)
    assert dest_dir.exists()

    # Session JSONL files preserved at the mirrored config-dir path.
    preserved_projects = dest_dir / "plugin" / "claude" / "anthropic" / "projects"
    assert preserved_projects.exists()
    preserved_session_files = list(preserved_projects.rglob("*.jsonl"))
    assert len(preserved_session_files) == 1
    assert preserved_session_files[0].read_text() == files["session_file"].read_text()

    # Raw transcript dir preserved at logs/claude_transcript.
    preserved_raw_transcript = dest_dir / "logs" / "claude_transcript" / "events.jsonl"
    assert preserved_raw_transcript.exists()
    assert preserved_raw_transcript.read_text() == '{"type":"message"}\n'

    # Common transcript dir preserved at events/claude/common_transcript.
    preserved_common_transcript = dest_dir / "events" / "claude" / "common_transcript" / "events.jsonl"
    assert preserved_common_transcript.exists()
    assert preserved_common_transcript.read_text() == '{"type":"user_message","text":"hello"}\n'

    # Session history preserved as a single file at the top level.
    preserved_history = dest_dir / "claude_session_id_history"
    assert preserved_history.exists()
    assert preserved_history.read_text() == "abc123 create\n"


def test_on_destroy_skips_preservation_when_disabled(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """on_destroy should not preserve sessions when preserve_sessions_on_destroy is False."""
    agent_config = ClaudeAgentConfig(check_installation=False, preserve_sessions_on_destroy=False)
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx, agent_config=agent_config)
    _populate_session_files(agent)
    _write_mngr_trust_entry(agent.work_dir)

    agent.on_destroy(host)

    dest_dir = _preserved_dir_for_agent(agent, temp_mngr_ctx)
    assert not dest_dir.exists()


def test_on_destroy_handles_no_session_data(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """on_destroy should not create a preserved dir when there is no session data."""
    agent_config = ClaudeAgentConfig(check_installation=False, preserve_sessions_on_destroy=True)
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx, agent_config=agent_config)
    _write_mngr_trust_entry(agent.work_dir)

    # No session files populated -- just destroy
    agent.on_destroy(host)

    dest_dir = _preserved_dir_for_agent(agent, temp_mngr_ctx)
    assert not dest_dir.exists()


def test_on_destroy_skips_keychain_cleanup_in_shared_mode(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In shared mode, on_destroy must NOT call the macOS keychain delete -- the
    shared $CLAUDE_CONFIG_DIR exists, so the per-agent-keychain branch would
    otherwise hash the user's own config dir and delete the user's real
    credentials.
    """
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(shared_dir))
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, isolate_local_config_dir=False),
    )

    delete_calls: list[str] = []

    def _fake_delete(label: str, _cg: object) -> bool:
        delete_calls.append(label)
        return False

    with (
        patch(f"{_CLAUDE_AGENT_MODULE}.is_macos", return_value=True),
        patch(f"{_CLAUDE_AGENT_MODULE}._delete_macos_keychain_credential", side_effect=_fake_delete),
    ):
        agent.on_destroy(host)

    assert delete_calls == []


def test_on_destroy_still_calls_keychain_cleanup_in_default_mode(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Default (per-agent) mode must keep invoking the macOS keychain delete on
    its own per-agent config dir -- this is the existing behavior, kept stable
    by the shared-mode fix.
    """
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)
    # Ensure per-agent config dir exists on disk so the keychain branch fires.
    agent.get_claude_config_dir().mkdir(parents=True, exist_ok=True)

    delete_calls: list[str] = []

    def _fake_delete(label: str, _cg: object) -> bool:
        delete_calls.append(label)
        return False

    with (
        patch(f"{_CLAUDE_AGENT_MODULE}.is_macos", return_value=True),
        patch(f"{_CLAUDE_AGENT_MODULE}._delete_macos_keychain_credential", side_effect=_fake_delete),
    ):
        agent.on_destroy(host)

    # User-visible effect: both per-agent credential kinds (API key and OAuth
    # credentials) are targeted for deletion under the suffix Claude Code derives
    # from the per-agent config dir. We assert the exact target labels (rather
    # than a raw call count) because they form the OS-keychain contract that must
    # match what Claude Code itself wrote -- a wrong suffix or kind would leave a
    # stale credential behind.
    suffix = _compute_keychain_label_suffix(agent.get_claude_config_dir())
    assert set(delete_calls) == {f"Claude Code{suffix}", f"Claude Code-credentials{suffix}"}


@pytest.mark.rsync
def test_preserve_session_files_partial_data(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """Preservation should work when only some session data exists (e.g., only raw transcript)."""
    agent_config = ClaudeAgentConfig(check_installation=False, preserve_sessions_on_destroy=True)
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx, agent_config=agent_config)
    agent_dir = agent._get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)

    # Only create the raw transcript, no projects, common transcript, or history
    transcript_dir = agent_dir / "logs" / "claude_transcript"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    (transcript_dir / "events.jsonl").write_text('{"partial":"data"}\n')

    agent.on_destroy(host)

    dest_dir = _preserved_dir_for_agent(agent, temp_mngr_ctx)
    assert dest_dir.exists()
    assert (dest_dir / "logs" / "claude_transcript" / "events.jsonl").exists()
    assert not (dest_dir / "plugin" / "claude" / "anthropic" / "projects").exists()
    assert not (dest_dir / "events" / "claude" / "common_transcript").exists()
    assert not (dest_dir / "claude_session_id_history").exists()


@pytest.mark.rsync
def test_preserve_session_files_skips_projects_in_shared_mode(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In shared mode (isolate_local_config_dir=False), preservation must NOT copy the per-agent
    plugin/claude/anthropic/projects directory -- in shared mode the projects
    live in the user's persistent $CLAUDE_CONFIG_DIR (not under the agent state
    dir) and hold the user's full cross-project session history. Transcripts and
    history (under the agent state dir) are still preserved.
    """
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(shared_dir))
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(
            check_installation=False, isolate_local_config_dir=False, preserve_sessions_on_destroy=True
        ),
    )
    agent_dir = agent._get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)

    # Populate a projects dir under the agent state dir. In shared mode this is
    # NOT one of the declared preserved items, so it must be ignored even if it
    # exists on disk (the real projects dir lives in the shared config dir).
    projects_under_state = agent_dir / "plugin" / "claude" / "anthropic" / "projects" / "-Users-someone-other"
    projects_under_state.mkdir(parents=True)
    (projects_under_state / "deadbeef.jsonl").write_text('{"private":"data"}\n')

    # Populate the per-agent transcript + history (these live under the agent
    # state dir, so they DO need preservation regardless of shared mode).
    raw_transcript_dir = agent_dir / "logs" / "claude_transcript"
    raw_transcript_dir.mkdir(parents=True, exist_ok=True)
    (raw_transcript_dir / "events.jsonl").write_text('{"type":"message"}\n')
    history_file = agent_dir / "claude_session_id_history"
    history_file.write_text("abc123 create\n")

    agent.on_destroy(host)

    dest_dir = _preserved_dir_for_agent(agent, temp_mngr_ctx)
    assert dest_dir.exists()
    # Projects dir must NOT be preserved in shared mode.
    assert not (dest_dir / "plugin" / "claude" / "anthropic" / "projects").exists()
    # Transcript and history must still be preserved.
    assert (dest_dir / "logs" / "claude_transcript" / "events.jsonl").read_text() == '{"type":"message"}\n'
    assert (dest_dir / "claude_session_id_history").read_text() == "abc123 create\n"


def test_provision_prompts_for_all_dialogs_when_interactive(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    interactive_mngr_ctx: MngrContext,
    setup_git_config: None,
) -> None:
    """provision should prompt for trust, effort callout, onboarding, and bypass permissions when none are set."""
    source_path, worktree_path, agent, host = _setup_worktree_agent(
        local_provider,
        tmp_path,
        interactive_mngr_ctx,
    )

    with _mock_all_dialog_prompts():
        agent.provision(host=host, options=_WORKTREE_OPTIONS, mngr_ctx=interactive_mngr_ctx)

    # Verify dialogs were resolved in the global config (user intent). We assert
    # on the on-disk end state rather than the internal prompt-call counts so a
    # behavior-preserving refactor (e.g. consolidating the three prompts) doesn't
    # break the test; the declined-prompt cases are covered separately.
    config_path = Path.home() / ".claude.json"
    config = json.loads(config_path.read_text())
    assert config["projects"][str(source_path.resolve())]["hasTrustDialogAccepted"] is True
    assert config["effortCalloutDismissed"] is True
    assert config["hasCompletedOnboarding"] is True

    # Verify worktree trust was added to the per-agent config
    per_agent_config_path = agent.get_claude_config_dir() / ".claude.json"
    per_agent_config = json.loads(per_agent_config_path.read_text())
    assert str(worktree_path.resolve()) in per_agent_config["projects"]
    worktree_entry = per_agent_config["projects"][str(worktree_path.resolve())]
    assert worktree_entry["hasTrustDialogAccepted"] is True


def test_provision_raises_when_non_interactive_and_untrusted(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    setup_git_config: None,
) -> None:
    """provision should raise when non-interactive and source is untrusted."""
    source_path, worktree_path, agent, host = _setup_worktree_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
    )

    with pytest.raises(ConcurrencyExceptionGroup) as exc_info:
        agent.provision(host=host, options=_WORKTREE_OPTIONS, mngr_ctx=temp_mngr_ctx)
    assert exc_info.value.only_exception_is_instance_of(ClaudeDirectoryNotTrustedError)


def test_provision_raises_when_user_declines_trust(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    interactive_mngr_ctx: MngrContext,
    setup_git_config: None,
) -> None:
    """provision should raise when user declines the trust prompt."""
    source_path, worktree_path, agent, host = _setup_worktree_agent(
        local_provider,
        tmp_path,
        interactive_mngr_ctx,
    )

    with _mock_all_dialog_prompts(trust_accepted=False):
        with pytest.raises(ConcurrencyExceptionGroup) as exc_info:
            agent.provision(host=host, options=_WORKTREE_OPTIONS, mngr_ctx=interactive_mngr_ctx)
        assert exc_info.value.only_exception_is_instance_of(ClaudeDirectoryNotTrustedError)


# =============================================================================
# API Credential Check Tests
# =============================================================================

_DEFAULT_CREDENTIAL_CHECK_OPTIONS = CreateAgentOptions(agent_type=AgentTypeName("claude"))


@pytest.fixture()
def _no_api_key_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure ANTHROPIC_API_KEY is not in os.environ."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture()
def credential_check_host(local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext) -> Host:
    """Create a local host for credential check tests."""
    _, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)
    return host


@pytest.fixture()
def credential_check_cg(temp_mngr_ctx: MngrContext) -> ConcurrencyGroup:
    """Provide the concurrency group for credential check tests."""
    return temp_mngr_ctx.concurrency_group


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


_NO_CREDENTIALS_WARNING_SUBSTRING = "No API credentials detected for Claude Code"


@pytest.mark.usefixtures("_no_api_key_in_env")
def test_on_before_provisioning_does_not_raise_when_no_credentials(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """on_before_provisioning should warn (not raise) when no API credentials are detected.

    The autouse env isolation redirects HOME to a temp dir (so no ~/.claude.json or
    credentials file exists) and the _no_api_key_in_env fixture clears the env var, so
    the real _has_api_credentials_available genuinely returns False here -- the same
    setup the direct test_has_api_credentials_returns_false_when_no_credentials relies on.
    """
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=True),
    )
    _write_all_dialogs_dismissed(agent.work_dir)

    with capture_loguru() as log_output:
        agent.on_before_provisioning(host=host, options=_DEFAULT_CREDENTIAL_CHECK_OPTIONS, mngr_ctx=temp_mngr_ctx)

    # It must not raise, and it must emit the missing-credentials warning so the
    # user is told the agent may fail to start.
    assert _NO_CREDENTIALS_WARNING_SUBSTRING in log_output.getvalue()


def test_on_before_provisioning_succeeds_with_credentials(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """on_before_provisioning should succeed WITHOUT the missing-credentials warning when creds exist."""
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=True),
    )
    _write_all_dialogs_dismissed(agent.work_dir)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")

    with capture_loguru() as log_output:
        agent.on_before_provisioning(host=host, options=_DEFAULT_CREDENTIAL_CHECK_OPTIONS, mngr_ctx=temp_mngr_ctx)

    # With a real credential available, the missing-credentials warning must NOT
    # be emitted (a flipped warn/no-warn branch would be caught here).
    assert _NO_CREDENTIALS_WARNING_SUBSTRING not in log_output.getvalue()


# =============================================================================
# Deprecated use_env_config_dir alias reconciliation Tests
# =============================================================================


def test_resolve_isolate_defaults_to_true_without_either_key() -> None:
    """With neither key set, the default isolate_local_config_dir=True is used."""
    assert ClaudeAgentConfig(check_installation=False).resolve_isolate_local_config_dir() is True


def test_resolve_isolate_uses_new_key_when_only_it_is_set() -> None:
    assert (
        ClaudeAgentConfig(check_installation=False, isolate_local_config_dir=False).resolve_isolate_local_config_dir()
        is False
    )


def test_resolve_isolate_inverts_deprecated_use_env_true() -> None:
    """The deprecated use_env_config_dir=true means isolate_local_config_dir=false (its inverse)."""
    config = ClaudeAgentConfig(check_installation=False, use_env_config_dir=True)
    assert config.resolve_isolate_local_config_dir() is False


def test_resolve_isolate_inverts_deprecated_use_env_false() -> None:
    config = ClaudeAgentConfig(check_installation=False, use_env_config_dir=False)
    assert config.resolve_isolate_local_config_dir() is True


def test_resolve_isolate_allows_consistent_inverse_dual_spec() -> None:
    """Setting both keys to consistent inverses (isolate=false, use_env=true) is allowed."""
    config = ClaudeAgentConfig(check_installation=False, isolate_local_config_dir=False, use_env_config_dir=True)
    assert config.resolve_isolate_local_config_dir() is False


def test_resolve_isolate_raises_on_contradictory_dual_spec() -> None:
    """Setting both keys to the same value (they are inverses) is contradictory and raises."""
    config = ClaudeAgentConfig(check_installation=False, isolate_local_config_dir=True, use_env_config_dir=True)
    with pytest.raises(ConfigError, match="Contradictory"):
        config.resolve_isolate_local_config_dir()


def test_on_before_provisioning_warns_when_use_env_config_dir_is_set(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting the deprecated use_env_config_dir emits a deprecation warning at provisioning time."""
    shared_dir = tmp_path / "shared"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(shared_dir))
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, use_env_config_dir=True),
    )
    # Shared mode now validates dialogs against the shared config, so dismiss them
    # there to reach the deprecation warning without raising.
    _write_dialogs_dismissed_at(shared_dir / ".claude.json", agent.work_dir)

    options = CreateAgentOptions(agent_type=AgentTypeName("claude"))
    with capture_loguru() as log_output:
        agent.on_before_provisioning(host=host, options=options, mngr_ctx=temp_mngr_ctx)

    assert "use_env_config_dir" in log_output.getvalue()
    assert "deprecated" in log_output.getvalue()


# =============================================================================
# Subscription-credential isolation warning Tests
# =============================================================================

# The actionable part of the warning: the exact command to disable isolation.
_SUBSCRIPTION_ISOLATION_WARNING_SUBSTRING = "isolate_local_config_dir false"


def test_is_using_claude_oauth_subscription_detects_credentials_file(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """A .credentials.json containing the claudeAiOauth key signals subscription usage."""
    config_dir = tmp_path / "claude"
    config_dir.mkdir()
    (config_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok"}}))

    # Patch the keychain read so the file branch is what determines the result
    # (and so the test never touches the real macOS login keychain).
    with patch(f"{_CLAUDE_AGENT_MODULE}._read_macos_keychain_credential", return_value=None):
        assert _is_using_claude_oauth_subscription(config_dir, temp_mngr_ctx.concurrency_group) is True


def test_is_using_claude_oauth_subscription_false_without_oauth(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    """No OAuth credentials file and no OAuth keychain entry means no subscription detected."""
    config_dir = tmp_path / "claude"
    config_dir.mkdir()

    with patch(f"{_CLAUDE_AGENT_MODULE}._read_macos_keychain_credential", return_value=None):
        assert _is_using_claude_oauth_subscription(config_dir, temp_mngr_ctx.concurrency_group) is False


def test_warns_about_subscription_credentials_with_isolation_on_macos(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS + subscription OAuth credentials + config-dir isolation enabled (the default)
    must warn, printing the command to disable isolation."""
    user_claude = tmp_path / "user-claude"
    user_claude.mkdir()
    (user_claude / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok"}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(user_claude))
    monkeypatch.delenv("ORIGINAL_CLAUDE_CONFIG_DIR", raising=False)
    # Default config isolates the local config dir (isolate_local_config_dir=True).
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)

    with (
        patch(f"{_CLAUDE_AGENT_MODULE}.is_macos", return_value=True),
        patch(f"{_CLAUDE_AGENT_MODULE}._read_macos_keychain_credential", return_value=None),
        capture_loguru() as log_output,
    ):
        agent._maybe_warn_subscription_credentials(host, temp_mngr_ctx)

    assert _SUBSCRIPTION_ISOLATION_WARNING_SUBSTRING in log_output.getvalue()


def test_no_subscription_warning_in_shared_mode(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared mode (isolate_local_config_dir=False) reuses the user's keychain entry, so
    even with subscription credentials on macOS there is no stale-credential problem to warn about."""
    user_claude = tmp_path / "user-claude"
    user_claude.mkdir()
    (user_claude / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok"}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(user_claude))
    monkeypatch.delenv("ORIGINAL_CLAUDE_CONFIG_DIR", raising=False)
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, isolate_local_config_dir=False),
    )

    with (
        patch(f"{_CLAUDE_AGENT_MODULE}.is_macos", return_value=True),
        patch(f"{_CLAUDE_AGENT_MODULE}._read_macos_keychain_credential", return_value=None),
        capture_loguru() as log_output,
    ):
        agent._maybe_warn_subscription_credentials(host, temp_mngr_ctx)

    assert _SUBSCRIPTION_ISOLATION_WARNING_SUBSTRING not in log_output.getvalue()


def test_no_subscription_warning_without_oauth_credentials(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Isolation on macOS but no OAuth credentials (e.g. API-key auth) must not warn."""
    user_claude = tmp_path / "user-claude"
    user_claude.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(user_claude))
    monkeypatch.delenv("ORIGINAL_CLAUDE_CONFIG_DIR", raising=False)
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)

    with (
        patch(f"{_CLAUDE_AGENT_MODULE}.is_macos", return_value=True),
        patch(f"{_CLAUDE_AGENT_MODULE}._read_macos_keychain_credential", return_value=None),
        capture_loguru() as log_output,
    ):
        agent._maybe_warn_subscription_credentials(host, temp_mngr_ctx)

    assert _SUBSCRIPTION_ISOLATION_WARNING_SUBSTRING not in log_output.getvalue()


# =============================================================================
# CostThresholdDialogIndicator Tests
# =============================================================================


def test_cost_threshold_indicator_matches_when_both_strings_present() -> None:
    """CostThresholdDialogIndicator.matches should return True when both strings are present."""
    indicator = CostThresholdDialogIndicator()
    content = (
        "You've spent $5 on the Anthropic API this session.\n\n"
        "Learn more about how to monitor your spending:\n"
        "https://code.claude.com/docs/en/costs"
    )
    assert indicator.matches(content) is True


def test_cost_threshold_indicator_no_match_with_only_spending_text() -> None:
    """CostThresholdDialogIndicator.matches should return False with only the spending text."""
    indicator = CostThresholdDialogIndicator()
    content = "Learn more about how to monitor your spending:\nhttps://example.com"
    assert indicator.matches(content) is False


def test_cost_threshold_indicator_no_match_with_only_url() -> None:
    """CostThresholdDialogIndicator.matches should return False with only the docs URL."""
    indicator = CostThresholdDialogIndicator()
    content = "Visit https://code.claude.com/docs for help"
    assert indicator.matches(content) is False


def test_cost_threshold_indicator_no_match_with_neither_string() -> None:
    """CostThresholdDialogIndicator.matches should return False with unrelated content."""
    indicator = CostThresholdDialogIndicator()
    content = "Claude Code is running normally"
    assert indicator.matches(content) is False


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
    temp_mngr_ctx: MngrContext,
    setup_git_config: None,
) -> None:
    """on_before_provisioning should raise when effortCalloutDismissed is not set."""
    source_path, worktree_path, agent, host = _setup_worktree_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
    )

    # Write trust but without effortCalloutDismissed
    _write_claude_trust_without_dialog_dismissed(source_path)

    with pytest.raises(ClaudeEffortCalloutNotDismissedError):
        agent.on_before_provisioning(host=host, options=_WORKTREE_OPTIONS, mngr_ctx=temp_mngr_ctx)


def test_provision_dismisses_dialogs_when_auto_approve(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_config: MngrConfig,
    temp_profile_dir: Path,
    plugin_manager: "pluggy.PluginManager",
    setup_git_config: None,
) -> None:
    """provision should auto-dismiss dialogs when auto_approve is enabled."""
    with ConcurrencyGroup(name="test-auto-approve-dialogs") as cg:
        auto_approve_ctx = make_mngr_ctx(
            temp_config, plugin_manager, temp_profile_dir, is_auto_approve=True, concurrency_group=cg
        )
        source_path, worktree_path, agent, host = _setup_worktree_agent(
            local_provider,
            tmp_path,
            auto_approve_ctx,
        )

        # Write trust but without effortCalloutDismissed
        _write_claude_trust_without_dialog_dismissed(source_path)

        agent.provision(host=host, options=_WORKTREE_OPTIONS, mngr_ctx=auto_approve_ctx)

        # Verify effortCalloutDismissed was set
        config_path = Path.home() / ".claude.json"
        config = json.loads(config_path.read_text())
        assert config["effortCalloutDismissed"] is True


def test_provision_prompts_for_dialog_dismissal_when_interactive(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    interactive_mngr_ctx: MngrContext,
    setup_git_config: None,
) -> None:
    """provision should prompt and dismiss dialogs when interactive and not yet dismissed."""
    source_path, worktree_path, agent, host = _setup_worktree_agent(
        local_provider,
        tmp_path,
        interactive_mngr_ctx,
    )

    # Write trust but without effortCalloutDismissed or hasCompletedOnboarding
    _write_claude_trust_without_dialog_dismissed(source_path)

    with _mock_all_dialog_prompts():
        agent.provision(host=host, options=_WORKTREE_OPTIONS, mngr_ctx=interactive_mngr_ctx)

    # Assert on the on-disk end state: the previously-undismissed dialogs are now
    # dismissed, while the already-set trust entry is preserved. This captures the
    # real effect without coupling to which/how-many prompt helpers were invoked.
    config_path = Path.home() / ".claude.json"
    config = json.loads(config_path.read_text())
    assert config["projects"][str(source_path.resolve())]["hasTrustDialogAccepted"] is True
    assert config["effortCalloutDismissed"] is True
    assert config["hasCompletedOnboarding"] is True


def test_provision_raises_when_user_declines_dialog_dismissal(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    interactive_mngr_ctx: MngrContext,
    setup_git_config: None,
) -> None:
    """provision should raise when user declines dialog dismissal prompt."""
    source_path, worktree_path, agent, host = _setup_worktree_agent(
        local_provider,
        tmp_path,
        interactive_mngr_ctx,
    )

    # Write trust but without effortCalloutDismissed
    _write_claude_trust_without_dialog_dismissed(source_path)

    with _mock_all_dialog_prompts(effort_accepted=False):
        with pytest.raises(ConcurrencyExceptionGroup) as exc_info:
            agent.provision(host=host, options=_WORKTREE_OPTIONS, mngr_ctx=interactive_mngr_ctx)
        assert exc_info.value.only_exception_is_instance_of(ClaudeEffortCalloutNotDismissedError)


def test_provision_raises_when_non_interactive_and_dialogs_not_dismissed(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    setup_git_config: None,
) -> None:
    """provision should raise when non-interactive and dialogs are not dismissed."""
    source_path, worktree_path, agent, host = _setup_worktree_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
    )

    # Write trust but without effortCalloutDismissed
    _write_claude_trust_without_dialog_dismissed(source_path)

    with pytest.raises(ConcurrencyExceptionGroup) as exc_info:
        agent.provision(host=host, options=_WORKTREE_OPTIONS, mngr_ctx=temp_mngr_ctx)
    assert exc_info.value.only_exception_is_instance_of(ClaudeEffortCalloutNotDismissedError)


# =============================================================================
# Remote Trust Tests
# =============================================================================


# provision() runs the local `claude --version` check via a real subprocess
# (_get_local_claude_version), which can exceed the default 10s pytest-timeout under
# CI load, so this test gets a longer timeout.
@pytest.mark.timeout(30)
def test_provision_adds_trust_for_remote_work_dir(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provision should add hasTrustDialogAccepted for work_dir in the claude.json synced to remote hosts."""
    monkeypatch.chdir(tmp_path)

    agent, _ = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, sync_claude_json=True),
        work_dir=temp_work_dir,
    )

    _write_claude_trust(temp_work_dir)

    host = cast(OnlineHostInterface, FakeHost(is_local=False, host_dir=tmp_path / "host_dir"))
    agent.provision(host=host, options=CreateAgentOptions(agent_type=AgentTypeName("claude")), mngr_ctx=temp_mngr_ctx)

    transferred_config = json.loads((tmp_path / ".claude.json").read_text())
    assert transferred_config["projects"][str(temp_work_dir)]["hasTrustDialogAccepted"] is True


def test_provision_preserves_existing_remote_project_config(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provision should preserve existing project config when adding trust for remote work_dir."""
    monkeypatch.chdir(tmp_path)

    agent, _ = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, sync_claude_json=True),
        work_dir=temp_work_dir,
    )

    # Write trust with extra fields that should be preserved
    _write_claude_trust(temp_work_dir)

    host = cast(OnlineHostInterface, FakeHost(is_local=False, host_dir=tmp_path / "host_dir"))
    agent.provision(host=host, options=CreateAgentOptions(agent_type=AgentTypeName("claude")), mngr_ctx=temp_mngr_ctx)

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
    temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    """get_files_for_deploy returns generated defaults when no local claude config files exist."""
    # Exclude project settings since the test repo_root may contain .claude/ files
    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=True, include_project_settings=False, repo_root=tmp_path
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


def test_get_files_for_deploy_includes_claude_json(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """get_files_for_deploy always includes ~/.claude.json with generated defaults (not local content).

    The deploy uses generated defaults with a fixed timestamp for better Docker
    layer caching, rather than syncing the user's local ~/.claude.json content.
    """
    claude_json = Path.home() / ".claude.json"
    claude_json.write_text('{"test": true}')

    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=True, include_project_settings=False, repo_root=tmp_path
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


def test_get_files_for_deploy_includes_claude_settings(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """get_files_for_deploy includes ~/.claude/settings.json with skipDangerousModePermissionPrompt when it exists."""
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings = claude_dir / "settings.json"
    settings.write_text('{"settings": true}')

    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=True, include_project_settings=False, repo_root=tmp_path
    )

    assert Path("~/.claude/settings.json") in result
    settings_content = result[Path("~/.claude/settings.json")]
    assert isinstance(settings_content, str)
    settings_data = json.loads(settings_content)
    assert settings_data["settings"] is True
    assert settings_data["skipDangerousModePermissionPrompt"] is True


def test_get_files_for_deploy_includes_claude_json_and_settings(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """get_files_for_deploy includes both claude.json and settings.json when both exist."""
    claude_json = Path.home() / ".claude.json"
    claude_json.write_text('{"test": true}')

    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings = claude_dir / "settings.json"
    settings.write_text('{"settings": true}')

    # Exclude project settings to avoid picking up .claude/*.local.* from the repo_root
    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=True, include_project_settings=False, repo_root=tmp_path
    )

    assert Path("~/.claude.json") in result
    assert Path("~/.claude/settings.json") in result


def test_get_files_for_deploy_ships_defaults_when_user_settings_excluded(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """get_files_for_deploy ships generated defaults even when include_user_settings is False."""
    claude_json = Path.home() / ".claude.json"
    claude_json.write_text('{"test": true}')

    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=False, include_project_settings=True, repo_root=tmp_path
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
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """get_files_for_deploy includes .claude/settings.local.json from the repo root."""
    project_claude_dir = tmp_path / ".claude"
    project_claude_dir.mkdir(parents=True, exist_ok=True)
    local_settings = project_claude_dir / "settings.local.json"
    local_settings.write_text('{"local": true}')

    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=False, include_project_settings=True, repo_root=tmp_path
    )

    assert Path(".claude/settings.local.json") in result
    assert result[Path(".claude/settings.local.json")] == local_settings


def test_get_files_for_deploy_excludes_project_settings_when_flag_false(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """get_files_for_deploy skips project local files when include_project_settings is False, but always ships defaults."""
    project_claude_dir = tmp_path / ".claude"
    project_claude_dir.mkdir(parents=True, exist_ok=True)
    local_settings = project_claude_dir / "settings.local.json"
    local_settings.write_text('{"local": true}')

    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=False, include_project_settings=False, repo_root=tmp_path
    )

    # Generated defaults are always shipped
    assert Path("~/.claude/settings.json") in result
    assert Path("~/.claude.json") in result
    # But project local files should NOT be included
    assert Path(".claude/settings.local.json") not in result


def test_get_files_for_deploy_includes_skills_directory(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """get_files_for_deploy includes files from ~/.claude/skills/ recursively."""
    claude_dir = Path.home() / ".claude"
    skills_dir = claude_dir / "skills" / "my-skill"
    skills_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skills_dir / "SKILL.md"
    skill_file.write_text("# My Skill")

    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=True, include_project_settings=False, repo_root=tmp_path
    )

    assert Path("~/.claude/skills/my-skill/SKILL.md") in result
    assert result[Path("~/.claude/skills/my-skill/SKILL.md")] == skill_file


def test_get_files_for_deploy_includes_commands_directory(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """get_files_for_deploy includes files from ~/.claude/commands/ recursively."""
    claude_dir = Path.home() / ".claude"
    commands_dir = claude_dir / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    cmd_file = commands_dir / "my-command.md"
    cmd_file.write_text("# Command")

    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=True, include_project_settings=False, repo_root=tmp_path
    )

    assert Path("~/.claude/commands/my-command.md") in result
    assert result[Path("~/.claude/commands/my-command.md")] == cmd_file


def test_get_files_for_deploy_includes_agents_directory(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """get_files_for_deploy includes files from ~/.claude/agents/ recursively."""
    claude_dir = Path.home() / ".claude"
    agents_dir = claude_dir / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    agent_file = agents_dir / "my-agent.json"
    agent_file.write_text('{"agent": true}')

    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=True, include_project_settings=False, repo_root=tmp_path
    )

    assert Path("~/.claude/agents/my-agent.json") in result
    assert result[Path("~/.claude/agents/my-agent.json")] == agent_file


def test_get_files_for_deploy_includes_credentials(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """get_files_for_deploy includes ~/.claude/.credentials.json when it exists."""
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    credentials = claude_dir / ".credentials.json"
    credentials.write_text('{"oauth_token": "test"}')

    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=True, include_project_settings=False, repo_root=tmp_path
    )

    assert Path("~/.claude/.credentials.json") in result
    assert result[Path("~/.claude/.credentials.json")] == credentials


def test_get_files_for_deploy_includes_keybindings(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """get_files_for_deploy includes ~/.claude/keybindings.json when it exists."""
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    keybindings = claude_dir / "keybindings.json"
    keybindings.write_text('{"bindings": []}')

    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=True, include_project_settings=False, repo_root=tmp_path
    )

    assert Path("~/.claude/keybindings.json") in result
    assert result[Path("~/.claude/keybindings.json")] == keybindings


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
    accumulates command strings passed to execute_idempotent_command.
    """
    executed_commands: list[str] = []

    def mock_execute_idempotent_command(cmd: str, *args: object, **kwargs: object) -> SimpleNamespace:
        executed_commands.append(cmd)
        return SimpleNamespace(success=True, stdout="", stderr="")

    host = cast(
        OnlineHostInterface,
        SimpleNamespace(
            execute_idempotent_command=mock_execute_idempotent_command,
        ),
    )
    return host, executed_commands


def test_get_claude_version_returns_version_on_success() -> None:
    """_get_claude_version should return the version string when claude --version succeeds."""
    issued_commands: list[str] = []

    def _execute(cmd: str, *args: object, **kwargs: object) -> SimpleNamespace:
        issued_commands.append(cmd)
        return SimpleNamespace(success=True, stdout="2.1.50 (Claude Code)\n", stderr="")

    host = cast(OnlineHostInterface, SimpleNamespace(execute_idempotent_command=_execute))

    assert _get_claude_version(host) == "2.1.50"
    # The version must be probed via `claude --version`; a bug invoking a
    # different command (e.g. `claude --ver`) would otherwise go undetected.
    assert issued_commands == ["claude --version"]


def test_get_claude_version_returns_none_on_failure() -> None:
    """_get_claude_version should return None when claude --version fails."""
    issued_commands: list[str] = []

    def _execute(cmd: str, *args: object, **kwargs: object) -> SimpleNamespace:
        issued_commands.append(cmd)
        return SimpleNamespace(success=False, stdout="", stderr="command not found")

    host = cast(OnlineHostInterface, SimpleNamespace(execute_idempotent_command=_execute))

    assert _get_claude_version(host) is None
    assert issued_commands == ["claude --version"]


def test_provision_raises_on_version_mismatch(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_host_dir: Path,
    temp_profile_dir: Path,
    plugin_manager: "pluggy.PluginManager",
    mngr_test_prefix: str,
) -> None:
    """provision should raise when installed claude version does not match pinned version."""
    config = MngrConfig(
        prefix=mngr_test_prefix,
        default_host_dir=temp_host_dir,
    )
    with ConcurrencyGroup(name="test-version-mismatch") as cg:
        ctx = make_mngr_ctx(config, plugin_manager, temp_profile_dir, concurrency_group=cg)
        agent, _ = make_claude_agent(
            local_provider,
            tmp_path,
            ctx,
            agent_config=ClaudeAgentConfig(check_installation=True, version="99.99.99"),
        )

        # Simulate a host where claude is installed but at a different version.
        # FakeHost executes commands as real subprocesses, so it cannot return a
        # canned `claude --version`; we keep a SimpleNamespace for the controlled
        # version output but give write_file a real implementation that writes to
        # disk. This lets the background-script provisioning threads (which call
        # host.write_file) complete cleanly instead of silently swallowing the
        # write, which previously left a thread raising an unhandled exception
        # (the reason the PytestUnhandledThreadExceptionWarning filter was needed).
        def _real_write_file(path: Path, content: bytes, mode: str | None = None) -> None:
            del mode
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        host_with_wrong_version = cast(
            OnlineHostInterface,
            SimpleNamespace(
                is_local=True,
                execute_idempotent_command=lambda cmd, *args, **kwargs: SimpleNamespace(
                    success=True,
                    stdout="2.1.50 (Claude Code)\n",
                    stderr="",
                ),
                write_file=_real_write_file,
            ),
        )

        _write_all_dialogs_dismissed(agent.work_dir)
        options = CreateAgentOptions(agent_type=AgentTypeName("claude"))

        with pytest.raises(ConcurrencyExceptionGroup) as exc_info:
            agent.provision(host=host_with_wrong_version, options=options, mngr_ctx=ctx)
        assert isinstance(exc_info.value.main_exception, AgentInstallationError)
        assert "Claude version mismatch" in str(exc_info.value.main_exception)


def _install_clause_tokens(install_command: str, marker: str) -> list[str]:
    """Return the shlex-parsed tokens of the `&&`-joined clause containing ``marker``.

    _install_claude builds a single string of clauses joined by ` && `. Parsing
    the relevant clause into tokens lets the install-command tests assert on the
    semantically load-bearing arguments (e.g. presence/absence of a version arg)
    without pinning the exact whitespace or clause ordering of the full string.
    """
    clauses = [clause for clause in install_command.split(" && ") if marker in clause]
    assert len(clauses) == 1, f"Expected exactly one clause containing {marker!r}, got {clauses!r}"
    return shlex.split(clauses[0])


def test_install_claude_passes_version_to_command() -> None:
    """_install_claude should pass the version as a positional arg to the install script."""
    host, executed_commands = _make_command_tracking_host()

    _install_claude(host, version="2.1.50")

    assert len(executed_commands) == 1
    tokens = _install_clause_tokens(executed_commands[0], "bash /tmp/install_claude.sh")
    # bash <script> <version>: the version must follow the install script path.
    script_index = tokens.index("/tmp/install_claude.sh")
    assert tokens[:script_index] == ["bash"]
    assert tokens[script_index + 1 :] == ["2.1.50"]


def test_install_claude_without_version() -> None:
    """_install_claude should not pass version arg when no version is specified."""
    host, executed_commands = _make_command_tracking_host()

    _install_claude(host, version=None)

    assert len(executed_commands) == 1
    tokens = _install_clause_tokens(executed_commands[0], "bash /tmp/install_claude.sh")
    # bash <script> with no trailing positional version argument.
    assert tokens == ["bash", "/tmp/install_claude.sh"]


def test_install_claude_verifies_binary_exists() -> None:
    """_install_claude should verify the binary is executable after install."""
    host, executed_commands = _make_command_tracking_host()

    _install_claude(host, version=None)

    assert len(executed_commands) == 1
    # The installer must verify the binary it placed under CLAUDE_INSTALL_PATH is
    # executable; anchor to the imported constant rather than a hand-typed literal.
    tokens = _install_clause_tokens(executed_commands[0], "test -x")
    assert tokens == ["test", "-x", f"{CLAUDE_INSTALL_PATH}/claude"]


# =============================================================================
# Capability-mixin contract methods (install / unattended / version)
# =============================================================================


def test_get_install_binary_name_is_claude() -> None:
    agent = ClaudeAgent.model_construct(agent_config=ClaudeAgentConfig())
    assert agent.get_install_binary_name() == "claude"


def test_get_install_command_installs_claude() -> None:
    agent = ClaudeAgent.model_construct(agent_config=ClaudeAgentConfig())
    assert agent.get_install_command() == _build_claude_install_command(None)


def test_get_install_command_pins_configured_version() -> None:
    agent = ClaudeAgent.model_construct(agent_config=ClaudeAgentConfig(version="2.1.50"))
    assert agent.get_install_command() == _build_claude_install_command("2.1.50")


def test_is_unattended_enabled_reflects_auto_allow_permissions() -> None:
    unattended = ClaudeAgent.model_construct(agent_config=ClaudeAgentConfig(auto_allow_permissions=True))
    attended = ClaudeAgent.model_construct(agent_config=ClaudeAgentConfig())
    assert unattended.is_unattended_enabled() is True
    assert attended.is_unattended_enabled() is False


def _version_stub_host(version_output: str) -> OnlineHostInterface:
    """A host whose `claude --version` returns ``version_output`` (for reconcile tests)."""
    return cast(
        OnlineHostInterface,
        SimpleNamespace(
            execute_idempotent_command=lambda *args, **kwargs: SimpleNamespace(
                success=True, stdout=version_output, stderr=""
            )
        ),
    )


def test_reconcile_installed_version_unpinned_is_noop() -> None:
    # Unpinned: claude follows its own auto-update, so there is nothing to enforce and
    # reconcile returns without touching the host.
    agent = ClaudeAgent.model_construct(agent_config=ClaudeAgentConfig())
    agent.reconcile_installed_version(
        cast(OnlineHostInterface, SimpleNamespace()), cast(MngrContext, SimpleNamespace())
    )


def test_reconcile_installed_version_pinned_match_is_noop() -> None:
    agent = ClaudeAgent.model_construct(agent_config=ClaudeAgentConfig(version="2.1.50"))
    agent.reconcile_installed_version(_version_stub_host("2.1.50 (Claude Code)"), cast(MngrContext, SimpleNamespace()))


def test_reconcile_installed_version_raises_on_mismatch() -> None:
    agent = ClaudeAgent.model_construct(agent_config=ClaudeAgentConfig(version="2.1.50"))
    with pytest.raises(AgentInstallationError, match="version mismatch"):
        agent.reconcile_installed_version(
            _version_stub_host("9.9.9 (Claude Code)"), cast(MngrContext, SimpleNamespace())
        )


# =============================================================================
# register_cli_options Tests
# =============================================================================


# The --adopt option declaration + the agent-agnostic gate (type must support
# session adoption; mutual exclusion with --from) now live in core, tested
# there; claude only retains its claude-specific fail-fast pre-resolution below.


# =============================================================================
# on_before_create Tests (claude-specific fail-fast pre-resolution)
# =============================================================================


def test_on_before_create_skips_when_no_adopt_session(temp_mngr_ctx: MngrContext) -> None:
    """on_before_create should return None when adopt_session is empty."""
    args = OnBeforeCreateArgs(
        agent_options=CreateAgentOptions(agent_type=AgentTypeName("claude")),
        target_host=NewHostOptions(provider=ProviderInstanceName("local")),
        create_work_dir=True,
    )
    assert on_before_create(args=args, mngr_ctx=temp_mngr_ctx) is None


def test_on_before_create_passes_with_adopt_session(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    """on_before_create should pass when --adopt names a resolvable session with a claude agent."""
    session_file = tmp_path / "abc123.jsonl"
    session_file.write_text('{"type":"message"}\n')
    args = OnBeforeCreateArgs(
        agent_options=CreateAgentOptions(
            agent_type=AgentTypeName("claude"),
            adopt_session=(str(session_file),),
        ),
        target_host=NewHostOptions(provider=ProviderInstanceName("local")),
        create_work_dir=True,
    )
    result = on_before_create(args=args, mngr_ctx=temp_mngr_ctx)
    assert result is None


def test_on_before_create_passes_with_claude_subtype(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    """on_before_create should accept a config-defined subtype whose parent_type
    chain reaches claude (e.g. a custom ``coder`` template), not just the literal
    ``claude`` type name. This is the centralized "is a claude agent" check via
    resolve_agent_type, rather than a string comparison against "claude".
    """
    subtype = AgentTypeName("coder")
    config_with_subtype = temp_mngr_ctx.config.model_copy_update(
        to_update(
            temp_mngr_ctx.config.field_ref().agent_types,
            {subtype: AgentTypeConfig(parent_type=AgentTypeName("claude"))},
        ),
    )
    mngr_ctx = temp_mngr_ctx.model_copy_update(
        to_update(temp_mngr_ctx.field_ref().config, config_with_subtype),
    )
    session_file = tmp_path / "abc123.jsonl"
    session_file.write_text('{"type":"message"}\n')
    args = OnBeforeCreateArgs(
        agent_options=CreateAgentOptions(
            agent_type=subtype,
            adopt_session=(str(session_file),),
        ),
        target_host=NewHostOptions(provider=ProviderInstanceName("local")),
        create_work_dir=True,
    )
    assert on_before_create(args=args, mngr_ctx=mngr_ctx) is None


def test_on_before_create_rejects_unknown_adopt_session(temp_mngr_ctx: MngrContext) -> None:
    """on_before_create should raise UserInputError when an --adopt ID does not resolve.

    Validating here -- before any host or worktree is created, and outside the provisioning
    ConcurrencyGroup -- means a bad session ID surfaces as a clean, fail-fast user error
    rather than being wrapped in a ConcurrencyExceptionGroup and reported mid-provisioning
    as an "Unexpected error".
    """
    args = OnBeforeCreateArgs(
        agent_options=CreateAgentOptions(
            agent_type=AgentTypeName("claude"),
            adopt_session=("nonexistent-session",),
        ),
        target_host=NewHostOptions(provider=ProviderInstanceName("local")),
        create_work_dir=True,
    )
    # Pin config-dir resolution to the isolated test HOME (~/.claude) so the search is
    # deterministic even when CLAUDE_CONFIG_DIR is set (e.g. inside an mngr agent).
    with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": ""}):
        with pytest.raises(UserInputError, match="Session nonexistent-session not found"):
            on_before_create(args=args, mngr_ctx=temp_mngr_ctx)


# =============================================================================
# on_after_provisioning Session Adoption Tests
# =============================================================================


def test_on_after_provisioning_skips_when_no_adopt_session(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """on_after_provisioning should do nothing when adopt_session is None."""
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)
    options = CreateAgentOptions(agent_type=AgentTypeName("claude"))

    # Should complete without error
    agent.on_after_provisioning(host=host, options=options, mngr_ctx=temp_mngr_ctx)


@pytest.mark.rsync
def test_on_after_provisioning_adopts_session_by_id(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """on_after_provisioning should find session by ID, copy project dir, and write session ID."""
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)

    # Set up a session under ~/.claude/ (HOME is already a temp dir via autouse fixture)
    project_dir = Path.home() / ".claude" / "projects" / "test-project"
    project_dir.mkdir(parents=True)
    target_session_id = "adopt-test-session-id"
    (project_dir / f"{target_session_id}.jsonl").write_text('{"type":"message"}\n')
    (project_dir / "CLAUDE.md").write_text("# Memory\n")

    agent_state_dir = agent._get_agent_dir()
    agent_state_dir.mkdir(parents=True, exist_ok=True)

    options = CreateAgentOptions(
        agent_type=AgentTypeName("claude"),
        adopt_session=(target_session_id,),
    )

    with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": ""}):
        agent.on_after_provisioning(host=host, options=options, mngr_ctx=temp_mngr_ctx)

    # Session ID should be written
    assert (agent_state_dir / "claude_session_id").read_text() == target_session_id

    # Session should be placed in the project dir matching the agent's work_dir,
    # not the source project dir name. This is how Claude Code finds sessions.
    expected_project_name = encode_claude_project_dir_name(agent.work_dir)
    dest_project_dir = agent.get_claude_config_dir() / "projects" / expected_project_name
    dest_session_file = dest_project_dir / f"{target_session_id}.jsonl"
    assert dest_session_file.exists(), f"Session file not found at {dest_session_file}"
    assert dest_session_file.read_text() == '{"type":"message"}\n'
    dest_memory_file = dest_project_dir / "CLAUDE.md"
    assert dest_memory_file.exists(), f"Memory file not found at {dest_memory_file}"
    assert dest_memory_file.read_text() == "# Memory\n"

    # Regression: verify the session file is discoverable the same way Claude Code
    # finds it at runtime: `find "${CLAUDE_CONFIG_DIR:-$HOME/.claude}" -name "$SESSION_ID.jsonl"`.
    claude_config_dir = agent.get_claude_config_dir()
    matches = list(claude_config_dir.rglob(target_session_id + ".jsonl"))
    assert len(matches) == 1, (
        f"Expected exactly 1 session file under {claude_config_dir}, found {len(matches)}: {matches}"
    )


def test_on_after_provisioning_raises_when_session_not_found(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """on_after_provisioning should raise UserInputError when session ID is not found."""
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)

    (Path.home() / ".claude" / "projects" / "some-project").mkdir(parents=True)

    agent_state_dir = agent._get_agent_dir()
    agent_state_dir.mkdir(parents=True, exist_ok=True)

    options = CreateAgentOptions(
        agent_type=AgentTypeName("claude"),
        adopt_session=("nonexistent-session",),
    )

    with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": ""}):
        with pytest.raises(UserInputError, match="Session nonexistent-session not found"):
            agent.on_after_provisioning(host=host, options=options, mngr_ctx=temp_mngr_ctx)


@pytest.mark.rsync
def test_on_after_provisioning_finds_session_despite_claude_config_dir(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """Session lookup should find sessions in ~/.claude/ even when CLAUDE_CONFIG_DIR points elsewhere."""
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)

    # Session lives under ~/.claude/ (HOME is already a temp dir via autouse fixture)
    project_dir = Path.home() / ".claude" / "projects" / "test-project"
    project_dir.mkdir(parents=True)
    target_session_id = "session-in-home-dir"
    (project_dir / f"{target_session_id}.jsonl").write_text('{"type":"message"}\n')

    # CLAUDE_CONFIG_DIR points to an agent-specific dir that does NOT have the session
    agent_config_dir = tmp_path / "agent_claude_config"
    (agent_config_dir / "projects").mkdir(parents=True)

    agent_state_dir = agent._get_agent_dir()
    agent_state_dir.mkdir(parents=True, exist_ok=True)

    options = CreateAgentOptions(
        agent_type=AgentTypeName("claude"),
        adopt_session=(target_session_id,),
    )

    home_claude = str(Path.home() / ".claude")
    with patch.dict(
        "os.environ", {"CLAUDE_CONFIG_DIR": str(agent_config_dir), "ORIGINAL_CLAUDE_CONFIG_DIR": home_claude}
    ):
        agent.on_after_provisioning(host=host, options=options, mngr_ctx=temp_mngr_ctx)

    assert (agent_state_dir / "claude_session_id").read_text() == target_session_id
    expected_project_name = encode_claude_project_dir_name(agent.work_dir)
    dest_session_file = (
        agent.get_claude_config_dir() / "projects" / expected_project_name / f"{target_session_id}.jsonl"
    )
    assert dest_session_file.exists()


@pytest.mark.rsync
def test_on_after_provisioning_adopts_session_from_jsonl_path(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """on_after_provisioning should accept a .jsonl file path and extract the session ID."""
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)

    # Create a session file at an arbitrary path
    project_dir = tmp_path / "my_sessions" / "some-project"
    project_dir.mkdir(parents=True)
    session_file = project_dir / "abc123-def456.jsonl"
    session_file.write_text('{"type":"message"}\n')

    agent_state_dir = agent._get_agent_dir()
    agent_state_dir.mkdir(parents=True, exist_ok=True)

    options = CreateAgentOptions(
        agent_type=AgentTypeName("claude"),
        adopt_session=(str(session_file),),
    )

    agent.on_after_provisioning(host=host, options=options, mngr_ctx=temp_mngr_ctx)

    # Session ID should be the stem of the file
    assert (agent_state_dir / "claude_session_id").read_text() == "abc123-def456"

    # Project dir should be copied into the agent's work_dir-based project dir
    expected_project_name = encode_claude_project_dir_name(agent.work_dir)
    dest_project_dir = agent.get_claude_config_dir() / "projects" / expected_project_name
    assert (dest_project_dir / "abc123-def456.jsonl").exists()


@pytest.mark.rsync
def test_on_after_provisioning_adopts_session_from_preserved_agent(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """A session ID is resolvable against a destroyed agent's preserved session files."""
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)

    # Mirror the on-disk layout that preserve_sessions_on_destroy produces:
    # <local_host_dir>/preserved/<name>--<id>/plugin/claude/anthropic/projects/<encoded>/<sid>.jsonl
    local_host_dir = Path(temp_mngr_ctx.config.default_host_dir).expanduser()
    preserved_project_dir = (
        local_host_dir
        / "preserved"
        / "old-agent--00000000-0000-0000-0000-000000000001"
        / "plugin"
        / "claude"
        / "anthropic"
        / "projects"
        / "encoded-source-project"
    )
    preserved_project_dir.mkdir(parents=True)
    target_session_id = "preserved-session-id"
    (preserved_project_dir / f"{target_session_id}.jsonl").write_text('{"type":"message"}\n')

    agent_state_dir = agent._get_agent_dir()
    agent_state_dir.mkdir(parents=True, exist_ok=True)

    options = CreateAgentOptions(
        agent_type=AgentTypeName("claude"),
        adopt_session=(target_session_id,),
    )

    with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": ""}):
        agent.on_after_provisioning(host=host, options=options, mngr_ctx=temp_mngr_ctx)

    assert (agent_state_dir / "claude_session_id").read_text() == target_session_id
    expected_project_name = encode_claude_project_dir_name(agent.work_dir)
    dest_session_file = (
        agent.get_claude_config_dir() / "projects" / expected_project_name / f"{target_session_id}.jsonl"
    )
    assert dest_session_file.exists()


@pytest.mark.rsync
def test_on_after_provisioning_adopts_session_from_live_mngr_agent(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """A session ID is resolvable against another live local mngr agent's per-agent config dir."""
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)

    # Another live agent's session lives under its per-agent state dir:
    # <local_host_dir>/agents/<other-id>/plugin/claude/anthropic/projects/<encoded>/<sid>.jsonl
    local_host_dir = Path(temp_mngr_ctx.config.default_host_dir).expanduser()
    other_agent_project_dir = (
        local_host_dir
        / "agents"
        / "11111111-1111-1111-1111-111111111111"
        / "plugin"
        / "claude"
        / "anthropic"
        / "projects"
        / "encoded-source-project"
    )
    other_agent_project_dir.mkdir(parents=True)
    target_session_id = "live-mngr-session-id"
    (other_agent_project_dir / f"{target_session_id}.jsonl").write_text('{"type":"message"}\n')

    agent_state_dir = agent._get_agent_dir()
    agent_state_dir.mkdir(parents=True, exist_ok=True)

    options = CreateAgentOptions(
        agent_type=AgentTypeName("claude"),
        adopt_session=(target_session_id,),
    )

    with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": ""}):
        agent.on_after_provisioning(host=host, options=options, mngr_ctx=temp_mngr_ctx)

    assert (agent_state_dir / "claude_session_id").read_text() == target_session_id
    expected_project_name = encode_claude_project_dir_name(agent.work_dir)
    dest_session_file = (
        agent.get_claude_config_dir() / "projects" / expected_project_name / f"{target_session_id}.jsonl"
    )
    assert dest_session_file.exists()


@pytest.mark.rsync
def test_on_after_provisioning_multi_adopt_resumes_last(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """``--adopt A B`` copies both sessions in but resumes the *last* (B).

    Claude can only resume one session at a time, so every named session is
    made available under the destination's encoded project dir while
    ``claude_session_id`` is set to the last named session.
    """
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)

    # Two sessions in distinct source project dirs (so both dirs are copied).
    first_project = Path.home() / ".claude" / "projects" / "first-project"
    second_project = Path.home() / ".claude" / "projects" / "second-project"
    first_project.mkdir(parents=True)
    second_project.mkdir(parents=True)
    first_session_id = "first-session-id"
    second_session_id = "second-session-id"
    (first_project / f"{first_session_id}.jsonl").write_text('{"type":"first"}\n')
    (second_project / f"{second_session_id}.jsonl").write_text('{"type":"second"}\n')

    agent_state_dir = agent._get_agent_dir()
    agent_state_dir.mkdir(parents=True, exist_ok=True)

    options = CreateAgentOptions(
        agent_type=AgentTypeName("claude"),
        adopt_session=(first_session_id, second_session_id),
    )

    with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": ""}):
        agent.on_after_provisioning(host=host, options=options, mngr_ctx=temp_mngr_ctx)

    # The last named session is the one resumed.
    assert (agent_state_dir / "claude_session_id").read_text() == second_session_id

    # Both sessions are available under the destination's encoded project dir.
    expected_project_name = encode_claude_project_dir_name(agent.work_dir)
    dest_project_dir = agent.get_claude_config_dir() / "projects" / expected_project_name
    assert (dest_project_dir / f"{first_session_id}.jsonl").exists()
    assert (dest_project_dir / f"{second_session_id}.jsonl").exists()


# =============================================================================
# Clone session-adoption tests
#
# Drive both halves of the clone flow as it runs in production:
# (1) ``_transfer_source_plugin_data`` rsyncs source plugin/ over;
# (2) ``_adopt_cloned_session`` (later, from on_after_provisioning) renames
#     the project subdir, drops the stale sessions-index, writes
#     claude_session_id.
# =============================================================================


def _run_clone_adoption(agent: ClaudeAgent, host: OnlineHostInterface, source_dir: Path) -> None:
    """Drive the clone flow end-to-end against the test agent/host.

    Mirrors production: rsync the source plugin/, then run ``adopt_session`` with the
    clone location so ``_adopt_cloned_session`` rekeys the subdir and the resume step
    finalizes (writes ``claude_session_id``). Warns and adopts nothing when the clone
    has no resumable session.
    """
    location = HostLocation(host=host, path=source_dir)
    agent._transfer_source_plugin_data(location)
    options = CreateAgentOptions(agent_type=AgentTypeName("claude"), source_agent_state_location=location)
    agent.adopt_session(host, options, agent.mngr_ctx)


@pytest.mark.rsync
def test_clone_adoption_copies_plugin_dir(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """Top-of-state files (e.g. ``.claude.json``) under plugin/ are preserved
    as-is, and the agent's state dir's own ``data.json`` is untouched (only
    plugin/ is rsynced).
    """
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)

    dest_dir = agent._get_agent_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "data.json").write_text('{"id": "new-agent"}')

    # Create a source agent state dir with plugin data
    source_dir = tmp_path / "source_agent_state"
    source_dir.mkdir()
    (source_dir / "data.json").write_text('{"id": "old-agent"}')
    plugin_dir = source_dir / "plugin" / "claude" / "anthropic"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / ".claude.json").write_text('{"trust": true}')
    source_project_subdir = "source-encoded-work-dir"
    projects_dir = plugin_dir / "projects" / source_project_subdir
    projects_dir.mkdir(parents=True)
    (projects_dir / "session.jsonl").write_text('{"type":"message"}\n')

    _run_clone_adoption(agent, host, source_dir)

    # data.json (outside plugin/) untouched.
    assert json.loads((dest_dir / "data.json").read_text())["id"] == "new-agent"
    # Plugin files under plugin/ are preserved as-is.
    assert (dest_dir / "plugin" / "claude" / "anthropic" / ".claude.json").exists()
    # The session JSONL ended up under the destination's encoded work_dir,
    # not the source's -- verified more directly in
    # test_clone_adoption_rekeys_project_dir below.
    dest_project_name = encode_claude_project_dir_name(agent.work_dir)
    assert (dest_dir / "plugin" / "claude" / "anthropic" / "projects" / dest_project_name / "session.jsonl").exists()
    assert not (dest_dir / "plugin" / "claude" / "anthropic" / "projects" / source_project_subdir).exists()


def test_clone_adoption_warns_when_no_plugin_dir(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    log_warnings: list[str],
) -> None:
    """The rsync step is a no-op when the source has no plugin/ dir, so the
    subsequent adopt step finds no session JSONL. A ``--from`` clone is a
    workspace clone (carrying the session forward is a bonus), so that warns
    and adopts nothing rather than raising -- no ``claude_session_id`` is written.
    """
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)

    dest_dir = agent._get_agent_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)

    source_dir = tmp_path / "source_agent_state"
    source_dir.mkdir()

    _run_clone_adoption(agent, host, source_dir)

    assert any("no session JSONL found at source" in message for message in log_warnings), log_warnings
    assert not (dest_dir / "claude_session_id").exists()


@pytest.mark.rsync
def test_clone_adoption_rekeys_project_dir(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """After clone adoption, the project subdir under plugin/claude/anthropic/projects/
    should be renamed from the source agent's encoded work_dir to the
    destination agent's, so ``claude --resume`` on the destination finds the
    session JSONL. The destination's ``claude_session_id`` should be set to
    the JSONL filename's stem so the startup command's
    ``claude --resume "$MAIN_CLAUDE_SESSION_ID"`` targets that file.
    """
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)
    dest_dir = agent._get_agent_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)

    source_dir = tmp_path / "source_agent_state"
    plugin_dir = source_dir / "plugin" / "claude" / "anthropic"
    # Project subdir name uses the source's (different) work_dir encoding.
    src_project = plugin_dir / "projects" / "-Users-ev-some-source-workdir"
    src_project.mkdir(parents=True)
    session_id = "11111111-2222-3333-4444-555555555555"
    (src_project / f"{session_id}.jsonl").write_text('{"type":"message"}\n')

    _run_clone_adoption(agent, host, source_dir)

    dest_project_name = encode_claude_project_dir_name(agent.work_dir)
    rekeyed = dest_dir / "plugin" / "claude" / "anthropic" / "projects" / dest_project_name
    assert (rekeyed / f"{session_id}.jsonl").exists(), (
        f"Expected session JSONL under {rekeyed}, dest projects dir is "
        f"{[p.name for p in (dest_dir / 'plugin' / 'claude' / 'anthropic' / 'projects').iterdir()]}"
    )
    # Source-encoded subdir should be gone.
    assert not (dest_dir / "plugin" / "claude" / "anthropic" / "projects" / "-Users-ev-some-source-workdir").exists()
    # Session id should be the JSONL stem so ``claude --resume`` finds the file.
    assert (dest_dir / "claude_session_id").read_text().strip() == session_id


@pytest.mark.rsync
def test_clone_adoption_uses_jsonl_filename_not_source_session_id_file(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """``claude_session_id`` on the destination must be the *actual* session
    JSONL filename's stem, not the contents of the source's
    ``claude_session_id`` file. When the source ran ``claude -p``, claude's
    ``--session-id`` flag was ignored and claude auto-generated its own id,
    so the source's ``claude_session_id`` file (which the SessionStart hook
    default-fills with the agent UUID) doesn't agree with the JSONL on disk.
    The JSONL filename is the ground truth for what ``claude --resume <id>``
    can find.
    """
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)
    dest_dir = agent._get_agent_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)

    source_dir = tmp_path / "source_agent_state"
    plugin_dir = source_dir / "plugin" / "claude" / "anthropic"
    src_project = plugin_dir / "projects" / "-source-encoded"
    src_project.mkdir(parents=True)

    actual_session_id_from_jsonl = "aaaa1111-bbbb-2222-cccc-333344445555"
    stale_agent_uuid = "ffffffff-eeee-dddd-cccc-bbbbbbbbbbbb"
    (src_project / f"{actual_session_id_from_jsonl}.jsonl").write_text('{"type":"message"}\n')
    # The source's claude_session_id contains a *different* id (the agent
    # UUID written by the SessionStart hook default).
    (source_dir / "claude_session_id").write_text(f"{stale_agent_uuid}\n")
    (source_dir / "claude_session_id_history").write_text(f"{stale_agent_uuid} startup\n")

    _run_clone_adoption(agent, host, source_dir)

    # Destination's claude_session_id must be the JSONL filename's stem,
    # not the source's claude_session_id contents.
    assert (dest_dir / "claude_session_id").read_text().strip() == actual_session_id_from_jsonl, (
        f"claude_session_id on destination should be {actual_session_id_from_jsonl} "
        f"(JSONL stem), got {(dest_dir / 'claude_session_id').read_text().strip()}"
    )
    # History is still carried verbatim for traceability.
    assert stale_agent_uuid in (dest_dir / "claude_session_id_history").read_text()


@pytest.mark.rsync
def test_adopt_and_from_resumes_clone(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """``--adopt A --from X`` copies both A and the clone, then resumes the *clone*.

    The explicit ``--adopt`` session is made available alongside the clone, but
    the resumed session (``claude_session_id``) is the clone's, since a ``--from``
    clone is the session the new agent is meant to continue.
    """
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)
    dest_dir = agent._get_agent_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Explicit ``--adopt`` session lives under ~/.claude/.
    adopt_project = Path.home() / ".claude" / "projects" / "adopt-project"
    adopt_project.mkdir(parents=True)
    adopt_session_id = "explicit-adopt-session-id"
    (adopt_project / f"{adopt_session_id}.jsonl").write_text('{"type":"adopt"}\n')

    # ``--from`` clone source: a separate agent state dir whose plugin/ holds
    # the session to clone.
    source_dir = tmp_path / "source_agent_state"
    src_project = source_dir / "plugin" / "claude" / "anthropic" / "projects" / "-Users-ev-some-source-workdir"
    src_project.mkdir(parents=True)
    clone_session_id = "11111111-2222-3333-4444-555555555555"
    (src_project / f"{clone_session_id}.jsonl").write_text('{"type":"clone"}\n')

    location = HostLocation(host=host, path=source_dir)
    agent._transfer_source_plugin_data(location)
    options = CreateAgentOptions(
        agent_type=AgentTypeName("claude"),
        adopt_session=(adopt_session_id,),
        source_agent_state_location=location,
    )

    with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": ""}):
        agent.adopt_session(host, options, agent.mngr_ctx)

    expected_project_name = encode_claude_project_dir_name(agent.work_dir)
    dest_project_dir = agent.get_claude_config_dir() / "projects" / expected_project_name
    # Both sessions are available; the clone is the one resumed.
    assert (dest_project_dir / f"{adopt_session_id}.jsonl").exists()
    assert (dest_project_dir / f"{clone_session_id}.jsonl").exists()
    assert (dest_dir / "claude_session_id").read_text().strip() == clone_session_id


@pytest.mark.rsync
def test_clone_adoption_merges_into_existing_target(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """When the source has a project subdir whose name coincidentally matches
    the destination's encoded work_dir AND a separate, more-recently-active
    source-encoded subdir, the rsync brings both over. With *distinct* session-id
    filenames, the rekey merges the source-encoded subdir's files into the
    pre-existing target rather than clobbering it: both sessions coexist under
    the destination's encoded work_dir, and the latest is resumed.
    """
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)
    dest_dir = agent._get_agent_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)

    source_dir = tmp_path / "source_agent_state"
    plugin_dir = source_dir / "plugin" / "claude" / "anthropic"

    # (a) A project subdir whose name happens to equal the destination's
    # encoded work_dir -- this becomes the pre-existing target after rsync.
    dest_project_name = encode_claude_project_dir_name(agent.work_dir)
    coincident_subdir = plugin_dir / "projects" / dest_project_name
    coincident_subdir.mkdir(parents=True)
    older_session_id = "00000000-0000-0000-0000-000000000001"
    older_jsonl = coincident_subdir / f"{older_session_id}.jsonl"
    older_jsonl.write_text('{"type":"older"}\n')

    # (b) A separate source-encoded subdir holding the most-recently-active
    # session JSONL so ``ls -t`` picks it as latest_on_source. Its filename
    # differs from the target's, so the merge is non-destructive.
    src_project = plugin_dir / "projects" / "-Users-ev-some-source-workdir"
    src_project.mkdir(parents=True)
    newer_session_id = "11111111-2222-3333-4444-555555555555"
    newer_jsonl = src_project / f"{newer_session_id}.jsonl"
    newer_jsonl.write_text('{"type":"newer"}\n')

    # Set explicit mtimes so ``ls -t`` ordering is deterministic regardless
    # of write-order timing.
    os.utime(older_jsonl, (1_000_000_000, 1_000_000_000))
    os.utime(newer_jsonl, (2_000_000_000, 2_000_000_000))

    _run_clone_adoption(agent, host, source_dir)

    dest_projects = dest_dir / "plugin" / "claude" / "anthropic" / "projects"
    # Both sessions now coexist under the destination's encoded work_dir.
    assert (dest_projects / dest_project_name / f"{older_session_id}.jsonl").exists()
    assert (dest_projects / dest_project_name / f"{newer_session_id}.jsonl").exists()
    # The source-encoded subdir was emptied and removed by the merge.
    assert not (dest_projects / "-Users-ev-some-source-workdir").exists()
    # The most-recently-active session is the one resumed.
    assert (dest_dir / "claude_session_id").read_text().strip() == newer_session_id


@pytest.mark.rsync
def test_clone_adoption_refuses_per_file_collision(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """A genuine per-file collision -- the same session-id filename present in
    both the source-encoded subdir and the pre-existing target -- would lose
    data on merge. _adopt_cloned_session refuses and raises ``AgentStartError``
    without writing claude_session_id, leaving both subdirs intact.
    """
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)
    dest_dir = agent._get_agent_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)

    source_dir = tmp_path / "source_agent_state"
    plugin_dir = source_dir / "plugin" / "claude" / "anthropic"

    # Same session-id filename in both the target-named subdir and the
    # source-encoded subdir -- merging would overwrite the target's copy.
    colliding_session_id = "11111111-2222-3333-4444-555555555555"

    dest_project_name = encode_claude_project_dir_name(agent.work_dir)
    coincident_subdir = plugin_dir / "projects" / dest_project_name
    coincident_subdir.mkdir(parents=True)
    target_jsonl = coincident_subdir / f"{colliding_session_id}.jsonl"
    target_jsonl.write_text('{"type":"target"}\n')

    src_project = plugin_dir / "projects" / "-Users-ev-some-source-workdir"
    src_project.mkdir(parents=True)
    source_jsonl = src_project / f"{colliding_session_id}.jsonl"
    source_jsonl.write_text('{"type":"source"}\n')

    # Make the source copy the most-recently-active so ``ls -t`` selects it.
    os.utime(target_jsonl, (1_000_000_000, 1_000_000_000))
    os.utime(source_jsonl, (2_000_000_000, 2_000_000_000))

    with pytest.raises(AgentStartError, match="already exist in the target"):
        _run_clone_adoption(agent, host, source_dir)

    dest_projects = dest_dir / "plugin" / "claude" / "anthropic" / "projects"
    # Both subdirs survive: the merge was refused, no clobber happened.
    assert (dest_projects / dest_project_name / f"{colliding_session_id}.jsonl").exists()
    assert (dest_projects / "-Users-ev-some-source-workdir" / f"{colliding_session_id}.jsonl").exists()
    # claude_session_id was NOT written: the clone raised before finalize.
    assert not (dest_dir / "claude_session_id").exists()


# =============================================================================
# _rewrite_installed_plugins_paths Tests
# =============================================================================


def test_rewrite_installed_plugins_paths_rebases_install_paths() -> None:
    """installPath values under local_claude_dir are rebased onto remote_config_dir."""
    local_claude_dir = Path("/Users/testuser/.claude")
    remote_config_dir = Path("/mngr/agents/abc123/plugin/claude/anthropic")
    content = json.dumps(
        {
            "version": 2,
            "plugins": {
                "my-plugin@my-org": [
                    {
                        "scope": "user",
                        "installPath": "/Users/testuser/.claude/plugins/cache/my-org/my-plugin/1.0.0",
                        "version": "1.0.0",
                    }
                ]
            },
        }
    )

    result = json.loads(_rewrite_installed_plugins_paths(content, local_claude_dir, remote_config_dir))

    entry = result["plugins"]["my-plugin@my-org"][0]
    assert entry["installPath"] == "/mngr/agents/abc123/plugin/claude/anthropic/plugins/cache/my-org/my-plugin/1.0.0"


def test_rewrite_installed_plugins_paths_handles_multiple_plugins() -> None:
    """All plugins in the file have their installPath rewritten."""
    local_claude_dir = Path("/home/user/.claude")
    remote_config_dir = Path("/remote/config")
    content = json.dumps(
        {
            "version": 2,
            "plugins": {
                "plugin-a@org-a": [
                    {
                        "installPath": "/home/user/.claude/plugins/cache/org-a/plugin-a/1.0.0",
                        "version": "1.0.0",
                    }
                ],
                "plugin-b@org-b": [
                    {
                        "installPath": "/home/user/.claude/plugins/cache/org-b/plugin-b/2.0.0",
                        "version": "2.0.0",
                    }
                ],
            },
        }
    )

    result = json.loads(_rewrite_installed_plugins_paths(content, local_claude_dir, remote_config_dir))

    assert result["plugins"]["plugin-a@org-a"][0]["installPath"] == "/remote/config/plugins/cache/org-a/plugin-a/1.0.0"
    assert result["plugins"]["plugin-b@org-b"][0]["installPath"] == "/remote/config/plugins/cache/org-b/plugin-b/2.0.0"


def test_rewrite_installed_plugins_paths_rewrites_non_matching_prefix_best_effort() -> None:
    """installPath values that don't start with the expected prefix are rewritten best-effort."""
    local_claude_dir = Path("/Users/testuser/.claude")
    remote_config_dir = Path("/remote/config")
    content = json.dumps(
        {
            "version": 2,
            "plugins": {
                "other-plugin@other-org": [
                    {
                        "installPath": "/some/other/path/plugins/cache/other-org/other-plugin/1.0.0",
                        "version": "1.0.0",
                    }
                ]
            },
        }
    )

    result = json.loads(_rewrite_installed_plugins_paths(content, local_claude_dir, remote_config_dir))
    entry = result["plugins"]["other-plugin@other-org"][0]
    assert entry["installPath"] == "/remote/config/plugins/cache/other-org/other-plugin/1.0.0"


def test_rewrite_installed_plugins_paths_preserves_other_fields() -> None:
    """Fields other than installPath are preserved unchanged."""
    local_claude_dir = Path("/Users/testuser/.claude")
    remote_config_dir = Path("/remote/config")
    content = json.dumps(
        {
            "version": 2,
            "plugins": {
                "my-plugin@my-org": [
                    {
                        "scope": "user",
                        "installPath": "/Users/testuser/.claude/plugins/cache/my-org/my-plugin/1.0.0",
                        "version": "1.0.0",
                        "installedAt": "2026-01-14T22:13:26.484Z",
                        "gitCommitSha": "abc123",
                    }
                ]
            },
        }
    )

    result = json.loads(_rewrite_installed_plugins_paths(content, local_claude_dir, remote_config_dir))

    entry = result["plugins"]["my-plugin@my-org"][0]
    assert entry["scope"] == "user"
    assert entry["version"] == "1.0.0"
    assert entry["installedAt"] == "2026-01-14T22:13:26.484Z"
    assert entry["gitCommitSha"] == "abc123"
    assert result["version"] == 2


def test_rewrite_installed_plugins_paths_handles_empty_plugins() -> None:
    """An installed_plugins.json with no plugins is handled gracefully."""
    local_claude_dir = Path("/Users/testuser/.claude")
    remote_config_dir = Path("/remote/config")
    content = json.dumps({"version": 2, "plugins": {}})

    result = json.loads(_rewrite_installed_plugins_paths(content, local_claude_dir, remote_config_dir))

    assert result["version"] == 2
    assert result["plugins"] == {}


def test_rewrite_installed_plugins_paths_rewrites_similar_prefix_best_effort() -> None:
    """A path like /Users/testuser/.claude2/ is rewritten best-effort via the plugins/ marker."""
    local_claude_dir = Path("/Users/testuser/.claude")
    remote_config_dir = Path("/remote/config")
    content = json.dumps(
        {
            "version": 2,
            "plugins": {
                "plugin@org": [
                    {
                        "installPath": "/Users/testuser/.claude2/plugins/cache/org/plugin/1.0.0",
                        "version": "1.0.0",
                    }
                ]
            },
        }
    )

    result = json.loads(_rewrite_installed_plugins_paths(content, local_claude_dir, remote_config_dir))
    entry = result["plugins"]["plugin@org"][0]
    assert entry["installPath"] == "/remote/config/plugins/cache/org/plugin/1.0.0"


def test_rewrite_installed_plugins_paths_best_effort_for_mngr_agent_path() -> None:
    """installPath from an mngr agent is rewritten best-effort via /plugins/ marker."""
    local_claude_dir = Path("/Users/testuser/.claude")
    remote_config_dir = Path("/remote/config")
    stale_path = (
        "/Users/testuser/.mngr/agents/agent-abc123/plugin/claude/anthropic/plugins/cache/my-org/my-plugin/1.0.0"
    )
    content = json.dumps(
        {
            "version": 2,
            "plugins": {
                "my-plugin@my-org": [
                    {
                        "installPath": stale_path,
                        "version": "1.0.0",
                    }
                ]
            },
        }
    )

    result = json.loads(_rewrite_installed_plugins_paths(content, local_claude_dir, remote_config_dir))

    assert (
        result["plugins"]["my-plugin@my-org"][0]["installPath"]
        == "/remote/config/plugins/cache/my-org/my-plugin/1.0.0"
    )


# =============================================================================
# _generate_installed_plugins_content Tests
# =============================================================================


def test_generate_installed_plugins_content_rewrites_paths(tmp_path: Path) -> None:
    """_generate_installed_plugins_content rewrites installPaths from source to target."""
    source_claude_dir = tmp_path / "source_claude"
    plugins_dir = source_claude_dir / "plugins"
    plugins_dir.mkdir(parents=True)
    target_config_dir = tmp_path / "target"

    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "test@org": [
                        {
                            "installPath": f"{source_claude_dir}/plugins/cache/org/test/1.0.0",
                            "version": "1.0.0",
                        }
                    ]
                },
            }
        )
    )

    content = _generate_installed_plugins_content(source_claude_dir, target_config_dir)

    assert content is not None
    result = json.loads(content)
    assert result["plugins"]["test@org"][0]["installPath"] == str(
        target_config_dir / "plugins" / "cache" / "org" / "test" / "1.0.0"
    )


def test_generate_installed_plugins_content_returns_none_when_no_file(tmp_path: Path) -> None:
    """_generate_installed_plugins_content returns None when file does not exist."""
    source_claude_dir = tmp_path / "source_claude"
    source_claude_dir.mkdir()
    target_config_dir = tmp_path / "target"

    result = _generate_installed_plugins_content(source_claude_dir, target_config_dir)
    assert result is None


# =============================================================================
# _rewrite_known_marketplaces_paths / _generate_known_marketplaces_content Tests
# =============================================================================


def test_rewrite_known_marketplaces_paths_rebases_install_location() -> None:
    """installLocation values under local_claude_dir are rebased onto remote_config_dir."""
    local_claude_dir = Path("/Users/testuser/.claude")
    remote_config_dir = Path("/mngr/agents/abc123/plugin/claude/anthropic")
    content = json.dumps(
        {
            "imbue-code-guardian": {
                "source": {"source": "github", "repo": "imbue-ai/code-guardian"},
                "installLocation": "/Users/testuser/.claude/plugins/marketplaces/imbue-code-guardian",
                "lastUpdated": "2026-04-08T23:35:41.300Z",
            }
        }
    )

    result = json.loads(_rewrite_known_marketplaces_paths(content, local_claude_dir, remote_config_dir))

    assert (
        result["imbue-code-guardian"]["installLocation"]
        == "/mngr/agents/abc123/plugin/claude/anthropic/plugins/marketplaces/imbue-code-guardian"
    )


def test_rewrite_known_marketplaces_paths_handles_multiple_marketplaces() -> None:
    """All marketplaces in the file have their installLocation rewritten."""
    local_claude_dir = Path("/home/user/.claude")
    remote_config_dir = Path("/remote/config")
    content = json.dumps(
        {
            "org-a": {
                "installLocation": "/home/user/.claude/plugins/marketplaces/org-a",
            },
            "org-b": {
                "installLocation": "/home/user/.claude/plugins/marketplaces/org-b",
            },
        }
    )

    result = json.loads(_rewrite_known_marketplaces_paths(content, local_claude_dir, remote_config_dir))

    assert result["org-a"]["installLocation"] == "/remote/config/plugins/marketplaces/org-a"
    assert result["org-b"]["installLocation"] == "/remote/config/plugins/marketplaces/org-b"


def test_rewrite_known_marketplaces_paths_best_effort_for_non_matching_prefix() -> None:
    """installLocation from a different base path is rewritten using /plugins/ marker."""
    local_claude_dir = Path("/Users/testuser/.claude")
    remote_config_dir = Path("/remote/config")
    content = json.dumps(
        {
            "best-of-n": {
                "installLocation": "/Users/other/.mngr/agents/old-agent/plugin/claude/anthropic/plugins/marketplaces/best-of-n",
            }
        }
    )

    result = json.loads(_rewrite_known_marketplaces_paths(content, local_claude_dir, remote_config_dir))

    assert result["best-of-n"]["installLocation"] == "/remote/config/plugins/marketplaces/best-of-n"


def test_rewrite_known_marketplaces_paths_preserves_other_fields() -> None:
    """Fields other than installLocation are preserved unchanged."""
    local_claude_dir = Path("/Users/testuser/.claude")
    remote_config_dir = Path("/remote/config")
    content = json.dumps(
        {
            "my-marketplace": {
                "source": {"source": "github", "repo": "org/repo"},
                "installLocation": "/Users/testuser/.claude/plugins/marketplaces/my-marketplace",
                "lastUpdated": "2026-01-01T00:00:00.000Z",
            }
        }
    )

    result = json.loads(_rewrite_known_marketplaces_paths(content, local_claude_dir, remote_config_dir))

    assert result["my-marketplace"]["source"] == {"source": "github", "repo": "org/repo"}
    assert result["my-marketplace"]["lastUpdated"] == "2026-01-01T00:00:00.000Z"


def test_generate_known_marketplaces_content_rewrites_paths(tmp_path: Path) -> None:
    """_generate_known_marketplaces_content rewrites installLocation from source to target."""
    source_claude_dir = tmp_path / "source_claude"
    plugins_dir = source_claude_dir / "plugins"
    plugins_dir.mkdir(parents=True)
    target_config_dir = tmp_path / "target"

    (plugins_dir / "known_marketplaces.json").write_text(
        json.dumps(
            {
                "test-marketplace": {
                    "installLocation": f"{source_claude_dir}/plugins/marketplaces/test-marketplace",
                }
            }
        )
    )

    content = _generate_known_marketplaces_content(source_claude_dir, target_config_dir)

    assert content is not None
    result = json.loads(content)
    assert result["test-marketplace"]["installLocation"] == str(
        target_config_dir / "plugins" / "marketplaces" / "test-marketplace"
    )


def test_generate_known_marketplaces_content_returns_none_when_no_file(tmp_path: Path) -> None:
    """_generate_known_marketplaces_content returns None when file does not exist."""
    source_claude_dir = tmp_path / "source_claude"
    source_claude_dir.mkdir()
    target_config_dir = tmp_path / "target"

    result = _generate_known_marketplaces_content(source_claude_dir, target_config_dir)
    assert result is None


# =============================================================================
# get_files_for_deploy sentinel rewrite Tests
# =============================================================================


def test_get_files_for_deploy_rewrites_install_paths_to_sentinel(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """get_files_for_deploy rewrites installPath values to use the sentinel prefix."""
    claude_dir = Path.home() / ".claude"
    plugins_dir = claude_dir / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "test@org": [
                        {
                            "installPath": f"{claude_dir}/plugins/cache/org/test/1.0.0",
                            "version": "1.0.0",
                        }
                    ]
                },
            }
        )
    )

    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=True, include_project_settings=False, repo_root=tmp_path
    )

    plugins_json_key = Path("~/.claude/plugins/installed_plugins.json")
    assert plugins_json_key in result
    plugins_json_content = result[plugins_json_key]
    assert isinstance(plugins_json_content, str)
    data = json.loads(plugins_json_content)
    assert data["plugins"]["test@org"][0]["installPath"] == "/__mngr_plugins_source__/plugins/cache/org/test/1.0.0"
    # No marker file should be present
    marker_key = Path("~/.claude/plugins/.installed_plugins_source_dir")
    assert marker_key not in result


def test_get_files_for_deploy_rewrites_marketplace_paths_to_sentinel(
    temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    """get_files_for_deploy rewrites installLocation values in known_marketplaces.json to use the sentinel prefix."""
    claude_dir = Path.home() / ".claude"
    plugins_dir = claude_dir / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    (plugins_dir / "known_marketplaces.json").write_text(
        json.dumps(
            {
                "imbue-code-guardian": {
                    "source": {"source": "github", "repo": "imbue-ai/code-guardian"},
                    "installLocation": f"{claude_dir}/plugins/marketplaces/imbue-code-guardian",
                    "lastUpdated": "2026-04-08T23:35:41.300Z",
                }
            }
        )
    )

    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=True, include_project_settings=False, repo_root=tmp_path
    )

    marketplaces_json_key = Path("~/.claude/plugins/known_marketplaces.json")
    assert marketplaces_json_key in result
    marketplaces_json_content = result[marketplaces_json_key]
    assert isinstance(marketplaces_json_content, str)
    data = json.loads(marketplaces_json_content)
    assert (
        data["imbue-code-guardian"]["installLocation"]
        == "/__mngr_plugins_source__/plugins/marketplaces/imbue-code-guardian"
    )


# =============================================================================
# _build_settings_json tests
# =============================================================================


def test_build_settings_json_unattended_defaults() -> None:
    """_build_settings_json with unattended context returns base settings with unattended flags."""
    ctx = ProvisioningContext(is_unattended=True)
    config = ClaudeAgentConfig(check_installation=False)
    content = _build_settings_json(Path.home() / ".claude", config, ctx, sync_local=False)
    data = json.loads(content)
    assert data["skipDangerousModePermissionPrompt"] is True
    assert "model" not in data
    assert data["fastMode"] is False


def test_build_settings_json_settings_overrides_model() -> None:
    """_build_settings_json with settings_overrides sets the model field."""
    ctx = ProvisioningContext(is_unattended=True)
    config = ClaudeAgentConfig(check_installation=False, settings_overrides={"model": "opus[1m]"})
    content = _build_settings_json(Path.home() / ".claude", config, ctx, sync_local=False)
    data = json.loads(content)
    assert data["model"] == "opus[1m]"


def test_build_settings_json_settings_overrides_fast_mode() -> None:
    """_build_settings_json with settings_overrides fastMode=True sets fastMode."""
    ctx = ProvisioningContext(is_unattended=True)
    config = ClaudeAgentConfig(check_installation=False, settings_overrides={"fastMode": True})
    content = _build_settings_json(Path.home() / ".claude", config, ctx, sync_local=False)
    data = json.loads(content)
    assert data["fastMode"] is True


def test_build_settings_json_settings_overrides_model_and_fast() -> None:
    """_build_settings_json with both model and fastMode in overrides."""
    ctx = ProvisioningContext(is_unattended=True)
    config = ClaudeAgentConfig(check_installation=False, settings_overrides={"model": "sonnet", "fastMode": True})
    content = _build_settings_json(Path.home() / ".claude", config, ctx, sync_local=False)
    data = json.loads(content)
    assert data["model"] == "sonnet"
    assert data["fastMode"] is True


def test_build_settings_json_overrides_win_over_local() -> None:
    """settings_overrides take precedence over local settings."""
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(json.dumps({"fastMode": True}))

    ctx = ProvisioningContext(is_unattended=True)
    config = ClaudeAgentConfig(check_installation=False, settings_overrides={"fastMode": True})
    content = _build_settings_json(claude_dir, config, ctx, sync_local=True)
    data = json.loads(content)
    assert data["fastMode"] is True


def test_build_settings_json_unattended_disables_fast_by_default() -> None:
    """Unattended context disables fastMode by default (API limitation)."""
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(json.dumps({"fastMode": True, "other": "value"}))

    ctx = ProvisioningContext(is_unattended=True)
    config = ClaudeAgentConfig(check_installation=False)
    content = _build_settings_json(claude_dir, config, ctx, sync_local=True)
    data = json.loads(content)
    assert data["fastMode"] is False
    assert data["other"] == "value"


def test_build_settings_json_local_context_no_flags() -> None:
    """Local (attended) context applies no extra flags."""
    ctx = ProvisioningContext(is_unattended=False)
    config = ClaudeAgentConfig(check_installation=False, settings_overrides={"model": "opus[1m]"})
    content = _build_settings_json(Path.home() / ".claude", config, ctx, sync_local=False)
    data = json.loads(content)
    assert data["model"] == "opus[1m]"
    # _generate_claude_home_settings provides skipDangerousModePermissionPrompt
    assert "skipDangerousModePermissionPrompt" in data
    # Local (attended) context does not force fastMode
    assert "fastMode" not in data


def test_build_settings_json_includes_readiness_hooks() -> None:
    """_build_settings_json folds mngr's always-on readiness hooks into settings.json."""
    ctx = ProvisioningContext(is_unattended=False)
    config = ClaudeAgentConfig(check_installation=False)
    content = _build_settings_json(Path.home() / ".claude", config, ctx, sync_local=False)
    data = json.loads(content)
    assert "SessionStart" in data["hooks"]


def test_build_settings_json_extend_override_preserves_siblings() -> None:
    """A deferred ``__extend`` settings_override merges onto the home base so a
    nested override preserves sibling keys (#1647).

    A base settings.json with ``permissions.defaultMode`` plus a settings_overrides
    patch ``permissions__extend = {allow__extend: [...]}`` must end up with BOTH
    keys -- the extend merges rather than replacing, so the home ``defaultMode``
    survives alongside the new ``allow``.
    """
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(json.dumps({"permissions": {"defaultMode": "acceptEdits"}}))

    ctx = ProvisioningContext(is_unattended=False)
    config = ClaudeAgentConfig(
        check_installation=False,
        settings_overrides={"permissions__extend": {"allow__extend": ["Bash(npm *)"]}},
    )
    content = _build_settings_json(claude_dir, config, ctx, sync_local=True)
    data = json.loads(content)
    assert data["permissions"]["defaultMode"] == "acceptEdits"
    assert data["permissions"]["allow"] == ["Bash(npm *)"]


def test_build_settings_json_bare_override_narrows_raises() -> None:
    """A *bare* settings_override that drops a non-empty sibling aggregate from the
    home base raises the narrowing error (bare = assign + narrowing guard).

    Base ``permissions = {defaultMode, allow:[X]}``; a bare override
    ``permissions = {allow:[Y]}`` drops ``defaultMode`` and ``allow:[X]`` -> error.
    """
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(
        json.dumps({"permissions": {"defaultMode": "acceptEdits", "allow": ["Bash(git *)"]}})
    )

    ctx = ProvisioningContext(is_unattended=False)
    config = ClaudeAgentConfig(
        check_installation=False,
        settings_overrides={"permissions": {"allow": ["Bash(npm *)"]}},
    )
    with pytest.raises(ConfigParseError, match="narrow") as exc_info:
        _build_settings_json(claude_dir, config, ctx, sync_local=True)
    # The error attributes both sides: the settings_overrides assigning, and the home
    # settings.json whose value would be dropped (named by path).
    message = str(exc_info.value)
    assert "settings_overrides" in message
    assert str(claude_dir / "settings.json") in message


def test_build_settings_json_narrowing_error_emits_mngr_merge_remediation() -> None:
    """The narrowing error surfaces through the full provision path as a Claude-compatible
    ``__mngr_merge`` remediation, never the internal suffix form. (The exact recursive patch
    is pinned in external_settings_test.)
    """
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(git *)"], "deny": ["Bash(rm *)"]}})
    )

    ctx = ProvisioningContext(is_unattended=False)
    config = ClaudeAgentConfig(
        check_installation=False,
        settings_overrides={"permissions": {"allow": ["Bash(npm *)"]}},
    )
    with pytest.raises(ConfigParseError) as exc_info:
        _build_settings_json(claude_dir, config, ctx, sync_local=True)
    message = str(exc_info.value)
    assert "__mngr_merge" in message
    assert "allow__extend" not in message


def test_build_settings_json_bare_override_narrows_allowed_with_escape_hatch() -> None:
    """The narrowing escape hatch lets a bare override replace the sibling aggregate."""
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(
        json.dumps({"permissions": {"defaultMode": "acceptEdits", "allow": ["Bash(git *)"]}})
    )

    ctx = ProvisioningContext(is_unattended=False)
    config = ClaudeAgentConfig(
        check_installation=False,
        settings_overrides={"permissions": {"allow": ["Bash(npm *)"]}},
    )
    content = _build_settings_json(claude_dir, config, ctx, sync_local=True, allow_narrowing=True)
    data = json.loads(content)
    assert data["permissions"] == {"allow": ["Bash(npm *)"]}


def test_build_settings_json_normalizes_extend_marker_in_home_base() -> None:
    """A literal ``__extend`` key in the home settings.json is stripped (normalized
    to a bare key) by folding ``B`` against an empty base before the patch fold.

    Home ``permissions__extend = {allow: [X]}`` -> base ``permissions = {allow:[X]}``;
    output has no ``__extend`` key.
    """
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(json.dumps({"permissions__extend": {"allow": ["Bash(git *)"]}}))

    ctx = ProvisioningContext(is_unattended=False)
    config = ClaudeAgentConfig(check_installation=False)
    content = _build_settings_json(claude_dir, config, ctx, sync_local=True)
    data = json.loads(content)
    assert "permissions__extend" not in data
    assert data["permissions"] == {"allow": ["Bash(git *)"]}
    assert "__extend" not in content


def test_build_settings_json_strips_mngr_merge_key_from_home_base() -> None:
    """A ``__mngr_merge`` key a user placed in their home settings.json is dropped: it is a
    no-op on the base (the floor merges onto nothing), and vanilla Claude ignores it, so it
    must not leak into the generated settings.json.
    """
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(git *)"]}, "__mngr_merge": {"permissions.allow": "extend"}})
    )

    ctx = ProvisioningContext(is_unattended=False)
    config = ClaudeAgentConfig(check_installation=False)
    content = _build_settings_json(claude_dir, config, ctx, sync_local=True)
    data = json.loads(content)
    assert "__mngr_merge" not in data
    assert data["permissions"] == {"allow": ["Bash(git *)"]}


def test_build_settings_json_stacked_suffix_override_does_not_raise() -> None:
    """A malformed stacked-suffix override key is handled gracefully: the node lift
    strips only the outermost suffix, so ``foo__extend__extend`` becomes a literal
    ``foo__extend`` field and is finalized into a plain key, with no spurious internal
    error. (The node ``finalize`` is total -- no marker can survive the fold -- so
    there is no leaked-marker assertion to false-fire on the literal key.)
    """
    ctx = ProvisioningContext(is_unattended=False)
    config = ClaudeAgentConfig(
        check_installation=False,
        settings_overrides={"foo__extend__extend": ["x"]},
    )
    content = _build_settings_json(Path.home() / ".claude", config, ctx, sync_local=False)
    data = json.loads(content)
    assert data["foo__extend"] == ["x"]


def test_build_settings_json_extend_hooks_concatenates_session_start() -> None:
    """A ``hooks__extend.SessionStart__extend`` override concatenates onto mngr's
    own readiness ``SessionStart`` group instead of replacing it -- both groups
    present -- while preserving the other hook events mngr installed.

    Writing the intermediate ``hooks`` key as ``hooks__extend`` is what merges
    onto the base hooks dict; a bare ``hooks`` would assign-and-narrow (dropping
    mngr's other hook events).
    """
    ctx = ProvisioningContext(is_unattended=False)
    user_group = {"matcher": "*", "hooks": [{"type": "command", "command": "echo user"}]}
    config = ClaudeAgentConfig(
        check_installation=False,
        settings_overrides={"hooks__extend": {"SessionStart__extend": [user_group]}},
    )
    content = _build_settings_json(Path.home() / ".claude", config, ctx, sync_local=False)
    data = json.loads(content)
    session_start = data["hooks"]["SessionStart"]
    # mngr's readiness group plus the user's appended group.
    assert len(session_start) >= 2
    assert user_group in session_start
    # Other hook events mngr installed (e.g. Notification) survive the extend.
    assert len(data["hooks"]) >= 2


def test_build_settings_json_output_has_no_extend_markers() -> None:
    """After the provision fold the built settings.json contains no ``__extend``
    key anywhere (every deferred marker is consumed against the concrete base)."""
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(json.dumps({"permissions": {"defaultMode": "acceptEdits"}}))

    ctx = ProvisioningContext(is_unattended=False)
    config = ClaudeAgentConfig(
        check_installation=False,
        settings_overrides={"permissions__extend": {"allow__extend": ["Bash(npm *)"]}},
    )
    content = _build_settings_json(claude_dir, config, ctx, sync_local=True)
    assert "__extend" not in content


def test_build_settings_json_nested_bare_inside_extend_narrows_raises() -> None:
    """The known-gap fix: a *bare* key nested inside an ``__extend`` value that
    drops a non-empty aggregate from the base raises the narrowing error.

    Base ``permissions = {defaultMode, allow:[X]}``; override
    ``permissions__extend = {allow: [Y]}`` -- the outer ``permissions__extend``
    merges (preserving ``defaultMode``), but the *nested bare* ``allow`` replaces
    the non-empty base ``allow:[X]``, dropping ``X``. The recursive fold now
    catches this nested bare drop (the old top-level-only check missed it).
    """
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(
        json.dumps({"permissions": {"defaultMode": "acceptEdits", "allow": ["Bash(git *)"]}})
    )

    ctx = ProvisioningContext(is_unattended=False)
    config = ClaudeAgentConfig(
        check_installation=False,
        settings_overrides={"permissions__extend": {"allow": ["Bash(npm *)"]}},
    )
    with pytest.raises(ConfigParseError, match="permissions.allow"):
        _build_settings_json(claude_dir, config, ctx, sync_local=True)


def test_build_settings_json_nested_bare_inside_extend_allowed_with_escape_hatch() -> None:
    """With the escape hatch, the nested-bare drop is permitted: ``defaultMode``
    (untouched by the nested patch) survives the outer extend while ``allow`` is
    replaced wholesale."""
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(
        json.dumps({"permissions": {"defaultMode": "acceptEdits", "allow": ["Bash(git *)"]}})
    )

    ctx = ProvisioningContext(is_unattended=False)
    config = ClaudeAgentConfig(
        check_installation=False,
        settings_overrides={"permissions__extend": {"allow": ["Bash(npm *)"]}},
    )
    content = _build_settings_json(claude_dir, config, ctx, sync_local=True, allow_narrowing=True)
    data = json.loads(content)
    assert data["permissions"]["defaultMode"] == "acceptEdits"
    assert data["permissions"]["allow"] == ["Bash(npm *)"]


def test_build_settings_json_assign_override_suppresses_narrowing() -> None:
    """A ``key__assign`` settings_override assigns like a bare key but suppresses the
    narrowing guard, so a drop that a bare key would reject is permitted without the
    global escape hatch."""
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(
        json.dumps({"permissions": {"defaultMode": "acceptEdits", "allow": ["Bash(git *)"]}})
    )

    ctx = ProvisioningContext(is_unattended=False)
    config = ClaudeAgentConfig(
        check_installation=False,
        settings_overrides={"permissions__assign": {"allow": ["Bash(npm *)"]}},
    )
    content = _build_settings_json(claude_dir, config, ctx, sync_local=True)
    data = json.loads(content)
    assert data["permissions"] == {"allow": ["Bash(npm *)"]}
    assert "__assign" not in content


def test_build_settings_json_nested_assign_inside_extend_suppresses_narrowing() -> None:
    """A nested ``allow__assign`` inside a ``permissions__extend`` suppresses the
    nested narrowing that a nested bare ``allow`` would raise, while ``defaultMode``
    still survives the outer extend."""
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(
        json.dumps({"permissions": {"defaultMode": "acceptEdits", "allow": ["Bash(git *)"]}})
    )

    ctx = ProvisioningContext(is_unattended=False)
    config = ClaudeAgentConfig(
        check_installation=False,
        settings_overrides={"permissions__extend": {"allow__assign": ["Bash(npm *)"]}},
    )
    content = _build_settings_json(claude_dir, config, ctx, sync_local=True)
    data = json.loads(content)
    assert data["permissions"]["defaultMode"] == "acceptEdits"
    assert data["permissions"]["allow"] == ["Bash(npm *)"]


def test_build_settings_json_static_override_suppresses_narrowing() -> None:
    """A ``Static*`` settings_override value replaces a non-empty base aggregate as a
    value-set, suppressing the narrowing guard -- here via a *bare* nested ``allow``
    whose value is a ``StaticList``, so only the ``Static*`` exemption (not
    ``__assign``) keeps it from raising."""
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(git *)", "Bash(ls)"]}}))

    ctx = ProvisioningContext(is_unattended=False)
    config = ClaudeAgentConfig(
        check_installation=False,
        settings_overrides={"permissions__extend": {"allow": StaticList(["Bash(npm *)"])}},
    )
    content = _build_settings_json(claude_dir, config, ctx, sync_local=True)
    data = json.loads(content)
    assert data["permissions"]["allow"] == ["Bash(npm *)"]


def test_compute_claude_json_flags_auto_approve_dismisses_dialogs_but_not_permission_mode() -> None:
    """--yes (is_auto_approve) on a local agent dismisses the cosmetic first-run dialogs, but does
    NOT accept bypass-permissions mode (that stays an unattended/auto_allow_permissions concern)."""
    flags = compute_claude_json_flags(ProvisioningContext(is_unattended=False, is_auto_approve=True))
    assert flags["effortCalloutDismissed"] is True
    assert flags["hasCompletedOnboarding"] is True
    assert flags["hasAcknowledgedCostThreshold"] is True
    assert "bypassPermissionsModeAccepted" not in flags


def test_compute_claude_json_flags_unattended_also_accepts_permission_mode() -> None:
    flags = compute_claude_json_flags(ProvisioningContext(is_unattended=True))
    assert flags["bypassPermissionsModeAccepted"] is True
    assert flags["hasCompletedOnboarding"] is True


def test_compute_claude_json_flags_attended_no_auto_approve_only_cost() -> None:
    flags = compute_claude_json_flags(ProvisioningContext(is_unattended=False, is_auto_approve=False))
    assert flags == {"hasAcknowledgedCostThreshold": True}


def test_compute_settings_json_flags_auto_approve_does_not_change_permissions() -> None:
    """--yes must not silently flip tool-permission settings; only a genuinely unattended agent does."""
    assert compute_settings_json_flags(ProvisioningContext(is_unattended=False, is_auto_approve=True)) == {}
    assert (
        compute_settings_json_flags(ProvisioningContext(is_unattended=True))["skipDangerousModePermissionPrompt"]
        is True
    )


def test_should_trust_work_dir_auto_approve() -> None:
    config = ClaudeAgentConfig(check_installation=False)
    assert should_trust_work_dir(config, ProvisioningContext(is_unattended=False, is_auto_approve=True)) is True
    assert should_trust_work_dir(config, ProvisioningContext(is_unattended=False, is_auto_approve=False)) is False


# =============================================================================
# Volume-based session preservation tests
# =============================================================================


def _make_offline_host_with_volume(
    local_provider: LocalProviderInstance, temp_mngr_ctx: MngrContext
) -> OfflineHostWithVolume:
    """Build an OfflineHostWithVolume backed by the local provider's host_dir volume.

    Uses the same ``make_readable_offline_host`` wrapping the providers use, so
    the volume is the local provider's (rooted at host_dir). Agent state lives at
    host_dir/agents/<id>/... and is read back through the HostFileReadInterface
    exactly as on a real stopped host.
    """
    now = datetime.now(timezone.utc)
    offline_host = OfflineHost(
        id=local_provider.host_id,
        certified_host_data=CertifiedHostData(
            host_id=str(local_provider.host_id),
            host_name="test-offline-host",
            created_at=now,
            updated_at=now,
        ),
        provider_instance=local_provider,
        mngr_ctx=temp_mngr_ctx,
    )
    host = make_readable_offline_host(offline_host)
    assert isinstance(host, OfflineHostWithVolume)
    return host


def _populate_volume_session_files(volume_root: Path, agent_id: AgentId) -> dict[str, Path]:
    """Create fake session files on a volume-backed directory for testing volume-based preservation.

    Mirrors the structure that a real agent would create on the host volume.
    Returns a dict mapping logical names to their on-volume paths.
    """
    agent_dir = volume_root / "agents" / str(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)

    # Session JSONL files in the per-agent Claude config dir
    projects_dir = agent_dir / "plugin" / "claude" / "anthropic" / "projects" / "encoded-project-name"
    projects_dir.mkdir(parents=True, exist_ok=True)
    session_file = projects_dir / "abc123.jsonl"
    session_file.write_text('{"type":"assistant","uuid":"u1"}\n')

    # Raw transcript
    raw_transcript_dir = agent_dir / "logs" / "claude_transcript"
    raw_transcript_dir.mkdir(parents=True, exist_ok=True)
    raw_transcript_file = raw_transcript_dir / "events.jsonl"
    raw_transcript_file.write_text('{"type":"message"}\n')

    # Common transcript
    common_transcript_dir = agent_dir / "events" / "claude" / "common_transcript"
    common_transcript_dir.mkdir(parents=True, exist_ok=True)
    common_transcript_file = common_transcript_dir / "events.jsonl"
    common_transcript_file.write_text('{"type":"user_message","text":"hello"}\n')

    # Session history
    history_file = agent_dir / "claude_session_id_history"
    history_file.write_text("abc123 create\n")

    return {
        "session_file": session_file,
        "raw_transcript_file": raw_transcript_file,
        "common_transcript_file": common_transcript_file,
        "history_file": history_file,
    }


def test_preserve_session_files_from_volume_all_data(
    local_provider: LocalProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """All 4 categories of session data are preserved from a volume-backed offline host."""
    agent_id = AgentId.generate()
    agent_name = AgentName("test-vol-agent")
    host = _make_offline_host_with_volume(local_provider, temp_mngr_ctx)

    files = _populate_volume_session_files(host.host_dir, agent_id)

    preserve_agent_data(
        _claude_preserved_items(is_shared_config=False),
        host,
        get_agent_state_dir_path(host.host_dir, agent_id),
        get_local_preserved_agent_dir(temp_mngr_ctx, agent_name, agent_id),
        temp_mngr_ctx,
    )

    dest_dir = get_local_preserved_agent_dir(temp_mngr_ctx, agent_name, agent_id)
    assert dest_dir.exists()

    # Session JSONL files at the mirrored config-dir path.
    preserved_projects = dest_dir / "plugin" / "claude" / "anthropic" / "projects"
    assert preserved_projects.exists()
    preserved_session_files = list(preserved_projects.rglob("*.jsonl"))
    assert len(preserved_session_files) == 1
    assert preserved_session_files[0].read_text() == files["session_file"].read_text()

    # Raw transcript
    preserved_raw = dest_dir / "logs" / "claude_transcript" / "events.jsonl"
    assert preserved_raw.exists()
    assert preserved_raw.read_text() == '{"type":"message"}\n'

    # Common transcript
    preserved_common = dest_dir / "events" / "claude" / "common_transcript" / "events.jsonl"
    assert preserved_common.exists()
    assert preserved_common.read_text() == '{"type":"user_message","text":"hello"}\n'

    # Session history
    preserved_history = dest_dir / "claude_session_id_history"
    assert preserved_history.exists()
    assert preserved_history.read_text() == "abc123 create\n"


def test_preserve_session_files_from_volume_partial_data(
    local_provider: LocalProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """Preservation works when only some session data exists on the volume."""
    agent_id = AgentId.generate()
    agent_name = AgentName("test-vol-partial")
    host = _make_offline_host_with_volume(local_provider, temp_mngr_ctx)

    # Only create the raw transcript
    agent_dir = host.host_dir / "agents" / str(agent_id)
    raw_transcript_dir = agent_dir / "logs" / "claude_transcript"
    raw_transcript_dir.mkdir(parents=True, exist_ok=True)
    (raw_transcript_dir / "events.jsonl").write_text('{"partial":"data"}\n')

    preserve_agent_data(
        _claude_preserved_items(is_shared_config=False),
        host,
        get_agent_state_dir_path(host.host_dir, agent_id),
        get_local_preserved_agent_dir(temp_mngr_ctx, agent_name, agent_id),
        temp_mngr_ctx,
    )

    dest_dir = get_local_preserved_agent_dir(temp_mngr_ctx, agent_name, agent_id)
    assert (dest_dir / "logs" / "claude_transcript" / "events.jsonl").exists()
    assert not (dest_dir / "plugin" / "claude" / "anthropic" / "projects").exists()
    assert not (dest_dir / "events" / "claude" / "common_transcript").exists()
    # History file was not created on the volume, so it is skipped (no dest file).
    assert not (dest_dir / "claude_session_id_history").exists()


def test_preserve_session_files_from_volume_no_data(
    local_provider: LocalProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """Empty volume produces no errors and no preserved dir."""
    agent_id = AgentId.generate()
    agent_name = AgentName("test-vol-empty")
    host = _make_offline_host_with_volume(local_provider, temp_mngr_ctx)
    # Create the agent dir but leave it empty
    (host.host_dir / "agents" / str(agent_id)).mkdir(parents=True, exist_ok=True)

    preserve_agent_data(
        _claude_preserved_items(is_shared_config=False),
        host,
        get_agent_state_dir_path(host.host_dir, agent_id),
        get_local_preserved_agent_dir(temp_mngr_ctx, agent_name, agent_id),
        temp_mngr_ctx,
    )

    dest_dir = get_local_preserved_agent_dir(temp_mngr_ctx, agent_name, agent_id)
    assert not dest_dir.exists()


def _write_docker_agent_record(
    host_id: HostId,
    volume_root: Path,
    agent_id: AgentId,
    agent_name: AgentName,
    *,
    isolate_local_config_dir: bool,
) -> None:
    """Persist a Claude agent record so the offline host's discover_agents() returns it.

    The docker host store reads agent records from ``host_state/<host_id>/*.json``
    on its state volume (rooted at ``volume_root``).
    """
    record_dir = volume_root / "host_state" / str(host_id)
    record_dir.mkdir(parents=True, exist_ok=True)
    (record_dir / f"{agent_id}.json").write_text(
        json.dumps(
            {
                "id": str(agent_id),
                "name": str(agent_name),
                "type": "claude",
                "agent_config": {
                    "preserve_sessions_on_destroy": True,
                    "isolate_local_config_dir": isolate_local_config_dir,
                },
            }
        )
    )


def _docker_host_volume_root(host_id: HostId, volume_root: Path) -> Path:
    """Return the on-disk directory backing the host's file volume (its host_dir root)."""
    vol_id = DockerProviderInstance._volume_id_for_host(host_id)
    root = volume_root / "volumes" / str(vol_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_on_before_host_destroy_offline_skips_projects_in_shared_config_mode(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """The offline destroy hook honors isolate_local_config_dir: in shared mode the
    per-agent ``projects`` dir is skipped (it lives in the shared $CLAUDE_CONFIG_DIR)
    while the transcripts and history are still preserved.

    Exercises ``on_before_host_destroy`` end-to-end -- the HostFileReadInterface
    guard, discover_agents, the isolate_local_config_dir extraction from raw certified
    data, and the preserve call -- rather than calling ``preserve_agent_data``
    directly as the other offline tests do.
    """
    host_id = HostId("host-0000000000000000000000000000beef")
    agent_id = AgentId.generate()
    agent_name = AgentName("test-offline-hook")
    provider = make_docker_provider_with_local_volume(temp_mngr_ctx, tmp_path)
    _write_docker_agent_record(host_id, tmp_path, agent_id, agent_name, isolate_local_config_dir=False)

    record = HostRecord(
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name="h",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )
    host = provider._create_host_from_host_record(record)
    assert isinstance(host, OfflineHostWithVolume)

    # Populate the agent's on-volume state (under the host's file volume root),
    # including the per-agent projects dir that shared-config mode must skip.
    _populate_volume_session_files(_docker_host_volume_root(host_id, tmp_path), agent_id)

    on_before_host_destroy(host, temp_mngr_ctx)

    dest_dir = get_local_preserved_agent_dir(temp_mngr_ctx, agent_name, agent_id)
    # Transcripts and history are preserved...
    assert (dest_dir / "logs" / "claude_transcript" / "events.jsonl").exists()
    assert (dest_dir / "events" / "claude" / "common_transcript" / "events.jsonl").exists()
    assert (dest_dir / "claude_session_id_history").exists()
    # ...but the per-agent projects dir is skipped in shared mode.
    assert not (dest_dir / "plugin" / "claude" / "anthropic" / "projects").exists()


def test_should_preserve_sessions_true_for_claude_agent() -> None:
    """_should_preserve_sessions returns True when preserve_sessions_on_destroy is True."""
    ref = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("test"),
        provider_name=ProviderInstanceName("local"),
        certified_data={
            "type": "claude",
            "agent_config": {"preserve_sessions_on_destroy": True},
        },
    )
    assert _should_preserve_sessions(ref) is True


def test_should_preserve_sessions_false_when_disabled() -> None:
    """_should_preserve_sessions returns False when preserve_sessions_on_destroy is False."""
    ref = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("test"),
        provider_name=ProviderInstanceName("local"),
        certified_data={
            "type": "claude",
            "agent_config": {"preserve_sessions_on_destroy": False},
        },
    )
    assert _should_preserve_sessions(ref) is False


def test_should_preserve_sessions_false_for_non_claude_agent() -> None:
    """_should_preserve_sessions returns False when the field is absent (non-Claude agent)."""
    ref = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("test"),
        provider_name=ProviderInstanceName("local"),
        certified_data={"type": "generic"},
    )
    assert _should_preserve_sessions(ref) is False


# =============================================================================
# _write_generated_files Tests
# =============================================================================


def test_write_generated_files_writes_through_symlink_safely(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    """generated_files that don't include installed_plugins.json should not touch symlinked plugins."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_file = source_dir / "installed_plugins.json"
    source_file.write_text('{"original": true}')

    config_dir = tmp_path / "config"
    plugins_dir = config_dir / "plugins"
    plugins_dir.mkdir(parents=True)
    symlink = plugins_dir / "installed_plugins.json"
    symlink.symlink_to(source_file)

    host = cast(OnlineHostInterface, FakeHost())
    # Only settings.json, no installed_plugins.json (as happens for local hosts)
    generated_files = {Path("settings.json"): '{"some": "setting"}'}

    _write_generated_files(host, config_dir, generated_files)

    # The symlink and source file should both be untouched
    assert symlink.is_symlink()
    assert json.loads(source_file.read_text()) == {"original": True}


def test_write_generated_files_breaks_symlink_before_writing(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    """When a generated file targets a path that is a symlink, the symlink is broken and a regular file is written.

    This prevents corruption of the source file (e.g. ~/.claude/plugins/known_marketplaces.json)
    when _sync_user_resources creates child-level symlinks and a generated file needs to overwrite one.
    """
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_file = source_dir / "known_marketplaces.json"
    source_file.write_text('{"original": true}')

    config_dir = tmp_path / "config"
    plugins_dir = config_dir / "plugins"
    plugins_dir.mkdir(parents=True)
    symlink = plugins_dir / "known_marketplaces.json"
    symlink.symlink_to(source_file)

    host = cast(OnlineHostInterface, FakeHost())
    rewritten_content = '{"rewritten": true}'
    generated_files = {Path("plugins") / "known_marketplaces.json": rewritten_content}

    _write_generated_files(host, config_dir, generated_files)

    # The symlink should be replaced with a regular file containing the rewritten content
    assert not symlink.is_symlink()
    assert symlink.read_text() == rewritten_content
    # The original source file must NOT be modified
    assert json.loads(source_file.read_text()) == {"original": True}


def test_sync_user_resources_is_idempotent_without_self_referential_symlinks(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """Re-running _sync_user_resources must not create self-referential symlink loops in the shared source.

    Regression test: plain `ln -sf` (no -n) dereferences an existing dest symlink-to-directory on
    the second run and nests a new link inside the shared source (e.g. ~/.claude/agents/agents ->
    ~/.claude/agents, or ~/.claude/skills/<skill>/<skill>). `ln -sfn` replaces the dest symlink
    instead. Covers both the dir-level branch (agents/commands) and the child-level branch
    (skills/plugins).
    """
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))

    home_claude = tmp_path / "home_claude"
    # dir-level branch source (agents)
    (home_claude / "agents").mkdir(parents=True)
    (home_claude / "agents" / "my-agent.md").write_text("agent")
    # child-level branch source (skills)
    (home_claude / "skills" / "user-skill").mkdir(parents=True)
    (home_claude / "skills" / "user-skill" / "SKILL.md").write_text("skill")

    config_dir = tmp_path / "agent_config"
    config_dir.mkdir()

    with patch(f"{_CLAUDE_AGENT_MODULE}.get_user_claude_config_dir", return_value=home_claude):
        _sync_user_resources(host, config_dir, symlink=True)
        _sync_user_resources(host, config_dir, symlink=True)

    # No self-referential loop nested inside the shared source dirs.
    assert not (home_claude / "agents" / "agents").exists()
    assert not (home_claude / "skills" / "skills").exists()
    assert not (home_claude / "skills" / "user-skill" / "user-skill").exists()

    # dir-level: config_dir/agents is a symlink to the shared source dir.
    assert (config_dir / "agents").is_symlink()
    assert (config_dir / "agents").resolve() == (home_claude / "agents").resolve()

    # child-level: config_dir/skills/user-skill is a symlink to the shared source skill.
    assert (config_dir / "skills" / "user-skill").is_symlink()
    assert (config_dir / "skills" / "user-skill").resolve() == (home_claude / "skills" / "user-skill").resolve()


# =============================================================================
# modify_env_vars Tests
# =============================================================================


def test_modify_env_vars_sets_claude_config_dirs(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """modify_env_vars always writes CLAUDE_CONFIG_DIR and ORIGINAL_CLAUDE_CONFIG_DIR."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    agent, host = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)
    env_vars: dict[str, str] = {}

    agent.modify_env_vars(host, env_vars)

    assert env_vars["CLAUDE_CONFIG_DIR"] == str(agent.get_claude_config_dir())
    # ORIGINAL_CLAUDE_CONFIG_DIR points at the user's real ~/.claude dir. The
    # autouse home-isolation fixture redirects $HOME to a temp dir and clears
    # $CLAUDE_CONFIG_DIR / $ORIGINAL_CLAUDE_CONFIG_DIR, so the resolved value is
    # known: it must be exactly ~/.claude (not, e.g., the per-agent config dir).
    assert env_vars["ORIGINAL_CLAUDE_CONFIG_DIR"] == str(Path.home() / ".claude")
    # The default policy disables claude's auto-updater even on an attended local host.
    assert env_vars["DISABLE_AUTOUPDATER"] == "1"


def test_modify_env_vars_sets_shared_claude_config_dir_in_shared_mode(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In shared mode, when the user's own shell already exported CLAUDE_CONFIG_DIR,
    modify_env_vars propagates that value (their .claude.json already lives inside it,
    so sharing stays consistent) but does NOT set ORIGINAL_CLAUDE_CONFIG_DIR or force
    DISABLE_AUTOUPDATER (it leaves the user's claude environment otherwise alone)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    shared = tmp_path / "shared"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(shared))
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, isolate_local_config_dir=False),
    )
    env_vars: dict[str, str] = {}

    agent.modify_env_vars(host, env_vars)

    # CLAUDE_CONFIG_DIR points at the shared dir so claude reads the user's real config.
    assert env_vars == {"CLAUDE_CONFIG_DIR": str(shared)}


def test_modify_env_vars_does_not_set_config_dir_in_shared_mode_when_env_unset(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In shared mode with $CLAUDE_CONFIG_DIR unset in the user's shell, modify_env_vars
    must NOT export CLAUDE_CONFIG_DIR. Exporting it -- even to claude's own ~/.claude
    default -- would relocate claude's global .claude.json lookup from ~/.claude.json
    to ~/.claude/.claude.json (an inner stub lacking the user's onboarding state),
    re-triggering the theme/onboarding screen on every shared-mode agent. The launch
    command's session-file lookup falls back to $HOME/.claude on its own."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, isolate_local_config_dir=False),
    )
    env_vars: dict[str, str] = {}

    agent.modify_env_vars(host, env_vars)

    assert "CLAUDE_CONFIG_DIR" not in env_vars
    assert "ORIGINAL_CLAUDE_CONFIG_DIR" not in env_vars


def test_modify_env_vars_disables_autoupdater_when_policy_never(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_policy=NEVER sets DISABLE_AUTOUPDATER=1 even on an attended local host."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, update_policy=AgentUpdatePolicy.NEVER),
    )
    env_vars: dict[str, str] = {}

    agent.modify_env_vars(host, env_vars)

    assert env_vars["DISABLE_AUTOUPDATER"] == "1"


def test_modify_env_vars_leaves_autoupdater_alone_when_policy_auto(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_policy=AUTO leaves claude's auto-updater enabled (no DISABLE_AUTOUPDATER)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, update_policy=AgentUpdatePolicy.AUTO),
    )
    env_vars: dict[str, str] = {}

    agent.modify_env_vars(host, env_vars)

    assert "DISABLE_AUTOUPDATER" not in env_vars


def test_modify_env_vars_respects_explicit_disable_autoupdater(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit user-provided DISABLE_AUTOUPDATER value is not overwritten."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    agent, host = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, update_policy=AgentUpdatePolicy.NEVER),
    )
    env_vars: dict[str, str] = {"DISABLE_AUTOUPDATER": "0"}

    agent.modify_env_vars(host, env_vars)

    assert env_vars["DISABLE_AUTOUPDATER"] == "0"


def test_generate_claude_json_autoupdates_follows_disable_flag() -> None:
    """The generated .claude.json autoUpdates flag mirrors the disable_auto_update arg."""
    assert _generate_claude_json(None, disable_auto_update=True)["autoUpdates"] is False
    assert _generate_claude_json(None, disable_auto_update=False)["autoUpdates"] is True


# =============================================================================
# get_claude_config_dir Tests
# =============================================================================


def test_get_claude_config_dir_returns_per_agent_dir_by_default(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """With isolate_local_config_dir=True (default), get_claude_config_dir returns the per-agent path."""
    agent, _ = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)

    config_dir = agent.get_claude_config_dir()

    assert config_dir == agent._get_agent_dir() / "plugin" / "claude" / "anthropic"


def test_get_claude_config_dir_returns_shared_env_value_in_shared_mode(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In shared mode, get_claude_config_dir returns the value of $CLAUDE_CONFIG_DIR."""
    shared = tmp_path / "shared-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(shared))
    agent, _ = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, isolate_local_config_dir=False),
    )

    assert agent.get_claude_config_dir() == shared


def test_get_claude_config_dir_falls_back_to_home_in_shared_mode_when_env_unset(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In shared mode with $CLAUDE_CONFIG_DIR unset, get_claude_config_dir falls back
    to ``~/.claude/`` (claude's own default), so shared mode effectively
    means "don't touch the config dir at all -- inherit whatever the parent shell would
    have used."
    """
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    agent, _ = make_claude_agent(
        local_provider,
        tmp_path,
        temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, isolate_local_config_dir=False),
    )

    assert agent.get_claude_config_dir() == Path.home() / ".claude"


# =============================================================================
# approve_api_key_for_claude Tests
# =============================================================================


class _EnvVarFakeHost(FakeHost):
    """``FakeHost`` extension that stores a host env-var dict so tests can simulate
    the result of ``--host-env-file`` / ``--pass-host-env`` having been written.
    """

    host_env_vars: dict[str, str] = Field(default_factory=dict, description="Stand-in for /mngr/env contents")

    def get_env_var(self, key: str) -> str | None:
        return self.host_env_vars.get(key)

    def get_env_vars(self) -> dict[str, str]:
        return dict(self.host_env_vars)


def _empty_create_agent_options() -> CreateAgentOptions:
    return CreateAgentOptions(agent_type=AgentTypeName("claude"))


def _create_agent_options_with_env_var(value: str) -> CreateAgentOptions:
    return CreateAgentOptions(
        agent_type=AgentTypeName("claude"),
        environment=AgentEnvironmentOptions(env_vars=(EnvVar(key="ANTHROPIC_API_KEY", value=value),)),
    )


def test_approve_api_key_no_keys_anywhere_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    data: dict[str, object] = {}
    host = cast(OnlineHostInterface, _EnvVarFakeHost())
    approve_api_key_for_claude(data, host=host, options=_empty_create_agent_options())
    assert "customApiKeyResponses" not in data


def test_approve_api_key_picks_up_host_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """The LOCAL/Docker minds path: ANTHROPIC_API_KEY arrives only via --host-env-file,
    so the approval must consult ``host.get_env_var`` to find it."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    key = "sk-ant-api03-" + "a" * 80 + "host-env-trailing"
    data: dict[str, object] = {}
    host = cast(OnlineHostInterface, _EnvVarFakeHost(host_env_vars={"ANTHROPIC_API_KEY": key}))
    approve_api_key_for_claude(data, host=host, options=_empty_create_agent_options())
    approved = cast(dict[str, list[str]], data["customApiKeyResponses"])["approved"]
    assert key[-20:] in approved


def test_approve_api_key_picks_up_options_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """The IMBUE_CLOUD lease path supplies ANTHROPIC_API_KEY via ``--env``; the
    approval must walk ``options.environment.env_vars`` to pick that up."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    key = "sk-ant-api03-" + "b" * 80 + "options-env-trail"
    data: dict[str, object] = {}
    host = cast(OnlineHostInterface, _EnvVarFakeHost())
    approve_api_key_for_claude(data, host=host, options=_create_agent_options_with_env_var(key))
    approved = cast(dict[str, list[str]], data["customApiKeyResponses"])["approved"]
    assert key[-20:] in approved


def test_approve_api_key_picks_up_process_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """``os.environ`` remains the source for the legacy IMBUE_CLOUD ``subprocess_env`` injection."""
    key = "sk-ant-api03-" + "c" * 80 + "proc-env-trailing"
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    data: dict[str, object] = {}
    host = cast(OnlineHostInterface, _EnvVarFakeHost())
    approve_api_key_for_claude(data, host=host, options=_empty_create_agent_options())
    approved = cast(dict[str, list[str]], data["customApiKeyResponses"])["approved"]
    assert key[-20:] in approved


def test_approve_api_key_collects_keys_from_every_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Different sources yield different keys; all suffixes should end up approved."""
    proc_key = "sk-ant-api03-" + "1" * 80 + "proc-tail-end-here"
    options_key = "sk-ant-api03-" + "2" * 80 + "options-tail-here"
    host_key = "sk-ant-api03-" + "3" * 80 + "host-tail-end-here"
    monkeypatch.setenv("ANTHROPIC_API_KEY", proc_key)
    data: dict[str, object] = {}
    host = cast(OnlineHostInterface, _EnvVarFakeHost(host_env_vars={"ANTHROPIC_API_KEY": host_key}))
    approve_api_key_for_claude(
        data,
        host=host,
        options=_create_agent_options_with_env_var(options_key),
    )
    approved = cast(dict[str, list[str]], data["customApiKeyResponses"])["approved"]
    assert proc_key[-20:] in approved
    assert options_key[-20:] in approved
    assert host_key[-20:] in approved


def test_approve_api_key_no_host_argument_falls_back_to_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deploy-image caller passes neither ``host`` nor ``options``; the function still
    has to honor ``os.environ`` so the deploy path keeps working."""
    key = "sk-ant-api03-" + "d" * 80 + "deploy-tail-here"
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    data: dict[str, object] = {}
    approve_api_key_for_claude(data)
    approved = cast(dict[str, list[str]], data["customApiKeyResponses"])["approved"]
    assert key[-20:] in approved
