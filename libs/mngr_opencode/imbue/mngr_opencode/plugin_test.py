"""Unit tests for OpenCodeAgentConfig and OpenCodeAgent."""

import json
import shlex
import shutil
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import ClassVar

import pytest

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.agents.update_policy import AgentUpdatePolicy
from imbue.mngr.api.preservation import get_local_preserved_agent_dir
from imbue.mngr.api.testing import FakeHost
from imbue.mngr.config.overlay_merge import merge_models_via_overlay
from imbue.mngr.errors import AgentStartError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import WaitingReason
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import cleanup_tmux_session
from imbue.mngr_opencode.opencode_config import ACTIVE_MARKER_FILENAME
from imbue.mngr_opencode.opencode_config import AGENT_OPENCODE_DB_RELPATH
from imbue.mngr_opencode.opencode_config import PERMISSIONS_WAITING_FILENAME
from imbue.mngr_opencode.opencode_config import READY_SENTINEL_FILENAME
from imbue.mngr_opencode.opencode_config import ROOT_SESSION_FILENAME
from imbue.mngr_opencode.opencode_config import get_opencode_auth_path_for_data_home
from imbue.mngr_opencode.opencode_config import get_opencode_config_file_path
from imbue.mngr_opencode.opencode_config import get_opencode_db_path_for_data_home
from imbue.mngr_opencode.opencode_config import get_opencode_plugin_path
from imbue.mngr_opencode.opencode_config import get_shared_opencode_auth_path
from imbue.mngr_opencode.plugin import OpenCodeAgent
from imbue.mngr_opencode.plugin import OpenCodeAgentConfig
from imbue.mngr_opencode.plugin import _build_prompt_post_command
from imbue.mngr_opencode.plugin import _resolve_lifecycle_state_for_permission
from imbue.mngr_opencode.plugin import _waiting_reason
from imbue.mngr_opencode.plugin import agent_field_generators
from imbue.mngr_opencode.plugin import register_agent_type
from imbue.mngr_opencode.testing import write_opencode_session


def test_opencode_agent_config_has_correct_defaults() -> None:
    config = OpenCodeAgentConfig()

    assert str(config.command) == "opencode"
    assert config.cli_args == ()
    assert config.parent_type is None
    assert config.config_overrides == {}
    assert config.sync_global_config is True
    assert config.symlink_auth is True
    assert config.auto_allow_permissions is False
    assert config.emit_common_transcript is True


def test_opencode_agent_config_merge_with_replaces_cli_args_and_overrides() -> None:
    """Override fields win under the base assign-by-default merge semantics."""
    base = OpenCodeAgentConfig()
    override = OpenCodeAgentConfig(cli_args=("--verbose",), config_overrides={"model": "anthropic/claude-sonnet-4-5"})

    merged, _ = merge_models_via_overlay(base, override)

    assert isinstance(merged, OpenCodeAgentConfig)
    assert merged.cli_args == ("--verbose",)
    assert merged.config_overrides == {"model": "anthropic/claude-sonnet-4-5"}
    assert str(merged.command) == "opencode"


def test_opencode_agent_subclasses_base_agent() -> None:
    # OpenCode is driven via its server (API send), not TUI keystrokes, so it is a
    # BaseAgent rather than an InteractiveTuiAgent (which models keystroke sending).
    assert issubclass(OpenCodeAgent, BaseAgent)
    assert not issubclass(OpenCodeAgent, InteractiveTuiAgent)


def test_opencode_agent_reports_opencode_process_name() -> None:
    agent = OpenCodeAgent.model_construct(agent_config=OpenCodeAgentConfig())
    assert agent.get_expected_process_name() == "opencode"


def test_register_agent_type_returns_opencode_class_and_config() -> None:
    name, agent_class, config_class = register_agent_type()
    assert name == "opencode"
    assert agent_class is OpenCodeAgent
    assert config_class is OpenCodeAgentConfig


# =============================================================================
# Capability-mixin contract methods (install / unattended / permission policy)
# =============================================================================


def test_get_install_binary_name_is_opencode() -> None:
    agent = OpenCodeAgent.model_construct(agent_config=OpenCodeAgentConfig())
    assert agent.get_install_binary_name() == "opencode"


def test_get_install_command_installs_opencode() -> None:
    agent = OpenCodeAgent.model_construct(agent_config=OpenCodeAgentConfig())
    assert agent.get_install_command() == "curl -fsSL https://opencode.ai/install | bash"


def test_get_install_command_pins_version() -> None:
    agent = OpenCodeAgent.model_construct(agent_config=OpenCodeAgentConfig(version="0.4.10"))
    assert agent.get_install_command() == "curl -fsSL https://opencode.ai/install | VERSION=0.4.10 bash"


def test_is_unattended_enabled_reflects_auto_allow_permissions() -> None:
    unattended = OpenCodeAgent.model_construct(agent_config=OpenCodeAgentConfig(auto_allow_permissions=True))
    attended = OpenCodeAgent.model_construct(agent_config=OpenCodeAgentConfig())
    assert unattended.is_unattended_enabled() is True
    assert attended.is_unattended_enabled() is False


def test_get_permission_policy_returns_configured_permission_block() -> None:
    policy = {"bash": {"*": "deny"}}
    agent = OpenCodeAgent.model_construct(agent_config=OpenCodeAgentConfig(config_overrides={"permission": policy}))
    assert agent.get_permission_policy() == policy


def test_get_permission_policy_is_empty_when_unset() -> None:
    agent = OpenCodeAgent.model_construct(agent_config=OpenCodeAgentConfig())
    assert agent.get_permission_policy() == {}


class _BinaryPresentStubHost(FakeHost):
    """FakeHost that reports the install-check binary as present and records commands.

    Lets ``provision`` run its install-check line (``command -v opencode``) without
    triggering an install: the binary is reported present, so ``ensure_cli_installed``
    returns after the check. All other commands fall through to the local FakeHost.
    """

    executed_commands: list[str] = []

    def _execute_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.executed_commands.append(command)
        if command.startswith("command -v "):
            return CommandResult(stdout="/usr/local/bin/opencode", stderr="", success=True)
        return super()._execute_command(command, user, cwd, env, timeout_seconds)


def test_provision_runs_install_check_when_enabled(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """With ``check_installation=True``, provision issues the ``command -v opencode`` install check.

    The binary is reported present so no install command runs; reaching the
    ``command -v`` probe proves the install-check line executed.
    """
    agent = _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig(check_installation=True))
    stub_host: Any = _BinaryPresentStubHost(host_dir=tmp_path, is_local=True)
    agent.provision(
        host=stub_host,
        options=CreateAgentOptions(agent_type=AgentTypeName("opencode")),
        mngr_ctx=agent.mngr_ctx,
    )
    assert any(command.startswith("command -v opencode") for command in stub_host.executed_commands)


def test_is_common_transcript_enabled_reflects_config() -> None:
    enabled = OpenCodeAgent.model_construct(agent_config=OpenCodeAgentConfig())
    disabled = OpenCodeAgent.model_construct(agent_config=OpenCodeAgentConfig(emit_common_transcript=False))
    assert enabled.is_common_transcript_enabled is True
    assert disabled.is_common_transcript_enabled is False


def test_transcripts_have_no_commands_scripts_both_in_process() -> None:
    """Both raw and common transcripts are written in-process by the .ts plugin -- no commands/ scripts."""
    agent = OpenCodeAgent.model_construct(agent_config=OpenCodeAgentConfig())
    assert agent.get_raw_transcript_scripts() == {}
    assert agent.get_common_transcript_scripts() == {}


def _make_opencode_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    agent_config: OpenCodeAgentConfig,
) -> OpenCodeAgent:
    # These setup tests run against a real local host where opencode is not installed; the
    # install check is irrelevant to provision setup (files/auth/config) and is covered
    # separately, so skip it unless a caller opted in explicitly.
    if "check_installation" not in agent_config.model_fields_set:
        agent_config = agent_config.model_copy_update(to_update(agent_config.field_ref().check_installation, False))
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    return OpenCodeAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-opencode"),
        agent_type=AgentTypeName("opencode"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=local_provider.mngr_ctx,
        agent_config=agent_config,
        host=host,
    )


@pytest.fixture
def opencode_agent(local_provider: LocalProviderInstance, tmp_path: Path) -> OpenCodeAgent:
    return _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig())


@pytest.fixture
def opencode_agent_no_common(local_provider: LocalProviderInstance, tmp_path: Path) -> OpenCodeAgent:
    return _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig(emit_common_transcript=False))


def test_assemble_command_runs_launch_script_with_isolation_and_server_env(opencode_agent: OpenCodeAgent) -> None:
    """The launch script runs with per-agent config/data isolation + the bin/port/workdir it needs."""
    command = str(opencode_agent.assemble_command(opencode_agent.host, (), command_override=None))
    config_dir = str(opencode_agent._get_opencode_config_dir())
    data_home = str(opencode_agent._get_opencode_data_home())
    assert f"OPENCODE_CONFIG_DIR={config_dir}" in command
    assert f"XDG_DATA_HOME={data_home}" in command
    assert "MNGR_OPENCODE_BIN=opencode" in command
    # Port 0 -> the server binds an OS-assigned free port (the script records it).
    assert "MNGR_OPENCODE_PORT=0" in command
    assert f"MNGR_OPENCODE_WORKDIR={opencode_agent.work_dir}" in command
    assert "bash $MNGR_AGENT_STATE_DIR/commands/opencode_launch.sh" in command


def test_assemble_command_uses_command_override_as_bin(opencode_agent: OpenCodeAgent) -> None:
    command = str(
        opencode_agent.assemble_command(opencode_agent.host, (), command_override=CommandString("/opt/opencode"))
    )
    assert "MNGR_OPENCODE_BIN=/opt/opencode" in command


def test_assemble_command_url_encodes_workdir_for_session_query(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """The work dir is URL-encoded (in Python) since it goes into the session-create query string."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "a work dir"
    work_dir.mkdir()
    agent = OpenCodeAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("spacey"),
        agent_type=AgentTypeName("opencode"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=local_provider.mngr_ctx,
        agent_config=OpenCodeAgentConfig(),
        host=host,
    )
    command = str(agent.assemble_command(host, (), command_override=None))
    # The space is percent-encoded; path separators stay readable.
    assert "MNGR_OPENCODE_WORKDIR=" in command
    assert "a%20work%20dir" in command
    assert "a work dir" not in command.split("MNGR_OPENCODE_WORKDIR=", 1)[1].split(" bash ", 1)[0]


def test_assemble_command_sets_emit_common_env_when_enabled(opencode_agent: OpenCodeAgent) -> None:
    """The in-process plugin emits the common transcript only when this env is set."""
    command = str(opencode_agent.assemble_command(opencode_agent.host, (), command_override=None))
    assert "MNGR_OPENCODE_EMIT_COMMON=1" in command
    # No backgrounded supervisor any more -- both transcripts are in-process.
    assert "opencode_background_tasks.sh" not in command
    assert command.strip().startswith("env ")


def test_assemble_command_omits_emit_common_env_when_disabled(
    opencode_agent_no_common: OpenCodeAgent,
) -> None:
    command = str(opencode_agent_no_common.assemble_command(opencode_agent_no_common.host, (), command_override=None))
    assert "MNGR_OPENCODE_EMIT_COMMON" not in command
    assert "bash $MNGR_AGENT_STATE_DIR/commands/opencode_launch.sh" in command


def test_assemble_command_forwards_user_args_to_attach_client(opencode_agent: OpenCodeAgent) -> None:
    command = str(opencode_agent.assemble_command(opencode_agent.host, ("--agent", "plan"), command_override=None))
    assert command.rstrip().endswith("opencode_launch.sh --agent plan")


def test_assemble_command_shell_quotes_user_args_with_spaces_and_parens(opencode_agent: OpenCodeAgent) -> None:
    """A value with spaces/parens is shell-quoted, not spliced in raw (bash would mis-parse `(`)."""
    command = str(opencode_agent.assemble_command(opencode_agent.host, ("--model", "A B (C)"), command_override=None))
    assert "'A B (C)'" in command


def _provision(agent: OpenCodeAgent) -> None:
    agent.provision(
        host=agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("opencode")),
        mngr_ctx=agent.mngr_ctx,
    )


def test_provision_writes_per_agent_config_with_schema(opencode_agent: OpenCodeAgent) -> None:
    _provision(opencode_agent)
    config_path = get_opencode_config_file_path(opencode_agent._get_opencode_config_dir())
    assert config_path.exists()
    parsed = json.loads(config_path.read_text())
    assert parsed["$schema"] == "https://opencode.ai/config.json"


def test_provision_inherits_user_global_config_and_applies_overrides(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """sync_global_config seeds from the user's ~/.config/opencode/opencode.json; overrides win."""
    user_config_path = Path.home() / ".config" / "opencode" / "opencode.json"
    user_config_path.parent.mkdir(parents=True, exist_ok=True)
    user_config_path.write_text(json.dumps({"theme": "user-theme", "model": "old/model"}))
    agent = _make_opencode_agent(
        local_provider, tmp_path, OpenCodeAgentConfig(config_overrides={"model": "anthropic/claude-sonnet-4-5"})
    )
    _provision(agent)
    parsed = json.loads(get_opencode_config_file_path(agent._get_opencode_config_dir()).read_text())
    assert parsed["theme"] == "user-theme"
    assert parsed["model"] == "anthropic/claude-sonnet-4-5"


def test_provision_injects_wildcard_allow_when_auto_allow(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig(auto_allow_permissions=True))
    _provision(agent)
    parsed = json.loads(get_opencode_config_file_path(agent._get_opencode_config_dir()).read_text())
    assert parsed["permission"] == {"*": "allow"}


def test_provision_disables_autoupdate_when_policy_never(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """update_policy=NEVER writes autoupdate:false into the per-agent opencode.json."""
    agent = _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig(update_policy=AgentUpdatePolicy.NEVER))
    _provision(agent)
    parsed = json.loads(get_opencode_config_file_path(agent._get_opencode_config_dir()).read_text())
    assert parsed["autoupdate"] is False


def test_provision_disables_autoupdate_by_default_on_attended_local(opencode_agent: OpenCodeAgent) -> None:
    """The default policy disables opencode's auto-update, even on an attended local host."""
    _provision(opencode_agent)
    parsed = json.loads(get_opencode_config_file_path(opencode_agent._get_opencode_config_dir()).read_text())
    assert parsed["autoupdate"] is False


def test_provision_leaves_autoupdate_unset_when_policy_auto(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """Explicit AUTO opts back into opencode's auto-update (no autoupdate key written)."""
    agent = _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig(update_policy=AgentUpdatePolicy.AUTO))
    _provision(agent)
    parsed = json.loads(get_opencode_config_file_path(agent._get_opencode_config_dir()).read_text())
    assert "autoupdate" not in parsed


def test_provision_installs_lifecycle_plugin(opencode_agent: OpenCodeAgent) -> None:
    _provision(opencode_agent)
    plugin_path = get_opencode_plugin_path(opencode_agent._get_opencode_config_dir())
    assert plugin_path.exists()
    assert "MngrLifecyclePlugin" in plugin_path.read_text()


def test_provision_symlinks_auth_to_shared_path_by_default(opencode_agent: OpenCodeAgent) -> None:
    _provision(opencode_agent)
    auth_path = get_opencode_auth_path_for_data_home(opencode_agent._get_opencode_data_home())
    assert auth_path.is_symlink()
    assert auth_path.readlink() == get_shared_opencode_auth_path(Path.home())


def test_provision_copies_auth_when_symlink_disabled(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    shared = get_shared_opencode_auth_path(Path.home())
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text('{"anthropic":{"type":"api","key":"seed"}}')
    agent = _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig(symlink_auth=False))
    _provision(agent)
    auth_path = get_opencode_auth_path_for_data_home(agent._get_opencode_data_home())
    assert auth_path.exists()
    assert not auth_path.is_symlink()
    assert json.loads(auth_path.read_text())["anthropic"]["type"] == "api"


def test_provision_installs_only_the_launch_script_in_commands(opencode_agent: OpenCodeAgent) -> None:
    """Both transcripts are in-process, so the only commands/ script is the launch orchestrator."""
    _provision(opencode_agent)
    commands_dir = opencode_agent._get_agent_dir() / "commands"
    assert (commands_dir / "opencode_launch.sh").exists()
    assert not (commands_dir / "opencode_common_transcript.sh").exists()
    assert not (commands_dir / "opencode_background_tasks.sh").exists()


def test_provision_does_not_write_into_work_dir(opencode_agent: OpenCodeAgent) -> None:
    """The plugin isolates everything under the agent state dir; the user's work_dir is untouched."""
    _provision(opencode_agent)
    assert not (opencode_agent.work_dir / "opencode.json").exists()
    assert not (opencode_agent.work_dir / ".opencode").exists()


def test_provision_skips_auth_copy_when_no_shared_auth(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """copy mode with no shared auth.json simply skips seeding (the agent runs OpenCode's login flow)."""
    agent = _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig(symlink_auth=False))
    _provision(agent)
    auth_path = get_opencode_auth_path_for_data_home(agent._get_opencode_data_home())
    assert not auth_path.exists()


# --- API-based send_message (POST to the agent's opencode server) ---


def test_build_prompt_post_command_targets_prompt_async_with_json_part() -> None:
    command = _build_prompt_post_command("50123", "ses_abc123", "count to twenty please")
    assert command.startswith("curl -fsS -X POST ")
    assert "http://127.0.0.1:50123/session/ses_abc123/prompt_async" in command
    # Delivered as a JSON text part so the body is structured, not screen-typed.
    assert '"count to twenty please"' in command
    assert "content-type: application/json" in command


def test_build_prompt_post_command_json_encodes_special_characters() -> None:
    command = _build_prompt_post_command("1", "ses_x", 'a "quoted" line\nand another')
    # JSON-encoded (escaped quotes + \n), not raw, so the HTTP body stays valid.
    assert '\\"quoted\\"' in command
    assert "\\n" in command


class _RecordingDispatchAgent(OpenCodeAgent):
    """Test agent that stubs the launch-file reads and records the _post_prompt dispatch.

    Overrides host-touching methods (no monkeypatch) so send_message's file-read +
    dispatch logic can be checked without a running server.
    """

    fake_port: ClassVar[str] = "50123"
    fake_session: ClassVar[str] = "ses_abc123"
    posted: ClassVar[tuple[str, str, str] | None] = None

    def _try_read_nonempty_file(self, path: Path) -> str | None:
        if path == self._get_server_port_file_path():
            return type(self).fake_port
        if path == self._get_root_session_file_path():
            return type(self).fake_session
        return None

    def _post_prompt(self, port: str, session_id: str, message: str) -> None:
        type(self).posted = (port, session_id, message)


def _make_dispatch_agent(local_provider: LocalProviderInstance, tmp_path: Path) -> _RecordingDispatchAgent:
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    agent = _RecordingDispatchAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("dispatch"),
        agent_type=AgentTypeName("opencode"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=local_provider.mngr_ctx,
        agent_config=OpenCodeAgentConfig(),
        host=host,
    )
    type(agent).posted = None
    return agent


def test_send_message_reads_launch_files_and_dispatches(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    agent = _make_dispatch_agent(local_provider, tmp_path)
    agent.send_message("count to twenty please")
    assert type(agent).posted == ("50123", "ses_abc123", "count to twenty please")


class _NoFilesAgent(OpenCodeAgent):
    """Test agent whose launch files never appear, to exercise the timeout path quickly."""

    _SEND_FILE_WAIT_SECONDS: ClassVar[float] = 0.2
    _SEND_FILE_POLL_INTERVAL_SECONDS: ClassVar[float] = 0.05

    def _try_read_nonempty_file(self, path: Path) -> str | None:
        return None


def _make_agent_of(
    agent_class: type[OpenCodeAgent], local_provider: LocalProviderInstance, tmp_path: Path
) -> OpenCodeAgent:
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    return agent_class.model_construct(
        id=AgentId.generate(),
        name=AgentName("oc"),
        agent_type=AgentTypeName("opencode"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=local_provider.mngr_ctx,
        agent_config=OpenCodeAgentConfig(),
        host=host,
    )


def test_send_message_raises_when_launch_files_missing(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    agent = _make_agent_of(_NoFilesAgent, local_provider, tmp_path)
    with pytest.raises(SendMessageError):
        agent.send_message("no server")


def test_post_prompt_raises_when_server_unreachable(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """A real curl to a closed port fails, and _post_prompt surfaces it as SendMessageError."""
    agent = _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig())
    # Port 1 is not listening; curl -fsS returns non-zero.
    with pytest.raises(SendMessageError):
        agent._post_prompt("1", "ses_nope", "hello")


# --- Readiness sentinel ---


def _noop_start() -> None:
    return None


def test_wait_for_ready_signal_returns_when_sentinel_present(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig())
    agent_dir = agent._get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / READY_SENTINEL_FILENAME).write_text("")
    # Returns without raising once the sentinel the launch script writes is present.
    agent.wait_for_ready_signal(is_creating=True, start_action=_noop_start, timeout=2.0)


def test_wait_for_ready_signal_skips_when_not_creating(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    agent = _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig())
    # On a restart (is_creating=False) we do not block on the sentinel.
    agent.wait_for_ready_signal(is_creating=False, start_action=_noop_start, timeout=0.2)


@pytest.mark.tmux
def test_wait_for_ready_signal_raises_when_sentinel_never_appears(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig())
    with pytest.raises(AgentStartError):
        agent.wait_for_ready_signal(is_creating=True, start_action=_noop_start, timeout=0.2)


# =============================================================================
# Preservation on destroy
# =============================================================================


def test_opencode_config_preserves_on_destroy_by_default() -> None:
    assert OpenCodeAgentConfig().preserve_on_destroy is True


def _populate_opencode_transcripts(agent: OpenCodeAgent) -> None:
    """Write the raw/common transcripts and the root session-id history into the state dir."""
    agent_dir = agent._get_agent_dir()
    (agent_dir / "logs" / "opencode_transcript").mkdir(parents=True, exist_ok=True)
    (agent_dir / "logs" / "opencode_transcript" / "events.jsonl").write_text('{"type":"raw"}\n')
    (agent_dir / "events" / "opencode" / "common_transcript").mkdir(parents=True, exist_ok=True)
    (agent_dir / "events" / "opencode" / "common_transcript" / "events.jsonl").write_text('{"type":"common"}\n')
    (agent_dir / ROOT_SESSION_FILENAME).write_text("sess-opencode\n")


@pytest.mark.rsync
def test_on_destroy_preserves_transcripts(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """on_destroy copies transcripts and session-id history to the mirrored preserved layout."""
    agent = _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig(preserve_on_destroy=True))
    _populate_opencode_transcripts(agent)

    agent.on_destroy(agent.host)

    dest_dir = get_local_preserved_agent_dir(agent.mngr_ctx, agent.name, agent.id)
    assert (dest_dir / "logs" / "opencode_transcript" / "events.jsonl").read_text() == '{"type":"raw"}\n'
    assert (
        dest_dir / "events" / "opencode" / "common_transcript" / "events.jsonl"
    ).read_text() == '{"type":"common"}\n'
    assert (dest_dir / ROOT_SESSION_FILENAME).read_text() == "sess-opencode\n"


def test_on_destroy_skips_preservation_when_disabled(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """on_destroy preserves nothing when preserve_on_destroy is False."""
    agent = _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig(preserve_on_destroy=False))
    _populate_opencode_transcripts(agent)

    agent.on_destroy(agent.host)

    dest_dir = get_local_preserved_agent_dir(agent.mngr_ctx, agent.name, agent.id)
    assert not dest_dir.exists()


# =============================================================================
# Session adoption (--adopt / --from), driven against the real local host
# =============================================================================


def _dest_db_sessions(agent: OpenCodeAgent) -> set[str]:
    """Return the session ids present in the agent's own ``opencode.db``."""
    dest_db = get_opencode_db_path_for_data_home(agent._get_opencode_data_home())
    connection = sqlite3.connect(dest_db)
    try:
        return {str(row[0]) for row in connection.execute("SELECT id FROM session").fetchall()}
    finally:
        connection.close()


def _resolved_work_dir(agent: OpenCodeAgent) -> str:
    """Return the agent's work dir as opencode stores it (symlinks resolved, e.g. /tmp on macOS)."""
    return str(Path(agent.work_dir).resolve())


@pytest.mark.rsync
def test_adopt_session_single_copies_db_rebinds_and_resumes(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """A single ``--adopt`` copies the source db in, rebinds it to the work dir, and resumes it."""
    agent = _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig())
    source_db = tmp_path / "src_a" / "opencode.db"
    write_opencode_session(source_db, "ses_a", "/old/work", message_id="msg_a")

    agent.adopt_session(
        agent.host,
        CreateAgentOptions(agent_type=AgentTypeName("opencode"), adopt_session=(str(source_db),)),
        agent.mngr_ctx,
    )

    assert _dest_db_sessions(agent) == {"ses_a"}
    assert agent._get_root_session_file_path().read_text() == "ses_a"
    dest_db = get_opencode_db_path_for_data_home(agent._get_opencode_data_home())
    connection = sqlite3.connect(dest_db)
    try:
        directory = connection.execute("SELECT directory FROM session WHERE id='ses_a'").fetchone()[0]
    finally:
        connection.close()
    assert directory == _resolved_work_dir(agent)


@pytest.mark.rsync
def test_adopt_session_multi_merges_both_sessions_and_resumes_last(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """``--adopt A B`` merges both sessions into one db (B folded in) and resumes B (the last)."""
    agent = _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig())
    source_a = tmp_path / "src_a" / "opencode.db"
    source_b = tmp_path / "src_b" / "opencode.db"
    write_opencode_session(source_a, "ses_a", "/work/a", message_id="msg_a")
    write_opencode_session(source_b, "ses_b", "/work/b", message_id="msg_b")

    agent.adopt_session(
        agent.host,
        CreateAgentOptions(agent_type=AgentTypeName("opencode"), adopt_session=(str(source_a), str(source_b))),
        agent.mngr_ctx,
    )

    # Both sessions live in the single dest db; the last named one is the one resumed.
    assert _dest_db_sessions(agent) == {"ses_a", "ses_b"}
    assert agent._get_root_session_file_path().read_text() == "ses_b"
    dest_db = get_opencode_db_path_for_data_home(agent._get_opencode_data_home())
    connection = sqlite3.connect(dest_db)
    try:
        messages = {row[0] for row in connection.execute("SELECT id FROM message").fetchall()}
        # Both adopted sessions are rebound to this agent's work dir.
        directories = {row[0] for row in connection.execute("SELECT directory FROM session").fetchall()}
    finally:
        connection.close()
    assert messages == {"msg_a", "msg_b"}
    assert directories == {_resolved_work_dir(agent)}


@pytest.mark.rsync
def test_adopt_session_from_clone_resumes_the_clone(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """``--adopt A --from X`` merges A and the clone, then resumes the clone's session."""
    agent = _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig())
    source_a = tmp_path / "src_a" / "opencode.db"
    write_opencode_session(source_a, "ses_a", "/work/a", message_id="msg_a")

    # The clone source is a state dir whose native store holds one root session.
    source_state_dir = tmp_path / "source_agent"
    clone_db = source_state_dir / AGENT_OPENCODE_DB_RELPATH
    write_opencode_session(clone_db, "ses_clone", "/work/clone", message_id="msg_clone")
    source_location = HostLocation(host=agent.host, path=source_state_dir)

    agent.adopt_session(
        agent.host,
        CreateAgentOptions(
            agent_type=AgentTypeName("opencode"),
            adopt_session=(str(source_a),),
            source_agent_state_location=source_location,
        ),
        agent.mngr_ctx,
    )

    # Both the explicit session and the clone's session are present; the clone is resumed.
    assert _dest_db_sessions(agent) == {"ses_a", "ses_clone"}
    assert agent._get_root_session_file_path().read_text() == "ses_clone"


def test_adopt_session_from_clone_with_no_store_starts_fresh(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """A ``--from`` clone whose source has no opencode store warns and starts fresh, not a hard error."""
    agent = _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig())
    # A source state dir with no native opencode store at all.
    source_location = HostLocation(host=agent.host, path=tmp_path / "empty_source")

    agent.adopt_session(
        agent.host,
        CreateAgentOptions(agent_type=AgentTypeName("opencode"), source_agent_state_location=source_location),
        agent.mngr_ctx,
    )

    # No session was resumed and no db was staged onto the agent: it starts fresh.
    assert not agent._get_root_session_file_path().exists()
    assert not get_opencode_db_path_for_data_home(agent._get_opencode_data_home()).exists()


class _RemoteSourceHost(FrozenModel):
    """A non-local host stand-in whose file reads map onto the local filesystem (for the remote-clone path)."""

    is_local: bool = False

    def path_exists(self, path: Path) -> bool:
        return path.exists()

    def read_file(self, path: Path) -> bytes:
        return path.read_bytes()


@pytest.mark.rsync
def test_adopt_cloned_session_from_remote_source_pulls_db_locally(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """A ``--from`` clone whose source lives on a remote host pulls the db locally, then stages it.

    ``_localize_source_db`` must copy the source ``opencode.db`` (and its sidecars) off the remote host
    before the local stdlib-``sqlite3`` merge/rebind can touch it; the staged db then lands on the dest.
    """
    agent = _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig())
    source_state_dir = tmp_path / "remote_source"
    clone_db = source_state_dir / AGENT_OPENCODE_DB_RELPATH
    write_opencode_session(clone_db, "ses_remote", "/remote/work", message_id="msg_remote")
    # A read-only sidecar alongside the db so the pull-sidecars branch is exercised too.
    (clone_db.parent / "opencode.db-wal").write_bytes(b"")
    source_location = HostLocation.model_construct(host=_RemoteSourceHost(), path=source_state_dir)

    agent.adopt_session(
        agent.host,
        CreateAgentOptions(agent_type=AgentTypeName("opencode"), source_agent_state_location=source_location),
        agent.mngr_ctx,
    )

    assert _dest_db_sessions(agent) == {"ses_remote"}
    assert agent._get_root_session_file_path().read_text() == "ses_remote"


# =============================================================================
# Lifecycle promotion + waiting_reason field generator
# =============================================================================


@pytest.mark.parametrize(
    "base_state, is_blocked, expected",
    [
        # Only a RUNNING base is promoted, and only while blocked on a prompt.
        (AgentLifecycleState.RUNNING, True, AgentLifecycleState.WAITING),
        (AgentLifecycleState.RUNNING, False, AgentLifecycleState.RUNNING),
        # Every non-RUNNING base passes through unchanged, blocked or not.
        (AgentLifecycleState.WAITING, True, AgentLifecycleState.WAITING),
        (AgentLifecycleState.STOPPED, True, AgentLifecycleState.STOPPED),
        (AgentLifecycleState.REPLACED, True, AgentLifecycleState.REPLACED),
        (AgentLifecycleState.DONE, True, AgentLifecycleState.DONE),
        (
            AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE,
            True,
            AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE,
        ),
    ],
)
def test_resolve_lifecycle_state_for_permission(
    base_state: AgentLifecycleState, is_blocked: bool, expected: AgentLifecycleState
) -> None:
    assert _resolve_lifecycle_state_for_permission(base_state, is_blocked) == expected


@pytest.mark.tmux
def test_get_lifecycle_state_promotes_running_to_waiting_when_blocked_on_permission(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """End-to-end override against a live pane: the base state is RUNNING, and a
    permissions_waiting marker promotes it to WAITING; removing the marker restores
    RUNNING. (The promotion rule itself is unit-tested above without tmux.)"""
    agent = _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig())
    # A long-lived process that ps reports as "opencode" (the expected process name)
    # so the base lifecycle reads RUNNING -- the renamed-sleep trick.
    sleep_bin = shutil.which("sleep")
    assert sleep_bin is not None
    fake_opencode = tmp_path / "opencode"
    shutil.copy(sleep_bin, fake_opencode)
    fake_opencode.chmod(0o755)
    agent_dir = agent._get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / ACTIVE_MARKER_FILENAME).write_text("")
    session_name = agent.session_name
    window_name = agent.mngr_ctx.config.tmux.primary_window_name
    # Name the primary window so lifecycle detection (which targets it by name) finds the pane.
    agent.host.execute_idempotent_command(
        f"tmux new-session -d -s {shlex.quote(session_name)} -n {shlex.quote(window_name)} {shlex.quote(str(fake_opencode))} 600",
        timeout_seconds=5.0,
    )
    try:
        wait_for(
            lambda: agent.get_lifecycle_state() == AgentLifecycleState.RUNNING,
            error_message="expected opencode agent to read RUNNING with a live pane",
        )
        (agent_dir / PERMISSIONS_WAITING_FILENAME).touch()
        assert agent.get_lifecycle_state() == AgentLifecycleState.WAITING
        (agent_dir / PERMISSIONS_WAITING_FILENAME).unlink()
        assert agent.get_lifecycle_state() == AgentLifecycleState.RUNNING
    finally:
        cleanup_tmux_session(session_name)


def test_agent_field_generators_exposes_opencode_waiting_reason() -> None:
    result = agent_field_generators()
    assert result is not None
    plugin_name, generators = result
    assert plugin_name == "opencode"
    assert "waiting_reason" in generators
    assert callable(generators["waiting_reason"])


def test_waiting_reason_returns_permissions_when_active_and_blocked(opencode_agent: OpenCodeAgent) -> None:
    """A real open prompt: the active marker (set when the session went busy) is
    present *and* permissions_waiting is present, so the agent is blocked on an
    approval prompt."""
    agent_dir = opencode_agent._get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / ACTIVE_MARKER_FILENAME).touch()
    (agent_dir / PERMISSIONS_WAITING_FILENAME).touch()
    assert _waiting_reason(opencode_agent, opencode_agent.host) == WaitingReason.PERMISSIONS


def test_waiting_reason_ignores_stranded_permissions_marker_after_turn(opencode_agent: OpenCodeAgent) -> None:
    """A stranded permissions_waiting marker (active absent -> turn over) reports
    END_OF_TURN, not PERMISSIONS. The PERMISSIONS verdict is gated on the active
    marker, so correctness does not depend on the root-idle safety net having
    deleted the file."""
    agent_dir = opencode_agent._get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / PERMISSIONS_WAITING_FILENAME).touch()
    assert _waiting_reason(opencode_agent, opencode_agent.host) == WaitingReason.END_OF_TURN


def test_waiting_reason_returns_end_of_turn_when_idle(opencode_agent: OpenCodeAgent) -> None:
    agent_dir = opencode_agent._get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)
    assert _waiting_reason(opencode_agent, opencode_agent.host) == WaitingReason.END_OF_TURN


def test_waiting_reason_returns_none_when_active(opencode_agent: OpenCodeAgent) -> None:
    agent_dir = opencode_agent._get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / ACTIVE_MARKER_FILENAME).touch()
    assert _waiting_reason(opencode_agent, opencode_agent.host) is None
