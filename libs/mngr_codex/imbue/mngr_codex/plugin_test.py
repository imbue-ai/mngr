"""Unit tests for CodexAgentConfig and CodexAgent."""

from __future__ import annotations

import json
import os
import shlex
import time
import tomllib
from collections import deque
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.ratchet_testing.ratchets import assert_posix_compatible
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.base_agent import SendKeysAgent
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.api.preservation import get_local_preserved_agent_dir
from imbue.mngr.api.testing import FakeHost
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.overlay_merge import merge_models_via_overlay
from imbue.mngr.errors import AgentInstallationError
from imbue.mngr.errors import AgentStartError
from imbue.mngr.errors import MessageDeliveredButBlockedError
from imbue.mngr.errors import PluginMngrError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.agent import InteractiveAgentMixin
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.plugins.hookspecs import OnBeforeCreateArgs
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import LifecycleProbeResult
from imbue.mngr.primitives import OutputStyleName
from imbue.mngr.primitives import SystemPromptText
from imbue.mngr.primitives import WaitingReason
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_codex.app_server_client import AppServerTransport
from imbue.mngr_codex.codex_config import APP_SERVER_THREAD_FILENAME
from imbue.mngr_codex.codex_config import RECORD_SESSION_POINTERS_SCRIPT_NAME
from imbue.mngr_codex.codex_config import ROOT_SESSION_FILENAME
from imbue.mngr_codex.codex_config import TRANSCRIPT_PATH_FILENAME
from imbue.mngr_codex.codex_config import get_codex_app_server_socket_path
from imbue.mngr_codex.codex_config import get_codex_auth_path
from imbue.mngr_codex.codex_config import get_codex_config_path
from imbue.mngr_codex.codex_config import get_codex_home
from imbue.mngr_codex.codex_config import get_codex_hooks_path
from imbue.mngr_codex.codex_config import get_codex_personality_migration_path
from imbue.mngr_codex.codex_config import is_project_trusted
from imbue.mngr_codex.plugin import CodexAgent
from imbue.mngr_codex.plugin import CodexAgentConfig
from imbue.mngr_codex.plugin import CodexUpdatePolicy
from imbue.mngr_codex.plugin import _app_server_client_version
from imbue.mngr_codex.plugin import _resolve_adopt_session
from imbue.mngr_codex.plugin import _resolve_lifecycle_state_for_permission
from imbue.mngr_codex.plugin import _session_id_from_rollout_path
from imbue.mngr_codex.plugin import _sessions_root_for_rollout
from imbue.mngr_codex.plugin import _user_native_codex_home
from imbue.mngr_codex.plugin import _waiting_reason
from imbue.mngr_codex.plugin import agent_field_generators
from imbue.mngr_codex.plugin import on_before_create
from imbue.mngr_codex.plugin import register_agent_type

# =============================================================================
# Config
# =============================================================================


def test_codex_agent_config_has_correct_defaults() -> None:
    config = CodexAgentConfig()
    assert str(config.command) == "codex"
    assert config.cli_args == ()
    assert config.parent_type is None
    assert config.model is None
    assert config.model_reasoning_effort is None
    assert config.sandbox_mode == "workspace-write"
    assert config.auto_allow_permissions is False
    assert config.auto_dismiss_dialogs_at_startup is False
    assert config.update_policy is CodexUpdatePolicy.ASK
    assert config.config_overrides == {}
    assert config.emit_common_transcript is True


def test_codex_agent_config_merge_with_replaces_cli_args() -> None:
    base = CodexAgentConfig()
    override = CodexAgentConfig(cli_args=("--foo",))
    merged, _ = merge_models_via_overlay(base, override)
    assert isinstance(merged, CodexAgentConfig)
    assert merged.cli_args == ("--foo",)
    assert str(merged.command) == "codex"


def test_codex_agent_subclasses_base_agent_not_send_keys_or_interactive_tui() -> None:
    """codex drives the app-server, so it subclasses ``BaseAgent`` directly -- not
    ``SendKeysAgent`` (it sends no keys) nor the send-keys/evidence-probe ``InteractiveTuiAgent``.

    Matching how ``pi`` / ``opencode`` subclass ``BaseAgent`` and override their own transport,
    this retires the abstract ``_build_submission_evidence_probes`` contract and the ready-glyph
    poll while leaving ``tui_agent.py`` / ``tui_utils.py`` intact for claude.
    """
    assert issubclass(CodexAgent, BaseAgent)
    assert not issubclass(CodexAgent, SendKeysAgent)
    assert not issubclass(CodexAgent, InteractiveTuiAgent)
    assert not hasattr(CodexAgent, "_build_submission_evidence_probes")
    assert not hasattr(CodexAgent, "TUI_READY_INDICATOR")
    # It MUST still declare InteractiveAgentMixin -- the marker `require_interactive_agent` gates
    # `mngr message` on. `SendKeysAgent` provided it implicitly; on BaseAgent it must be explicit
    # (as pi/opencode do), or `mngr message` fails "does not accept interactive messages".
    assert issubclass(CodexAgent, InteractiveAgentMixin)


def test_register_agent_type_returns_codex_class_and_config() -> None:
    name, agent_class, config_class = register_agent_type()
    assert name == "codex"
    assert agent_class is CodexAgent
    assert config_class is CodexAgentConfig


# =============================================================================
# Capability-mixin contract methods (install / unattended / permission / version)
# =============================================================================


def test_get_install_binary_name_is_codex() -> None:
    agent = CodexAgent.model_construct(agent_config=CodexAgentConfig())
    assert agent.get_install_binary_name() == "codex"


def test_get_install_command_installs_codex() -> None:
    agent = CodexAgent.model_construct(agent_config=CodexAgentConfig())
    assert agent.get_install_command() == "npm i -g @openai/codex"


def test_get_install_command_pins_version() -> None:
    agent = CodexAgent.model_construct(agent_config=CodexAgentConfig(version="0.139.0"))
    assert agent.get_install_command() == "npm i -g @openai/codex@0.139.0"


def test_verify_pinned_codex_version_passes_on_match(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    # `sh -c 'echo codex-cli 0.139.0'` ignores the appended --version, so the probe
    # parses 0.139.0 without a real codex binary.
    agent = _make_codex_agent(
        CodexAgent,
        local_provider,
        tmp_path,
        CodexAgentConfig(version="0.139.0", command=CommandString("sh -c 'echo codex-cli 0.139.0'")),
    )
    agent._verify_pinned_codex_version(agent.host)


def test_verify_pinned_codex_version_raises_on_mismatch(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    agent = _make_codex_agent(
        CodexAgent,
        local_provider,
        tmp_path,
        CodexAgentConfig(version="0.140.0", command=CommandString("sh -c 'echo codex-cli 0.139.0'")),
    )
    with pytest.raises(AgentInstallationError, match="version mismatch"):
        agent._verify_pinned_codex_version(agent.host)


def test_reconcile_verifies_pin_and_skips_update_check_when_pinned() -> None:
    """With a pinned version, reconcile verifies the pin and does NOT run the update check."""
    calls: list[str] = []

    class _RecordingAgent(CodexAgent):
        def _verify_pinned_codex_version(self, host: object) -> None:
            calls.append("verify")

        def _maybe_check_for_codex_update(self, host: object, user_codex_home: Path, mngr_ctx: object) -> None:
            calls.append("update_check")

    agent = _RecordingAgent.model_construct(agent_config=CodexAgentConfig(version="0.139.0"))
    agent.reconcile_installed_version(cast(OnlineHostInterface, object()), cast(MngrContext, object()))
    assert calls == ["verify"]


def test_is_unattended_enabled_reflects_auto_allow_permissions() -> None:
    unattended = CodexAgent.model_construct(agent_config=CodexAgentConfig(auto_allow_permissions=True))
    attended = CodexAgent.model_construct(agent_config=CodexAgentConfig())
    assert unattended.is_unattended_enabled() is True
    assert attended.is_unattended_enabled() is False


def test_get_permission_policy_carries_sandbox_mode() -> None:
    agent = CodexAgent.model_construct(agent_config=CodexAgentConfig(sandbox_mode="read-only"))
    policy = agent.get_permission_policy()
    assert policy["sandbox_mode"] == "read-only"


def test_get_permission_policy_includes_approval_policy_override() -> None:
    agent = CodexAgent.model_construct(agent_config=CodexAgentConfig(config_overrides={"approval_policy": "never"}))
    policy = agent.get_permission_policy()
    assert policy["sandbox_mode"] == "workspace-write"
    assert policy["approval_policy"] == "never"


def test_reconcile_installed_version_delegates_to_update_check() -> None:
    # codex's version reconciliation IS its update check: reconcile resolves the codex
    # home and runs _maybe_check_for_codex_update against it. (The update decision logic
    # itself is covered by the _read_codex_versions-override tests below.)
    recorded: dict[str, object] = {}

    class _RecordingAgent(CodexAgent):
        def _resolve_user_codex_home(self, host: object) -> Path:
            return Path("/sentinel/codex-home")

        def _maybe_check_for_codex_update(self, host: object, user_codex_home: Path, mngr_ctx: object) -> None:
            recorded["home"] = user_codex_home

    agent = _RecordingAgent.model_construct(agent_config=CodexAgentConfig())
    agent.reconcile_installed_version(cast(OnlineHostInterface, object()), cast(MngrContext, object()))
    assert recorded["home"] == Path("/sentinel/codex-home")


class _StubHost(FakeHost):
    """FakeHost that returns scripted results for commands matched by substring.

    Records every command and returns the first ``command_results`` entry whose
    pattern is a substring of the command; otherwise falls through to the local
    FakeHost. Lets the host-shell helpers (version probe, codex update,
    CODEX_HOME resolution) be exercised without a real codex binary.
    """

    command_results: dict[str, CommandResult] = {}
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
        for pattern, result in self.command_results.items():
            if pattern in command:
                return result
        return super()._execute_command(command, user, cwd, env, timeout_seconds)


def _stub_host(
    host_dir: Path,
    *,
    is_local: bool = True,
    command_results: dict[str, CommandResult] | None = None,
) -> Any:
    """Create a _StubHost typed as Any to satisfy OnlineHostInterface parameters in tests."""
    return _StubHost(host_dir=host_dir, is_local=is_local, command_results=command_results or {})


# =============================================================================
# Construction helpers
# =============================================================================


def _make_codex_agent(
    agent_cls: type[CodexAgent],
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    agent_config: CodexAgentConfig,
    *,
    is_interactive: bool = False,
    is_auto_approve: bool = False,
) -> CodexAgent:
    # These setup tests run against a real local host where codex is not installed; the
    # install check is irrelevant to provision setup (files/trust/config) and is covered
    # separately, so skip it unless a caller opted in explicitly.
    if "check_installation" not in agent_config.model_fields_set:
        agent_config = agent_config.model_copy_update(to_update(agent_config.field_ref().check_installation, False))
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    ctx = local_provider.mngr_ctx.model_copy_update(
        to_update(local_provider.mngr_ctx.field_ref().is_interactive, is_interactive),
        to_update(local_provider.mngr_ctx.field_ref().is_auto_approve, is_auto_approve),
    )
    return agent_cls.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-codex"),
        agent_type=AgentTypeName("codex"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=ctx,
        agent_config=agent_config,
        host=host,
    )


class _ConfirmingCodexAgent(CodexAgent):
    """Test subclass whose trust prompt auto-accepts without invoking click.confirm."""

    def _prompt_user_to_trust_workspace(self, source_path: Path, config_path: Path) -> bool:
        return True


class _DecliningCodexAgent(CodexAgent):
    """Test subclass whose trust prompt auto-declines without invoking click.confirm."""

    def _prompt_user_to_trust_workspace(self, source_path: Path, config_path: Path) -> bool:
        return False


@pytest.fixture
def codex_agent(local_provider: LocalProviderInstance, tmp_path: Path) -> CodexAgent:
    return _make_codex_agent(CodexAgent, local_provider, tmp_path, CodexAgentConfig(), is_auto_approve=True)


# =============================================================================
# send_message (app-server drive)
# =============================================================================


class _ScriptedTransport:
    """A scripted in-memory ``AppServerTransport`` for ``send_message`` tests.

    Configure per-method responses via ``respond_result`` / ``respond_error``; the client
    reads them back off the inbound queue. Sent frames are recorded for assertions, and
    ``is_closed`` proves ``send_message`` always closes its connection.
    """

    def __init__(self) -> None:
        self._inbound: deque[str] = deque()
        self.sent: list[str] = []
        self._responders: dict[str, Callable[[Mapping[str, Any]], None]] = {}
        self.is_closed = False

    def send(self, message: str) -> None:
        self.sent.append(message)
        request = json.loads(message)
        responder = self._responders.get(request.get("method"))
        if responder is not None:
            responder(request)

    def receive(self, timeout: float | None) -> str:
        if not self._inbound:
            raise TimeoutError("no frame available")
        return self._inbound.popleft()

    def close(self) -> None:
        self.is_closed = True

    def _push(self, frame: Mapping[str, Any]) -> None:
        self._inbound.append(json.dumps(frame))

    def respond_result(self, method: str, result: Mapping[str, Any]) -> None:
        self._responders[method] = lambda request: self._push(
            {"jsonrpc": "2.0", "id": request["id"], "result": result}
        )

    def respond_error(self, method: str, code: int, message: str) -> None:
        self._responders[method] = lambda request: self._push(
            {"jsonrpc": "2.0", "id": request["id"], "error": {"code": code, "message": message}}
        )

    def params_of(self, method: str) -> Mapping[str, Any]:
        for frame in self.sent:
            parsed = json.loads(frame)
            if parsed.get("method") == method:
                return parsed["params"]
        raise AssertionError(f"no {method!r} frame was sent")

    def sent_methods(self) -> list[str]:
        return [json.loads(frame).get("method") for frame in self.sent]


def _initialize_result() -> dict[str, Any]:
    return {"userAgent": "mngr", "codexHome": "/h", "platformFamily": "unix", "platformOs": "linux"}


def _thread_read_result(status: Mapping[str, Any], turns: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {"thread": {"id": "thread-1", "status": status, "turns": list(turns)}}


def _scripted_codex_agent(
    local_provider: LocalProviderInstance, tmp_path: Path, transport: _ScriptedTransport
) -> CodexAgent:
    """A codex agent whose app-server connection is the given scripted transport."""

    class _ScriptedCodexAgent(CodexAgent):
        def _connect_app_server_transport(self) -> AppServerTransport:
            return transport

    return _make_codex_agent(_ScriptedCodexAgent, local_provider, tmp_path, CodexAgentConfig(), is_auto_approve=True)


def test_send_message_delivers_when_idle(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """Idle thread -> ``turn/start``: a fresh thread is started, persisted, and delivered."""
    transport = _ScriptedTransport()
    transport.respond_result("initialize", _initialize_result())
    transport.respond_result("thread/loaded/list", {"data": []})
    transport.respond_result("thread/start", {"thread": {"id": "thread-1", "status": {"type": "idle"}}})
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    # Post-send block check: active, no waiting flags -> not blocked.
    transport.respond_result("thread/read", _thread_read_result({"type": "active", "activeFlags": []}, []))
    agent = _scripted_codex_agent(local_provider, tmp_path, transport)

    agent.send_message("hello world")

    assert "turn/start" in transport.sent_methods()
    assert "turn/steer" not in transport.sent_methods()
    start_params = transport.params_of("turn/start")
    assert start_params["input"] == [{"type": "text", "text": "hello world"}]
    assert start_params["clientUserMessageId"].startswith("mngr-cid-")
    assert start_params["threadId"] == "thread-1"
    # The started thread id is persisted as this agent's root thread.
    persisted = (agent._get_agent_dir() / APP_SERVER_THREAD_FILENAME).read_text().strip()
    assert persisted == "thread-1"
    assert transport.is_closed is True


def test_send_message_parks_when_busy(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """A running turn -> ``turn/steer``: the message parks into the live turn, not a new one."""
    transport = _ScriptedTransport()
    transport.respond_result("initialize", _initialize_result())
    transport.respond_result("thread/loaded/list", {"data": ["thread-1"]})
    transport.respond_result(
        "thread/read",
        _thread_read_result({"type": "active", "activeFlags": []}, [{"id": "turn-7", "status": "inProgress"}]),
    )
    transport.respond_result("turn/steer", {"turnId": "turn-7"})
    agent = _scripted_codex_agent(local_provider, tmp_path, transport)

    agent.send_message("park me")

    assert "turn/steer" in transport.sent_methods()
    assert "turn/start" not in transport.sent_methods()
    steer_params = transport.params_of("turn/steer")
    assert steer_params["expectedTurnId"] == "turn-7"
    assert steer_params["clientUserMessageId"].startswith("mngr-cid-")
    # The single loaded thread is adopted and persisted as the root.
    persisted = (agent._get_agent_dir() / APP_SERVER_THREAD_FILENAME).read_text().strip()
    assert persisted == "thread-1"
    assert transport.is_closed is True


def test_send_message_pins_persisted_root_among_loaded_subagents(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """With sub-agent threads also loaded, the persisted root id is the one sent to."""
    transport = _ScriptedTransport()
    transport.respond_result("initialize", _initialize_result())
    transport.respond_result("thread/loaded/list", {"data": ["subagent-a", "root-thread", "subagent-b"]})
    transport.respond_result("thread/read", _thread_read_result({"type": "idle"}, []))
    transport.respond_result("turn/start", {"turn": {"id": "turn-9", "status": "inProgress"}})
    agent = _scripted_codex_agent(local_provider, tmp_path, transport)
    agent.host.write_text_file(agent._get_agent_dir() / APP_SERVER_THREAD_FILENAME, "root-thread")

    agent.send_message("to the root")

    assert transport.params_of("turn/start")["threadId"] == "root-thread"


def test_send_message_raises_send_message_error_when_not_accepted(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """A daemon rejection of the submit is a non-delivery -> ``SendMessageError`` (not blocked)."""
    transport = _ScriptedTransport()
    transport.respond_result("initialize", _initialize_result())
    transport.respond_result("thread/loaded/list", {"data": []})
    transport.respond_result("thread/start", {"thread": {"id": "thread-1", "status": {"type": "idle"}}})
    transport.respond_error("turn/start", -32000, "model provider unavailable")
    agent = _scripted_codex_agent(local_provider, tmp_path, transport)

    with pytest.raises(SendMessageError) as exc_info:
        agent.send_message("nope")
    assert not isinstance(exc_info.value, MessageDeliveredButBlockedError)
    assert transport.is_closed is True


def test_send_message_raises_when_handshake_fails(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """A failed ``initialize`` is a send failure -> ``SendMessageError``, connection closed."""
    transport = _ScriptedTransport()
    transport.respond_error("initialize", -32600, "requires experimentalApi capability")
    agent = _scripted_codex_agent(local_provider, tmp_path, transport)

    with pytest.raises(SendMessageError):
        agent.send_message("hello")
    assert transport.is_closed is True


def test_send_message_raises_delivered_but_blocked_when_agent_blocks(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """Delivered, but the thread is left awaiting approval -> ``MessageDeliveredButBlockedError``."""
    transport = _ScriptedTransport()
    transport.respond_result("initialize", _initialize_result())
    transport.respond_result("thread/loaded/list", {"data": []})
    transport.respond_result("thread/start", {"thread": {"id": "thread-1", "status": {"type": "idle"}}})
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    transport.respond_result(
        "thread/read",
        _thread_read_result(
            {"type": "active", "activeFlags": ["waitingOnApproval"]}, [{"id": "turn-1", "status": "inProgress"}]
        ),
    )
    agent = _scripted_codex_agent(local_provider, tmp_path, transport)

    with pytest.raises(MessageDeliveredButBlockedError):
        agent.send_message("do the risky thing")
    # It IS delivered: the turn was started before the block was detected.
    assert "turn/start" in transport.sent_methods()
    assert transport.is_closed is True


# =============================================================================
# Lifecycle / waiting_reason re-sourced from live thread status (event-sourced)
# =============================================================================


def _status_transport(
    status: Mapping[str, Any],
    turns: Sequence[Mapping[str, Any]],
    *,
    loaded: list[str] | None = None,
) -> _ScriptedTransport:
    """A scripted transport that answers a lifecycle status probe of the root thread.

    Responds to the handshake, ``thread/loaded/list`` (the root ``thread-1`` is loaded by
    default), and ``thread/read`` with the given ``status`` / ``turns``.
    """
    transport = _ScriptedTransport()
    transport.respond_result("initialize", _initialize_result())
    transport.respond_result("thread/loaded/list", {"data": ["thread-1"] if loaded is None else loaded})
    transport.respond_result("thread/read", _thread_read_result(status, turns))
    return transport


def _live_codex_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    transport: _ScriptedTransport,
    base_state: AgentLifecycleState,
    *,
    base_pid: int | None = None,
) -> CodexAgent:
    """A codex agent whose daemon status is scripted AND whose process-presence probe is fixed.

    The scripted transport drives ``_read_live_thread_status``; the fixed base probe stands in
    for the tmux/ps process-presence result (RUNNING/WAITING = window alive) so the live
    RUNNING-vs-WAITING re-derivation is exercised without a real pane. The root thread id is
    persisted so the status probe binds it.
    """

    class _LiveCodexAgent(CodexAgent):
        def _connect_app_server_transport(self) -> AppServerTransport:
            return transport

        def _base_probe_lifecycle(self) -> LifecycleProbeResult:
            return LifecycleProbeResult(state=base_state, pid=base_pid)

    agent = _make_codex_agent(_LiveCodexAgent, local_provider, tmp_path, CodexAgentConfig(), is_auto_approve=True)
    agent.host.write_text_file(agent._get_agent_dir() / APP_SERVER_THREAD_FILENAME, "thread-1")
    return agent


def test_get_lifecycle_state_running_when_turn_in_flight(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """A live turn (thread active, in-progress turn) reads RUNNING."""
    transport = _status_transport({"type": "active", "activeFlags": []}, [{"id": "turn-1", "status": "inProgress"}])
    agent = _live_codex_agent(local_provider, tmp_path, transport, AgentLifecycleState.RUNNING)
    assert agent.get_lifecycle_state() == AgentLifecycleState.RUNNING


def test_get_lifecycle_state_waiting_when_thread_idle_even_if_marker_lags(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """An idle thread reads WAITING immediately -- even though the process is present (base RUNNING).

    This is the A6/lifecycle fix: once the turn completes the daemon is idle, so the state flips to
    WAITING at once instead of lingering on a stale ``active`` marker (base RUNNING) until a hook fires.
    """
    transport = _status_transport({"type": "idle"}, [])
    agent = _live_codex_agent(local_provider, tmp_path, transport, AgentLifecycleState.RUNNING)
    assert agent.get_lifecycle_state() == AgentLifecycleState.WAITING


def test_get_lifecycle_state_promotes_to_waiting_on_approval(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """A turn parked awaiting approval promotes RUNNING -> WAITING (no permissions_waiting marker)."""
    transport = _status_transport(
        {"type": "active", "activeFlags": ["waitingOnApproval"]}, [{"id": "turn-1", "status": "inProgress"}]
    )
    agent = _live_codex_agent(local_provider, tmp_path, transport, AgentLifecycleState.RUNNING)
    assert agent.get_lifecycle_state() == AgentLifecycleState.WAITING


def test_get_lifecycle_state_stays_running_through_the_flush_tail_until_completed(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """RUNNING lasts until the turn is FULLY done (contract A6), not when token-generation stops.

    While the daemon reports the thread ``active`` with an in-progress turn -- including the flush
    tail after the model has stopped producing tokens but before ``turn/completed`` -- the state is
    RUNNING (no ``waitingOn*`` flag, so it is not a parked WAITING). Only when the turn completes
    and the thread goes ``idle`` does the state flip to WAITING; there is no earlier idle blip.
    """
    # The flush tail: the turn is still in progress (active, no waiting flags) even though tokens
    # may have stopped. This must read RUNNING, not WAITING.
    running = _status_transport({"type": "active", "activeFlags": []}, [{"id": "turn-1", "status": "inProgress"}])
    agent = _live_codex_agent(local_provider, tmp_path, running, AgentLifecycleState.RUNNING)
    assert agent.get_lifecycle_state() == AgentLifecycleState.RUNNING

    # turn/completed: the thread is now idle, so the state flips to WAITING at once.
    completed = _status_transport({"type": "idle"}, [])
    idle_agent = _live_codex_agent(local_provider, tmp_path, completed, AgentLifecycleState.RUNNING)
    assert idle_agent.get_lifecycle_state() == AgentLifecycleState.WAITING


def test_get_lifecycle_state_keeps_stopped_without_querying_the_daemon(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """A dead/absent window (base STOPPED) is reported verbatim and never queries the daemon.

    Process presence is authoritative for STOPPED/DONE/REPLACED, so no WebSocket connection is
    opened for a stopped agent (no per-``mngr ls`` churn against a daemon that is gone anyway).
    """
    transport = _status_transport({"type": "idle"}, [])
    agent = _live_codex_agent(local_provider, tmp_path, transport, AgentLifecycleState.STOPPED)
    assert agent.get_lifecycle_state() == AgentLifecycleState.STOPPED
    assert transport.sent == []


def test_probe_lifecycle_resources_split_and_preserves_pid(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """``probe_lifecycle`` re-derives the RUNNING/WAITING split from live status, keeping the pid."""
    transport = _status_transport({"type": "idle"}, [])
    agent = _live_codex_agent(local_provider, tmp_path, transport, AgentLifecycleState.RUNNING, base_pid=4242)
    probe = agent.probe_lifecycle()
    assert probe.state == AgentLifecycleState.WAITING
    assert probe.pid == 4242


@pytest.mark.parametrize(
    "status, flags, expected",
    [
        ("active", [], None),
        ("idle", [], WaitingReason.END_OF_TURN),
        ("active", ["waitingOnApproval"], WaitingReason.PERMISSIONS),
        ("active", ["waitingOnUserInput"], WaitingReason.PERMISSIONS),
    ],
)
def test_compute_waiting_reason_from_live_status(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    status: str,
    flags: list[str],
    expected: WaitingReason | None,
) -> None:
    """The waiting reason is read live from thread status (running -> None; parked -> PERMISSIONS)."""
    turns = [{"id": "turn-1", "status": "inProgress"}] if status == "active" else []
    transport = _status_transport({"type": status, "activeFlags": flags}, turns)
    agent = _live_codex_agent(local_provider, tmp_path, transport, AgentLifecycleState.RUNNING)
    assert agent.compute_waiting_reason() == expected


def test_waiting_reason_generator_uses_live_status(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """The ``waiting_reason`` field generator delegates to the live-status read for a codex agent."""
    transport = _status_transport({"type": "active", "activeFlags": []}, [{"id": "turn-1", "status": "inProgress"}])
    agent = _live_codex_agent(local_provider, tmp_path, transport, AgentLifecycleState.RUNNING)
    assert _waiting_reason(agent, agent.host) is None


def test_status_probe_never_starts_a_thread(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """With no root thread loadable, a status probe returns None and starts nothing.

    A lifecycle read must be side-effect-light: it must never create a conversation, so no
    ``thread/start`` is sent when there is no root thread to bind.
    """
    transport = _ScriptedTransport()
    transport.respond_result("initialize", _initialize_result())
    transport.respond_result("thread/loaded/list", {"data": []})

    class _NoPersistLiveAgent(CodexAgent):
        def _connect_app_server_transport(self) -> AppServerTransport:
            return transport

    agent = _make_codex_agent(_NoPersistLiveAgent, local_provider, tmp_path, CodexAgentConfig(), is_auto_approve=True)
    # No persisted root id and nothing loaded -> nothing to bind.
    assert agent._read_live_thread_status() is None
    assert "thread/start" not in transport.sent_methods()
    assert "thread/read" not in transport.sent_methods()


def test_read_live_thread_status_returns_none_on_remote_host(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """A remote host's unix socket is unreachable from the mngr process -> None (no marker fallback)."""
    agent = _make_remote_codex_agent(local_provider, tmp_path, {})
    assert agent._read_live_thread_status() is None


def test_get_lifecycle_state_degrades_to_waiting_when_daemon_unreachable(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """When the daemon can't be read, the split degrades to WAITING -- there is NO marker fallback.

    A failed ``initialize`` yields no live status. With the marker path deleted, an alive-but-
    unreadable daemon is reported WAITING (the conservative "cannot confirm a running turn"
    default), regardless of any on-disk file. ``waiting_reason`` likewise degrades to END_OF_TURN.
    """
    transport = _ScriptedTransport()
    transport.respond_error("initialize", -32600, "handshake refused")
    agent = _live_codex_agent(local_provider, tmp_path, transport, AgentLifecycleState.RUNNING)
    assert agent.get_lifecycle_state() == AgentLifecycleState.WAITING
    assert agent.compute_waiting_reason() == WaitingReason.END_OF_TURN


# =============================================================================
# Simple accessors
# =============================================================================


def test_get_expected_process_name(codex_agent: CodexAgent) -> None:
    assert codex_agent.get_expected_process_name() == "codex"


def test_is_common_transcript_enabled_follows_config(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    on = _make_codex_agent(CodexAgent, local_provider, tmp_path, CodexAgentConfig())
    off = _make_codex_agent(CodexAgent, local_provider, tmp_path, CodexAgentConfig(emit_common_transcript=False))
    assert on.is_common_transcript_enabled is True
    assert off.is_common_transcript_enabled is False


def test_transcript_scripts_are_loadable(codex_agent: CodexAgent) -> None:
    """Both transcript-script accessors return non-empty shell content from resources/."""
    raw = codex_agent.get_raw_transcript_scripts()
    common = codex_agent.get_common_transcript_scripts()
    assert "stream_transcript.sh" in raw
    assert raw["stream_transcript.sh"].strip() != ""
    assert "common_transcript.sh" in common
    assert common["common_transcript.sh"].strip() != ""


def test_codex_home_and_root_session_paths(codex_agent: CodexAgent) -> None:
    state_dir = codex_agent._get_agent_dir()
    assert codex_agent._get_codex_home() == get_codex_home(state_dir)
    assert codex_agent._get_root_session_file_path() == state_dir / "codex_root_session"


# =============================================================================
# Host-shell resolution helpers
# =============================================================================


def test_resolve_user_codex_home_defaults_to_home_dot_codex(codex_agent: CodexAgent, tmp_path: Path) -> None:
    """With no CODEX_HOME override, resolves to $HOME/.codex (HOME is test-redirected)."""
    host = codex_agent.host
    resolved = codex_agent._resolve_user_codex_home(host)
    assert resolved.name == ".codex"
    assert resolved.parent == Path.home()


def test_resolve_user_codex_home_aborts_when_resolution_fails(codex_agent: CodexAgent, tmp_path: Path) -> None:
    """A failed CODEX_HOME-resolution probe is a user-facing error: the shared auth.json can't be located."""
    host = _stub_host(
        tmp_path,
        command_results={"${CODEX_HOME:-$HOME/.codex}": CommandResult(stdout="", stderr="boom", success=False)},
    )
    with pytest.raises(PluginMngrError):
        codex_agent._resolve_user_codex_home(host)


def test_resolve_canonical_path_resolves_symlinks(codex_agent: CodexAgent, tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    resolved = codex_agent._resolve_canonical_path(codex_agent.host, link)
    assert Path(resolved) == real.resolve()


# =============================================================================
# assemble_command
# =============================================================================


def test_assemble_command_structure(codex_agent: CodexAgent) -> None:
    command = str(codex_agent.assemble_command(codex_agent.host, (), None))
    codex_home = str(codex_agent._get_codex_home())
    # The socket lives at a short /tmp path (NOT under CODEX_HOME) to stay under the unix-socket
    # SUN_LEN limit; every client resolves it via get_codex_app_server_socket_path.
    socket_path = str(get_codex_app_server_socket_path(codex_agent._get_codex_home()))
    # Backgrounded supervisor, scoped to `&` so codex is the foreground process.
    assert "codex_background_tasks.sh" in command
    assert command.split("&", 1)[0].strip().startswith("( bash")
    # cwd is the work dir (codex accepts the dotted path; no symlink workaround).
    assert f"cd {codex_agent.work_dir}" in command
    # CODEX_HOME injected only on the codex process.
    assert f"env CODEX_HOME={codex_home}" in command
    # The stale socket is cleaned before the daemon binds.
    assert f"rm -f {socket_path}" in command
    # The daemon runs in a detached sidecar window; the visible TUI is `codex --remote`.
    assert "tmux new-window -d" in command
    assert "app-server --listen" in command
    assert "--remote" in command
    assert f"unix://{socket_path}" in command


def test_assemble_command_launches_both_windows(codex_agent: CodexAgent) -> None:
    """The primary window runs the visible `--remote` TUI; a detached sidecar runs the daemon."""
    command = str(codex_agent.assemble_command(codex_agent.host, (), None))
    socket_url = f"unix://{get_codex_app_server_socket_path(codex_agent._get_codex_home())}"
    # Sidecar daemon window, named and detached, running the app-server on the socket. The daemon
    # carries --dangerously-bypass-hook-trust --enable hooks (top-level, before the subcommand):
    # codex gates hooks behind a default-off feature flag, and an enabled command hook still stays
    # untrusted until bypassed, so without both the daemon fires NO hooks (verified live).
    assert "codex-app-server" in command
    daemon_index = command.index("app-server --listen")
    assert f"--dangerously-bypass-hook-trust --enable hooks app-server --listen {socket_url}" in command
    # Primary window: the visible remote TUI on the same socket, after the daemon spawn.
    remote_index = command.index("codex --remote")
    assert f"codex --remote {socket_url}" in command
    # The remote TUI also carries --dangerously-bypass-hook-trust. (Hooks fire only on turns TYPED
    # into this TUI; codex does not fire hooks on programmatic app-server turns -- verified live.)
    assert "codex --remote" in command
    assert "--dangerously-bypass-hook-trust" in command[remote_index:]
    # The primary window waits (bounded) for the socket before attaching.
    assert "-S" in command and "while" in command
    # The daemon is spawned before the primary `--remote` attaches.
    assert daemon_index < remote_index


def test_assemble_command_daemon_window_sources_agent_env(codex_agent: CodexAgent) -> None:
    """The daemon sidecar window must inherit the agent env, or its hooks die with exit 127.

    Turns run in the daemon, so the daemon is where codex fires its hooks (the UserPromptSubmit
    transcript-marker writer, the PreToolUse policy hooks). Those are invoked as
    ``bash "$MNGR_AGENT_STATE_DIR/..."`` / ``bash "$MNGR_AGENT_WORK_DIR/..."``; the detached
    ``tmux new-window`` starts with a bare environment, so without sourcing the agent env every
    hook path resolves to a nonexistent file. Assert the env-sourcing step is spawned INSIDE the
    daemon window (after ``tmux new-window``) and BEFORE the ``app-server`` exec.
    """
    command = str(codex_agent.assemble_command(codex_agent.host, (), None))
    new_window_index = command.index("tmux new-window")
    daemon_index = command.index("app-server --listen")
    # The full env-sourcing block (set -a ... set +a) is spawned inside the daemon window,
    # before the app-server exec.
    assert new_window_index < command.index("set -a") < daemon_index
    assert new_window_index < command.index("set +a") < daemon_index
    # An env file is sourced (the source paths end in `/env`).
    assert "/env" in command[new_window_index:daemon_index]


def test_assemble_command_has_no_marker_reset_but_stamps_process_start(codex_agent: CodexAgent) -> None:
    """The lifecycle no longer reads any marker, so the launch prelude resets none.

    RUNNING/WAITING comes from the daemon's thread/status, so there is no stale ``active`` /
    ``codex_root_active`` / ``codex_subagents`` / lock to clear on relaunch. The process-start
    stamp is still written (the system_interface activity tracker uses its mtime as a stale-tail
    gate). The primary window READS ``codex_root_session`` to resume the root conversation, but
    only reads it (``cat``) -- it is never removed or overwritten -- and the transcript path is
    left untouched.
    """
    command = str(codex_agent.assemble_command(codex_agent.host, (), None))
    assert "rm -rf" not in command
    assert "$MNGR_AGENT_STATE_DIR/active" not in command
    assert "codex_root_active" not in command
    assert "codex_subagents" not in command
    assert "codex_marker.lock" not in command
    # The process-start boundary is still stamped.
    assert 'touch "$MNGR_AGENT_STATE_DIR/codex_process_started"' in command
    # codex_root_session is read (for the self-healing resume, via command substitution) but
    # never written or removed: it appears only in read contexts (`[ ! -s <file> ]`, `$(cat ...)`).
    assert "codex_root_session" in command
    assert "$(cat " in command
    assert "rm -f " + shlex.quote(str(codex_agent._get_root_session_file_path())) not in command
    assert "codex_transcript_path" not in command


def test_assemble_command_resumes_root_conversation_self_healing(codex_agent: CodexAgent) -> None:
    """The primary window resumes the persisted root, falling back to a fresh TUI when there is none.

    On start/resume ``codex_root_session`` already holds the root id, so the TUI attaches to the
    ONE root conversation via ``codex resume <id> --remote``; when the file is empty (a degraded
    create, or genuinely no prior session) it launches a fresh ``codex --remote`` -- self-healing,
    never a hang. A bounded wait for the id file synchronizes the TUI with mngr writing it at create.
    """
    command = str(codex_agent.assemble_command(codex_agent.host, (), None))
    assert 'if [ -n "$__mngr_sid" ]; then' in command
    assert 'resume "$__mngr_sid" --remote' in command
    # Fresh fallback (the else branch) still launches --remote against the same socket.
    assert "--remote unix://" in command
    # Bounded wait for the persisted id file before the TUI launches (create/start synchronization).
    assert "codex_root_session" in command and "-lt" in command


def test_assemble_command_appends_cli_and_agent_args(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    agent = _make_codex_agent(CodexAgent, local_provider, tmp_path, CodexAgentConfig(cli_args=("--oss",)))
    command = str(agent.assemble_command(agent.host, ("hello world",), None))
    assert "--oss" in command
    # agent_args are shell-quoted (raw argv).
    assert "'hello world'" in command


def test_assemble_command_honors_command_override(codex_agent: CodexAgent) -> None:
    command = str(codex_agent.assemble_command(codex_agent.host, (), CommandString("/opt/codex")))
    assert "env CODEX_HOME=" in command
    # The override binary drives both the daemon and the visible remote TUI.
    assert "/opt/codex --dangerously-bypass-hook-trust --enable hooks app-server --listen" in command
    assert "/opt/codex --remote" in command


# assert_posix_compatible shells out to shellcheck; fast locally but observed
# exceeding the default 10s pytest-timeout under CI sandbox load.
@pytest.mark.flaky
@pytest.mark.timeout(60)
def test_assemble_command_is_posix_compatible(codex_agent: CodexAgent) -> None:
    """The assembled command runs in the user's interactive shell (possibly zsh), so it
    must avoid bash-only constructs (the socket-wait loop uses POSIX `[ -S ]` / `$(())`)."""
    command = str(codex_agent.assemble_command(codex_agent.host, ("a b", "--flag"), None))
    assert_posix_compatible(command)


# =============================================================================
# wait_for_ready_signal (app-server readiness)
# =============================================================================


def _make_remote_codex_agent(
    local_provider: LocalProviderInstance, tmp_path: Path, command_results: dict[str, CommandResult]
) -> CodexAgent:
    """Build a codex agent whose (non-local) host answers shell probes from a script."""
    host = _stub_host(tmp_path / "remote_host", is_local=False, command_results=command_results)
    return CodexAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-codex"),
        agent_type=AgentTypeName("codex"),
        work_dir=tmp_path / "work",
        create_time=datetime.now(timezone.utc),
        mngr_ctx=local_provider.mngr_ctx,
        agent_config=CodexAgentConfig(),
        host=host,
    )


def test_wait_for_ready_signal_runs_start_action_and_skips_wait_when_not_awaited(
    codex_agent: CodexAgent,
) -> None:
    calls: list[str] = []
    codex_agent.wait_for_ready_signal(False, lambda: calls.append("started"))
    # start_action ran; no readiness wait was performed (nothing to connect to).
    assert calls == ["started"]


def test_wait_for_ready_signal_times_out_when_daemon_never_binds(codex_agent: CodexAgent) -> None:
    """On a local host, readiness connects to the socket; a missing daemon times out cleanly."""
    calls: list[str] = []
    with pytest.raises(AgentStartError):
        codex_agent.wait_for_ready_signal(True, lambda: calls.append("started"), timeout=0.3)
    assert calls == ["started"]


def test_wait_for_ready_signal_ready_when_remote_socket_present(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _make_remote_codex_agent(
        local_provider, tmp_path, {"test -S": CommandResult(stdout="ready\n", stderr="", success=True)}
    )
    # Returns without raising once the socket is present on the (remote) host.
    agent.wait_for_ready_signal(True, lambda: None, timeout=5.0)


def test_wait_for_ready_signal_remote_times_out_when_socket_absent(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _make_remote_codex_agent(
        local_provider, tmp_path, {"test -S": CommandResult(stdout="", stderr="", success=True)}
    )
    with pytest.raises(AgentStartError):
        agent.wait_for_ready_signal(True, lambda: None, timeout=0.3)


def test_wait_for_ready_signal_establishes_and_persists_root_conversation(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """On create (awaited), once the daemon is ready mngr establishes ONE root conversation.

    It ``thread/start``s a thread, materializes its rollout with ``thread/inject_items`` (a single
    environmentContext item, no model turn), and persists the returned id to BOTH pointer files --
    so ``assemble_command``'s ``codex resume <id> --remote`` and the send/status bind path all land
    on the same conversation.
    """
    transport = _ScriptedTransport()
    transport.respond_result("initialize", _initialize_result())
    transport.respond_result("thread/start", {"thread": {"id": "root-1", "status": {"type": "idle"}}})
    transport.respond_result("thread/inject_items", {})

    fake_rollout = tmp_path / "sessions" / "2026" / "01" / "01" / "rollout-2026-01-01T00-00-00-root-1.jsonl"

    class _Agent(CodexAgent):
        def _connect_app_server_transport(self) -> AppServerTransport:
            return transport

        # Skip the real socket handshake; the scripted transport stands in for the daemon.
        def _wait_for_app_server_ready(self, timeout: float | None) -> None:
            return None

        # No live tmux pane here; the hook-trust clear is a tmux concern, tested on its own below.
        def _clear_hook_trust_prompt(self) -> None:
            return None

        # Stand in for the rollout the daemon would materialize, so the transcript-path write runs
        # without a live daemon (and without the find racing a real file).
        def _find_adopted_rollout_path(
            self, host: OnlineHostInterface, sessions_dir: Path, session_id: str
        ) -> Path | None:
            return fake_rollout if session_id == "root-1" else None

    agent = _make_codex_agent(_Agent, local_provider, tmp_path, CodexAgentConfig(), is_auto_approve=True)
    calls: list[str] = []
    agent.wait_for_ready_signal(True, lambda: calls.append("started"))

    assert calls == ["started"]
    assert "thread/start" in transport.sent_methods()
    assert "thread/inject_items" in transport.sent_methods()
    assert transport.params_of("thread/inject_items")["items"] == [{"type": "environmentContext", "text": ""}]
    # Materialize only -- never a model turn.
    assert "turn/start" not in transport.sent_methods()
    # The root id is persisted to both pointer files (same value == the rollout session id).
    assert (agent._get_agent_dir() / ROOT_SESSION_FILENAME).read_text().strip() == "root-1"
    assert (agent._get_agent_dir() / APP_SERVER_THREAD_FILENAME).read_text().strip() == "root-1"
    # mngr records the rollout path for the transcript streamer (the UserPromptSubmit hook that
    # would normally do this never fires on a programmatic turn).
    assert (agent._get_agent_dir() / TRANSCRIPT_PATH_FILENAME).read_text().strip() == str(fake_rollout)


def test_wait_for_ready_signal_skips_establish_when_root_already_persisted(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """An adopted root (``codex_root_session`` already written) is resumed as-is.

    ``on_after_provisioning`` writes the pointer for an ``--adopt`` / ``--from`` clone, so
    establishment must NOT start a second thread over it (no handshake, no ``thread/start``) -- but it
    MUST still finish the pointers adopt does not write: bind the send/status path to the adopted
    thread (``codex_app_server_thread``) and record its rollout (``codex_transcript_path``).
    """
    transport = _ScriptedTransport()
    transport.respond_result("initialize", _initialize_result())
    transport.respond_result("thread/start", {"thread": {"id": "root-new", "status": {"type": "idle"}}})
    fake_rollout = tmp_path / "sessions" / "2026" / "01" / "01" / "rollout-2026-01-01T00-00-00-adopted-9.jsonl"

    class _Agent(CodexAgent):
        def _connect_app_server_transport(self) -> AppServerTransport:
            return transport

        def _wait_for_app_server_ready(self, timeout: float | None) -> None:
            return None

        # No live tmux pane here; the hook-trust clear is a tmux concern, tested on its own below.
        def _clear_hook_trust_prompt(self) -> None:
            return None

        def _find_adopted_rollout_path(
            self, host: OnlineHostInterface, sessions_dir: Path, session_id: str
        ) -> Path | None:
            return fake_rollout if session_id == "adopted-9" else None

    agent = _make_codex_agent(_Agent, local_provider, tmp_path, CodexAgentConfig(), is_auto_approve=True)
    agent_dir = agent._get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / ROOT_SESSION_FILENAME).write_text("adopted-9")

    agent.wait_for_ready_signal(True, lambda: None)

    # Nothing sent: establishment short-circuited on the already-persisted root (no new thread).
    assert transport.sent_methods() == []
    assert (agent_dir / ROOT_SESSION_FILENAME).read_text().strip() == "adopted-9"
    # But the adopted pointers are completed so sends bind the adopted thread and the transcript fills.
    assert (agent_dir / APP_SERVER_THREAD_FILENAME).read_text().strip() == "adopted-9"
    assert (agent_dir / TRANSCRIPT_PATH_FILENAME).read_text().strip() == str(fake_rollout)


_HOOK_TRUST_SCREEN = "  Hooks need review\n  8 hooks are new or changed.\n  2. Trust all and continue\n"
_COMPOSER_SCREEN = "  OpenAI Codex (v0.147.0)\n  model: gpt-5.5   /model to change\n"


def test_clear_hook_trust_prompt_selects_trust_all_when_prompt_is_showing(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """When the --remote TUI shows "Hooks need review", mngr selects "Trust all and continue".

    Until this is cleared the daemon's hooks stay untrusted and never fire (safety guards + the
    session-pointer recorder), on typed OR programmatic turns -- so the create-time clear is what
    makes the workspace hooks actually run.
    """
    sent: list[str] = []

    class _Agent(CodexAgent):
        def capture_pane_content(
            self, include_scrollback: bool = False, window: "int | str | None" = None
        ) -> str | None:
            return _HOOK_TRUST_SCREEN

        def _send_hook_trust_keypress(self) -> None:
            sent.append("trust")

    agent = _make_codex_agent(_Agent, local_provider, tmp_path, CodexAgentConfig(), is_auto_approve=True)
    agent._clear_hook_trust_prompt()
    assert sent == ["trust"]


def test_is_on_hook_trust_prompt_detects_only_the_trust_screen(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """The detector is True only for the trust screen -- not the composer, not a blank/absent pane
    (a false positive there would send a stray "2" into the conversation)."""

    def _agent_with_pane(pane: str | None) -> CodexAgent:
        class _Agent(CodexAgent):
            def capture_pane_content(
                self, include_scrollback: bool = False, window: "int | str | None" = None
            ) -> str | None:
                return pane

        return _make_codex_agent(_Agent, local_provider, tmp_path, CodexAgentConfig(), is_auto_approve=True)

    assert _agent_with_pane(_HOOK_TRUST_SCREEN)._is_on_hook_trust_prompt() is True
    assert _agent_with_pane(_COMPOSER_SCREEN)._is_on_hook_trust_prompt() is False
    assert _agent_with_pane(None)._is_on_hook_trust_prompt() is False


def test_app_server_client_version_is_a_nonempty_string() -> None:
    assert isinstance(_app_server_client_version(), str)
    assert _app_server_client_version() != ""


# =============================================================================
# provision
# =============================================================================


def _provision(agent: CodexAgent) -> None:
    agent.provision(
        agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("codex")),
        mngr_ctx=agent.mngr_ctx,
    )


def test_provision_builds_the_codex_home_tree(codex_agent: CodexAgent, isolated_codex_home: Path) -> None:
    """provision writes config.toml (pin + trust), hooks.json, the auth symlink, and the NUX marker."""
    _provision(codex_agent)
    codex_home = codex_agent._get_codex_home()

    config_path = get_codex_config_path(codex_home)
    assert config_path.exists()
    config = tomllib.loads(config_path.read_text())
    assert config["cli_auth_credentials_store"] == "file"
    assert config["sandbox_mode"] == "workspace-write"
    # The (canonicalized) work dir is seeded as a trusted project.
    canonical_work = str(codex_agent.work_dir.resolve())
    assert is_project_trusted(config, canonical_work)

    hooks_path = get_codex_hooks_path(codex_home)
    assert hooks_path.exists()
    hooks_text = hooks_path.read_text()
    # The one mngr hook records the session/transcript pointers; no lifecycle-marker hooks.
    assert RECORD_SESSION_POINTERS_SCRIPT_NAME in hooks_text
    assert "set_active_marker.sh" not in hooks_text
    assert "clear_active_marker.sh" not in hooks_text
    assert "permissions_waiting" not in hooks_text
    # The session-pointer recorder script is provisioned into commands/.
    assert (codex_agent._get_agent_dir() / "commands" / RECORD_SESSION_POINTERS_SCRIPT_NAME).exists()

    # auth.json is a symlink to the shared user auth.json. With the shared auth
    # seeded (isolated_codex_home), the symlink resolves to that real file rather
    # than dangling.
    auth_path = get_codex_auth_path(codex_home)
    assert auth_path.is_symlink()
    shared_auth = get_codex_auth_path(isolated_codex_home / ".codex")
    assert auth_path.resolve() == shared_auth.resolve()
    assert auth_path.read_text() == shared_auth.read_text()

    # The personality-migration NUX-skip marker exists.
    assert get_codex_personality_migration_path(codex_home).exists()


def test_provision_sets_approval_never_when_auto_allow(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    agent = _make_codex_agent(
        CodexAgent,
        local_provider,
        tmp_path,
        CodexAgentConfig(auto_allow_permissions=True),
        is_auto_approve=True,
    )
    _provision(agent)
    config = tomllib.loads(get_codex_config_path(agent._get_codex_home()).read_text())
    assert config["approval_policy"] == "never"


# =============================================================================
# Trust / hook-bypass consent
# =============================================================================


def _read_user_codex_config(agent: CodexAgent) -> dict[str, object]:
    user_config = get_codex_config_path(agent._resolve_user_codex_home(agent.host))
    if not user_config.exists():
        return {}
    return tomllib.loads(user_config.read_text())


def test_auto_approve_silently_persists_durable_trust(codex_agent: CodexAgent) -> None:
    """`--yes` (is_auto_approve) records the source repo trust in the user's global config."""
    _provision(codex_agent)
    user_config = _read_user_codex_config(codex_agent)
    canonical_source = str(codex_agent.work_dir.resolve())
    assert is_project_trusted(user_config, canonical_source)


def test_already_trusted_source_is_a_noop(codex_agent: CodexAgent) -> None:
    """A second provision does not re-prompt or error (idempotent durable trust)."""
    _provision(codex_agent)
    # Second provision: source is already trusted -> the consent path is skipped.
    _provision(codex_agent)
    user_config = _read_user_codex_config(codex_agent)
    canonical_source = str(codex_agent.work_dir.resolve())
    assert is_project_trusted(user_config, canonical_source)


def test_interactive_confirm_persists_trust(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    agent = _make_codex_agent(_ConfirmingCodexAgent, local_provider, tmp_path, CodexAgentConfig(), is_interactive=True)
    _provision(agent)
    assert is_project_trusted(_read_user_codex_config(agent), str(agent.work_dir.resolve()))


def test_interactive_decline_aborts(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    agent = _make_codex_agent(_DecliningCodexAgent, local_provider, tmp_path, CodexAgentConfig(), is_interactive=True)
    with pytest.raises(SystemExit):
        _provision(agent)


def test_non_interactive_without_optin_aborts(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """Default ctx (not interactive, not auto-approve) must refuse to run on untrusted code."""
    agent = _make_codex_agent(CodexAgent, local_provider, tmp_path, CodexAgentConfig())
    with pytest.raises(SystemExit):
        _provision(agent)


def test_auto_dismiss_dialogs_persists_trust_without_optin_ctx(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """auto_dismiss_dialogs_at_startup trusts silently even when the ctx is non-interactive."""
    agent = _make_codex_agent(
        CodexAgent, local_provider, tmp_path, CodexAgentConfig(auto_dismiss_dialogs_at_startup=True)
    )
    _provision(agent)
    assert is_project_trusted(_read_user_codex_config(agent), str(agent.work_dir.resolve()))


# =============================================================================
# Update check / auto-update
# =============================================================================


class _CodexUpdateRan(Exception):
    """Raised by a test override to signal that `codex update` would have run."""


class _CodexUpdatePrompted(Exception):
    """Raised by a test override to signal that the interactive update prompt was shown."""


class _OutdatedCodexAgent(CodexAgent):
    """Reports a fixed outdated (installed, latest) and records an update via a sentinel.

    Overriding `_read_codex_versions` keeps the decision tests off any real codex
    binary or version.json; `_run_codex_update` raises so a test can assert whether
    the update path was taken.
    """

    def _read_codex_versions(self, host: object, user_codex_home: Path) -> tuple[str | None, str | None]:
        return ("0.138.0", "0.139.0")

    def _run_codex_update(self, host: object, installed: str, latest: str) -> None:
        raise _CodexUpdateRan()


class _OutdatedPromptYesAgent(_OutdatedCodexAgent):
    def _prompt_user_to_update_codex(self, installed: str, latest: str) -> bool:
        return True


class _OutdatedPromptNoAgent(_OutdatedCodexAgent):
    def _prompt_user_to_update_codex(self, installed: str, latest: str) -> bool:
        return False


class _OutdatedPromptRaisesAgent(_OutdatedCodexAgent):
    """Prompt raises, so a test can assert the prompt is NOT consulted."""

    def _prompt_user_to_update_codex(self, installed: str, latest: str) -> bool:
        raise _CodexUpdatePrompted()


class _UpToDateCodexAgent(_OutdatedCodexAgent):
    def _read_codex_versions(self, host: object, user_codex_home: Path) -> tuple[str | None, str | None]:
        return ("0.139.0", "0.139.0")


class _UnknownVersionCodexAgent(_OutdatedCodexAgent):
    def _read_codex_versions(self, host: object, user_codex_home: Path) -> tuple[str | None, str | None]:
        return (None, None)


class _OutdatedRealUpdateAgent(CodexAgent):
    """Reports a fixed outdated pair but runs the *real* ``_run_codex_update``.

    Unlike ``_OutdatedCodexAgent`` it does not stub ``_run_codex_update``, so the
    AUTO path exercises the actual ``codex update`` host call (stubbed on the host).
    """

    def _read_codex_versions(self, host: object, user_codex_home: Path) -> tuple[str | None, str | None]:
        return ("0.140.0", "0.141.0")


def _check_update(agent: CodexAgent) -> None:
    # user_codex_home is irrelevant in the decision tests (the probe is overridden).
    agent._maybe_check_for_codex_update(agent.host, agent.work_dir, agent.mngr_ctx)


def test_auto_policy_runs_codex_update_without_prompting(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """The AUTO policy applies the update directly and never consults the interactive prompt."""
    agent = _make_codex_agent(
        _OutdatedPromptRaisesAgent,
        local_provider,
        tmp_path,
        CodexAgentConfig(update_policy=CodexUpdatePolicy.AUTO),
        is_interactive=True,
    )
    with pytest.raises(_CodexUpdateRan):
        _check_update(agent)


def test_interactive_prompt_yes_runs_update(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    agent = _make_codex_agent(
        _OutdatedPromptYesAgent, local_provider, tmp_path, CodexAgentConfig(), is_interactive=True
    )
    with pytest.raises(_CodexUpdateRan):
        _check_update(agent)


def test_interactive_prompt_no_does_not_update(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """A declined prompt falls through to the non-blocking notice -- no update, no abort."""
    agent = _make_codex_agent(
        _OutdatedPromptNoAgent, local_provider, tmp_path, CodexAgentConfig(), is_interactive=True
    )
    _check_update(agent)


def test_non_interactive_only_notifies(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """Non-interactive: never prompt and never mutate the global install -- just notify."""
    agent = _make_codex_agent(_OutdatedPromptRaisesAgent, local_provider, tmp_path, CodexAgentConfig())
    _check_update(agent)


def test_unattended_remote_host_only_notifies(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """A remote (non-local) host is unattended even from an interactive tty: notify, never prompt.

    Mirrors the claude plugin's ``is_unattended = not host.is_local``: provisioning a
    remote codex agent from a local interactive terminal must not prompt to upgrade the
    remote's global install (the prompt override raises if consulted), nor run the update.
    """
    agent = _make_codex_agent(
        _OutdatedPromptRaisesAgent, local_provider, tmp_path, CodexAgentConfig(), is_interactive=True
    )
    remote_host = cast(OnlineHostInterface, FakeHost(is_local=False))
    agent._maybe_check_for_codex_update(remote_host, agent.work_dir, agent.mngr_ctx)


def test_unattended_remote_host_still_auto_updates(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """The AUTO policy is an explicit opt-in, so it upgrades even on an unattended remote host."""
    agent = _make_codex_agent(
        _OutdatedPromptRaisesAgent,
        local_provider,
        tmp_path,
        CodexAgentConfig(update_policy=CodexUpdatePolicy.AUTO),
        is_interactive=True,
    )
    remote_host = cast(OnlineHostInterface, FakeHost(is_local=False))
    with pytest.raises(_CodexUpdateRan):
        agent._maybe_check_for_codex_update(remote_host, agent.work_dir, agent.mngr_ctx)


def test_auto_approve_does_not_trigger_global_update(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """`--yes` clears blocking prerequisites but does NOT opt into a heavy global upgrade."""
    agent = _make_codex_agent(
        _OutdatedPromptRaisesAgent,
        local_provider,
        tmp_path,
        CodexAgentConfig(),
        is_interactive=True,
        is_auto_approve=True,
    )
    _check_update(agent)


def test_up_to_date_skips_update(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    agent = _make_codex_agent(
        _UpToDateCodexAgent, local_provider, tmp_path, CodexAgentConfig(update_policy=CodexUpdatePolicy.AUTO)
    )
    _check_update(agent)


def test_unknown_version_skips_update(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """An undeterminable version (codex absent / no cache) skips the check entirely."""
    agent = _make_codex_agent(
        _UnknownVersionCodexAgent, local_provider, tmp_path, CodexAgentConfig(update_policy=CodexUpdatePolicy.AUTO)
    )
    _check_update(agent)


def test_never_policy_only_notifies(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """The NEVER policy runs the (cheap) check but only notifies -- never prompts, never updates.

    Even on an attended local interactive run, the prompt override raises if consulted and
    the update sentinel fires if run, so reaching neither confirms NEVER just logs the notice.
    """
    agent = _make_codex_agent(
        _OutdatedPromptRaisesAgent,
        local_provider,
        tmp_path,
        CodexAgentConfig(update_policy=CodexUpdatePolicy.NEVER),
        is_interactive=True,
    )
    _check_update(agent)


def test_auto_policy_runs_real_codex_update_on_success(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """AUTO with an outdated codex shells out ``codex update`` over the host; a clean exit just logs."""
    agent = _make_codex_agent(
        _OutdatedRealUpdateAgent, local_provider, tmp_path, CodexAgentConfig(update_policy=CodexUpdatePolicy.AUTO)
    )
    host = _stub_host(tmp_path, command_results={"codex update": CommandResult(stdout="ok", stderr="", success=True)})
    agent._maybe_check_for_codex_update(host, agent.work_dir, agent.mngr_ctx)
    assert any("codex update" in command for command in host.executed_commands)


def test_auto_policy_real_codex_update_failure_is_not_fatal(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """A non-zero ``codex update`` is warned about but never aborts provisioning."""
    agent = _make_codex_agent(
        _OutdatedRealUpdateAgent, local_provider, tmp_path, CodexAgentConfig(update_policy=CodexUpdatePolicy.AUTO)
    )
    host = _stub_host(
        tmp_path,
        command_results={"codex update": CommandResult(stdout="", stderr="updater missing", success=False)},
    )
    agent._maybe_check_for_codex_update(host, agent.work_dir, agent.mngr_ctx)
    assert any("codex update" in command for command in host.executed_commands)


def test_read_codex_versions_parses_installed_and_cached(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """The real probe parses `codex --version` and the version.json cache over the host.

    Uses a fake `command` (`sh -c 'echo ...'` ignores the appended --version) so the
    test needs no real codex binary; the cache is a real file under the user's home.
    """
    agent = _make_codex_agent(
        CodexAgent, local_provider, tmp_path, CodexAgentConfig(command=CommandString("sh -c 'echo codex-cli 1.2.3'"))
    )
    user_home = agent._resolve_user_codex_home(agent.host)
    user_home.mkdir(parents=True, exist_ok=True)
    (user_home / "version.json").write_text(
        json.dumps({"latest_version": "1.3.0", "last_checked_at": "2026-06-09T00:00:00Z", "dismissed_version": None})
    )
    installed, latest = agent._read_codex_versions(agent.host, user_home)
    assert installed == "1.2.3"
    assert latest == "1.3.0"


def test_read_codex_versions_latest_is_none_when_cache_absent(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _make_codex_agent(
        CodexAgent, local_provider, tmp_path, CodexAgentConfig(command=CommandString("sh -c 'echo codex-cli 1.2.3'"))
    )
    installed, latest = agent._read_codex_versions(agent.host, agent._resolve_user_codex_home(agent.host))
    assert installed == "1.2.3"
    assert latest is None


def test_read_codex_versions_returns_none_when_probe_fails(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """A non-zero version probe (codex absent / shell error) yields (None, None) without raising."""
    agent = _make_codex_agent(CodexAgent, local_provider, tmp_path, CodexAgentConfig())
    host = _stub_host(
        tmp_path,
        command_results={"--version": CommandResult(stdout="", stderr="no such file", success=False)},
    )
    installed, latest = agent._read_codex_versions(host, tmp_path / ".codex")
    assert installed is None
    assert latest is None


@pytest.mark.parametrize("cache_body", ["{not valid json", "[]", '"0.139.0"', "42"])
def test_read_codex_versions_latest_is_none_for_unusable_cache(
    local_provider: LocalProviderInstance, tmp_path: Path, cache_body: str
) -> None:
    """A corrupt or non-object version.json yields latest=None without raising.

    `_parse_latest_codex_version` warning-logs a JSON decode error and otherwise
    returns None for any cache that is not a JSON object with a clean `latest_version`
    -- the update check then just skips rather than aborting provisioning. `installed`
    still parses, proving the unusable cache does not poison the whole probe.
    """
    agent = _make_codex_agent(
        CodexAgent, local_provider, tmp_path, CodexAgentConfig(command=CommandString("sh -c 'echo codex-cli 1.2.3'"))
    )
    user_home = agent._resolve_user_codex_home(agent.host)
    user_home.mkdir(parents=True, exist_ok=True)
    (user_home / "version.json").write_text(cache_body)
    installed, latest = agent._read_codex_versions(agent.host, user_home)
    assert installed == "1.2.3"
    assert latest is None


# =============================================================================
# Preservation on destroy
# =============================================================================


def test_codex_config_preserves_on_destroy_by_default() -> None:
    assert CodexAgentConfig().preserve_on_destroy is True


def _populate_codex_transcripts(agent: CodexAgent) -> None:
    """Write the raw/common transcripts and the root session-id history into the state dir."""
    agent_dir = agent._get_agent_dir()
    (agent_dir / "logs" / "codex_transcript").mkdir(parents=True, exist_ok=True)
    (agent_dir / "logs" / "codex_transcript" / "events.jsonl").write_text('{"type":"raw"}\n')
    (agent_dir / "events" / "codex" / "common_transcript").mkdir(parents=True, exist_ok=True)
    (agent_dir / "events" / "codex" / "common_transcript" / "events.jsonl").write_text('{"type":"common"}\n')
    (agent_dir / ROOT_SESSION_FILENAME).write_text("sess-codex\n")


@pytest.mark.rsync
def test_on_destroy_preserves_transcripts(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """on_destroy copies transcripts and session-id history to the mirrored preserved layout."""
    agent = _make_codex_agent(CodexAgent, local_provider, tmp_path, CodexAgentConfig(preserve_on_destroy=True))
    _populate_codex_transcripts(agent)

    agent.on_destroy(agent.host)

    dest_dir = get_local_preserved_agent_dir(agent.mngr_ctx, agent.name, agent.id)
    assert (dest_dir / "logs" / "codex_transcript" / "events.jsonl").read_text() == '{"type":"raw"}\n'
    assert (dest_dir / "events" / "codex" / "common_transcript" / "events.jsonl").read_text() == '{"type":"common"}\n'
    assert (dest_dir / ROOT_SESSION_FILENAME).read_text() == "sess-codex\n"


def test_on_destroy_skips_preservation_when_disabled(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """on_destroy preserves nothing when preserve_on_destroy is False."""
    agent = _make_codex_agent(CodexAgent, local_provider, tmp_path, CodexAgentConfig(preserve_on_destroy=False))
    _populate_codex_transcripts(agent)

    agent.on_destroy(agent.host)

    dest_dir = get_local_preserved_agent_dir(agent.mngr_ctx, agent.name, agent.id)
    assert not dest_dir.exists()


# =============================================================================
# Lifecycle promotion + waiting_reason field generator
# =============================================================================


@pytest.mark.parametrize(
    "base_state, is_blocked, expected",
    [
        # Only a RUNNING base is promoted, and only while blocked on a dialog.
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


def test_agent_field_generators_exposes_codex_waiting_reason() -> None:
    result = agent_field_generators()
    assert result is not None
    plugin_name, generators = result
    assert plugin_name == "codex"
    assert "waiting_reason" in generators
    assert callable(generators["waiting_reason"])


def test_waiting_reason_generator_reports_permissions_from_live_status(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """The field generator delegates to the live-status read: a parked approval -> PERMISSIONS."""
    transport = _status_transport(
        {"type": "active", "activeFlags": ["waitingOnApproval"]}, [{"id": "turn-1", "status": "inProgress"}]
    )
    agent = _live_codex_agent(local_provider, tmp_path, transport, AgentLifecycleState.RUNNING)
    assert _waiting_reason(agent, agent.host) == WaitingReason.PERMISSIONS


def test_waiting_reason_generator_returns_end_of_turn_for_an_idle_daemon(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """An idle live thread reads END_OF_TURN through the generator (the daemon is the sole source)."""
    transport = _status_transport({"type": "idle"}, [])
    agent = _live_codex_agent(local_provider, tmp_path, transport, AgentLifecycleState.RUNNING)
    assert _waiting_reason(agent, agent.host) == WaitingReason.END_OF_TURN


# =============================================================================
# Session adoption
# =============================================================================

# A codex session id is a UUID; the rollout filename embeds it as
# ``rollout-<timestamp>-<uuid>.jsonl`` under ``sessions/YYYY/MM/DD/``.
_SESSION_ID = "01234567-89ab-cdef-0123-456789abcdef"
_OTHER_SESSION_ID = "fedcba98-7654-3210-fedc-ba9876543210"


def _write_rollout(
    sessions_dir: Path, session_id: str, cwd: str = "/old/work/dir", *, date: str = "2026/06/16"
) -> Path:
    """Write a minimal two-record codex rollout under ``sessions_dir`` and return its path."""
    day_dir = sessions_dir / date
    day_dir.mkdir(parents=True, exist_ok=True)
    rollout = day_dir / f"rollout-2026-06-16T12-00-00-{session_id}.jsonl"
    rollout.write_text(
        json.dumps({"type": "session_meta", "payload": {"cwd": cwd, "id": session_id}})
        + "\n"
        + json.dumps({"type": "turn_context", "payload": {"cwd": cwd}})
        + "\n"
    )
    return rollout


def _ctx_with_host_dir(base_ctx: MngrContext, host_dir: Path) -> MngrContext:
    """Return ``base_ctx`` with its config's ``default_host_dir`` pointed at ``host_dir``."""
    updated_config = base_ctx.config.model_copy_update(
        to_update(base_ctx.config.field_ref().default_host_dir, host_dir),
    )
    return base_ctx.model_copy_update(to_update(base_ctx.field_ref().config, updated_config))


def test_session_id_from_rollout_path_extracts_trailing_uuid() -> None:
    rollout = Path(f"/x/sessions/2026/06/16/rollout-2026-06-16T12-00-00-{_SESSION_ID}.jsonl")
    assert _session_id_from_rollout_path(rollout) == _SESSION_ID


def test_session_id_from_rollout_path_rejects_names_without_an_id() -> None:
    with pytest.raises(UserInputError):
        _session_id_from_rollout_path(Path("/x/sessions/rollout-short.jsonl"))


def test_sessions_root_for_rollout_returns_the_sessions_ancestor() -> None:
    sessions = Path("/x/home/sessions")
    rollout = sessions / "2026/06/16" / f"rollout-ts-{_SESSION_ID}.jsonl"
    assert _sessions_root_for_rollout(rollout) == sessions


def test_sessions_root_for_rollout_falls_back_to_parent_for_a_flat_layout() -> None:
    rollout = Path("/x/flat") / f"rollout-ts-{_SESSION_ID}.jsonl"
    assert _sessions_root_for_rollout(rollout) == Path("/x/flat")


def test_user_native_codex_home_honors_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", "/custom/codex")
    assert _user_native_codex_home() == Path("/custom/codex")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert _user_native_codex_home() == Path.home() / ".codex"


# --- _resolve_adopt_session -------------------------------------------------


def test_resolve_adopt_session_by_jsonl_path(codex_agent: CodexAgent, tmp_path: Path) -> None:
    sessions = tmp_path / "store" / "sessions"
    rollout = _write_rollout(sessions, _SESSION_ID)
    session_id, source_dir = _resolve_adopt_session(str(rollout), codex_agent.mngr_ctx, tmp_path / "unused")
    assert session_id == _SESSION_ID
    assert source_dir == sessions


def test_resolve_adopt_session_rejects_a_missing_jsonl_path(codex_agent: CodexAgent, tmp_path: Path) -> None:
    with pytest.raises(UserInputError, match="not found"):
        _resolve_adopt_session(str(tmp_path / "nope.jsonl"), codex_agent.mngr_ctx, tmp_path)


def test_resolve_adopt_session_by_id_in_user_native_store(codex_agent: CodexAgent, tmp_path: Path) -> None:
    user_home = tmp_path / "user_codex"
    sessions = user_home / "sessions"
    _write_rollout(sessions, _SESSION_ID)
    # An empty host dir means no mngr agent stores compete with the user store.
    ctx = _ctx_with_host_dir(codex_agent.mngr_ctx, tmp_path / "empty_host")
    session_id, source_dir = _resolve_adopt_session(_SESSION_ID, ctx, user_home)
    assert session_id == _SESSION_ID
    assert source_dir == sessions


def test_resolve_adopt_session_by_id_in_a_live_agent_store(codex_agent: CodexAgent, tmp_path: Path) -> None:
    host_dir = tmp_path / "host"
    agent_sessions = host_dir / "agents" / "agent-1" / "plugin" / "codex" / "home" / "sessions"
    _write_rollout(agent_sessions, _SESSION_ID)
    ctx = _ctx_with_host_dir(codex_agent.mngr_ctx, host_dir)
    session_id, source_dir = _resolve_adopt_session(_SESSION_ID, ctx, tmp_path / "no_user_home")
    assert session_id == _SESSION_ID
    assert source_dir == agent_sessions


def test_resolve_adopt_session_unknown_id_raises(codex_agent: CodexAgent, tmp_path: Path) -> None:
    ctx = _ctx_with_host_dir(codex_agent.mngr_ctx, tmp_path / "empty_host")
    with pytest.raises(UserInputError, match="not found"):
        _resolve_adopt_session(_SESSION_ID, ctx, tmp_path / "empty_user_home")


def test_resolve_adopt_session_ambiguous_id_raises(codex_agent: CodexAgent, tmp_path: Path) -> None:
    user_home = tmp_path / "user_codex"
    _write_rollout(user_home / "sessions", _SESSION_ID)
    host_dir = tmp_path / "host"
    _write_rollout(host_dir / "agents" / "agent-1" / "plugin" / "codex" / "home" / "sessions", _SESSION_ID)
    ctx = _ctx_with_host_dir(codex_agent.mngr_ctx, host_dir)
    with pytest.raises(UserInputError, match="multiple session stores"):
        _resolve_adopt_session(_SESSION_ID, ctx, user_home)


def test_resolve_adopt_session_dedupes_a_coinciding_store(codex_agent: CodexAgent, tmp_path: Path) -> None:
    """The user store and a scanned agent dir pointing at the same physical dir collapse to one match."""
    user_home = tmp_path / "user_codex"
    real_sessions = user_home / "sessions"
    _write_rollout(real_sessions, _SESSION_ID)
    host_dir = tmp_path / "host"
    agent_codex_home = host_dir / "agents" / "agent-1" / "plugin" / "codex" / "home"
    agent_codex_home.mkdir(parents=True, exist_ok=True)
    # Symlink the agent's sessions/ at the user store, so both candidates resolve to one dir.
    (agent_codex_home / "sessions").symlink_to(real_sessions)
    ctx = _ctx_with_host_dir(codex_agent.mngr_ctx, host_dir)
    session_id, _source_dir = _resolve_adopt_session(_SESSION_ID, ctx, user_home)
    assert session_id == _SESSION_ID


# --- find / rewrite / rebind helpers ----------------------------------------


def test_find_latest_session_id_picks_the_newest_rollout(codex_agent: CodexAgent, tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    older = _write_rollout(sessions, _OTHER_SESSION_ID, date="2026/06/15")
    newer = _write_rollout(sessions, _SESSION_ID, date="2026/06/16")
    # Make mtimes deterministic so "newest by mtime" is unambiguous.
    os.utime(older, (1000, 1000))
    os.utime(newer, (2000, 2000))
    assert codex_agent._find_latest_session_id(codex_agent.host, sessions) == _SESSION_ID


def test_find_latest_session_id_returns_none_for_an_empty_store(codex_agent: CodexAgent, tmp_path: Path) -> None:
    empty = tmp_path / "sessions"
    empty.mkdir()
    assert codex_agent._find_latest_session_id(codex_agent.host, empty) is None


def test_find_adopted_rollout_path_locates_the_session_file(codex_agent: CodexAgent, tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    rollout = _write_rollout(sessions, _SESSION_ID)
    found = codex_agent._find_adopted_rollout_path(codex_agent.host, sessions, _SESSION_ID)
    assert found == rollout


def test_find_adopted_rollout_path_returns_none_when_absent(codex_agent: CodexAgent, tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    assert codex_agent._find_adopted_rollout_path(codex_agent.host, sessions, _SESSION_ID) is None


def test_rewrite_rollout_text_cwd_rewrites_every_cwd_record(codex_agent: CodexAgent) -> None:
    text = (
        json.dumps({"type": "session_meta", "payload": {"cwd": "/old", "id": _SESSION_ID}})
        + "\n"
        + json.dumps({"type": "response_item", "payload": {"text": "hi"}})
        + "\n"
        + json.dumps({"type": "turn_context", "payload": {"cwd": "/old"}})
        + "\n"
    )
    rewritten = codex_agent._rewrite_rollout_text_cwd(text, "/new/work", Path("/x.jsonl"))
    lines = [json.loads(line) for line in rewritten.splitlines()]
    assert lines[0]["payload"]["cwd"] == "/new/work"
    assert lines[1] == {"type": "response_item", "payload": {"text": "hi"}}
    assert lines[2]["payload"]["cwd"] == "/new/work"
    assert rewritten.endswith("\n")


def test_rewrite_rollout_text_cwd_passes_through_malformed_lines(
    codex_agent: CodexAgent, log_warnings: list[str]
) -> None:
    text = "{not json\n" + json.dumps({"type": "turn_context", "payload": {"cwd": "/old"}}) + "\n"
    rewritten = codex_agent._rewrite_rollout_text_cwd(text, "/new", Path("/x.jsonl"))
    lines = rewritten.splitlines()
    assert lines[0] == "{not json"
    assert json.loads(lines[1])["payload"]["cwd"] == "/new"
    assert any("unparseable" in m for m in log_warnings)


def test_rewrite_rollout_text_cwd_passes_through_non_object_json(codex_agent: CodexAgent) -> None:
    text = "[1, 2, 3]\n"
    assert codex_agent._rewrite_rollout_text_cwd(text, "/new", Path("/x.jsonl")) == "[1, 2, 3]\n"


def test_rewrite_rollout_text_cwd_preserves_blank_lines(codex_agent: CodexAgent) -> None:
    text = json.dumps({"type": "turn_context", "payload": {"cwd": "/old"}}) + "\n\n"
    rewritten = codex_agent._rewrite_rollout_text_cwd(text, "/new", Path("/x.jsonl"))
    lines = rewritten.splitlines()
    assert json.loads(lines[0])["payload"]["cwd"] == "/new"
    assert lines[1] == ""


def test_rebind_adopted_rollout_rewrites_cwd(codex_agent: CodexAgent, tmp_path: Path) -> None:
    sessions = tmp_path / "dest_sessions"
    _write_rollout(sessions, _SESSION_ID, cwd="/old/dir")
    codex_agent._rebind_adopted_rollout_cwd(codex_agent.host, sessions, _SESSION_ID)
    # The rollout cwd now points at this agent's (canonical) work dir.
    rollout = codex_agent._find_adopted_rollout_path(codex_agent.host, sessions, _SESSION_ID)
    assert rollout is not None
    canonical_work = codex_agent._resolve_canonical_path(codex_agent.host, codex_agent.work_dir)
    for line in rollout.read_text().splitlines():
        record = json.loads(line)
        if record["type"] in ("session_meta", "turn_context"):
            assert record["payload"]["cwd"] == canonical_work


def test_write_codex_resume_pointer_writes_root_session(codex_agent: CodexAgent) -> None:
    codex_agent._write_codex_resume_pointer(codex_agent.host, _SESSION_ID)
    assert codex_agent._get_root_session_file_path().read_text() == _SESSION_ID


def test_rebind_adopted_rollout_cwd_warns_when_rollout_is_missing(
    codex_agent: CodexAgent, tmp_path: Path, log_warnings: list[str]
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    codex_agent._rebind_adopted_rollout_cwd(codex_agent.host, sessions, _SESSION_ID)
    assert any("no rollout file" in m for m in log_warnings)


# --- adopt_session / on_after_provisioning ----------------------------------


@pytest.mark.rsync
def test_adopt_session_copies_store_and_rebinds(codex_agent: CodexAgent, tmp_path: Path) -> None:
    """--adopt by .jsonl path copies the source sessions tree in and sets the resume pointer."""
    source_sessions = tmp_path / "source" / "sessions"
    rollout = _write_rollout(source_sessions, _SESSION_ID, cwd="/old")
    options = CreateAgentOptions(agent_type=AgentTypeName("codex"), adopt_session=(str(rollout),))
    codex_agent.on_after_provisioning(codex_agent.host, options, codex_agent.mngr_ctx)
    assert codex_agent._get_root_session_file_path().read_text() == _SESSION_ID
    dest_sessions = codex_agent._get_codex_home() / "sessions"
    assert codex_agent._find_adopted_rollout_path(codex_agent.host, dest_sessions, _SESSION_ID) is not None


def test_adopt_session_noop_without_adopt_or_source(codex_agent: CodexAgent) -> None:
    options = CreateAgentOptions(agent_type=AgentTypeName("codex"))
    codex_agent.adopt_session(codex_agent.host, options, codex_agent.mngr_ctx)
    assert not codex_agent._get_root_session_file_path().exists()


@pytest.mark.rsync
def test_adopt_cloned_session_transfers_store_and_rebinds(codex_agent: CodexAgent, tmp_path: Path) -> None:
    """--from transfers the source agent's native session store, then resumes its latest rollout."""
    source_state_dir = tmp_path / "source_agent_state"
    source_sessions = source_state_dir / "plugin" / "codex" / "home" / "sessions"
    _write_rollout(source_sessions, _SESSION_ID, cwd="/old")
    source_location = HostLocation(host=codex_agent.host, path=source_state_dir)
    options = CreateAgentOptions(agent_type=AgentTypeName("codex"), source_agent_state_location=source_location)
    codex_agent.adopt_session(codex_agent.host, options, codex_agent.mngr_ctx)
    assert codex_agent._get_root_session_file_path().read_text() == _SESSION_ID


def test_adopt_cloned_session_warns_when_source_has_no_store(
    codex_agent: CodexAgent, tmp_path: Path, log_warnings: list[str]
) -> None:
    source_state_dir = tmp_path / "source_agent_state"
    source_state_dir.mkdir()
    source_location = HostLocation(host=codex_agent.host, path=source_state_dir)
    assert codex_agent._copy_cloned_codex_session(codex_agent.host, source_location) is None
    assert any("no codex session store" in m for m in log_warnings)
    assert not codex_agent._get_root_session_file_path().exists()


def test_adopt_session_from_clone_with_no_session_warns_and_starts_fresh(
    codex_agent: CodexAgent, tmp_path: Path, log_warnings: list[str]
) -> None:
    """Integration: ``--from`` a sessionless source warns and starts fresh -- it does NOT raise.

    Exercises the full public adopt path (``adopt_session`` -> the shared ``adopt_sessions``
    orchestrator -> ``copy_clone``) against a real local host, pinning that a ``--from`` clone
    whose source has no resumable session is a warning, not a hard error (unlike an explicit
    ``--adopt`` of an unknown id, which raises).
    """
    source_state_dir = tmp_path / "sessionless_source"
    source_state_dir.mkdir()
    options = CreateAgentOptions(
        agent_type=AgentTypeName("codex"),
        source_agent_state_location=HostLocation(host=codex_agent.host, path=source_state_dir),
    )
    # Must not raise: a --from workspace clone with no session is tolerated.
    codex_agent.adopt_session(codex_agent.host, options, codex_agent.mngr_ctx)
    assert any("no codex session store" in m for m in log_warnings)
    # No resume pointer -> the agent will start a fresh session on launch.
    assert not codex_agent._get_root_session_file_path().exists()


def test_adopt_cloned_session_warns_when_store_has_no_rollout(
    codex_agent: CodexAgent, tmp_path: Path, log_warnings: list[str]
) -> None:
    source_state_dir = tmp_path / "source_agent_state"
    (source_state_dir / "plugin" / "codex" / "home" / "sessions").mkdir(parents=True)
    source_location = HostLocation(host=codex_agent.host, path=source_state_dir)
    assert codex_agent._copy_cloned_codex_session(codex_agent.host, source_location) is None
    assert any("no rollout found" in m for m in log_warnings)
    assert not codex_agent._get_root_session_file_path().exists()


@pytest.mark.rsync
def test_adopt_multiple_sessions_resumes_the_last(codex_agent: CodexAgent, tmp_path: Path) -> None:
    """``--adopt A B`` copies both source trees in and resumes the last named one."""
    rollout_a = _write_rollout(tmp_path / "a" / "sessions", _SESSION_ID, cwd="/old", date="2026/06/15")
    rollout_b = _write_rollout(tmp_path / "b" / "sessions", _OTHER_SESSION_ID, cwd="/old", date="2026/06/16")
    options = CreateAgentOptions(agent_type=AgentTypeName("codex"), adopt_session=(str(rollout_a), str(rollout_b)))
    codex_agent.adopt_session(codex_agent.host, options, codex_agent.mngr_ctx)
    # The last named session is the one resumed.
    assert codex_agent._get_root_session_file_path().read_text() == _OTHER_SESSION_ID
    # Both rollouts coexist in the destination store (date-nested).
    dest_sessions = codex_agent._get_codex_home() / "sessions"
    assert codex_agent._find_adopted_rollout_path(codex_agent.host, dest_sessions, _SESSION_ID) is not None
    assert codex_agent._find_adopted_rollout_path(codex_agent.host, dest_sessions, _OTHER_SESSION_ID) is not None


@pytest.mark.rsync
def test_adopt_and_from_resumes_the_clone(codex_agent: CodexAgent, tmp_path: Path) -> None:
    """``--adopt A --from X`` copies the explicit session in but resumes the ``--from`` clone."""
    explicit_rollout = _write_rollout(tmp_path / "explicit" / "sessions", _SESSION_ID, cwd="/old")
    source_state_dir = tmp_path / "source_agent_state"
    source_sessions = source_state_dir / "plugin" / "codex" / "home" / "sessions"
    clone_rollout = _write_rollout(source_sessions, _OTHER_SESSION_ID, cwd="/old")
    # Make the explicit ``--adopt`` rollout the *newest* by mtime in the merged dest store
    # (the worst case for any destination-mtime scan): the clone must still win because its
    # id is read from the source store before transfer, not picked by ``ls -t`` over the dest.
    far_future = time.time() + 1_000_000
    os.utime(clone_rollout, (1000, 1000))
    os.utime(explicit_rollout, (far_future, far_future))
    source_location = HostLocation(host=codex_agent.host, path=source_state_dir)
    options = CreateAgentOptions(
        agent_type=AgentTypeName("codex"),
        adopt_session=(str(explicit_rollout),),
        source_agent_state_location=source_location,
    )
    codex_agent.adopt_session(codex_agent.host, options, codex_agent.mngr_ctx)
    # The clone wins the resume pointer.
    assert codex_agent._get_root_session_file_path().read_text() == _OTHER_SESSION_ID
    # The explicit session is still copied in (available in the session switcher).
    dest_sessions = codex_agent._get_codex_home() / "sessions"
    assert codex_agent._find_adopted_rollout_path(codex_agent.host, dest_sessions, _SESSION_ID) is not None


# --- on_before_create -------------------------------------------------------


def _on_before_create_args(local_provider: LocalProviderInstance, **option_kwargs: Any) -> OnBeforeCreateArgs:
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    return OnBeforeCreateArgs(
        target_host=cast(OnlineHostInterface, host),
        agent_options=CreateAgentOptions(agent_type=AgentTypeName("codex"), **option_kwargs),
        create_work_dir=True,
    )


def test_on_before_create_noop_without_adopt(local_provider: LocalProviderInstance) -> None:
    args = _on_before_create_args(local_provider)
    assert on_before_create(args, local_provider.mngr_ctx) is None


# NOTE: the "on_before_create noops for a non-codex agent type" case is intentionally not
# tested here. Post-gate, a non-codex create that reaches this hook is necessarily another
# *adoption-capable* type (the core _validate_session_adoption gate rejects non-adoption types
# before any on_before_create runs, which run_adopt_session_preflight asserts). That
# noop-on-type-mismatch is core logic, covered by test_run_adopt_session_preflight_skips_for_
# nonmatching_type in libs/mngr/.../preservation_test.py; reproducing it here would require
# registering a fake adoptable agent type in the global class registry (no per-test reset).


def test_on_before_create_resolves_a_valid_jsonl_adopt(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    rollout = _write_rollout(tmp_path / "store" / "sessions", _SESSION_ID)
    args = _on_before_create_args(local_provider, adopt_session=(str(rollout),))
    assert on_before_create(args, local_provider.mngr_ctx) is None


def test_on_before_create_fails_fast_on_a_bad_adopt_id(local_provider: LocalProviderInstance) -> None:
    args = _on_before_create_args(local_provider, adopt_session=("not-a-real-session-id",))
    with pytest.raises(UserInputError):
        on_before_create(args, local_provider.mngr_ctx)


class _StyleReadingHost:
    """Minimal host exposing only what ``read_output_style_files`` touches."""

    def __init__(self, styles_dir: Path, style_body: str) -> None:
        self._styles_dir = styles_dir
        self._style_body = style_body

    def path_exists(self, path: Path) -> bool:
        return path == self._styles_dir

    def list_directory(self, path: Path, recursive: bool = False) -> list[Any]:
        return [SimpleNamespace(path="engineering-subordinate.md", file_type=FileType.FILE)]

    def read_text_file(self, path: Path) -> str:
        return self._style_body


def test_a_style_and_every_stacked_prompt_reach_codex_developer_instructions(tmp_path: Path) -> None:
    """Codex has no output-style concept, so the style body and the prompts share one channel.

    Each stacked role's block comes first in order, then the style body verbatim (frontmatter
    included, so a style reads identically whichever harness runs it). Three sentinels, so a
    dropped or reordered piece is visible rather than inferred.
    """
    styles_dir = tmp_path / ".agents" / "output-styles"
    style_body = "---\nname: Engineering Subordinate\n---\nSENTINEL_C"
    agent = CodexAgent.model_construct(
        agent_config=CodexAgentConfig(
            output_style=OutputStyleName("Engineering Subordinate"),
            append_system_prompt=(SystemPromptText("SENTINEL_A"), SystemPromptText("SENTINEL_B")),
        ),
        work_dir=str(tmp_path),
    )
    instructions = agent._build_developer_instructions(cast(Any, _StyleReadingHost(styles_dir, style_body)))

    assert instructions is not None
    for sentinel in ("SENTINEL_A", "SENTINEL_B", "SENTINEL_C"):
        assert sentinel in instructions, f"{sentinel} missing from developer_instructions"
    # Roles in stack order, style last.
    assert instructions.index("SENTINEL_A") < instructions.index("SENTINEL_B") < instructions.index("SENTINEL_C")


def test_codex_developer_instructions_is_none_when_no_role_contributed_anything() -> None:
    agent = CodexAgent.model_construct(agent_config=CodexAgentConfig(), work_dir="/nonexistent")
    assert agent._build_developer_instructions(cast(Any, object())) is None
