import io
import json
import os
import re
import shlex
import stat
import subprocess
import time
import tomllib
import uuid
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

import pytest
import tomlkit
from pydantic import Field

from imbue.imbue_common.model_update import to_update
from imbue.mngr.agents.base_agent import SendKeysAgent
from imbue.mngr.agents.installation import VersionMismatchPolicy
from imbue.mngr.agents.mock_host_test import ScriptedHost
from imbue.mngr.agents.tui_utils import PANE_TARGET_SHELL_VARIABLE
from imbue.mngr.agents.tui_utils import SubmissionConfirmationPolicy
from imbue.mngr.agents.tui_utils import SubmissionEvidenceProbe
from imbue.mngr.agents.update_policy import AgentUpdatePolicy
from imbue.mngr.api.create import create
from imbue.mngr.api.preservation import get_local_preserved_agent_dir
from imbue.mngr.api.preservation import get_preserved_agents_root_dir
from imbue.mngr.api.providers import get_local_host
from imbue.mngr.config.agent_class_registry import register_agent_class
from imbue.mngr.config.agent_class_registry import reset_agent_class_registry
from imbue.mngr.config.agent_config_registry import register_agent_config
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.overlay_merge import merge_models_via_overlay
from imbue.mngr.errors import AgentInstallationError
from imbue.mngr.errors import AgentStartError
from imbue.mngr.errors import ConfigParseError
from imbue.mngr.errors import SendFailureKind
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.common import resolve_expected_process_name
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.interfaces.agent import CliBackedAgentMixin
from imbue.mngr.interfaces.agent import HasAutoInstallMixin
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.agent import HasCompactionMixin
from imbue.mngr.interfaces.agent import HasPermissionPolicyMixin
from imbue.mngr.interfaces.agent import HasSessionAdoptionMixin
from imbue.mngr.interfaces.agent import HasSessionPreservationMixin
from imbue.mngr.interfaces.agent import HasUnattendedModeMixin
from imbue.mngr.interfaces.agent import HasVersionManagementMixin
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.data_types import VolumeFile
from imbue.mngr.interfaces.host import AgentLabelOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.interfaces.host import NewHostOptions
from imbue.mngr.plugins.hookspecs import OnBeforeCreateArgs
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import OutputStyleName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SystemPromptText
from imbue.mngr.primitives import TransferMode
from imbue.mngr.primitives import WaitingReason
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.providers.mock_provider_test import make_local_volume_provider
from imbue.mngr.providers.mock_provider_test import make_offline_host_with_volume
from imbue.mngr.utils.plugin_testing import PLACEHOLDER_AGENT_TYPE
from imbue.mngr.utils.testing import capture_loguru
from imbue.mngr.utils.testing import tmux_session_cleanup
from imbue.mngr.utils.testing import write_executable_script
from imbue.mngr_grok import resources as grok_resources
from imbue.mngr_grok.grok_config import FORK_FROM_RELATIVE_PATH
from imbue.mngr_grok.grok_config import GROK_PLUGIN_STATE_RELATIVE_PATH
from imbue.mngr_grok.grok_config import GrokRootTurn
from imbue.mngr_grok.grok_config import LAUNCH_ENV_RELATIVE_PATH
from imbue.mngr_grok.grok_config import ROLE_PROMPT_RELATIVE_PATH
from imbue.mngr_grok.grok_config import ROOT_TURNS_RELATIVE_PATH
from imbue.mngr_grok.grok_config import SESSION_ID_RELATIVE_PATH
from imbue.mngr_grok.grok_config import build_hooks_json
from imbue.mngr_grok.grok_config import build_launch_env
from imbue.mngr_grok.grok_config import compute_session_bucket_name
from imbue.mngr_grok.grok_config import get_grok_home
from imbue.mngr_grok.grok_screen import GROK_ENTER_RETRY_CONDITION
from imbue.mngr_grok.grok_screen import GrokGateScreen
from imbue.mngr_grok.grok_screen import is_ready_for_paste
from imbue.mngr_grok.grok_screen import read_composer_text
from imbue.mngr_grok.grok_sessions import GROK_MEMORY_RELATIVE_PATH
from imbue.mngr_grok.grok_sessions import GROK_SESSIONS_RELATIVE_PATH
from imbue.mngr_grok.grok_sessions import ROOTS_RELATIVE_PATH
from imbue.mngr_grok.grok_sessions import SESSION_SUMMARY_FILENAME
from imbue.mngr_grok.grok_sessions import build_grok_preserved_items
from imbue.mngr_grok.plugin import GrokAgent
from imbue.mngr_grok.plugin import GrokAgentConfig
from imbue.mngr_grok.plugin import GrokCancelReading
from imbue.mngr_grok.plugin import GrokCancelUnsettledError
from imbue.mngr_grok.plugin import GrokHostPaths
from imbue.mngr_grok.plugin import GrokInputBlockedError
from imbue.mngr_grok.plugin import GrokKeyRefusedError
from imbue.mngr_grok.plugin import GrokProvisioningError
from imbue.mngr_grok.plugin import _LAST_INTERRUPT_RELATIVE_PATH
from imbue.mngr_grok.plugin import _UNREADABLE_COMPOSER_TEXT
from imbue.mngr_grok.plugin import _grok_items_to_preserve_for_discovered_agent
from imbue.mngr_grok.plugin import _waiting_reason
from imbue.mngr_grok.plugin import agent_field_generators
from imbue.mngr_grok.plugin import build_cancel_settle_probe_command
from imbue.mngr_grok.plugin import build_grok_host_paths_command
from imbue.mngr_grok.plugin import build_queued_row_evidence_probe
from imbue.mngr_grok.plugin import build_session_id_evidence_probe
from imbue.mngr_grok.plugin import build_submissions_log_evidence_probe
from imbue.mngr_grok.plugin import collect_agent_definition_names
from imbue.mngr_grok.plugin import describe_grok_role
from imbue.mngr_grok.plugin import find_grok_key_refusal
from imbue.mngr_grok.plugin import is_cancel_ended_or_idle
from imbue.mngr_grok.plugin import is_cancelled_turn_dropped
from imbue.mngr_grok.plugin import is_grok_interrupt_key
from imbue.mngr_grok.plugin import on_before_create
from imbue.mngr_grok.plugin import on_before_host_destroy
from imbue.mngr_grok.plugin import parse_cancel_settle_probe_output
from imbue.mngr_grok.plugin import parse_claude_enabled_plugin_keys
from imbue.mngr_grok.plugin import parse_grok_host_paths
from imbue.mngr_grok.plugin import read_carried_over_privacy_banner_ack
from imbue.mngr_grok.plugin import register_agent_type
from imbue.mngr_grok.resources import grok_hook
from imbue.mngr_grok.resources.grok_hook import ACTIVE_MARKER_FILENAME
from imbue.mngr_grok.resources.grok_hook import AUTO_WAKE_TAIL_MARKER
from imbue.mngr_grok.resources.grok_hook import PENDING_WAKE_PROMPT_ID_PREFIX
from imbue.mngr_grok.resources.grok_hook import SUBMISSIONS_LOG_FILENAME
from imbue.mngr_grok.resources.grok_hook import normalized_prompt_tail
from imbue.mngr_grok.resources.testing import HOOK_FIXTURES_DIR
from imbue.mngr_grok.resources.testing import read_complete_jsonl_records
from imbue.mngr_grok.resources.testing import read_files_under
from imbue.mngr_grok.testing import make_grok_session_dir
from imbue.mngr_grok.testing import put_executable_first_on_path
from imbue.mngr_grok.testing import read_pane_fixture
from imbue.mngr_grok.testing import select_always_approve_option
from imbue.mngr_grok.testing import write_agent_env_prefix

_PANE_ID: Final[str] = "%41"
_IDLE_PANE: Final[str] = "w170_c_idle_after_turn.txt"
_BUSY_PANE: Final[str] = "w170_e_busy_empty.txt"
_TYPED_PANE: Final[str] = "w170_b_fresh_typed.txt"
_PERMISSION_CARD_PANE: Final[str] = "perm_card.txt"
_TOOL_RUNNING_PANE: Final[str] = "int_5b_tool_busy.txt"
_PAYWALL_PANE: Final[str] = "gates_paywall_free_tier_default_cta.txt"
_LOGIN_PANE: Final[str] = "gates_login_screen_after_paywall_l_logout.txt"
_TRUST_DIALOG_PANE: Final[str] = "g170_trust_dialog_t05.txt"
_CONSENT_PANE: Final[str] = "gates_consent_notice_default_title_and_label.txt"
_QUIT_ARMED_PANE: Final[str] = "int_5c_idle_ctrlc_01.txt"
_BUSY_TYPED_PANE: Final[str] = "w170_f_busy_typed.txt"
_SEND_NOW_PANE: Final[str] = "w170_g_busy_queued1.txt"
_ROOT_SESSION_ID: Final[str] = "01a156e0-c018-7ea0-8286-09c3b145c63a"
_CANCELLED_PROMPT_ID: Final[str] = "7c1d9e52-4b3a-4f6e-9d8c-2e5f1a0b3c47"
_CANCELLED_TURN: Final[GrokRootTurn] = GrokRootTurn(
    session_id=_ROOT_SESSION_ID, prompt_id=_CANCELLED_PROMPT_ID, is_pending_wake=False
)
_OTHER_ROOT_SESSION_ID: Final[str] = "0b2e7f41-9d3c-4a8e-b6f5-3c7d1e9a2f80"
_OTHER_ROOT_PROMPT_ID: Final[str] = "5e8a2c19-7f4b-4d6e-a1c3-9b0d2e4f6a85"
_RESTORED_ONE_LINE_PANE: Final[str] = "drop_restored_one_line.txt"
_RESTORED_FIVE_LINES_PANE: Final[str] = "drop_restored_five_lines.txt"
_RESTORED_PASTED_SIX_LINES_PANE: Final[str] = "drop_restored_pasted_six_lines.txt"
_CLEARED_PANE: Final[str] = "drop_after_clearing_ctrlc.txt"
_GROK_HOOK_LOG_RELATIVE_PATH: Final[Path] = Path("logs", "grok_hooks.log")
_TURN_DROPPED_EVENT: Final[str] = f"event={grok_hook.TURN_DROPPED_FLAG}"
_TURN_DROPPED_LOG_FRAGMENT: Final[str] = (
    f"{_TURN_DROPPED_EVENT} session={_ROOT_SESSION_ID} prompt={_CANCELLED_PROMPT_ID} turn:end active:off"
)
_QUEUED_MESSAGE: Final[str] = "typed while busy one"


def _read_resource(file_name: str) -> str:
    return (Path(grok_resources.__file__).parent / file_name).read_text()


def _read_unrecognised_dialog_pane() -> str:
    """A captured permission card whose footer detect_gate_screen does not know, standing in for grok's other dialogs."""
    return read_pane_fixture(_PERMISSION_CARD_PANE).replace("1/4:select", "↑/↓:choose")


def _make_grok_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    agent_config: GrokAgentConfig,
    agent_class: type[GrokAgent] = GrokAgent,
    is_auto_approve: bool = True,
    is_interactive: bool = False,
) -> GrokAgent:
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    mngr_ctx = local_provider.mngr_ctx.model_copy_update(
        to_update(local_provider.mngr_ctx.field_ref().is_auto_approve, is_auto_approve),
        to_update(local_provider.mngr_ctx.field_ref().is_interactive, is_interactive),
    )
    return agent_class.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-grok"),
        agent_type=AgentTypeName("grok"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=mngr_ctx,
        agent_config=_without_installation_check(agent_config),
        host=host,
    )


def _without_installation_check(agent_config: GrokAgentConfig) -> GrokAgentConfig:
    """The config with check_installation off unless it sets the field, so provisioning never runs npm."""
    if "check_installation" in agent_config.model_fields_set:
        return agent_config
    return agent_config.model_copy_update(to_update(agent_config.field_ref().check_installation, False))


def _write_agent_env_file(agent: GrokAgent, text: str) -> None:
    env_path = agent.host.get_agent_env_path(agent)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(text)


def _provision(agent: GrokAgent, options: CreateAgentOptions | None = None) -> None:
    agent.provision(agent.host, options or CreateAgentOptions(agent_type=AgentTypeName("grok")), agent.mngr_ctx)


def _provision_again_with(agent: GrokAgent, agent_config: GrokAgentConfig) -> None:
    """Provision the same agent again, as `--reuse --update` does after its config changed."""
    _provision(
        agent.model_copy_update(to_update(agent.field_ref().agent_config, _without_installation_check(agent_config)))
    )


def _read_launch_env(agent: GrokAgent) -> str:
    return (agent._get_agent_dir() / LAUNCH_ENV_RELATIVE_PATH).read_text()


def _grok_home(agent: GrokAgent) -> Path:
    return get_grok_home(agent._get_agent_dir())


def _read_config_toml(agent: GrokAgent) -> dict[str, Any]:
    return tomllib.loads((_grok_home(agent) / "config.toml").read_text())


class _ConfirmingGrokAgent(GrokAgent):
    """Answers yes at the folder-trust prompt."""

    def _prompt_user_to_trust_grok_folder(self, trust_key: Path, user_store_path: Path) -> bool:
        return True


class _DecliningGrokAgent(GrokAgent):
    """Answers no at the folder-trust prompt."""

    def _prompt_user_to_trust_grok_folder(self, trust_key: Path, user_store_path: Path) -> bool:
        return False


class _UnpromptableGrokAgent(GrokAgent):
    """Fails the test if provisioning asks the folder-trust question."""

    def _prompt_user_to_trust_grok_folder(self, trust_key: Path, user_store_path: Path) -> bool:
        raise AssertionError("the folder-trust prompt must not be shown")


class _PaneHost(ScriptedHost):
    """A ScriptedHost whose pane captures replay `pane_contents` (the last one repeats) and whose files live in memory."""

    is_local: bool = False
    pane_contents: list[str] = Field(default_factory=list)
    capture_count: int = 0
    capture_monotonic_times: list[float] = Field(default_factory=list)
    files: dict[str, str] = Field(default_factory=dict)

    def execute_idempotent_command(
        self, command: str, timeout_seconds: float | None = None, **_: object
    ) -> CommandResult:
        if "capture-pane" in command:
            pane_idx = min(self.capture_count, len(self.pane_contents) - 1)
            self.capture_count += 1
            self.capture_monotonic_times.append(time.monotonic())
            return CommandResult(stdout=self.pane_contents[pane_idx], stderr="", success=True)
        self.captured.append(command)
        self.captured_timeout_seconds.append(timeout_seconds)
        return CommandResult(stdout="", stderr="", success=True)

    def read_text_file(self, path: Path, encoding: str = "utf-8") -> str:
        if str(path) not in self.files:
            raise FileNotFoundError(str(path))
        return self.files[str(path)]

    def write_text_file(
        self, path: Path, content: str, encoding: str = "utf-8", mode: str | None = None, is_atomic: bool = True
    ) -> None:
        self.files[str(path)] = content

    def build_source_env_prefix(self, agent: object) -> str:
        return "set -a && set +a && "

    def list_directory(self, path: Path, *, recursive: bool = False) -> list[VolumeFile]:
        return [
            VolumeFile(path=file_path, file_type=FileType.FILE, mtime=0, size=len(content))
            for file_path, content in self.files.items()
            if Path(file_path).parent == path
        ]


def _make_pane_agent(temp_mngr_ctx: MngrContext, *pane_file_names: str) -> tuple[GrokAgent, _PaneHost]:
    host = _PaneHost(pane_id=_PANE_ID, pane_contents=[read_pane_fixture(name) for name in pane_file_names])
    agent = GrokAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("pane-grok"),
        agent_type=AgentTypeName("grok"),
        work_dir=Path("/tmp/pane-grok-work"),
        create_time=datetime.now(timezone.utc),
        host_id=HostId.generate(),
        mngr_ctx=temp_mngr_ctx,
        agent_config=GrokAgentConfig(),
        host=host,
    )
    return agent, host


def _record_root_turn(agent: GrokAgent, host: _PaneHost, prompt_id: str) -> None:
    """Record `prompt_id` as the root session's top-level turn in flight, in the hook handler's `turns/` file."""
    turn_path = agent._get_agent_dir() / ROOT_TURNS_RELATIVE_PATH / _ROOT_SESSION_ID
    host.files[str(turn_path)] = f"{prompt_id}\n{AUTO_WAKE_TAIL_MARKER}\n"


def _agent_file_key(agent: GrokAgent, relative_path: Path | str) -> str:
    return str(agent._get_agent_dir() / relative_path)


# Configuration and registration


def test_grok_agent_config_has_expected_defaults() -> None:
    config = GrokAgentConfig()

    assert str(config.command) == "grok"
    assert config.cli_args == ()
    assert config.model is None
    assert config.reasoning_effort is None
    assert config.output_style is None
    assert config.append_system_prompt == ()
    assert config.auto_allow_permissions is False
    assert config.auto_dismiss_dialogs_at_startup is False
    assert config.settings_overrides == {}
    assert config.share_auth is True
    assert config.isolate_from_claude_config is True
    assert config.pin_privacy_settings is True
    assert config.memory is False
    assert config.check_installation is True
    assert config.version is None
    assert config.version_mismatch == VersionMismatchPolicy.ERROR
    assert config.update_policy is None
    assert config.emit_common_transcript is True
    assert config.preserve_on_destroy is True


def test_settings_overrides_accumulate_across_config_layers() -> None:
    lower_layer = GrokAgentConfig(settings_overrides={"ui": {"show_tips": True}})
    upper_layer = GrokAgentConfig(settings_overrides={"features": {"feedback": True}})

    merged, narrowings = merge_models_via_overlay(lower_layer, upper_layer)

    assert merged.settings_overrides == {"ui": {"show_tips": True}, "features": {"feedback": True}}
    assert narrowings == []


def test_register_agent_type_returns_grok_class_and_config() -> None:
    name, agent_class, config_class = register_agent_type()

    assert name == "grok"
    assert agent_class is GrokAgent
    assert config_class is GrokAgentConfig


def test_grok_agent_declares_the_capability_mixins() -> None:
    for mixin in (
        CliBackedAgentMixin,
        HasCommonTranscriptMixin,
        HasSessionPreservationMixin,
        HasSessionAdoptionMixin,
        HasUnattendedModeMixin,
        HasPermissionPolicyMixin,
        HasVersionManagementMixin,
        HasAutoInstallMixin,
    ):
        assert issubclass(GrokAgent, mixin), mixin
    assert not issubclass(GrokAgent, HasCompactionMixin)


def test_get_expected_process_name_is_grok(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig(command=CommandString("env X=1 grok")))

    assert agent.get_expected_process_name() == "grok"


def test_a_listing_that_knows_only_the_grok_type_expects_the_grok_process() -> None:
    try:
        register_agent_class("grok", GrokAgent)
        result = resolve_expected_process_name(
            "grok",
            CommandString('bash "$MNGR_AGENT_STATE_DIR/commands/grok_launch.sh" python3 oom_launch.py grok'),
            MngrConfig.model_construct(agent_types={}),
        )
    finally:
        reset_agent_class_registry()

    assert result == "grok"


def test_ready_indicator_is_the_per_send_pane_predicate(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    indicator = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig()).get_tui_ready_indicator()

    assert indicator is is_ready_for_paste


def test_paste_placeholder_prefixes_match_the_chip_grok_draws_for_a_paste() -> None:
    composer_text = read_composer_text(read_pane_fixture("p170_paste_four_line_paste_composer.txt"))

    assert composer_text.startswith(GrokAgent.PASTE_PLACEHOLDER_PREFIXES)


def test_assemble_command_runs_the_launch_script_with_the_grok_words(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig(cli_args=("--verbose",)))

    command = agent.assemble_command(agent.host, ("-p", "two words"), None)

    assert command == CommandString(
        "bash \"$MNGR_AGENT_STATE_DIR/commands/grok_launch.sh\" grok --verbose -p 'two words'"
    )


def test_assemble_command_honours_command_override(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig())

    command = agent.assemble_command(agent.host, (), CommandString("env FOO=1 /opt/grok"))

    assert command == CommandString('bash "$MNGR_AGENT_STATE_DIR/commands/grok_launch.sh" env FOO=1 /opt/grok')


def test_enter_retry_gate_is_the_footer_first_hint_condition(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    gate = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig())._build_enter_retry_gate("hello typed text")

    assert gate is not None
    assert gate.condition_command == GROK_ENTER_RETRY_CONDITION


# Environment, installation and permissions


def test_modify_env_vars_disables_grok_autoupdater_by_default(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig())
    env_vars: dict[str, str] = {}

    agent.modify_env_vars(agent.host, env_vars)

    assert env_vars == {"GROK_DISABLE_AUTOUPDATER": "1"}


def test_modify_env_vars_keeps_a_user_autoupdater_value(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig())
    env_vars = {"GROK_DISABLE_AUTOUPDATER": "0"}

    agent.modify_env_vars(agent.host, env_vars)

    assert env_vars == {"GROK_DISABLE_AUTOUPDATER": "0"}


@pytest.mark.parametrize("update_policy", [AgentUpdatePolicy.AUTO, AgentUpdatePolicy.ASK])
def test_modify_env_vars_leaves_the_autoupdater_on_for_auto_updates(
    local_provider: LocalProviderInstance, tmp_path: Path, update_policy: AgentUpdatePolicy
) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig(update_policy=update_policy))
    env_vars: dict[str, str] = {}

    agent.modify_env_vars(agent.host, env_vars)

    assert env_vars == {}


@pytest.mark.parametrize("update_policy", [AgentUpdatePolicy.AUTO, AgentUpdatePolicy.ASK])
def test_provision_leaves_the_updater_on_in_the_config_for_auto_updates(
    local_provider: LocalProviderInstance, tmp_path: Path, update_policy: AgentUpdatePolicy
) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig(update_policy=update_policy))

    _provision(agent)

    assert _read_config_toml(agent)["cli"] == {"use_leader": False, "show_tips": False}


@pytest.mark.parametrize(
    ("version", "expected_command"),
    [
        (
            None,
            'env -u GROK_HOME npm install -g @xai-official/grok && "$(npm prefix -g)/bin/grok" --version >/dev/null',
        ),
        (
            "1.0.62",
            "env -u GROK_HOME npm install -g @xai-official/grok@1.0.62"
            ' && "$(npm prefix -g)/bin/grok" --version >/dev/null',
        ),
    ],
)
def test_get_install_command(
    local_provider: LocalProviderInstance, tmp_path: Path, version: str | None, expected_command: str
) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig(version=version))

    assert agent.get_install_binary_name() == "grok"
    assert agent.get_install_command() == expected_command


def _put_standin_grok_on_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, version_banner: str) -> Path:
    """Put a `grok` that prints `version_banner` first on PATH; returns the file it appends its arguments to."""
    invocation_log_path = tmp_path / "standin-grok-invocations.txt"
    put_executable_first_on_path(
        tmp_path / "standin-bin",
        monkeypatch,
        "grok",
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f'echo "$*" >> {shlex.quote(str(invocation_log_path))}\necho {shlex.quote(version_banner)}\n',
    )
    return invocation_log_path


def test_reconcile_installed_version_accepts_the_pinned_grok(
    local_provider: LocalProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocation_log_path = _put_standin_grok_on_path(monkeypatch, tmp_path, "grok 1.0.62 (4f2a9c1)")
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig(version="1.0.62"))

    agent.reconcile_installed_version(agent.host, agent.mngr_ctx)

    assert invocation_log_path.read_text() == "--version\n"


def test_reconcile_installed_version_rejects_another_grok(
    local_provider: LocalProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put_standin_grok_on_path(monkeypatch, tmp_path, "grok 1.0.70 (4f2a9c1)")
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig(version="1.0.62"))

    with pytest.raises(AgentInstallationError, match="1.0.62"):
        agent.reconcile_installed_version(agent.host, agent.mngr_ctx)


def test_reconcile_installed_version_continues_on_another_grok_under_warn(
    local_provider: LocalProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocation_log_path = _put_standin_grok_on_path(monkeypatch, tmp_path, "grok 1.0.70 (4f2a9c1)")
    agent = _make_grok_agent(
        local_provider, tmp_path, GrokAgentConfig(version="1.0.62", version_mismatch=VersionMismatchPolicy.WARN)
    )

    with capture_loguru(level="WARNING") as log_output:
        agent.reconcile_installed_version(agent.host, agent.mngr_ctx)

    assert "grok version mismatch" in log_output.getvalue()
    assert "version_mismatch = WARN" in log_output.getvalue()
    assert invocation_log_path.read_text() == "--version\n"


def test_reconcile_installed_version_without_a_pin_runs_nothing(temp_mngr_ctx: MngrContext) -> None:
    agent, host = _make_pane_agent(temp_mngr_ctx, _IDLE_PANE)

    agent.reconcile_installed_version(agent.host, temp_mngr_ctx)

    assert host.captured == []


def test_provision_refuses_a_grok_on_path_that_is_not_grok_build(
    local_provider: LocalProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put_standin_grok_on_path(monkeypatch, tmp_path, "0.0.34")
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig(check_installation=True))

    with pytest.raises(AgentInstallationError, match=r"reported '0\.0\.34', which is not xAI's Grok Build.*grok-cli"):
        _provision(agent)

    assert not (_grok_home(agent) / "config.toml").exists()


def test_provision_checks_the_grok_on_path_when_the_command_is_a_wrapper(
    local_provider: LocalProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put_standin_grok_on_path(monkeypatch, tmp_path, "0.0.34")
    wrapper_command = CommandString(f"{tmp_path / 'wrapper-that-resolves-only-at-launch'} grok")
    agent = _make_grok_agent(
        local_provider, tmp_path, GrokAgentConfig(command=wrapper_command, check_installation=True)
    )

    with pytest.raises(AgentInstallationError, match=r"`grok --version` reported '0\.0\.34'"):
        _provision(agent)


def test_provision_accepts_grok_build_on_path_after_one_version_check(
    local_provider: LocalProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocation_log_path = _put_standin_grok_on_path(monkeypatch, tmp_path, "grok 1.0.62 (4f2a9c1)")
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig(check_installation=True))

    _provision(agent)

    assert invocation_log_path.read_text() == "--version\n"
    assert (_grok_home(agent) / "config.toml").is_file()


@pytest.mark.parametrize("exit_status", [1, 0])
def test_provision_skips_the_grok_build_check_when_grok_reports_no_version(
    local_provider: LocalProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_status: int
) -> None:
    put_executable_first_on_path(tmp_path / "silent-grok-bin", monkeypatch, "grok", f"#!/bin/sh\nexit {exit_status}\n")
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig(check_installation=True))

    with capture_loguru(level="DEBUG") as log_output:
        _provision(agent)

    assert "skipping the Grok Build check" in log_output.getvalue()
    assert (_grok_home(agent) / "config.toml").is_file()


def test_provision_without_the_installation_check_runs_no_grok(
    local_provider: LocalProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocation_log_path = _put_standin_grok_on_path(monkeypatch, tmp_path, "0.0.34")
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig(check_installation=False))

    _provision(agent)

    assert not invocation_log_path.exists()
    assert (_grok_home(agent) / "config.toml").is_file()


@pytest.mark.parametrize("auto_allow_permissions", [True, False])
def test_is_unattended_enabled_follows_auto_allow_permissions(
    local_provider: LocalProviderInstance, tmp_path: Path, auto_allow_permissions: bool
) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig(auto_allow_permissions=auto_allow_permissions))

    assert agent.is_unattended_enabled() is auto_allow_permissions


@pytest.mark.parametrize(
    ("settings_overrides", "expected_policy"),
    [
        ({}, {}),
        ({"permission": {"deny": ["Bash(rm *)"]}}, {"deny": ["Bash(rm *)"]}),
        ({"permission__extend": {"allow": ["Read"]}}, {"allow": ["Read"]}),
        ({"permission": "ask"}, {}),
    ],
)
def test_get_permission_policy_reads_the_permission_table(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    settings_overrides: dict[str, Any],
    expected_policy: Mapping[str, Any],
) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig(settings_overrides=settings_overrides))

    assert agent.get_permission_policy() == expected_policy


# Provisioning


def test_provision_renders_the_grok_home_and_launch_state(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig())

    _provision(agent)

    grok_home = _grok_home(agent)
    agent_dir = agent._get_agent_dir()
    config = _read_config_toml(agent)
    assert config["ui"] == {
        "permission_mode": "ask",
        "prompt_suggestions": False,
        "cancel_subagents_on_turn_cancel": "always_continue",
    }
    assert config["memory"] == {"enabled": False}
    assert config["cli"] == {"auto_update": False, "use_leader": False, "show_tips": False}
    assert config["features"] == {"telemetry": False, "feedback": False, "campaigns": False, "remote_fetch": True}
    assert config["claude_compat"] == {"imported": True}
    assert stat.S_IMODE((grok_home / "config.toml").stat().st_mode) == 0o600
    assert (grok_home / "requirements.toml").is_file()
    assert (grok_home / "hooks" / "mngr.json").read_text() == _json_dump(build_hooks_json())
    hook_handler = grok_home / "hooks" / "grok_hook.py"
    assert hook_handler.read_text() == _read_resource("grok_hook.py")
    assert stat.S_IMODE(hook_handler.stat().st_mode) == 0o755
    assert (grok_home / "sessions").is_dir()
    assert (grok_home / "trusted_folders.toml").is_file()
    assert (agent_dir / SESSION_ID_RELATIVE_PATH).read_text() == f"{agent.id.get_uuid()}\n"
    assert not (agent_dir / ROLE_PROMPT_RELATIVE_PATH).exists()
    assert _read_launch_env(agent) == build_launch_env(
        grok_home=grok_home,
        work_dir=Path(agent.work_dir),
        shared_user_auth_path=tmp_path / ".grok" / "auth.json",
        model=None,
        reasoning_effort=None,
        is_privacy_pinned=True,
        is_isolated_from_claude_config=True,
    )
    for script_name in (
        "grok_launch.sh",
        "grok_background_tasks.sh",
        "stream_transcript.sh",
        "grok_transcript_streamer.py",
        "common_transcript.sh",
        "common_transcript_convert.py",
    ):
        script_path = agent_dir / "commands" / script_name
        assert script_path.read_text() == _read_resource(script_name), script_name
        assert stat.S_IMODE(script_path.stat().st_mode) == 0o755


def _json_dump(value: Mapping[str, Any]) -> str:
    return json.dumps(value, indent=2) + "\n"


def test_provision_twice_leaves_the_same_files(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    agent = _make_grok_agent(
        local_provider, tmp_path, GrokAgentConfig(append_system_prompt=(SystemPromptText("Be brief."),))
    )
    _provision(agent)
    first_files = read_files_under(agent._get_agent_dir())

    _provision(agent)

    assert read_files_under(agent._get_agent_dir()) == first_files


def test_provision_keeps_the_privacy_banner_ack_time_of_the_existing_config(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig())
    _provision(agent)
    config_path = _grok_home(agent) / "config.toml"
    config_document = tomlkit.parse(config_path.read_text())
    config_document["privacy"]["privacy_banner_acked"] = "2026-01-02T03:04:05Z"
    config_path.write_text(tomlkit.dumps(config_document))

    _provision(agent)

    assert _read_config_toml(agent)["privacy"] == {"privacy_banner_acked": "2026-01-02T03:04:05Z"}


def test_provision_carries_over_model_choices_consent_answers_and_session_id(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig())
    _provision(agent)
    config_path = _grok_home(agent) / "config.toml"
    config_path.write_text(
        config_path.read_text()
        + '\n[models]\ndefault = "grok-4.6"\ndefault_reasoning_effort = "high"\n'
        + '\n[consent.answers.retention]\nversion = 3\naccount = "a@example.com"\naccepted = true\n'
    )
    (tmp_path / ".grok" / "config.toml").write_text(
        '[consent.answers.terms]\nversion = 1\naccount = "a@example.com"\naccepted = true\n'
    )
    session_id_path = agent._get_agent_dir() / SESSION_ID_RELATIVE_PATH
    session_id_path.write_text("01a156d6-f927-7091-91a4-f6692a5800da\n")

    _provision(agent)

    config = _read_config_toml(agent)
    assert config["models"] == {"default": "grok-4.6", "default_reasoning_effort": "high"}
    assert sorted(config["consent"]["answers"]) == ["retention", "terms"]
    assert config["consent"]["answers"]["retention"]["version"] == 3
    assert session_id_path.read_text() == "01a156d6-f927-7091-91a4-f6692a5800da\n"


def test_provision_drops_the_model_and_effort_it_rendered_once_they_are_unset(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig(model="grok-4.6", reasoning_effort="high"))
    _provision(agent)
    assert _read_config_toml(agent)["models"] == {"default": "grok-4.6", "default_reasoning_effort": "high"}

    _provision_again_with(agent, GrokAgentConfig())

    assert "models" not in _read_config_toml(agent)


def test_provision_replaces_an_empty_session_id(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig())
    session_id_path = agent._get_agent_dir() / SESSION_ID_RELATIVE_PATH
    session_id_path.parent.mkdir(parents=True)
    session_id_path.write_text("\n")

    _provision(agent)

    assert session_id_path.read_text() == f"{agent.id.get_uuid()}\n"


def test_provision_writes_the_role_and_removes_it_once_the_role_is_gone(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    work_styles_dir = tmp_path / "work" / ".agents" / "output-styles"
    work_styles_dir.mkdir(parents=True)
    style_text = "---\nname: Terse\n---\nAnswer in one line.\n"
    (work_styles_dir / "terse.md").write_text(style_text)
    config = GrokAgentConfig(
        append_system_prompt=(SystemPromptText("You review code."),), output_style=OutputStyleName("Terse")
    )
    agent = _make_grok_agent(local_provider, tmp_path, config)

    _provision(agent)

    role_path = agent._get_agent_dir() / ROLE_PROMPT_RELATIVE_PATH
    assert role_path.read_text() == "You review code.\n\n" + style_text

    _provision_again_with(agent, GrokAgentConfig())

    assert not role_path.exists()


def test_provision_refuses_a_role_over_grok_argument_limit(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    oversized_role = SystemPromptText("é" * 50_001)
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig(append_system_prompt=(oversized_role,)))

    with pytest.raises(UserInputError, match="100002 bytes"):
        _provision(agent)


def test_provision_removes_requirements_when_privacy_pins_are_off(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig())
    _provision(agent)
    assert (_grok_home(agent) / "requirements.toml").is_file()

    _provision_again_with(agent, GrokAgentConfig(pin_privacy_settings=False))

    assert not (_grok_home(agent) / "requirements.toml").exists()


def test_provision_folds_settings_overrides_onto_the_rendered_config(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    overrides = {"ui__extend": {"follow_up_behavior": "queue"}, "model": {"local": {"base_url": "http://127.0.0.1"}}}
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig(settings_overrides=overrides))

    _provision(agent)

    config = _read_config_toml(agent)
    assert config["ui"] == {
        "permission_mode": "ask",
        "prompt_suggestions": False,
        "cancel_subagents_on_turn_cancel": "always_continue",
        "follow_up_behavior": "queue",
    }
    assert config["model"] == {"local": {"base_url": "http://127.0.0.1"}}
    assert config["features"]["remote_fetch"] is False


def test_provision_refuses_settings_overrides_that_drop_mngr_keys(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig(settings_overrides={"ui": {"theme": "dark"}}))

    with pytest.raises(ConfigParseError, match="__mngr_merge"):
        _provision(agent)


def test_provision_disables_claude_and_legacy_grok_agent_definitions(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    claude_agents_dir = tmp_path / ".claude" / "agents"
    claude_agents_dir.mkdir(parents=True)
    (claude_agents_dir / "reviewer.md").write_text("---\nname: code-reviewer\n---\nReview.\n")
    legacy_agents_dir = tmp_path / ".grok" / "agents"
    legacy_agents_dir.mkdir(parents=True)
    (legacy_agents_dir / "planner.md").write_text("Plan things.\n")
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig())

    _provision(agent)

    assert _read_config_toml(agent)["subagents"] == {
        "toggle": {"code-reviewer": False, "planner": False, "reviewer": False}
    }


def test_provision_without_claude_isolation_disables_the_enabled_claude_plugins(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    claude_dir = tmp_path / ".claude"
    (claude_dir / "agents").mkdir(parents=True)
    (claude_dir / "agents" / "reviewer.md").write_text("Review.\n")
    (claude_dir / "settings.json").write_text(
        '{"enabledPlugins": {"probe@market": true, "other@market": false, "probe@elsewhere": true}}'
    )
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig(isolate_from_claude_config=False))

    _provision(agent)

    config = _read_config_toml(agent)
    assert config["plugins"] == {"disabled": ["other", "probe"]}
    assert "claude_compat" not in config
    assert "subagents" not in config


def test_provision_without_shared_auth_keeps_the_credential_in_the_agent_home(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    (tmp_path / ".grok").mkdir()
    (tmp_path / ".grok" / "config.toml").write_text(
        '[consent.answers.terms]\nversion = 1\naccount = "a@example.com"\naccepted = true\n'
    )
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig(share_auth=False))

    _provision(agent)

    launch_env = _read_launch_env(agent)
    assert "launch_is_auth_shared=false" in launch_env
    assert "GROK_AUTH_PATH" in launch_env.split("launch_removed_environment=")[1]
    assert "consent" not in _read_config_toml(agent)


def test_provision_keeps_remote_settings_on_for_an_api_key_in_the_agent_env(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig())
    _write_agent_env_file(agent, "XAI_API_KEY=xai-test-key\n")

    _provision(agent)

    assert _read_config_toml(agent)["features"]["remote_fetch"] is True


def test_provision_takes_the_env_file_credential_path_and_the_consent_answers_beside_it(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    (tmp_path / "accounts").mkdir()
    (tmp_path / "accounts" / "config.toml").write_text(
        '[consent.answers.work_terms]\nversion = 1\naccount = "work@example.com"\naccepted = true\n'
    )
    (tmp_path / ".grok").mkdir()
    (tmp_path / ".grok" / "config.toml").write_text(
        '[consent.answers.home_terms]\nversion = 1\naccount = "home@example.com"\naccepted = true\n'
    )
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig())
    _write_agent_env_file(agent, f"GROK_AUTH_PATH={tmp_path / 'accounts' / 'work.json'}\n")

    _provision(agent)

    launch_env = _read_launch_env(agent)
    assert f"launch_user_auth_path={tmp_path / 'accounts' / 'work.json'}" in launch_env
    assert sorted(_read_config_toml(agent)["consent"]["answers"]) == ["work_terms"]


def test_provision_inside_a_grok_tool_ignores_the_inherited_credential_path(
    local_provider: LocalProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GROK_AGENT", "1")
    monkeypatch.setenv("GROK_AUTH_PATH", str(tmp_path / "creator-account.json"))
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig())

    _provision(agent)

    launch_env = _read_launch_env(agent)
    assert f"launch_user_auth_path={tmp_path / '.grok' / 'auth.json'}" in launch_env


@pytest.mark.parametrize(
    ("agent_class", "is_interactive", "is_auto_dismiss_dialogs_at_startup"),
    [
        pytest.param(_ConfirmingGrokAgent, True, False, id="confirmed_on_a_terminal"),
        pytest.param(_UnpromptableGrokAgent, True, True, id="auto_dismiss_without_a_prompt"),
    ],
)
def test_provision_records_the_work_dir_trust_once_consented(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    agent_class: type[GrokAgent],
    is_interactive: bool,
    is_auto_dismiss_dialogs_at_startup: bool,
) -> None:
    agent = _make_grok_agent(
        local_provider,
        tmp_path,
        GrokAgentConfig(auto_dismiss_dialogs_at_startup=is_auto_dismiss_dialogs_at_startup),
        agent_class=agent_class,
        is_auto_approve=False,
        is_interactive=is_interactive,
    )

    _provision(agent)

    expected_folders = {str((tmp_path / "work").resolve()): {"trusted": True}}
    assert tomllib.loads((_grok_home(agent) / "trusted_folders.toml").read_text())["folders"] == expected_folders
    assert tomllib.loads((tmp_path / ".grok" / "trusted_folders.toml").read_text())["folders"] == expected_folders


@pytest.mark.parametrize(
    ("agent_class", "is_interactive"),
    [
        pytest.param(_DecliningGrokAgent, True, id="declined_on_a_terminal"),
        pytest.param(_UnpromptableGrokAgent, False, id="no_terminal_and_no_opt_in"),
    ],
)
def test_provision_stops_without_consent_and_writes_nothing(
    local_provider: LocalProviderInstance, tmp_path: Path, agent_class: type[GrokAgent], is_interactive: bool
) -> None:
    agent = _make_grok_agent(
        local_provider,
        tmp_path,
        GrokAgentConfig(),
        agent_class=agent_class,
        is_auto_approve=False,
        is_interactive=is_interactive,
    )

    with pytest.raises(SystemExit) as exit_info:
        _provision(agent)

    assert exit_info.value.code == 1
    assert not (_grok_home(agent) / "config.toml").exists()
    assert not (_grok_home(agent) / "trusted_folders.toml").exists()
    assert not (tmp_path / ".grok" / "trusted_folders.toml").exists()


def test_provision_stops_when_the_agent_environment_breaks_the_host_paths_command(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig())
    _write_agent_env_file(agent, "echo env-file-output-52817\nexit 3\n")

    with pytest.raises(GrokProvisioningError, match=r"host's paths for grok \(stdout='env-file-output-52817'"):
        _provision(agent)

    assert not _grok_home(agent).exists()


def test_provision_stops_when_the_grok_home_cannot_be_prepared(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig())
    sessions_path = _grok_home(agent) / "sessions"
    sessions_path.parent.mkdir(parents=True)
    sessions_path.write_text("a file where grok's session store belongs\n")

    with pytest.raises(GrokProvisioningError, match=r"Could not prepare .* on the host \(stderr=.*File exists"):
        _provision(agent)

    assert not (_grok_home(agent) / "config.toml").exists()


def _run_grok_host_paths_command(env_prefix: str) -> GrokHostPaths:
    result = subprocess.run(
        ["sh", "-c", build_grok_host_paths_command(env_prefix, is_agent_definition_listed=True)],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return parse_grok_host_paths(result.stdout)


def test_build_grok_host_paths_command_reads_the_agent_environment(tmp_path: Path) -> None:
    agent_home = tmp_path / "agent-home"
    claude_agents_dir = agent_home / ".claude" / "agents"
    claude_agents_dir.mkdir(parents=True)
    (claude_agents_dir / "a.md").write_text("first\n")

    env_prefix = write_agent_env_prefix(tmp_path, {"HOME": str(agent_home), "GROK_HOME": str(tmp_path / "gh")})

    host_paths = _run_grok_host_paths_command(env_prefix)

    assert host_paths.home_dir == agent_home
    assert host_paths.user_grok_home == tmp_path / "gh"
    assert host_paths.user_auth_path == tmp_path / "gh" / "auth.json"
    assert host_paths.agent_definition_text_by_path == {str(claude_agents_dir / "a.md"): "first\n"}


@pytest.mark.parametrize(
    ("inherited_environment", "env_file_grok_home_name", "expected_grok_home_name"),
    [
        pytest.param({}, None, "agent-home/.grok", id="default_under_the_agent_home"),
        pytest.param({}, "env-file-grok", "env-file-grok", id="env_file"),
        pytest.param({"GROK_HOME": "inherited-grok"}, None, "inherited-grok", id="inherited_outside_a_grok_tool"),
        pytest.param(
            {"GROK_AGENT": "1", "GROK_HOME": "creator-agent-grok"},
            None,
            "agent-home/.grok",
            id="inherited_inside_a_grok_tool_is_ignored",
        ),
        pytest.param(
            {"GROK_AGENT": "1", "GROK_HOME": "creator-agent-grok"},
            "env-file-grok",
            "env-file-grok",
            id="env_file_inside_a_grok_tool",
        ),
    ],
)
def test_build_grok_host_paths_command_resolves_the_user_grok_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inherited_environment: dict[str, str],
    env_file_grok_home_name: str | None,
    expected_grok_home_name: str,
) -> None:
    for name, value in inherited_environment.items():
        monkeypatch.setenv(name, str(tmp_path / value) if name == "GROK_HOME" else value)
    env_file_variables = {"HOME": str(tmp_path / "agent-home")}
    if env_file_grok_home_name is not None:
        env_file_variables["GROK_HOME"] = str(tmp_path / env_file_grok_home_name)

    host_paths = _run_grok_host_paths_command(write_agent_env_prefix(tmp_path, env_file_variables))

    assert host_paths.user_grok_home == tmp_path / expected_grok_home_name


@pytest.mark.parametrize(
    ("command_stdout", "message"),
    [
        pytest.param("", "unexpected output", id="empty"),
        pytest.param("/home/u\n", "unexpected output", id="truncated"),
        pytest.param("/home/u\n/home/u/.grok", "unexpected output", id="no_credential_path"),
        pytest.param("relative\n/home/u/.grok\n/a.json\n", "must be absolute", id="relative_home"),
        pytest.param("/home/u\nrelative/grok\nrelative/grok/auth.json\n", "relative path", id="relative_grok_home"),
        pytest.param("/home/u\n/home/u/.grok\nwork.json\n", "must be absolute", id="relative_auth_path"),
    ],
)
def test_parse_grok_host_paths_rejects_unexpected_output(command_stdout: str, message: str) -> None:
    with pytest.raises(GrokProvisioningError, match=message):
        parse_grok_host_paths(command_stdout)


def test_collect_agent_definition_names_uses_stems_and_frontmatter_names() -> None:
    names = collect_agent_definition_names(
        {"/h/.claude/agents/one.md": "---\nname: First One\n---\n", "/h/.grok/agents/two.md": "no frontmatter\n"}
    )

    assert names == ["First One", "one", "two"]


def test_collect_agent_definition_names_reads_a_description_a_strict_frontmatter_parser_rejects() -> None:
    definition = (
        "---\nname: code-reviewer\ndescription: Use this agent for reviews. Examples: <example>Context: The user "
        'wrote code. user: "review it" assistant: "I will use the code-reviewer agent"</example>\nmodel: sonnet\n'
        "---\nReview.\n"
    )

    names = collect_agent_definition_names({"/h/.claude/agents/reviewer.md": definition})

    assert names == ["code-reviewer", "reviewer"]


@pytest.mark.parametrize(
    ("settings_text", "expected_keys"),
    [("", ()), ("{}", ()), ('{"enabledPlugins": {"b@m": true, "a@m": false}}', ("a@m", "b@m"))],
)
def test_parse_claude_enabled_plugin_keys(settings_text: str, expected_keys: tuple[str, ...]) -> None:
    assert parse_claude_enabled_plugin_keys(settings_text, Path("/h/.claude/settings.json")) == expected_keys


@pytest.mark.parametrize("settings_text", ["{not json", "[]", '{"enabledPlugins": []}'])
def test_parse_claude_enabled_plugin_keys_rejects_malformed_settings(settings_text: str) -> None:
    with pytest.raises(GrokProvisioningError, match="settings.json"):
        parse_claude_enabled_plugin_keys(settings_text, Path("/h/.claude/settings.json"))


@pytest.mark.parametrize(
    ("existing_config", "expected"),
    [
        ({}, None),
        (
            {"privacy": {"privacy_banner_acked": "2026-10-19T12:00:00Z"}},
            datetime(2026, 10, 19, 12, tzinfo=timezone.utc),
        ),
        (
            {"privacy": {"privacy_banner_acked": datetime(2026, 1, 2, 3, 4, 5)}},
            datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        ),
        ({"privacy": {"privacy_banner_acked": "yesterday"}}, None),
        ({"privacy": "acked"}, None),
    ],
)
def test_read_carried_over_privacy_banner_ack(existing_config: dict[str, Any], expected: datetime | None) -> None:
    assert read_carried_over_privacy_banner_ack(existing_config) == expected


# Readiness


def test_wait_for_ready_signal_returns_once_grok_is_idle_after_session_start(temp_mngr_ctx: MngrContext) -> None:
    agent, host = _make_pane_agent(temp_mngr_ctx, _BUSY_PANE, _BUSY_PANE, _IDLE_PANE)
    host.files[_agent_file_key(agent, "grok_session_started")] = ""
    started: list[str] = []

    agent.wait_for_ready_signal(is_readiness_awaited=True, start_action=lambda: started.append("start"), timeout=5.0)

    assert started == ["start"]
    assert host.capture_count == 3


@pytest.mark.parametrize(
    ("gate_screen", "is_auth_shared", "is_at_startup", "advice"),
    [
        pytest.param(GrokGateScreen.LOGIN, True, False, "then run `mngr create g --reuse --update`", id="login_send"),
        pytest.param(GrokGateScreen.CONSENT, True, True, "the same `mngr create` again", id="consent_startup"),
        pytest.param(GrokGateScreen.CONSENT, False, True, "does not copy your answer", id="consent_startup_unshared"),
        pytest.param(GrokGateScreen.CONSENT, True, False, "--reuse --update` so the agent copies", id="consent_send"),
        pytest.param(
            GrokGateScreen.CONSENT, False, False, "accept it in the agent's terminal", id="consent_send_unshared"
        ),
    ],
)
def test_gate_screen_advice_matches_auth_sharing_and_whether_the_agent_is_starting(
    gate_screen: GrokGateScreen, is_auth_shared: bool, is_at_startup: bool, advice: str
) -> None:
    error = GrokInputBlockedError("g", gate_screen, is_auth_shared=is_auth_shared, is_at_startup=is_at_startup)

    assert advice in error.reason
    assert ("copies the answer" in error.reason) == (gate_screen is GrokGateScreen.CONSENT and is_auth_shared)


@pytest.mark.parametrize(
    ("pane_file_name", "gate_screen", "message_fragment"),
    [
        (_TRUST_DIALOG_PANE, GrokGateScreen.TRUST_DIALOG, "folder trust.*report the problem"),
        (_LOGIN_PANE, GrokGateScreen.LOGIN, "grok login.*then run the same `mngr create` again"),
        (_CONSENT_PANE, GrokGateScreen.CONSENT, "accept it, then run the same `mngr create` again"),
        (_PAYWALL_PANE, GrokGateScreen.PAYWALL, "no subscription"),
        (_PERMISSION_CARD_PANE, GrokGateScreen.PERMISSION_CARD, "permission request while it starts"),
    ],
)
@pytest.mark.parametrize(
    "is_session_started",
    [pytest.param(False, id="before_session_start"), pytest.param(True, id="after_session_start")],
)
def test_wait_for_ready_signal_fails_fast_on_a_gate_screen_without_naming_the_agent_terminal(
    temp_mngr_ctx: MngrContext,
    pane_file_name: str,
    gate_screen: GrokGateScreen,
    message_fragment: str,
    is_session_started: bool,
) -> None:
    pane_file_names = (_BUSY_PANE, pane_file_name) if is_session_started else (pane_file_name,)
    agent, host = _make_pane_agent(temp_mngr_ctx, *pane_file_names)
    if is_session_started:
        host.files[_agent_file_key(agent, "grok_session_started")] = ""

    with pytest.raises(GrokInputBlockedError, match=message_fragment) as error_info:
        agent.wait_for_ready_signal(is_readiness_awaited=True, start_action=lambda: None, timeout=10.0)

    assert error_info.value.gate_screen is gate_screen
    assert error_info.value.kind is SendFailureKind.INPUT_BLOCKED
    assert not isinstance(error_info.value, AgentStartError)
    assert "terminal" not in error_info.value.reason
    assert host.capture_count == len(pane_file_names)


def _without_shared_auth(agent: GrokAgent) -> GrokAgent:
    return agent.model_copy_update(to_update(agent.field_ref().agent_config, GrokAgentConfig(share_auth=False)))


def _assert_names_only_xai_api_key(reason: str) -> None:
    assert "--pass-env XAI_API_KEY" in reason
    for ignored_remedy in ("grok login", "GROK_AUTH_PATH", "again"):
        assert ignored_remedy not in reason


def test_wait_for_ready_signal_on_the_login_screen_without_shared_auth_asks_only_for_xai_api_key(
    temp_mngr_ctx: MngrContext,
) -> None:
    agent, _ = _make_pane_agent(temp_mngr_ctx, _LOGIN_PANE)

    with pytest.raises(GrokInputBlockedError) as error_info:
        _without_shared_auth(agent).wait_for_ready_signal(
            is_readiness_awaited=True, start_action=lambda: None, timeout=10.0
        )

    assert error_info.value.gate_screen is GrokGateScreen.LOGIN
    _assert_names_only_xai_api_key(error_info.value.reason)


def test_preflight_on_the_login_screen_without_shared_auth_asks_only_for_xai_api_key(
    temp_mngr_ctx: MngrContext,
) -> None:
    agent, _ = _make_pane_agent(temp_mngr_ctx, _LOGIN_PANE)
    unshared_agent = _without_shared_auth(agent)

    with pytest.raises(GrokInputBlockedError) as error_info:
        unshared_agent._preflight_send_message(unshared_agent.tmux_target)

    assert error_info.value.gate_screen is GrokGateScreen.LOGIN
    _assert_names_only_xai_api_key(error_info.value.reason)


class _ConsentNoticeGrokAgent(GrokAgent):
    """A grok agent whose pane shows grok's consent notice."""

    def _capture_pane_content(
        self, tmux_target: TmuxWindowTarget | str, include_scrollback: bool = False, include_styles: bool = False
    ) -> str | None:
        return read_pane_fixture(_CONSENT_PANE)


@pytest.mark.tmux
def test_create_update_keeps_an_agent_whose_restart_stops_on_a_gate_screen(
    temp_mngr_ctx: MngrContext, temp_work_dir: Path
) -> None:
    consent_agent_type = AgentTypeName("grok-consent-notice")
    register_agent_class(consent_agent_type, _ConsentNoticeGrokAgent)
    register_agent_config(consent_agent_type, GrokAgentConfig)
    agent_types = {
        **temp_mngr_ctx.config.agent_types,
        consent_agent_type: GrokAgentConfig(check_installation=False, auto_dismiss_dialogs_at_startup=True),
    }
    config = temp_mngr_ctx.config.model_copy_update(
        to_update(temp_mngr_ctx.config.field_ref().agent_types, agent_types)
    )
    mngr_ctx = temp_mngr_ctx.model_copy_update(to_update(temp_mngr_ctx.field_ref().config, config))
    host = get_local_host(mngr_ctx)
    agent_name = AgentName("updated-grok")

    with tmux_session_cleanup(f"{mngr_ctx.config.prefix}{agent_name}"):
        original = create(
            source_location=HostLocation(host=host, path=temp_work_dir),
            target_host=host,
            agent_options=CreateAgentOptions(
                name=agent_name,
                agent_type=AgentTypeName(PLACEHOLDER_AGENT_TYPE),
                command=CommandString("sleep 60"),
                transfer_mode=TransferMode.NONE,
                label_options=AgentLabelOptions(),
            ),
            mngr_ctx=mngr_ctx,
            create_work_dir=False,
        )
        host.stop_agents([original.agent.id])

        with pytest.raises(AgentStartError, match="notice from xAI") as error_info:
            create(
                source_location=HostLocation(host=host, path=temp_work_dir),
                target_host=host,
                agent_options=CreateAgentOptions(
                    name=agent_name,
                    agent_id=original.agent.id,
                    agent_type=consent_agent_type,
                    command=CommandString("sleep 60"),
                    transfer_mode=TransferMode.NONE,
                    target_path=original.agent.work_dir,
                    label_options=AgentLabelOptions(),
                    is_update=True,
                ),
                mngr_ctx=mngr_ctx,
                create_work_dir=False,
            )

        assert [agent.id for agent in host.get_agents()] == [original.agent.id]
        assert isinstance(error_info.value.__cause__, GrokInputBlockedError)


def test_wait_for_ready_signal_times_out_without_session_start(temp_mngr_ctx: MngrContext) -> None:
    agent, _ = _make_pane_agent(temp_mngr_ctx, _IDLE_PANE)

    with pytest.raises(AgentStartError, match="SessionStart hook did not run within 0.3s"):
        agent.wait_for_ready_signal(is_readiness_awaited=True, start_action=lambda: None, timeout=0.3)


def test_wait_for_ready_signal_times_out_while_grok_stays_busy(temp_mngr_ctx: MngrContext) -> None:
    agent, host = _make_pane_agent(temp_mngr_ctx, _BUSY_PANE)
    host.files[_agent_file_key(agent, "grok_session_started")] = ""

    with pytest.raises(AgentStartError, match="not idle and empty"):
        agent.wait_for_ready_signal(is_readiness_awaited=True, start_action=lambda: None, timeout=0.3)


def test_wait_for_ready_signal_only_starts_when_readiness_is_not_awaited(temp_mngr_ctx: MngrContext) -> None:
    agent, host = _make_pane_agent(temp_mngr_ctx, _PAYWALL_PANE)
    started: list[str] = []

    agent.wait_for_ready_signal(is_readiness_awaited=False, start_action=lambda: started.append("start"))

    assert started == ["start"]
    assert host.capture_count == 0


# Sending


def test_preflight_refuses_an_unrecognised_screen_while_a_permission_request_is_recorded(
    temp_mngr_ctx: MngrContext,
) -> None:
    agent, host = _make_pane_agent(temp_mngr_ctx, _IDLE_PANE)
    host.pane_contents = [_read_unrecognised_dialog_pane()]
    host.files[_agent_file_key(agent, "permissions_waiting")] = ""

    with pytest.raises(GrokInputBlockedError, match="permission request") as error_info:
        agent._preflight_send_message(agent.tmux_target)

    assert error_info.value.kind is SendFailureKind.INPUT_BLOCKED
    assert error_info.value.gate_screen is GrokGateScreen.PERMISSION_CARD


def test_preflight_leaves_an_unrecognised_screen_to_the_send_without_a_permission_request(
    temp_mngr_ctx: MngrContext,
) -> None:
    agent, host = _make_pane_agent(temp_mngr_ctx, _IDLE_PANE)
    host.pane_contents = [_read_unrecognised_dialog_pane()]

    pane_content = agent._preflight_send_message(agent.tmux_target)

    assert pane_content == _read_unrecognised_dialog_pane().rstrip("\n")


def test_preflight_allows_a_send_while_a_tool_approved_on_its_permission_card_runs(temp_mngr_ctx: MngrContext) -> None:
    agent, host = _make_pane_agent(temp_mngr_ctx, _TOOL_RUNNING_PANE)
    host.files[_agent_file_key(agent, "permissions_waiting")] = ""

    pane_content = agent._preflight_send_message(agent.tmux_target)

    assert host.capture_count == 1
    assert pane_content == read_pane_fixture(_TOOL_RUNNING_PANE).rstrip("\n")


@pytest.mark.parametrize(
    ("pane_file_name", "gate_screen"),
    [(_PERMISSION_CARD_PANE, GrokGateScreen.PERMISSION_CARD), (_PAYWALL_PANE, GrokGateScreen.PAYWALL)],
)
def test_preflight_refuses_to_type_into_a_gate_screen(
    temp_mngr_ctx: MngrContext, pane_file_name: str, gate_screen: GrokGateScreen
) -> None:
    agent, _ = _make_pane_agent(temp_mngr_ctx, pane_file_name)

    with pytest.raises(GrokInputBlockedError) as error_info:
        agent._preflight_send_message(agent.tmux_target)

    assert error_info.value.gate_screen is gate_screen


def test_preflight_allows_a_busy_composer_and_returns_the_screen_it_read(temp_mngr_ctx: MngrContext) -> None:
    agent, host = _make_pane_agent(temp_mngr_ctx, _BUSY_PANE)

    pane_content = agent._preflight_send_message(agent.tmux_target)

    assert host.capture_count == 1
    assert pane_content == read_pane_fixture(_BUSY_PANE).rstrip("\n")


@pytest.mark.parametrize(
    ("pane_file_name", "expected_text"),
    [
        (_TYPED_PANE, "hello typed text"),
        ("p170_paste_four_line_paste_composer.txt", "[Pasted: 4 lines]"),
        (_IDLE_PANE, None),
        ("perm_after_ctrlc.txt", None),
    ],
)
def test_detect_preexisting_input_text_reads_the_footer_first(
    temp_mngr_ctx: MngrContext, pane_file_name: str, expected_text: str | None
) -> None:
    agent, _ = _make_pane_agent(temp_mngr_ctx, pane_file_name)

    assert agent._detect_preexisting_input_text(read_pane_fixture(pane_file_name)) == expected_text


def test_detect_preexisting_input_text_names_composer_text_it_cannot_read(temp_mngr_ctx: MngrContext) -> None:
    agent, _ = _make_pane_agent(temp_mngr_ctx, _TYPED_PANE)
    typed_text = "hello typed text"
    pane = read_pane_fixture(_TYPED_PANE).replace(typed_text, " " * len(typed_text))

    assert agent._detect_preexisting_input_text(pane) == _UNREADABLE_COMPOSER_TEXT


def _run_probe_command(command: str, base: str | None) -> str:
    script = command if base is None else f"base={shlex.quote(base)}; {command}"
    environment = {key: value for key, value in os.environ.items() if key != "MNGR_AGENT_STATE_DIR"}
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True, timeout=10, env=environment
    )
    return result.stdout.strip()


def _is_probe_confirmed(probe: SubmissionEvidenceProbe, baseline: str) -> bool:
    current = _run_probe_command(probe.poll_command, baseline)
    return current != "" and current != baseline


def _make_state_dir_env_prefix(tmp_path: Path) -> tuple[Path, str]:
    state_dir = tmp_path / "state"
    (state_dir / GROK_PLUGIN_STATE_RELATIVE_PATH).mkdir(parents=True)
    return state_dir, write_agent_env_prefix(tmp_path, {"MNGR_AGENT_STATE_DIR": str(state_dir)})


def _append_submissions_log_line(state_dir: Path, prompt_id: str, tail: str) -> None:
    """Append the `<promptId> <tail>` line mngr's hook logs for a submitted top-level prompt."""
    with (state_dir / GROK_PLUGIN_STATE_RELATIVE_PATH / SUBMISSIONS_LOG_FILENAME).open("a") as log_file:
        log_file.write(f"{prompt_id} {tail}\n")


def test_submissions_log_probe_confirms_on_a_new_matching_prompt(tmp_path: Path) -> None:
    state_dir, env_prefix = _make_state_dir_env_prefix(tmp_path)
    message = "Please fix the failing test in parser.py"
    probe = build_submissions_log_evidence_probe(env_prefix, message)
    baseline = _run_probe_command(probe.baseline_command, None)
    assert baseline == "0"

    _append_submissions_log_line(state_dir, "task-completed-1", AUTO_WAKE_TAIL_MARKER)
    _append_submissions_log_line(state_dir, "p-0", normalized_prompt_tail("other"))
    assert not _is_probe_confirmed(probe, baseline)

    _append_submissions_log_line(state_dir, "p-1", normalized_prompt_tail(message))
    assert _is_probe_confirmed(probe, baseline)


def test_submissions_log_probe_confirms_a_message_sent_after_text_left_in_the_composer(tmp_path: Path) -> None:
    state_dir, env_prefix = _make_state_dir_env_prefix(tmp_path)
    message = "hello there"
    probe = build_submissions_log_evidence_probe(env_prefix, message)
    _append_submissions_log_line(state_dir, "p-0", normalized_prompt_tail("say hello there again"))
    baseline = _run_probe_command(probe.baseline_command, None)

    assert not _is_probe_confirmed(probe, baseline)
    _append_submissions_log_line(state_dir, "p-1", normalized_prompt_tail("a draft someone typed " + message))
    assert _is_probe_confirmed(probe, baseline)


def test_submissions_log_probe_for_a_message_without_letters_or_digits_matches_only_an_empty_tail(
    tmp_path: Path,
) -> None:
    state_dir, env_prefix = _make_state_dir_env_prefix(tmp_path)
    probe = build_submissions_log_evidence_probe(env_prefix, "?!")
    baseline = _run_probe_command(probe.baseline_command, None)

    _append_submissions_log_line(state_dir, "p-1", normalized_prompt_tail("anything else"))
    assert not _is_probe_confirmed(probe, baseline)
    _append_submissions_log_line(state_dir, "p-2", normalized_prompt_tail("?!"))
    assert _is_probe_confirmed(probe, baseline)


def test_submissions_log_probe_counts_past_an_earlier_identical_prompt(tmp_path: Path) -> None:
    state_dir, env_prefix = _make_state_dir_env_prefix(tmp_path)
    message = "run the tests again"
    _append_submissions_log_line(state_dir, "p-1", normalized_prompt_tail(message))
    probe = build_submissions_log_evidence_probe(env_prefix, message)
    baseline = _run_probe_command(probe.baseline_command, None)

    assert baseline == "1"
    assert not _is_probe_confirmed(probe, baseline)
    _append_submissions_log_line(state_dir, "p-2", normalized_prompt_tail(message))
    assert _is_probe_confirmed(probe, baseline)


def test_queued_row_probe_confirms_once_the_message_is_queued(tmp_path: Path) -> None:
    pane_path = tmp_path / "pane.txt"
    pane_path.write_text(read_pane_fixture("w170_f_busy_typed.txt"))
    probe = build_queued_row_evidence_probe(f"cat {shlex.quote(str(pane_path))}", _QUEUED_MESSAGE)
    other_probe = build_queued_row_evidence_probe(f"cat {shlex.quote(str(pane_path))}", "second queued message two")
    baseline = _run_probe_command(probe.baseline_command, None)
    other_baseline = _run_probe_command(other_probe.baseline_command, None)
    assert not _is_probe_confirmed(probe, baseline)

    pane_path.write_text(read_pane_fixture("w170_g_busy_queued1.txt"))

    assert _is_probe_confirmed(probe, baseline)
    assert not _is_probe_confirmed(other_probe, other_baseline)


def test_queued_row_probe_confirms_a_message_without_letters_or_digits_past_an_earlier_row_like_it(
    tmp_path: Path,
) -> None:
    message = "Исправь падающий тест"
    two_queued_pane = read_pane_fixture("w170_g_busy_queued2.txt").replace(_QUEUED_MESSAGE, message)
    pane_path = tmp_path / "pane.txt"
    pane_path.write_text(read_pane_fixture("w170_g_busy_queued1.txt").replace(_QUEUED_MESSAGE, message))
    probe = build_queued_row_evidence_probe(f"cat {shlex.quote(str(pane_path))}", message)
    baseline = _run_probe_command(probe.baseline_command, None)

    assert baseline == "1"
    assert not _is_probe_confirmed(probe, baseline)
    pane_path.write_text(two_queued_pane)
    assert not _is_probe_confirmed(probe, baseline)
    pane_path.write_text(two_queued_pane.replace("second queued message two", message))
    assert _is_probe_confirmed(probe, baseline)


def _read_hook_fixture_payloads(fixture_name: str, event_name: str) -> list[dict[str, Any]]:
    records = read_complete_jsonl_records(HOOK_FIXTURES_DIR / f"{fixture_name}.jsonl")
    return [
        record["payload"]
        for record in records
        if record["kind"] == "hook" and record["payload"]["hookEventName"] == event_name
    ]


def _run_grok_hook(state_dir: Path, payload: Mapping[str, Any]) -> None:
    environ = {"MNGR_AGENT_STATE_DIR": str(state_dir), "MNGR_HOST_DIR": str(state_dir.parent)}
    assert grok_hook.main(["grok_hook.py"], io.BytesIO(json.dumps(payload).encode()), environ, ()) == 0


def test_session_id_probe_confirms_the_root_session_new_starts_and_not_a_prompt(tmp_path: Path) -> None:
    state_dir, env_prefix = _make_state_dir_env_prefix(tmp_path)
    launch_start, new_start, _ = _read_hook_fixture_payloads("new_session", "session_start")
    first_prompt = _read_hook_fixture_payloads("new_session", "user_prompt_submit")[0]
    probe = build_session_id_evidence_probe(env_prefix)
    _run_grok_hook(state_dir, launch_start)
    baseline = _run_probe_command(probe.baseline_command, None)

    assert baseline == launch_start["sessionId"]
    _run_grok_hook(state_dir, first_prompt)
    assert not _is_probe_confirmed(probe, baseline)
    _run_grok_hook(state_dir, new_start)
    assert _is_probe_confirmed(probe, baseline)


@pytest.mark.parametrize(
    ("message", "policy"),
    [
        ("!git status", SubmissionConfirmationPolicy.RELAXED),
        ("  !ls", SubmissionConfirmationPolicy.RELAXED),
        ("!", SubmissionConfirmationPolicy.RELAXED),
        ("/new", SubmissionConfirmationPolicy.RELAXED),
        ("hello there", SubmissionConfirmationPolicy.STRICT),
        ("echo !bang-in-the-middle", SubmissionConfirmationPolicy.STRICT),
    ],
)
def test_determine_confirmation_policy_relaxes_shell_mode_and_slash_commands(
    temp_mngr_ctx: MngrContext, message: str, policy: SubmissionConfirmationPolicy
) -> None:
    agent, _ = _make_pane_agent(temp_mngr_ctx, _IDLE_PANE)

    assert agent._determine_confirmation_policy(message) is policy


@pytest.mark.parametrize(
    ("policy", "probe_names"),
    [
        (SubmissionConfirmationPolicy.STRICT, ["submissions-log", "queued-row"]),
        (SubmissionConfirmationPolicy.RELAXED, ["submissions-log", "queued-row", "session-id"]),
    ],
)
def test_build_submission_evidence_probes_watches_the_session_id_only_for_a_relaxed_send(
    temp_mngr_ctx: MngrContext, policy: SubmissionConfirmationPolicy, probe_names: list[str]
) -> None:
    agent, _ = _make_pane_agent(temp_mngr_ctx, _IDLE_PANE)

    probes = agent._build_submission_evidence_probes("/new", policy)

    assert [probe.name for probe in probes] == probe_names


def test_build_submission_evidence_probes_reads_the_agent_state(temp_mngr_ctx: MngrContext) -> None:
    agent, _ = _make_pane_agent(temp_mngr_ctx, _IDLE_PANE)

    submissions_log_probe, _ = agent._build_submission_evidence_probes(
        "hello there", SubmissionConfirmationPolicy.STRICT
    )

    submissions_log_relative_path = (GROK_PLUGIN_STATE_RELATIVE_PATH / SUBMISSIONS_LOG_FILENAME).as_posix()
    assert submissions_log_probe.baseline_command.startswith("set -a && set +a && ")
    assert (
        f'log_path="$MNGR_AGENT_STATE_DIR/{submissions_log_relative_path}"' in submissions_log_probe.baseline_command
    )


def test_build_submission_evidence_probes_reads_the_pane_the_send_resolved_without_a_host_command(
    temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    agent, host = _make_pane_agent(temp_mngr_ctx, _IDLE_PANE)
    resolved_pane_target = "%77"
    pane_path = tmp_path / "pane.txt"
    pane_path.write_text(read_pane_fixture("w170_g_busy_queued1.txt"))
    stand_in_tmux = (
        f'tmux() {{ if [ "$*" = "capture-pane -t {resolved_pane_target} -p" ]; then '
        f"cat {shlex.quote(str(pane_path))}; fi; }}"
    )

    _, queued_row_probe = agent._build_submission_evidence_probes(_QUEUED_MESSAGE, SubmissionConfirmationPolicy.STRICT)

    assert host.captured == []
    assert host.capture_count == 0
    baseline = _run_probe_command(
        f"{stand_in_tmux}\n{PANE_TARGET_SHELL_VARIABLE}={resolved_pane_target}\n{queued_row_probe.baseline_command}",
        None,
    )
    assert baseline == "1"


# Key presses


@pytest.mark.parametrize(
    ("key", "pane_file_name", "is_cancellable_turn_in_flight", "refusal_fragment"),
    [
        ("C-o", _IDLE_PANE, False, "always-approve mode"),
        ("C-Enter", _BUSY_PANE, True, "Ctrl+Enter"),
        ("BTab", _IDLE_PANE, False, "Shift+Tab cycles grok's permission mode"),
        ("S-Tab", _BUSY_PANE, True, "Shift+Tab cycles grok's permission mode"),
        ("C-q", _IDLE_PANE, False, "Ctrl+Q quits grok"),
        ("Enter", _SEND_NOW_PANE, True, "Enter:send now"),
        ("C-m", _SEND_NOW_PANE, True, "Enter:send now"),
        ("Enter", _BUSY_TYPED_PANE, True, None),
        ("1", _PERMISSION_CARD_PANE, True, "always-approve for every tool"),
        ("KP1", _PERMISSION_CARD_PANE, True, "always-approve for every tool"),
        ("kp1", _PERMISSION_CARD_PANE, True, "always-approve for every tool"),
        ("Escape", _PERMISSION_CARD_PANE, True, None),
        ("Enter", _PERMISSION_CARD_PANE, True, None),
        ("Enter", _PAYWALL_PANE, False, "paywall screen"),
        ("l", _LOGIN_PANE, False, "login screen"),
        ("C-c", _IDLE_PANE, False, "grok is idle"),
        ("C-c", _IDLE_PANE, True, "no Ctrl+c:cancel hint"),
        ("C-c", _BUSY_PANE, False, "grok is idle"),
        ("C-c", _BUSY_PANE, True, None),
        ("^C", _BUSY_PANE, True, None),
        ("C-c", _BUSY_TYPED_PANE, True, "composer holds text"),
        ("C-c", _PERMISSION_CARD_PANE, True, None),
        ("C-c", _QUIT_ARMED_PANE, True, "second Ctrl+C"),
        ("Enter", _TYPED_PANE, False, None),
    ],
)
def test_find_grok_key_refusal(
    key: str, pane_file_name: str, is_cancellable_turn_in_flight: bool, refusal_fragment: str | None
) -> None:
    refusal = find_grok_key_refusal(key, read_pane_fixture(pane_file_name), is_cancellable_turn_in_flight)

    if refusal_fragment is None:
        assert refusal is None
    else:
        assert refusal is not None and refusal_fragment in refusal


@pytest.mark.parametrize(
    ("key", "is_refused"),
    [("Enter", True), ("KPEnter", True), ("C-m", True), ("^M", True), ("Tab", False), ("Escape", False)],
)
def test_find_grok_key_refusal_on_a_card_with_always_approve_selected(key: str, is_refused: bool) -> None:
    pane = select_always_approve_option(read_pane_fixture(_PERMISSION_CARD_PANE))

    refusal = find_grok_key_refusal(key, pane, is_cancellable_turn_in_flight=True)

    if is_refused:
        assert refusal is not None and "always-approve option is selected" in refusal
    else:
        assert refusal is None


@pytest.mark.parametrize(
    ("key", "is_interrupt"), [("C-c", True), ("c-C", True), ("^c", True), ("c", False), ("M-c", False)]
)
def test_is_grok_interrupt_key(key: str, is_interrupt: bool) -> None:
    assert is_grok_interrupt_key(key) is is_interrupt


def _sent_keys(host: _PaneHost) -> list[str]:
    return [command for command in host.sent_commands if command.startswith("tmux send-keys")]


def test_press_key_chord_refuses_a_key_and_sends_nothing(temp_mngr_ctx: MngrContext) -> None:
    agent, host = _make_pane_agent(temp_mngr_ctx, _IDLE_PANE)

    with pytest.raises(GrokKeyRefusedError, match="refused to press C-c: grok is idle"):
        agent.press_key_chord("C-c")

    assert _sent_keys(host) == []


@pytest.mark.parametrize(
    ("pane_file_name", "prompt_id", "refusal_fragment"),
    [
        pytest.param(_BUSY_PANE, None, "grok is idle", id="subagents_only"),
        pytest.param(_BUSY_PANE, f"{PENDING_WAKE_PROMPT_ID_PREFIX}child-1", "grok is idle", id="pending_wake"),
        pytest.param(_IDLE_PANE, "p-1", "no Ctrl+c:cancel hint", id="turn_ended_on_screen"),
        pytest.param(_BUSY_TYPED_PANE, "p-1", "composer holds text", id="composer_text"),
        pytest.param(_QUIT_ARMED_PANE, "p-1", "second Ctrl+C", id="quit_armed"),
    ],
)
def test_press_key_chord_refuses_an_interrupt_without_a_cancellable_turn_on_screen(
    temp_mngr_ctx: MngrContext, pane_file_name: str, prompt_id: str | None, refusal_fragment: str
) -> None:
    agent, host = _make_pane_agent(temp_mngr_ctx, pane_file_name)
    # The `active` marker, which a running subagent alone also keeps, never allows the press.
    host.files[_agent_file_key(agent, "active")] = ""
    if prompt_id is not None:
        _record_root_turn(agent, host, prompt_id)

    with pytest.raises(GrokKeyRefusedError, match=re.escape(refusal_fragment)):
        agent.press_key_chord("C-c")

    assert _sent_keys(host) == []
    assert _agent_file_key(agent, _LAST_INTERRUPT_RELATIVE_PATH) not in host.files


def test_press_key_chord_refuses_an_interrupt_while_the_hook_is_still_writing_a_turn_file(
    temp_mngr_ctx: MngrContext,
) -> None:
    """The hook writes each turn file through a dot-prefixed temp file beside it."""
    agent, host = _make_pane_agent(temp_mngr_ctx, _BUSY_PANE)
    temporary_turn_path = ROOT_TURNS_RELATIVE_PATH / f".{_ROOT_SESSION_ID}.1234.tmp"
    host.files[_agent_file_key(agent, temporary_turn_path)] = f"p-1\n{AUTO_WAKE_TAIL_MARKER}\n"

    with pytest.raises(GrokKeyRefusedError, match="grok is idle"):
        agent.press_key_chord("C-c")

    assert _sent_keys(host) == []


def test_press_key_chord_checks_the_interrupt_after_waiting_out_the_spacing(temp_mngr_ctx: MngrContext) -> None:
    agent, host = _make_pane_agent(temp_mngr_ctx, _IDLE_PANE)
    _record_root_turn(agent, host, "p-1")
    host.files[_agent_file_key(agent, _LAST_INTERRUPT_RELATIVE_PATH)] = f"{time.time() - 1.0}\n"

    started_at = time.monotonic()
    with pytest.raises(GrokKeyRefusedError, match="no Ctrl"):
        agent.press_key_chord("C-c")

    assert len(host.capture_monotonic_times) == 1
    assert host.capture_monotonic_times[0] - started_at >= 0.4


@pytest.mark.parametrize(
    ("seconds_since_the_recorded_interrupt", "min_wait_seconds", "max_wait_seconds"),
    [
        pytest.param(1.2, 0.25, 1.2, id="recent"),
        pytest.param(-60.0, 1.45, 5.0, id="recorded_in_the_future"),
        pytest.param(60.0, 0.0, 1.0, id="old"),
    ],
)
def test_press_key_chord_spaces_an_interrupt_after_the_recorded_one(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seconds_since_the_recorded_interrupt: float,
    min_wait_seconds: float,
    max_wait_seconds: float,
) -> None:
    agent, pane_dir = _make_agent_whose_turn_grok_ends_at_the_press(local_provider, tmp_path, monkeypatch)
    (agent._get_agent_dir() / _LAST_INTERRUPT_RELATIVE_PATH).write_text(
        f"{time.time() - seconds_since_the_recorded_interrupt}\n"
    )

    started_at = time.monotonic()
    agent.press_key_chord("C-c")

    assert min_wait_seconds <= time.monotonic() - started_at < max_wait_seconds
    assert _read_pressed_keys(pane_dir) == ["C-c"]


def test_press_key_chord_ignores_an_unreadable_interrupt_record(
    local_provider: LocalProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, pane_dir = _make_agent_whose_turn_grok_ends_at_the_press(local_provider, tmp_path, monkeypatch)
    (agent._get_agent_dir() / _LAST_INTERRUPT_RELATIVE_PATH).write_text("not a time")

    with capture_loguru(level="WARNING") as log_output:
        agent.press_key_chord("C-c")

    assert "unreadable last Ctrl+C record" in log_output.getvalue()
    assert _read_pressed_keys(pane_dir) == ["C-c"]


def _get_scripted_pane_dir(tmp_path: Path) -> Path:
    return tmp_path / "scripted-grok-pane"


def _put_scripted_grok_pane_on_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pane_file_names: Sequence[str],
    on_press_scripts: Sequence[str] = (),
) -> Path:
    """Put a `tmux` first on PATH whose captures show the n-th pane after n `send-keys` presses (the last one repeats).

    Each press appends its key to `keys.log` in the returned directory, then runs the n-th of `on_press_scripts` in
    `sh`.
    """
    pane_dir = _get_scripted_pane_dir(tmp_path)
    pane_dir.mkdir()
    for pane_idx, pane_file_name in enumerate(pane_file_names):
        (pane_dir / f"pane_{pane_idx}.txt").write_text(read_pane_fixture(pane_file_name))
    for script_idx, script in enumerate(on_press_scripts):
        (pane_dir / f"on_press_{script_idx + 1}.sh").write_text(script)
    last_pane_idx = len(pane_file_names) - 1
    put_executable_first_on_path(
        tmp_path / "scripted-tmux-bin",
        monkeypatch,
        "tmux",
        "#!/usr/bin/env bash\n"
        f"dir={shlex.quote(str(pane_dir))}\n"
        'presses="$(cat "$dir/presses" 2>/dev/null || echo 0)"\n'
        'case "$1" in\n'
        f"  show-options) echo {shlex.quote(_PANE_ID)} ;;\n"
        f'  capture-pane) cat "$dir/pane_$(( presses < {last_pane_idx} ? presses : {last_pane_idx} )).txt" ;;\n'
        "  send-keys)\n"
        '    printf \'%s\\n\' "$4" >> "$dir/keys.log"\n'
        '    presses=$((presses + 1)); echo "$presses" > "$dir/presses"\n'
        '    if [ -f "$dir/on_press_$presses.sh" ]; then sh "$dir/on_press_$presses.sh"; fi ;;\n'
        "esac\n"
        "exit 0\n",
    )
    return pane_dir


def _read_pressed_keys(pane_dir: Path) -> list[str]:
    keys_path = pane_dir / "keys.log"
    return keys_path.read_text().splitlines() if keys_path.is_file() else []


def _get_hook_path(agent: GrokAgent) -> Path:
    return get_grok_home(agent._get_agent_dir()) / "hooks" / "grok_hook.py"


def _get_cancelled_turn_path(agent: GrokAgent) -> Path:
    return agent._get_agent_dir() / ROOT_TURNS_RELATIVE_PATH / _ROOT_SESSION_ID


def _submit_root_prompt(state_dir: Path, session_id: str, prompt_id: str, prompt: str) -> None:
    """Run mngr's hook handler on grok's UserPromptSubmit for a top-level session, which records that turn in flight."""
    _run_grok_hook(
        state_dir,
        {"hookEventName": "UserPromptSubmit", "sessionId": session_id, "promptId": prompt_id, "prompt": prompt},
    )


def _make_agent_with_a_turn_in_flight(local_provider: LocalProviderInstance, tmp_path: Path) -> GrokAgent:
    """A local grok agent with mngr's hook handler installed, which has recorded a top-level turn in flight."""
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig())
    agent_dir = agent._get_agent_dir()
    hook_path = _get_hook_path(agent)
    hook_path.parent.mkdir(parents=True)
    write_executable_script(hook_path, _read_resource("grok_hook.py"))
    _write_agent_env_file(
        agent,
        f"MNGR_AGENT_STATE_DIR={shlex.quote(str(agent_dir))}\nMNGR_HOST_DIR={shlex.quote(str(agent.host.host_dir))}\n",
    )
    _submit_root_prompt(agent_dir, _ROOT_SESSION_ID, _CANCELLED_PROMPT_ID, "HOLDTEST hold the reply")
    return agent


def _with_cancel_settle_timeout(agent: GrokAgent, timeout_seconds: float) -> GrokAgent:
    return agent.model_copy_update(to_update(agent.field_ref().cancel_settle_timeout_seconds, timeout_seconds))


def _build_stop_cancelled_hook_run(agent: GrokAgent) -> str:
    """A shell command running the agent's hook handler as grok's StopCancelled hook for the cancelled turn."""
    agent_dir = agent._get_agent_dir()
    payload = {"hookEventName": "StopCancelled", "sessionId": _ROOT_SESSION_ID, "promptId": _CANCELLED_PROMPT_ID}
    return (
        f"printf '%s' {shlex.quote(json.dumps(payload))} | MNGR_AGENT_STATE_DIR={shlex.quote(str(agent_dir))} "
        f"MNGR_HOST_DIR={shlex.quote(str(agent.host.host_dir))} {shlex.quote(str(_get_hook_path(agent)))}\n"
    )


def _build_turn_record_copy(agent: GrokAgent, copy_path: Path) -> str:
    """A shell command copying the cancelled turn's record, or an empty file once it is gone, to `copy_path`."""
    return f"cat {shlex.quote(str(_get_cancelled_turn_path(agent)))} > {shlex.quote(str(copy_path))} 2>/dev/null\n"


def _make_agent_whose_turn_grok_ends_at_the_press(
    local_provider: LocalProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[GrokAgent, Path]:
    agent = _make_agent_with_a_turn_in_flight(local_provider, tmp_path)
    pane_dir = _put_scripted_grok_pane_on_path(
        monkeypatch, tmp_path, (_BUSY_PANE, _IDLE_PANE), on_press_scripts=(_build_stop_cancelled_hook_run(agent),)
    )
    return agent, pane_dir


def _read_hook_log(agent: GrokAgent) -> str:
    return (agent._get_agent_dir() / _GROK_HOOK_LOG_RELATIVE_PATH).read_text()


def _is_cancelled_turn_recorded(agent: GrokAgent) -> bool:
    turn_path = _get_cancelled_turn_path(agent)
    return turn_path.is_file() and turn_path.read_text().split("\n", 1)[0] == _CANCELLED_PROMPT_ID


def _is_active(agent: GrokAgent) -> bool:
    return (agent._get_agent_dir() / ACTIVE_MARKER_FILENAME).exists()


def test_press_key_chord_returns_once_grok_reports_the_cancelled_turn_s_end_after_going_idle(
    local_provider: LocalProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """grok shows an idle, empty composer before its StopCancelled hook reports the turn's end."""
    agent = _make_agent_with_a_turn_in_flight(local_provider, tmp_path)
    hook_delay_seconds = 0.3
    stop_cancelled_hook_run = _build_stop_cancelled_hook_run(agent).strip()
    pane_dir = _put_scripted_grok_pane_on_path(
        monkeypatch,
        tmp_path,
        (_BUSY_PANE, _IDLE_PANE),
        on_press_scripts=(f"( sleep {hook_delay_seconds}; {stop_cancelled_hook_run} ) >/dev/null 2>&1 &\n",),
    )

    started_at = time.monotonic()
    agent.press_key_chord("C-c")

    assert hook_delay_seconds <= time.monotonic() - started_at < 1.5
    assert _read_pressed_keys(pane_dir) == ["C-c"]
    assert not _is_cancelled_turn_recorded(agent)
    assert "event=stop_cancelled" in _read_hook_log(agent)
    assert _TURN_DROPPED_EVENT not in _read_hook_log(agent)
    recorded_time = float((agent._get_agent_dir() / _LAST_INTERRUPT_RELATIVE_PATH).read_text())
    assert abs(recorded_time - time.time()) < 5.0


@pytest.mark.parametrize(
    "restored_pane_file_name",
    [
        pytest.param(_RESTORED_ONE_LINE_PANE, id="one_line"),
        pytest.param(_RESTORED_FIVE_LINES_PANE, id="five_lines"),
        pytest.param(_RESTORED_PASTED_SIX_LINES_PANE, id="pasted_chip"),
    ],
)
def test_press_key_chord_clears_what_grok_restores_then_ends_the_turn_it_dropped(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restored_pane_file_name: str,
) -> None:
    agent = _make_agent_with_a_turn_in_flight(local_provider, tmp_path)
    record_at_clear_path = tmp_path / "turn_record_at_clear.txt"
    pane_dir = _put_scripted_grok_pane_on_path(
        monkeypatch,
        tmp_path,
        (_BUSY_PANE, restored_pane_file_name, _CLEARED_PANE),
        on_press_scripts=("", _build_turn_record_copy(agent, record_at_clear_path)),
    )

    started_at = time.monotonic()
    agent.press_key_chord("C-c")

    assert time.monotonic() - started_at >= 1.45
    assert _read_pressed_keys(pane_dir) == ["C-c", "C-c"]
    assert record_at_clear_path.read_text().split("\n", 1)[0] == _CANCELLED_PROMPT_ID
    assert not _is_cancelled_turn_recorded(agent)
    assert not _is_active(agent)
    assert _TURN_DROPPED_LOG_FRAGMENT in _read_hook_log(agent)


def test_press_key_chord_ends_a_dropped_turn_at_the_deadline_when_the_composer_is_empty(
    local_provider: LocalProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _with_cancel_settle_timeout(_make_agent_with_a_turn_in_flight(local_provider, tmp_path), 0.5)
    pane_dir = _put_scripted_grok_pane_on_path(monkeypatch, tmp_path, (_BUSY_PANE, _IDLE_PANE))

    started_at = time.monotonic()
    agent.press_key_chord("C-c")

    assert 0.5 <= time.monotonic() - started_at < 1.45
    assert _read_pressed_keys(pane_dir) == ["C-c"]
    assert not _is_cancelled_turn_recorded(agent)
    assert not _is_active(agent)
    assert _TURN_DROPPED_LOG_FRAGMENT in _read_hook_log(agent)


def test_press_key_chord_leaves_the_dropped_turn_recorded_when_the_clear_does_not_take(
    local_provider: LocalProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _with_cancel_settle_timeout(_make_agent_with_a_turn_in_flight(local_provider, tmp_path), 0.5)
    pane_dir = _put_scripted_grok_pane_on_path(monkeypatch, tmp_path, (_BUSY_PANE, _RESTORED_PASTED_SIX_LINES_PANE))

    with pytest.raises(GrokCancelUnsettledError, match="which still held it"):
        agent.press_key_chord("C-c")

    assert _read_pressed_keys(pane_dir) == ["C-c", "C-c"]
    assert _is_cancelled_turn_recorded(agent)
    assert _is_active(agent)
    assert _TURN_DROPPED_EVENT not in _read_hook_log(agent)


def test_press_key_chord_reports_a_dropped_turn_the_hook_handler_does_not_end(
    local_provider: LocalProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _with_cancel_settle_timeout(_make_agent_with_a_turn_in_flight(local_provider, tmp_path), 0.5)
    write_executable_script(_get_hook_path(agent), "#!/bin/sh\nexit 0\n")
    pane_dir = _put_scripted_grok_pane_on_path(monkeypatch, tmp_path, (_BUSY_PANE, _IDLE_PANE))

    with pytest.raises(GrokCancelUnsettledError, match="mngr's hook handler did not end it"):
        agent.press_key_chord("C-c")

    assert _read_pressed_keys(pane_dir) == ["C-c"]
    assert _is_cancelled_turn_recorded(agent)


def test_press_key_chord_returns_when_grok_still_runs_the_cancelled_turn_at_the_deadline(
    local_provider: LocalProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _with_cancel_settle_timeout(_make_agent_with_a_turn_in_flight(local_provider, tmp_path), 0.5)
    pane_dir = _put_scripted_grok_pane_on_path(monkeypatch, tmp_path, (_BUSY_PANE,))

    with capture_loguru(level="INFO") as log_output:
        agent.press_key_chord("C-c")

    assert _read_pressed_keys(pane_dir) == ["C-c"]
    assert _is_cancelled_turn_recorded(agent)
    assert _is_active(agent)
    assert _TURN_DROPPED_EVENT not in _read_hook_log(agent)
    assert "Left the turn cancelled in agent" in log_output.getvalue()


def _clear_restored_prompt_long_after_the_cancel(agent: GrokAgent) -> None:
    agent._clear_restored_prompt(agent._send_target_arg(agent.tmux_target), time.time() - 60.0)


def test_clear_restored_prompt_presses_nothing_when_the_composer_is_already_empty(
    local_provider: LocalProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig())
    pane_dir = _put_scripted_grok_pane_on_path(monkeypatch, tmp_path, (_IDLE_PANE,))

    _clear_restored_prompt_long_after_the_cancel(agent)

    assert _read_pressed_keys(pane_dir) == []


@pytest.mark.parametrize(
    "pane_file_name",
    [
        pytest.param(_BUSY_PANE, id="running_a_turn"),
        pytest.param(_PERMISSION_CARD_PANE, id="permission_card"),
    ],
)
def test_clear_restored_prompt_presses_nothing_and_says_so_when_grok_shows_no_idle_input_box(
    local_provider: LocalProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pane_file_name: str
) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig())
    pane_dir = _put_scripted_grok_pane_on_path(monkeypatch, tmp_path, (pane_file_name,))

    with pytest.raises(GrokCancelUnsettledError) as raised:
        _clear_restored_prompt_long_after_the_cancel(agent)

    assert _read_pressed_keys(pane_dir) == []
    assert str(raised.value).endswith(
        "then showed no idle input box when mngr came to clear it, so mngr cleared nothing and did not end the turn "
        "in mngr's records"
    )


def test_press_key_chord_leaves_text_typed_after_grok_first_shows_an_idle_empty_composer(
    local_provider: LocalProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """grok puts a dropped prompt back as it goes idle, so text that appears later is a person's draft and stays."""
    settle_timeout_seconds = 2.0
    agent = _with_cancel_settle_timeout(
        _make_agent_with_a_turn_in_flight(local_provider, tmp_path), settle_timeout_seconds
    )
    pane_dir = _get_scripted_pane_dir(tmp_path)
    typed_pane_path = tmp_path / "typed_pane.txt"
    typed_pane_path.write_text(read_pane_fixture(_RESTORED_ONE_LINE_PANE))
    type_after_the_cancel = (
        f"( sleep {settle_timeout_seconds / 2:g}; "
        f"cp {shlex.quote(str(typed_pane_path))} {shlex.quote(str(pane_dir / 'pane_1.txt'))} ) >/dev/null 2>&1 &\n"
    )
    _put_scripted_grok_pane_on_path(monkeypatch, tmp_path, (_BUSY_PANE, _IDLE_PANE), (type_after_the_cancel,))

    agent.press_key_chord("C-c")

    assert _read_pressed_keys(pane_dir) == ["C-c"]
    assert (pane_dir / "pane_1.txt").read_text() == typed_pane_path.read_text()
    assert not _is_cancelled_turn_recorded(agent)
    assert _TURN_DROPPED_LOG_FRAGMENT in _read_hook_log(agent)


def test_press_key_chord_settles_nothing_while_another_top_level_session_has_a_turn_recorded(
    local_provider: LocalProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _make_agent_with_a_turn_in_flight(local_provider, tmp_path)
    _submit_root_prompt(
        agent._get_agent_dir(), _OTHER_ROOT_SESSION_ID, _OTHER_ROOT_PROMPT_ID, "a turn in another top-level session"
    )
    pane_dir = _put_scripted_grok_pane_on_path(
        monkeypatch, tmp_path, (_BUSY_PANE, _RESTORED_ONE_LINE_PANE, _CLEARED_PANE)
    )

    started_at = time.monotonic()
    agent.press_key_chord("C-c")
    seconds_to_return = time.monotonic() - started_at

    other_turn_path = agent._get_agent_dir() / ROOT_TURNS_RELATIVE_PATH / _OTHER_ROOT_SESSION_ID
    assert seconds_to_return < agent.cancel_settle_timeout_seconds / 2
    assert _read_pressed_keys(pane_dir) == ["C-c"]
    assert _is_cancelled_turn_recorded(agent)
    assert other_turn_path.read_text().split("\n", 1)[0] == _OTHER_ROOT_PROMPT_ID
    assert _is_active(agent)
    assert _TURN_DROPPED_EVENT not in _read_hook_log(agent)


@pytest.mark.parametrize(
    ("is_turn_recorded", "recorded_prompt_id"),
    [pytest.param(True, _CANCELLED_PROMPT_ID, id="recorded"), pytest.param(False, "", id="ended")],
)
def test_cancel_settle_probe_command_prints_the_turn_record_then_the_pane(
    tmp_path: Path, is_turn_recorded: bool, recorded_prompt_id: str
) -> None:
    state_dir = tmp_path / "state"
    _submit_root_prompt(state_dir, _ROOT_SESSION_ID, _CANCELLED_PROMPT_ID, "hi")
    if not is_turn_recorded:
        (state_dir / ROOT_TURNS_RELATIVE_PATH / _ROOT_SESSION_ID).unlink()
    pane_path = tmp_path / "pane.txt"
    pane_path.write_text(read_pane_fixture(_RESTORED_ONE_LINE_PANE))
    probe_command = build_cancel_settle_probe_command(state_dir, _CANCELLED_TURN, f"cat {shlex.quote(str(pane_path))}")

    stdout = subprocess.run(
        ["sh", "-c", probe_command],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout
    reading = parse_cancel_settle_probe_output(_CANCELLED_TURN, stdout)

    assert reading is not None
    assert reading.recorded_prompt_id == recorded_prompt_id
    assert reading.pane == read_pane_fixture(_RESTORED_ONE_LINE_PANE)


def test_parse_cancel_settle_probe_output_refuses_output_cut_short() -> None:
    assert parse_cancel_settle_probe_output(_CANCELLED_TURN, f"turn:{_CANCELLED_PROMPT_ID}\n") is None


@pytest.mark.parametrize(
    ("pane_file_name", "recorded_prompt_id", "is_shown", "is_dropped"),
    [
        pytest.param(_BUSY_PANE, _CANCELLED_PROMPT_ID, False, False, id="still_running"),
        pytest.param(_BUSY_TYPED_PANE, _CANCELLED_PROMPT_ID, False, False, id="still_running_with_text"),
        pytest.param(_QUIT_ARMED_PANE, _CANCELLED_PROMPT_ID, False, False, id="quit_armed"),
        pytest.param(_BUSY_PANE, "", True, False, id="ended"),
        pytest.param(_IDLE_PANE, "", True, False, id="ended_and_idle"),
        pytest.param(_BUSY_PANE, "next-prompt-id", True, False, id="followed_by_another_turn"),
        pytest.param(_IDLE_PANE, _CANCELLED_PROMPT_ID, True, True, id="idle_and_empty_before_grok_reports_it"),
        pytest.param(_RESTORED_ONE_LINE_PANE, _CANCELLED_PROMPT_ID, True, True, id="dropped_with_its_prompt_restored"),
        pytest.param(_RESTORED_PASTED_SIX_LINES_PANE, _CANCELLED_PROMPT_ID, True, True, id="dropped_with_a_chip"),
    ],
)
def test_cancel_is_settled_by_a_reported_end_or_an_idle_composer_and_an_idle_turn_is_dropped(
    pane_file_name: str, recorded_prompt_id: str, is_shown: bool, is_dropped: bool
) -> None:
    reading = GrokCancelReading(
        cancelled_turn=_CANCELLED_TURN, recorded_prompt_id=recorded_prompt_id, pane=read_pane_fixture(pane_file_name)
    )

    assert is_cancel_ended_or_idle(reading) is is_shown
    assert is_cancelled_turn_dropped(reading) is is_dropped


@pytest.mark.parametrize(("key", "sent_key_argument"), [("Escape", "Escape"), (";", "'\\;'")])
def test_press_key_chord_passes_other_keys_through(
    temp_mngr_ctx: MngrContext, key: str, sent_key_argument: str
) -> None:
    agent, host = _make_pane_agent(temp_mngr_ctx, _PERMISSION_CARD_PANE)

    agent.press_key_chord(key)

    assert _sent_keys(host) == [f"tmux send-keys -t {_PANE_ID} {sent_key_argument}"]
    assert _agent_file_key(agent, _LAST_INTERRUPT_RELATIVE_PATH) not in host.files


# Lifecycle fields


def _write_agent_marker(agent: GrokAgent, file_name: str) -> None:
    marker_path = agent._get_agent_dir() / file_name
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text("")


def _put_standin_tmux_on_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pane: str | None, agent_pane: str | None = None
) -> None:
    """Put a `tmux` first on PATH whose `capture-pane` prints `pane`; it fails every call when `pane` is None.

    With `agent_pane`, the session records `_PANE_ID` as the agent's pane, and a capture of that pane prints it.
    """
    agent_pane_branch = ""
    if agent_pane is not None:
        agent_pane_path = tmp_path / "standin-agent-pane.txt"
        agent_pane_path.write_text(agent_pane)
        agent_pane_branch = (
            f'if [ "$1" = show-options ]; then echo {shlex.quote(_PANE_ID)}; exit 0; fi\n'
            f'if [ "$1" = capture-pane ] && [ "$3" = {shlex.quote(_PANE_ID)} ]; then '
            f"cat {shlex.quote(str(agent_pane_path))}; exit 0; fi\n"
        )
    capture_branch = ""
    if pane is not None:
        pane_path = tmp_path / "standin-pane.txt"
        pane_path.write_text(pane)
        capture_branch = f'if [ "$1" = capture-pane ]; then cat {shlex.quote(str(pane_path))}; exit 0; fi\n'
    put_executable_first_on_path(
        tmp_path / "standin-tmux-bin",
        monkeypatch,
        "tmux",
        "#!/usr/bin/env bash\n" + agent_pane_branch + capture_branch + "exit 1\n",
    )


@pytest.mark.parametrize(
    ("marker_names", "pane", "expected_reason", "is_blocked"),
    [
        pytest.param((), read_pane_fixture(_IDLE_PANE), WaitingReason.END_OF_TURN, False, id="idle"),
        pytest.param(("active",), read_pane_fixture(_BUSY_PANE), None, False, id="running"),
        pytest.param(("active",), read_pane_fixture(_PERMISSION_CARD_PANE), None, False, id="card_without_request"),
        pytest.param(
            ("active", "permissions_waiting"),
            read_pane_fixture(_PERMISSION_CARD_PANE),
            WaitingReason.PERMISSIONS,
            True,
            id="card_open",
        ),
        pytest.param(
            ("active", "permissions_waiting"),
            _read_unrecognised_dialog_pane(),
            WaitingReason.PERMISSIONS,
            True,
            id="unrecognised_dialog_open",
        ),
        pytest.param(("active", "permissions_waiting"), None, WaitingReason.PERMISSIONS, True, id="pane_unreadable"),
        pytest.param(
            ("active", "permissions_waiting"),
            read_pane_fixture(_TOOL_RUNNING_PANE),
            None,
            False,
            id="approved_tool_running",
        ),
        pytest.param(
            ("permissions_waiting",),
            read_pane_fixture(_PERMISSION_CARD_PANE),
            WaitingReason.END_OF_TURN,
            True,
            id="request_without_turn",
        ),
    ],
)
def test_waiting_reason_reads_the_hook_markers_and_the_pane(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    marker_names: tuple[str, ...],
    pane: str | None,
    expected_reason: WaitingReason | None,
    is_blocked: bool,
) -> None:
    _put_standin_tmux_on_path(monkeypatch, tmp_path, pane)
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig())
    for marker_name in marker_names:
        _write_agent_marker(agent, marker_name)

    assert _waiting_reason(agent, agent.host) == expected_reason
    assert agent.is_blocked_on_dialog() is is_blocked


def test_waiting_reason_reads_the_agent_pane_when_another_pane_of_its_window_is_active(
    local_provider: LocalProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put_standin_tmux_on_path(
        monkeypatch, tmp_path, pane="user@host:~$ ", agent_pane=read_pane_fixture(_TOOL_RUNNING_PANE)
    )
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig())
    for marker_name in ("active", "permissions_waiting"):
        _write_agent_marker(agent, marker_name)

    assert _waiting_reason(agent, agent.host) is None
    assert agent.is_blocked_on_dialog() is False


def test_waiting_reason_leaves_other_agent_types_alone(temp_mngr_ctx: MngrContext) -> None:
    # A ScriptedHost has no file reads, so any host access by the generator would raise.
    other_agent = SendKeysAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("not-grok"),
        agent_type=AgentTypeName("generic"),
        mngr_ctx=temp_mngr_ctx,
        host=ScriptedHost(),
    )

    assert _waiting_reason(other_agent, other_agent.host) is None


def test_agent_field_generators_expose_waiting_reason() -> None:
    result = agent_field_generators()

    assert result == ("grok", {"waiting_reason": _waiting_reason})


# Preservation and offline preservation


def _seed_agent_session_state(agent_dir: Path, session_id: str) -> None:
    make_grok_session_dir(agent_dir / GROK_SESSIONS_RELATIVE_PATH, "%2Fw", session_id)
    (agent_dir / SESSION_ID_RELATIVE_PATH).write_text(session_id + "\n")
    (agent_dir / ROOTS_RELATIVE_PATH).write_text(session_id + "\n")
    memory_dir = agent_dir / GROK_MEMORY_RELATIVE_PATH
    memory_dir.mkdir(parents=True)
    (memory_dir / "store.json").write_text("{}\n")


@pytest.mark.rsync
@pytest.mark.parametrize("is_memory_enabled", [False, True])
def test_on_destroy_preserves_the_session_store_and_the_memory_store_only_with_memory_on(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_host_dir: Path, is_memory_enabled: bool
) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig(memory=is_memory_enabled))
    session_id = str(uuid.uuid4())
    _seed_agent_session_state(agent._get_agent_dir(), session_id)

    agent.on_destroy(agent.host)

    preserved_dirs = list(get_preserved_agents_root_dir(temp_host_dir).iterdir())
    assert len(preserved_dirs) == 1
    preserved_dir = preserved_dirs[0]
    assert (preserved_dir / SESSION_ID_RELATIVE_PATH).read_text() == session_id + "\n"
    assert (preserved_dir / GROK_SESSIONS_RELATIVE_PATH / "%2Fw" / session_id / SESSION_SUMMARY_FILENAME).is_file()
    assert (preserved_dir / ROOTS_RELATIVE_PATH).read_text() == session_id + "\n"
    assert (preserved_dir / GROK_MEMORY_RELATIVE_PATH / "store.json").is_file() is is_memory_enabled


def test_on_destroy_preserves_nothing_when_preservation_is_off(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_host_dir: Path
) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig(preserve_on_destroy=False))
    _seed_agent_session_state(agent._get_agent_dir(), str(uuid.uuid4()))

    agent.on_destroy(agent.host)

    assert not get_preserved_agents_root_dir(temp_host_dir).exists()


@pytest.mark.parametrize(
    ("certified_data", "expected_items"),
    [
        ({"type": "grok"}, None),
        (
            {"type": "grok", "agent_config": {"preserve_on_destroy": True}},
            build_grok_preserved_items(is_memory_enabled=False),
        ),
        (
            {"type": "grok", "agent_config": {"preserve_on_destroy": True, "memory": True}},
            build_grok_preserved_items(is_memory_enabled=True),
        ),
    ],
)
def test_offline_preservation_selector_reads_the_recorded_flags(
    certified_data: dict[str, Any], expected_items: list[Any] | None
) -> None:
    ref = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("offline-grok"),
        provider_name=ProviderInstanceName(LOCAL_PROVIDER_NAME),
        certified_data=certified_data,
    )

    assert _grok_items_to_preserve_for_discovered_agent(ref) == expected_items


@pytest.mark.parametrize(
    ("recorded_agent_config", "is_session_preserved", "is_memory_preserved"),
    [
        (None, False, False),
        ({"preserve_on_destroy": False}, False, False),
        ({"preserve_on_destroy": True}, True, False),
        ({"preserve_on_destroy": True, "memory": True}, True, True),
    ],
)
def test_on_before_host_destroy_preserves_the_volume_state_its_recorded_flags_select(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
    recorded_agent_config: dict[str, Any] | None,
    is_session_preserved: bool,
    is_memory_preserved: bool,
) -> None:
    agent_id = AgentId.generate()
    agent_name = AgentName("offline-grok")
    session_id = str(uuid.uuid4())
    provider = make_local_volume_provider(temp_mngr_ctx, tmp_path)
    agent_record: dict[str, Any] = {"id": str(agent_id), "name": str(agent_name), "type": "grok"}
    if recorded_agent_config is not None:
        agent_record["agent_config"] = recorded_agent_config
    provider.mock_agent_data.append(agent_record)
    _seed_agent_session_state(tmp_path / "agents" / str(agent_id), session_id)
    host = make_offline_host_with_volume(HostId.generate(), provider, temp_mngr_ctx)

    on_before_host_destroy(host, temp_mngr_ctx)

    preserved_dir = get_local_preserved_agent_dir(temp_mngr_ctx, agent_name, agent_id)
    preserved_session_dir = preserved_dir / GROK_SESSIONS_RELATIVE_PATH / "%2Fw" / session_id
    assert (preserved_dir / SESSION_ID_RELATIVE_PATH).is_file() is is_session_preserved
    assert (preserved_session_dir / SESSION_SUMMARY_FILENAME).is_file() is is_session_preserved
    assert (preserved_dir / GROK_MEMORY_RELATIVE_PATH / "store.json").is_file() is is_memory_preserved


# Session adoption and clones


def test_on_before_create_rejects_an_unknown_adopted_session(temp_mngr_ctx: MngrContext) -> None:
    args = OnBeforeCreateArgs(
        target_host=NewHostOptions(provider=LOCAL_PROVIDER_NAME, name=HostName(LOCAL_HOST_NAME)),
        agent_options=CreateAgentOptions(agent_type=AgentTypeName("grok"), adopt_session=(str(uuid.uuid4()),)),
        create_work_dir=True,
    )

    with pytest.raises(UserInputError, match="not found"):
        on_before_create(args, temp_mngr_ctx)


def _make_source_agent_state(tmp_path: Path, session_id: str, role_text: str | None) -> Path:
    source_state_dir = tmp_path / "source-agent"
    make_grok_session_dir(source_state_dir / GROK_SESSIONS_RELATIVE_PATH, "%2Fsource%2Fwork", session_id)
    (source_state_dir / SESSION_ID_RELATIVE_PATH).write_text(session_id + "\n")
    if role_text is not None:
        (source_state_dir / ROLE_PROMPT_RELATIVE_PATH).write_text(role_text)
    return source_state_dir


def _work_dir_bucket(agent: GrokAgent) -> Path:
    canonical_work_dir = str(Path(agent.work_dir).resolve())
    return agent._get_agent_dir() / GROK_SESSIONS_RELATIVE_PATH / compute_session_bucket_name(canonical_work_dir)


@pytest.mark.rsync
def test_adopt_session_forks_the_clone_source_session_from_the_new_work_dir(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig())
    session_id = str(uuid.uuid4())
    source_state_dir = _make_source_agent_state(tmp_path, session_id, "Source role line\nmore\n")
    options = CreateAgentOptions(
        agent_type=AgentTypeName("grok"),
        source_agent_state_location=HostLocation(host=agent.host, path=source_state_dir),
    )

    with capture_loguru(level="INFO") as log_output:
        agent.on_after_provisioning(agent.host, options, agent.mngr_ctx)

    assert (_work_dir_bucket(agent) / session_id / SESSION_SUMMARY_FILENAME).is_file()
    assert (agent._get_agent_dir() / FORK_FROM_RELATIVE_PATH).read_text() == session_id + "\n"
    assert "with the source's role ('Source role line'); its own role (no role)" in log_output.getvalue()


def test_adopt_session_without_a_source_session_starts_fresh(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig())
    source_state_dir = tmp_path / "empty-source"
    source_state_dir.mkdir()
    options = CreateAgentOptions(
        agent_type=AgentTypeName("grok"),
        source_agent_state_location=HostLocation(host=agent.host, path=source_state_dir),
    )

    agent.adopt_session(agent.host, options, agent.mngr_ctx)

    assert not (agent._get_agent_dir() / FORK_FROM_RELATIVE_PATH).exists()


@pytest.mark.rsync
def test_adopt_session_copies_every_adopted_session_and_resumes_the_clone(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig())
    adopted_id = str(uuid.uuid4())
    make_grok_session_dir(tmp_path / ".grok" / "sessions", "%2Felsewhere", adopted_id)
    cloned_id = str(uuid.uuid4())
    source_state_dir = _make_source_agent_state(tmp_path, cloned_id, None)
    options = CreateAgentOptions(
        agent_type=AgentTypeName("grok"),
        adopt_session=(adopted_id,),
        source_agent_state_location=HostLocation(host=agent.host, path=source_state_dir),
    )

    agent.adopt_session(agent.host, options, agent.mngr_ctx)

    assert sorted(path.name for path in _work_dir_bucket(agent).iterdir()) == sorted([adopted_id, cloned_id])
    assert (agent._get_agent_dir() / FORK_FROM_RELATIVE_PATH).read_text() == cloned_id + "\n"


@pytest.mark.rsync
def test_adopt_session_resumes_the_last_adopted_session_and_names_the_role_it_keeps(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _make_grok_agent(local_provider, tmp_path, GrokAgentConfig())
    role_path = agent._get_agent_dir() / ROLE_PROMPT_RELATIVE_PATH
    role_path.parent.mkdir(parents=True)
    role_path.write_text("Reviewer role\n")
    first_adopted_id = str(uuid.uuid4())
    last_adopted_id = str(uuid.uuid4())
    for adopted_id in (first_adopted_id, last_adopted_id):
        make_grok_session_dir(tmp_path / ".grok" / "sessions", "%2Felsewhere", adopted_id)
    options = CreateAgentOptions(agent_type=AgentTypeName("grok"), adopt_session=(first_adopted_id, last_adopted_id))

    with capture_loguru(level="INFO") as log_output:
        agent.adopt_session(agent.host, options, agent.mngr_ctx)

    assert sorted(path.name for path in _work_dir_bucket(agent).iterdir()) == sorted(
        [first_adopted_id, last_adopted_id]
    )
    assert (agent._get_agent_dir() / FORK_FROM_RELATIVE_PATH).read_text() == last_adopted_id + "\n"
    assert "its own role ('Reviewer role') applies to the sessions it starts later" in log_output.getvalue()


@pytest.mark.parametrize(
    ("role_text", "expected"),
    [("", "no role"), ("\n  \nFirst line\nsecond", "'First line'"), ("x" * 90, "'" + "x" * 80 + "…'")],
)
def test_describe_grok_role(role_text: str, expected: str) -> None:
    assert describe_grok_role(role_text) == expected
