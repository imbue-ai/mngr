"""Binary-contract tests: what the plugin relies on in the pinned grok (``resources/test_fixtures/grok_version.txt``).

The agent tests create a grok agent with mngr's API on the local provider, with every proxy and xAI origin in its
environment pointing at the stub: on the stub model with no credential, except the consent-notice test, which signs
in with the stub's auth-provider token. They need the pinned binary, named ``grok``, first on PATH: without it they
skip, or fail when ``MNGR_GROK_REQUIRE_PINNED_BINARY=1``.

``release-tests.yml`` runs them before every mngr release, to catch a grok version that breaks what the plugin
assumes. Run them yourself after changing the pinned version or how this package drives grok.
"""

import json
import os
import shlex
import subprocess
import time
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import Final

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveFloat
from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr.agents.agent_release_testing import RECALL_PROMPT
from imbue.mngr.api.create import create
from imbue.mngr.api.find import start_agents_locked
from imbue.mngr.api.providers import get_local_host
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentStartError
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.tmux import LONG_MESSAGE_THRESHOLD
from imbue.mngr.interfaces.host import AgentProvisioningOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.host import UploadFileSpec
from imbue.mngr.plugin_catalog import GrokSignalCheck
from imbue.mngr.plugin_catalog import check_signal
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import SystemPromptText
from imbue.mngr.primitives import TransferMode
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import capture_loguru
from imbue.mngr.utils.testing import capture_tmux_pane_contents
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import tmux_session_cleanup
from imbue.mngr.utils.testing import write_executable_script
from imbue.mngr_grok.grok_config import FORK_FROM_RELATIVE_PATH
from imbue.mngr_grok.grok_config import GROK_HOOK_EVENT_NAMES
from imbue.mngr_grok.grok_config import GROK_PLUGIN_STATE_RELATIVE_PATH
from imbue.mngr_grok.grok_config import REMOVED_GROK_ENVIRONMENT_VARIABLE_NAMES
from imbue.mngr_grok.grok_config import ROOT_TURNS_RELATIVE_PATH
from imbue.mngr_grok.grok_config import SESSION_ID_RELATIVE_PATH
from imbue.mngr_grok.grok_config import build_pinned_grok_environment
from imbue.mngr_grok.grok_config import compute_session_bucket_name
from imbue.mngr_grok.grok_config import get_grok_home
from imbue.mngr_grok.grok_screen import GrokGateScreen
from imbue.mngr_grok.grok_screen import composer_holds_text
from imbue.mngr_grok.grok_screen import detect_gate_screen
from imbue.mngr_grok.grok_screen import has_cancel_hint
from imbue.mngr_grok.grok_screen import has_press_again_hint
from imbue.mngr_grok.grok_screen import is_idle_and_empty
from imbue.mngr_grok.grok_screen import is_ready_for_paste
from imbue.mngr_grok.grok_screen import read_composer_text
from imbue.mngr_grok.grok_sessions import GROK_SESSIONS_RELATIVE_PATH
from imbue.mngr_grok.grok_sessions import SESSION_SUMMARY_FILENAME
from imbue.mngr_grok.plugin import GrokAgent
from imbue.mngr_grok.plugin import GrokAgentConfig
from imbue.mngr_grok.plugin import GrokInputBlockedError
from imbue.mngr_grok.plugin import GrokKeyRefusedError
from imbue.mngr_grok.resources import common_transcript_convert
from imbue.mngr_grok.resources.grok_hook import ACTIVE_MARKER_FILENAME
from imbue.mngr_grok.resources.grok_hook import AUTO_WAKE_TAIL_MARKER
from imbue.mngr_grok.resources.grok_hook import HOOK_EVENT_HANDLERS
from imbue.mngr_grok.resources.grok_hook import PERMISSIONS_WAITING_FILENAME
from imbue.mngr_grok.resources.grok_hook import SUBAGENT_WAKE_PROMPT_ID_PREFIX
from imbue.mngr_grok.resources.grok_hook import SUBMISSIONS_LOG_FILENAME
from imbue.mngr_grok.resources.grok_hook import TURN_DROPPED_FLAG
from imbue.mngr_grok.resources.grok_hook import WORK_LEASE_NAME_PREFIX
from imbue.mngr_grok.resources.grok_hook import normalized_prompt_tail
from imbue.mngr_grok.resources.grok_hook import snake_case_event_name
from imbue.mngr_grok.resources.testing import read_common_transcript_records
from imbue.mngr_grok.resources.testing import read_complete_jsonl_records
from imbue.mngr_grok.testing import BACKGROUND_SHELL_TRIGGER
from imbue.mngr_grok.testing import BACKGROUND_SUBAGENT_TRIGGER
from imbue.mngr_grok.testing import EMPTY_REPLY_TRIGGER
from imbue.mngr_grok.testing import FOREGROUND_SUBAGENT_TRIGGER
from imbue.mngr_grok.testing import GrokStubConsentNotice
from imbue.mngr_grok.testing import GrokStubModelServer
from imbue.mngr_grok.testing import GrokStubReplyKind
from imbue.mngr_grok.testing import GrokStubScenario
from imbue.mngr_grok.testing import GrokStubServerConfig
from imbue.mngr_grok.testing import HELD_REPLY_TRIGGER
from imbue.mngr_grok.testing import SLOW_REPLY_TRIGGER
from imbue.mngr_grok.testing import SLOW_TOOL_TRIGGER
from imbue.mngr_grok.testing import SUBAGENT_PROMPT
from imbue.mngr_grok.testing import TOOL_CALL_COMMAND
from imbue.mngr_grok.testing import TOOL_CALL_MARKER_FILENAME
from imbue.mngr_grok.testing import TOOL_CALL_MARKER_TEXT
from imbue.mngr_grok.testing import TOOL_CALL_TRIGGER
from imbue.mngr_grok.testing import build_stub_auth_provider_environment
from imbue.mngr_grok.testing import build_stub_grok_config
from imbue.mngr_grok.testing import extract_message_text
from imbue.mngr_grok.testing import find_last_user_text
from imbue.mngr_grok.testing import get_chat_messages
from imbue.mngr_grok.testing import read_pinned_grok_version
from imbue.mngr_grok.testing import running_grok_stub_model_server
from imbue.mngr_grok.testing import write_stub_auth_provider_script

_GROK_AGENT_TYPE: Final[AgentTypeName] = AgentTypeName("grok")
_READY_TIMEOUT_SECONDS: Final[float] = 90.0
_TURN_TIMEOUT_SECONDS: Final[float] = 60.0
# Comfortably above grok's default delay of about a minute before its idle_prompt notification.
_UNREPORTED_TURN_END_TIMEOUT_SECONDS: Final[float] = 150.0
# Well under grok's idle_prompt delay: mngr's own Ctrl+C ends a dropped turn without waiting for that notification.
_SETTLED_CANCEL_TIMEOUT_SECONDS: Final[float] = 10.0
# Under GrokAgent's cancel settle deadline: mngr clears and ends a dropped turn about 2 seconds after its Ctrl+C.
_DROPPED_TURN_SETTLED_BOUND_SECONDS: Final[float] = 3.5
_PROCESS_TIMEOUT_SECONDS: Final[float] = 30.0
_HOOK_EXECUTION_KIND: Final[str] = "hook_execution"
_MNGR_HOOK_RUN_PREFIX: Final[str] = "global/mngr:"
_HOOK_LOG_RELATIVE_PATH: Final[Path] = Path("logs", "grok_hooks.log")
_LAUNCH_LOG_RELATIVE_PATH: Final[Path] = Path("events", "logs", "grok_launch", "events.jsonl")
_FOOTER_HINT_SEPARATOR: Final[str] = "  │"
# grok's git refresh of a GitHub-sourced Claude plugin marketplace, which it attempts even under mngr's isolation.
_GITHUB_MARKETPLACE_PROXY_TARGET: Final[str] = "github.com:443"
_CLAUDE_PROBE_HOOK_EVENT_NAMES: Final[tuple[str, ...]] = ("UserPromptSubmit", "Stop")
_CURSOR_PROBE_HOOK_EVENT_NAMES: Final[tuple[str, ...]] = ("beforeSubmitPrompt", "stop")
_CLAUDE_SETTINGS_HOOK_PROBE_NAME: Final[str] = "claude_settings_hook.sh"
_CLAUDE_PLUGIN_HOOK_PROBE_NAME: Final[str] = "claude_plugin_hook.sh"
_CURSOR_HOOK_PROBE_NAME: Final[str] = "cursor_hook.sh"
_ISOLATION_PROBE_MARKER_NAMES: Final[tuple[str, ...]] = (
    "plugin_skill",
    "plugin_agent",
    "claude_skill",
    "claude_agent",
    "legacy_grok_agent",
    "control_skill",
)
# Runs grok with its Claude Code and Cursor hook import on, past the launch environment that turns it off.
_FOREIGN_HOOKS_ENABLED_GROK_COMMAND: Final[CommandString] = CommandString(
    "env GROK_CLAUDE_HOOKS_ENABLED=1 GROK_CURSOR_HOOKS_ENABLED=1 grok"
)


def _build_hermetic_stub_grok_config(
    stub: GrokStubModelServer, build_hermetic_environment: Callable[[str], dict[str, str]]
) -> GrokAgentConfig:
    return build_stub_grok_config(stub, build_hermetic_environment(stub.get_origin_url()))


def _with_grok_config(mngr_ctx: MngrContext, grok_config: GrokAgentConfig) -> MngrContext:
    agent_types = {**mngr_ctx.config.agent_types, _GROK_AGENT_TYPE: grok_config}
    config = mngr_ctx.config.model_copy_update(to_update(mngr_ctx.config.field_ref().agent_types, agent_types))
    return mngr_ctx.model_copy_update(to_update(mngr_ctx.field_ref().config, config))


@contextmanager
def _running_grok_agent(
    mngr_ctx: MngrContext,
    grok_config: GrokAgentConfig,
    work_dir: Path,
    *,
    agent_id: AgentId | None = None,
    upload_files: Sequence[UploadFileSpec] = (),
    adopt_session: Sequence[str] = (),
) -> Generator[GrokAgent, None, None]:
    """A local grok agent, created and past its startup check; its tmux session is killed when the block exits."""
    agent_name = AgentName(f"grok-contract-{get_short_random_string()}")
    work_dir.mkdir(parents=True, exist_ok=True)
    grok_mngr_ctx = _with_grok_config(mngr_ctx, grok_config)
    local_host = get_local_host(grok_mngr_ctx)
    with tmux_session_cleanup(f"{mngr_ctx.config.prefix}{agent_name}"):
        result = create(
            source_location=HostLocation(host=local_host, path=work_dir),
            target_host=local_host,
            agent_options=CreateAgentOptions(
                agent_id=agent_id,
                name=agent_name,
                agent_type=_GROK_AGENT_TYPE,
                transfer_mode=TransferMode.NONE,
                ready_timeout_seconds=_READY_TIMEOUT_SECONDS,
                provisioning=AgentProvisioningOptions(upload_files=tuple(upload_files)),
                adopt_session=tuple(adopt_session),
            ),
            mngr_ctx=grok_mngr_ctx,
        )
        agent = result.agent
        assert isinstance(agent, GrokAgent)
        yield agent


def _state_dir(agent: GrokAgent) -> Path:
    return get_agent_state_dir_path(agent.host.host_dir, agent.id)


def _agent_session_id(agent: GrokAgent) -> str:
    return (_state_dir(agent) / SESSION_ID_RELATIVE_PATH).read_text().strip()


def _session_bucket_dir(agent: GrokAgent) -> Path:
    canonical_work_dir = os.path.realpath(agent.work_dir)
    return _state_dir(agent) / GROK_SESSIONS_RELATIVE_PATH / compute_session_bucket_name(canonical_work_dir)


def _list_session_dirs(sessions_dir: Path) -> list[Path]:
    """Every `<bucket>/<session id>` directory under a grok home's sessions dir."""
    return sorted(path.relative_to(sessions_dir) for path in sessions_dir.glob("*/*") if path.is_dir())


def _read_updates(updates_path: Path) -> list[dict[str, Any]]:
    """The `params.update` objects of a session's updates.jsonl, in file order."""
    updates = (record.get("params", {}).get("update") for record in read_complete_jsonl_records(updates_path))
    return [update for update in updates if isinstance(update, dict)]


def _read_all_session_updates(agent: GrokAgent) -> list[dict[str, Any]]:
    """Every update of every session (root and child) under the agent's grok home."""
    sessions_dir = _state_dir(agent) / GROK_SESSIONS_RELATIVE_PATH
    return [update for path in sorted(sessions_dir.rglob("updates.jsonl")) for update in _read_updates(path)]


def _read_submissions(agent: GrokAgent) -> list[tuple[str, str]]:
    """The (promptId, tail) pairs mngr's handler recorded in submissions.log, in order."""
    submissions_path = _state_dir(agent) / GROK_PLUGIN_STATE_RELATIVE_PATH / SUBMISSIONS_LOG_FILENAME
    if not submissions_path.is_file():
        return []
    return [
        (prompt_id, line_tail)
        for prompt_id, _, line_tail in (line.partition(" ") for line in submissions_path.read_text().splitlines())
    ]


def _find_submitted_prompt_id(agent: GrokAgent, message: str) -> str:
    """The promptId that mngr's handler recorded for `message` in submissions.log."""
    tail = normalized_prompt_tail(message)
    matching_prompt_ids = [prompt_id for prompt_id, line_tail in _read_submissions(agent) if line_tail == tail]
    assert len(matching_prompt_ids) == 1, f"expected one submission of {message!r}, found {matching_prompt_ids}"
    return matching_prompt_ids[0]


def _select_turn_completions(updates: Sequence[Mapping[str, Any]], prompt_id: str) -> list[Mapping[str, Any]]:
    """The `turn_completed` updates of the turn `prompt_id`."""
    return [
        update
        for update in updates
        if update.get("sessionUpdate") == "turn_completed" and update.get("prompt_id") == prompt_id
    ]


def _is_turn_completed(agent: GrokAgent, prompt_id: str) -> bool:
    return len(_select_turn_completions(_read_all_session_updates(agent), prompt_id)) > 0


def _is_active(agent: GrokAgent) -> bool:
    return (_state_dir(agent) / ACTIVE_MARKER_FILENAME).exists()


def _is_busy_composer_shown(pane: str) -> bool:
    return is_ready_for_paste(pane) and has_cancel_hint(pane)


def _is_completed_tool_call_recorded(agent: GrokAgent) -> bool:
    return any(
        update.get("sessionUpdate") == "tool_call_update" and update.get("status") == "completed"
        for update in _read_all_session_updates(agent)
    )


def _wait_for_idle_after_turn(agent: GrokAgent, prompt_id: str) -> None:
    wait_for(
        lambda: _is_turn_completed(agent, prompt_id) and not _is_active(agent),
        timeout=_TURN_TIMEOUT_SECONDS,
        poll_interval=0.2,
        error_message=f"grok's turn {prompt_id} did not complete and clear the active marker",
    )


def _send_and_wait_for_turn(agent: GrokAgent, message: str) -> str:
    """Send `message`, wait for its turn to end and the active marker to clear, and return its promptId."""
    agent.send_message(message)
    prompt_id = _find_submitted_prompt_id(agent, message)
    _wait_for_idle_after_turn(agent, prompt_id)
    return prompt_id


def _wait_for_completed_wake_turn(agent: GrokAgent, prompt_id_prefix: str) -> dict[str, Any]:
    """Wait until an auto-wake turn whose promptId starts with `prompt_id_prefix` completed and the agent is idle."""
    wake_completions: list[dict[str, Any]] = []

    def is_wake_turn_completed() -> bool:
        wake_completions[:] = [
            update
            for update in _read_all_session_updates(agent)
            if update.get("sessionUpdate") == "turn_completed"
            and str(update.get("prompt_id", "")).startswith(prompt_id_prefix)
        ]
        return len(wake_completions) > 0 and not _is_active(agent)

    wait_for(
        is_wake_turn_completed,
        timeout=_TURN_TIMEOUT_SECONDS,
        error_message=f"no {prompt_id_prefix}<id> wake turn completed",
    )
    return wake_completions[0]


def _turn_end_log_fragment(session_id: str, prompt_id: str) -> str:
    """The part of the hook log line the handler writes when the streamer's turn_completed ends that turn."""
    return f"event=--turn-completed session={session_id} prompt={prompt_id} turn:end"


def _turn_dropped_log_fragment(session_id: str, prompt_id: str) -> str:
    """The part of the hook log line the handler writes when mngr ends a turn grok dropped at mngr's Ctrl+C."""
    return f"event={TURN_DROPPED_FLAG} session={session_id} prompt={prompt_id} turn:end"


def _hook_outcomes_of_turn(hook_log: str, session_id: str, prompt_id: str) -> list[str]:
    """What the handler logged for each hook run of one turn, in order, e.g. `turn:start active:on`."""
    turn_fields = f" session={session_id} prompt={prompt_id} "
    return [line.split(turn_fields, 1)[1] for line in hook_log.splitlines() if turn_fields in line]


def _wait_for_hook_log_containing(agent: GrokAgent, fragment: str) -> str:
    """The agent's grok_hooks.log once it contains `fragment`; the handler logs a run after updating the markers."""
    hook_log_path = _state_dir(agent) / _HOOK_LOG_RELATIVE_PATH
    wait_for(
        lambda: hook_log_path.is_file() and fragment in hook_log_path.read_text(),
        timeout=_TURN_TIMEOUT_SECONDS,
        error_message=f"grok's hook handler never logged {fragment!r}",
    )
    return hook_log_path.read_text()


def _restart_agent(agent: GrokAgent, local_host: OnlineHostInterface) -> None:
    """Stop the agent, then start it and wait for grok's startup check."""
    local_host.stop_agents([agent.id])
    agent.wait_for_ready_signal(
        is_readiness_awaited=True,
        start_action=lambda: start_agents_locked(local_host, [agent.id], is_restart=False),
        timeout=_READY_TIMEOUT_SECONDS,
    )


def _launch_log_messages(agent: GrokAgent) -> list[str]:
    return [
        str(event.get("message", ""))
        for event in read_complete_jsonl_records(_state_dir(agent) / _LAUNCH_LOG_RELATIVE_PATH)
    ]


def _hook_execution_runs(updates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"event_name": update.get("event_name"), **run}
        for update in updates
        if update.get("sessionUpdate") == _HOOK_EXECUTION_KIND
        for run in update.get("runs", [])
    ]


def _build_hooks_file(command: str, event_names: Sequence[str]) -> dict[str, Any]:
    return {
        "hooks": {event_name: [{"hooks": [{"type": "command", "command": command}]}] for event_name in event_names}
    }


def _upload_extra_hooks_file(
    tmp_path: Path, mngr_ctx: MngrContext, agent_id: AgentId, file_stem: str, hooks_file: Mapping[str, Any]
) -> UploadFileSpec:
    """Place `<file_stem>.json` beside mngr's hooks file in the agent's grok home, before grok first starts."""
    local_path = tmp_path / f"{file_stem}_hooks.json"
    local_path.write_text(json.dumps(hooks_file, indent=2) + "\n")
    agent_hooks_dir = get_grok_home(get_agent_state_dir_path(mngr_ctx.config.default_host_dir, agent_id)) / "hooks"
    return UploadFileSpec(local_path=local_path, remote_path=agent_hooks_dir / f"{file_stem}.json")


def _upload_payload_probe_hooks(
    tmp_path: Path, mngr_ctx: MngrContext, agent_id: AgentId, event_names: Sequence[str]
) -> tuple[UploadFileSpec, Path]:
    """A `probe` hooks file whose argument-free hook logs each event's payload and parts of its environment.

    Returns the hooks file's upload and the log's path.
    """
    log_path = tmp_path / "probe_hook_payloads.jsonl"
    probe_path = tmp_path / "probe_hook.py"
    write_executable_script(
        probe_path,
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "record = {\n"
        '    "payload": json.loads(sys.stdin.read() or "{}"),\n'
        '    "grok_agent": os.environ.get("GROK_AGENT"),\n'
        '    "grok_hook_event": os.environ.get("GROK_HOOK_EVENT"),\n'
        '    "agent_state_dir": os.environ.get("MNGR_AGENT_STATE_DIR"),\n'
        "}\n"
        f"with open({str(log_path)!r}, 'a') as log_file:\n"
        "    log_file.write(json.dumps(record) + '\\n')\n",
    )
    probe_hooks = _upload_extra_hooks_file(
        tmp_path, mngr_ctx, agent_id, "probe", _build_hooks_file(str(probe_path), event_names)
    )
    return probe_hooks, log_path


def _read_probe_payloads(log_path: Path, event_name: str) -> list[dict[str, Any]]:
    return [
        record["payload"]
        for record in read_complete_jsonl_records(log_path)
        if record["payload"].get("hookEventName") == event_name
    ]


def _footer_first_hint(pane: str) -> str:
    non_blank_lines = [line for line in pane.split("\n") if line.strip()]
    return non_blank_lines[-1].strip().split(_FOOTER_HINT_SEPARATOR, 1)[0].strip() if non_blank_lines else ""


def _send_tmux_keys(agent: GrokAgent, *key_args: str) -> None:
    target = f"={agent.tmux_target.session_name}:{agent.tmux_target.window}"
    subprocess.run(["tmux", "send-keys", "-t", target, *key_args], check=True, timeout=_PROCESS_TIMEOUT_SECONDS)


def _type_into_pane(agent: GrokAgent, text: str) -> None:
    _send_tmux_keys(agent, "-l", text)


def _press_enter(agent: GrokAgent) -> None:
    _send_tmux_keys(agent, "Enter")


def _wait_for_footer_first_hint(agent: GrokAgent, expected_hint: str) -> str:
    """Wait until the footer's first hint reads `expected_hint`, and return that pane."""
    panes: list[str] = []

    def is_expected_hint_shown() -> bool:
        panes.append(capture_tmux_pane_contents(agent.tmux_target))
        return _footer_first_hint(panes[-1]) == expected_hint

    is_shown = poll_until(is_expected_hint_shown, timeout=15.0, poll_interval=0.2)
    assert is_shown, f"the footer never started with {expected_hint!r}; last pane:\n{panes[-1] if panes else ''}"
    return panes[-1]


def _stub_requests_with_reply_kind(stub: GrokStubModelServer, kind: GrokStubReplyKind) -> list[Mapping[str, Any]]:
    return [
        record.body
        for record in stub.get_request_records()
        if record.chat_reply is not None and record.chat_reply.kind == kind and isinstance(record.body, Mapping)
    ]


def _system_prompt_text(request_body: Mapping[str, Any]) -> str:
    return "\n".join(
        extract_message_text(message) for message in get_chat_messages(request_body) if message.get("role") == "system"
    )


@pytest.mark.release
@pytest.mark.harness_internals(harness="grok")
@pytest.mark.usefixtures("pinned_grok_binary")
def test_pinned_grok_passes_the_catalog_signal() -> None:
    assert check_signal(GrokSignalCheck())


@pytest.mark.release
@pytest.mark.harness_internals(harness="grok")
@pytest.mark.tmux
def test_hooks_run_from_the_relative_path_and_carry_the_fields_the_handler_reads(
    pinned_grok_binary: Path,
    grok_stub_server: GrokStubModelServer,
    build_hermetic_grok_agent_environment: Callable[[str], dict[str, str]],
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    agent_id = AgentId.generate()
    probe_hooks, probe_log_path = _upload_payload_probe_hooks(tmp_path, temp_mngr_ctx, agent_id, GROK_HOOK_EVENT_NAMES)
    environment = {
        **build_hermetic_grok_agent_environment(grok_stub_server.get_origin_url()),
        "GROK_IDLE_NOTIFICATION_DELAY_MS": "1500",
    }
    plain_message = f"hello hook contract {get_short_random_string()}"
    tool_message = f"{TOOL_CALL_TRIGGER} please run the tool {get_short_random_string()}"
    subagent_message = f"{FOREGROUND_SUBAGENT_TRIGGER} please delegate {get_short_random_string()}"

    with _running_grok_agent(
        temp_mngr_ctx,
        build_stub_grok_config(grok_stub_server, environment),
        tmp_path / "project",
        agent_id=agent_id,
        upload_files=(probe_hooks,),
    ) as agent:
        plain_prompt_id = _send_and_wait_for_turn(agent, plain_message)
        _send_and_wait_for_turn(agent, tool_message)
        _send_and_wait_for_turn(agent, subagent_message)
        wait_for(
            lambda: len(_read_probe_payloads(probe_log_path, "notification")) > 0,
            timeout=_TURN_TIMEOUT_SECONDS,
            error_message="grok sent no idle_prompt notification",
        )
        root_session_id = _agent_session_id(agent)
        updates = _read_all_session_updates(agent)
        hook_log = (_state_dir(agent) / _HOOK_LOG_RELATIVE_PATH).read_text()

    # The relative `./grok_hook.py` resolved against the hooks dir and ran: every mngr run succeeded.
    mngr_runs = [run for run in _hook_execution_runs(updates) if str(run["name"]).startswith(_MNGR_HOOK_RUN_PREFIX)]
    assert {run["event_name"] for run in mngr_runs} >= {"session_start", "user_prompt_submit", "post_tool_use", "stop"}
    assert all(run["status"]["status"] == "success" for run in mngr_runs), mngr_runs
    assert f"event=user_prompt_submit session={root_session_id} prompt={plain_prompt_id} turn:start" in hook_log

    # Every event name grok sends maps from a registered name to one of the handler's events, and matches the env.
    probe_records = read_complete_jsonl_records(probe_log_path)
    registered_snake_case_names = {snake_case_event_name(name) for name in GROK_HOOK_EVENT_NAMES}
    observed_event_names = {record["payload"]["hookEventName"] for record in probe_records}
    assert observed_event_names <= registered_snake_case_names
    assert observed_event_names <= set(HOOK_EVENT_HANDLERS)
    assert all(record["grok_hook_event"] == record["payload"]["hookEventName"] for record in probe_records)
    assert observed_event_names >= {
        "session_start",
        "user_prompt_submit",
        "post_tool_use",
        "stop",
        "subagent_start",
        "session_end",
        "notification",
    }

    session_starts = _read_probe_payloads(probe_log_path, "session_start")
    assert [payload["sessionId"] for payload in session_starts] == [root_session_id]

    prompt_submits = _read_probe_payloads(probe_log_path, "user_prompt_submit")
    root_prompt_submits = [payload for payload in prompt_submits if "subagentType" not in payload]
    assert [payload["prompt"] for payload in root_prompt_submits] == [plain_message, tool_message, subagent_message]
    assert all(payload["sessionId"] == root_session_id for payload in root_prompt_submits)
    assert root_prompt_submits[0]["promptId"] == plain_prompt_id

    end_turn_stops = [
        payload for payload in _read_probe_payloads(probe_log_path, "stop") if payload.get("reason") == "end_turn"
    ]
    assert [payload["promptId"] for payload in end_turn_stops] == [p["promptId"] for p in root_prompt_submits]
    assert all(isinstance(payload["backgroundTasks"], list) for payload in end_turn_stops)

    subagent_starts = _read_probe_payloads(probe_log_path, "subagent_start")
    assert [payload["sessionId"] for payload in subagent_starts] == [root_session_id]
    child_session_id = subagent_starts[0]["subagentId"]
    child_session_ends = [
        payload for payload in _read_probe_payloads(probe_log_path, "session_end") if payload.get("subagentType")
    ]
    assert [payload["sessionId"] for payload in child_session_ends] == [child_session_id]
    child_prompt_submits = [payload for payload in prompt_submits if payload.get("subagentType")]
    assert [payload["sessionId"] for payload in child_prompt_submits] == [child_session_id]

    notifications = _read_probe_payloads(probe_log_path, "notification")
    assert {payload["notificationType"] for payload in notifications} == {"idle_prompt"}
    assert all(payload["sessionId"] == root_session_id for payload in notifications)


@pytest.mark.release
@pytest.mark.harness_internals(harness="grok")
@pytest.mark.tmux
def test_grok_agent_is_unset_for_hooks_and_set_for_tools(
    pinned_grok_binary: Path,
    grok_stub_server: GrokStubModelServer,
    build_hermetic_grok_agent_environment: Callable[[str], dict[str, str]],
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    agent_id = AgentId.generate()
    probe_hooks, probe_log_path = _upload_payload_probe_hooks(tmp_path, temp_mngr_ctx, agent_id, GROK_HOOK_EVENT_NAMES)
    # Grok runs a tool command with `$SHELL -c`, which sources BASH_ENV, then exports GROK_AGENT and runs the
    # command, so a shell function wrapping the stub tool's leading `echo` sees the tool's final environment.
    assert TOOL_CALL_COMMAND.startswith(f"echo {TOOL_CALL_MARKER_TEXT} ")
    tool_environment_path = tmp_path / "tool_environment.txt"
    bash_env_path = tmp_path / "bash_env_probe.sh"
    bash_env_path.write_text(
        "echo() {\n"
        f'    if [ "${{1:-}}" = {shlex.quote(TOOL_CALL_MARKER_TEXT)} ]; then env > {shlex.quote(str(tool_environment_path))}; fi\n'
        '    builtin echo "$@"\n'
        "}\n"
    )
    environment = {
        **build_hermetic_grok_agent_environment(grok_stub_server.get_origin_url()),
        "SHELL": "/bin/bash",
        "BASH_ENV": str(bash_env_path),
    }

    with _running_grok_agent(
        temp_mngr_ctx,
        build_stub_grok_config(grok_stub_server, environment),
        tmp_path / "project",
        agent_id=agent_id,
        upload_files=(probe_hooks,),
    ) as agent:
        _send_and_wait_for_turn(agent, f"{TOOL_CALL_TRIGGER} env check {get_short_random_string()}")
        state_dir = _state_dir(agent)

    hook_records = read_complete_jsonl_records(probe_log_path)
    assert {record["payload"]["hookEventName"] for record in hook_records} >= {
        "session_start",
        "user_prompt_submit",
        "post_tool_use",
        "stop",
    }
    assert all(record["grok_agent"] is None for record in hook_records), hook_records
    assert all(record["agent_state_dir"] == str(state_dir) for record in hook_records)

    assert (agent.work_dir / TOOL_CALL_MARKER_FILENAME).read_text().strip() == TOOL_CALL_MARKER_TEXT
    tool_environment_lines = tool_environment_path.read_text().splitlines()
    assert "GROK_AGENT=1" in tool_environment_lines
    assert f"MNGR_AGENT_STATE_DIR={state_dir}" in tool_environment_lines


def _write_skill(skills_dir: Path, name: str, marker: str) -> None:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {marker} marker skill\n---\n\nSay {marker}.\n"
    )


def _write_agent_definition(path: Path, name: str, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {marker} marker subagent\n---\n\nYou are a probe.\n")


def _write_hook_probe_script(path: Path, log_path: Path) -> str:
    """An executable hook that logs its own name when it runs; returns the command that runs it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_executable_script(path, f"#!/bin/sh\necho {shlex.quote(path.name)} >> {shlex.quote(str(log_path))}\n")
    return str(path)


def _build_isolation_probe_markers(run_id: str) -> dict[str, str]:
    return {name: f"PROBE{name.upper().replace('_', '')}{run_id}" for name in _ISOLATION_PROBE_MARKER_NAMES}


def _write_claude_configuration(home: Path, hook_log_path: Path, markers: Mapping[str, str]) -> None:
    """A user's Claude Code and Cursor setup: settings hooks, an enabled plugin with hooks, skills and agents."""
    claude_dir = home / ".claude"
    probe_bin = home / ".probe_bin"
    settings_hook = _write_hook_probe_script(probe_bin / _CLAUDE_SETTINGS_HOOK_PROBE_NAME, hook_log_path)
    (claude_dir / "settings.json").parent.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(
        json.dumps(
            {
                **_build_hooks_file(settings_hook, _CLAUDE_PROBE_HOOK_EVENT_NAMES),
                "enabledPlugins": {"probe-plugin@probe-mkt": True},
            }
        )
    )
    plugin_root = claude_dir / "plugins" / "cache" / "probe-mkt" / "probe-plugin" / "1.0.0"
    marketplace_root = claude_dir / "plugins" / "marketplaces" / "probe-mkt"
    (claude_dir / "plugins" / "known_marketplaces.json").parent.mkdir(parents=True, exist_ok=True)
    (claude_dir / "plugins" / "known_marketplaces.json").write_text(
        json.dumps(
            {
                "probe-mkt": {
                    "source": {"source": "github", "repo": "example/probe-mkt"},
                    "installLocation": str(marketplace_root),
                    "lastUpdated": "2026-10-01T00:00:00.000Z",
                }
            }
        )
    )
    (claude_dir / "plugins" / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "probe-plugin@probe-mkt": [
                        {
                            "scope": "user",
                            "installPath": str(plugin_root),
                            "version": "1.0.0",
                            "installedAt": "2026-10-01T00:00:00.000Z",
                            "lastUpdated": "2026-10-01T00:00:00.000Z",
                        }
                    ]
                },
            }
        )
    )
    (marketplace_root / ".claude-plugin").mkdir(parents=True)
    (marketplace_root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "probe-mkt",
                "owner": {"name": "probe"},
                "plugins": [{"name": "probe-plugin", "source": "./plugins/probe-plugin", "description": "probe"}],
            }
        )
    )
    for root in (plugin_root, marketplace_root / "plugins" / "probe-plugin"):
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "probe-plugin", "version": "1.0.0"}))
        plugin_hook = _write_hook_probe_script(root / "hooks" / _CLAUDE_PLUGIN_HOOK_PROBE_NAME, hook_log_path)
        (root / "hooks" / "hooks.json").write_text(
            json.dumps(_build_hooks_file(plugin_hook, _CLAUDE_PROBE_HOOK_EVENT_NAMES))
        )
        _write_skill(root / "skills", "probe-plugin-skill", markers["plugin_skill"])
        _write_agent_definition(
            root / "agents" / "probe-plugin-agent.md", "probe-plugin-agent", markers["plugin_agent"]
        )
    _write_skill(claude_dir / "skills", "probe-claude-skill", markers["claude_skill"])
    _write_agent_definition(
        claude_dir / "agents" / "probe-claude-stem.md", "probe-claude-agent-name", markers["claude_agent"]
    )
    _write_agent_definition(
        home / ".grok" / "agents" / "probe-legacy-stem.md", "probe-legacy-agent-name", markers["legacy_grok_agent"]
    )
    cursor_hook = _write_hook_probe_script(probe_bin / _CURSOR_HOOK_PROBE_NAME, hook_log_path)
    (home / ".cursor").mkdir(parents=True, exist_ok=True)
    # Grok imports Claude-shaped entries from Cursor's hooks.json and ignores Cursor's own `{"command": ...}` entries.
    (home / ".cursor" / "hooks.json").write_text(
        json.dumps({"version": 1, **_build_hooks_file(cursor_hook, _CURSOR_PROBE_HOOK_EVENT_NAMES)})
    )
    _write_skill(home / ".agents" / "skills", "probe-agents-skill", markers["control_skill"])


@pytest.mark.release
@pytest.mark.harness_internals(harness="grok")
@pytest.mark.tmux
@pytest.mark.parametrize(
    ("is_isolated_from_claude_config", "expected_marker_names"),
    [
        pytest.param(True, {"control_skill"}, id="isolated"),
        pytest.param(
            False,
            {"control_skill", "claude_skill", "claude_agent", "legacy_grok_agent"},
            id="not_isolated",
        ),
    ],
)
def test_claude_isolation_runs_no_claude_or_cursor_hook_and_loads_only_the_definitions_it_keeps(
    pinned_grok_binary: Path,
    build_hermetic_grok_agent_environment: Callable[[str], dict[str, str]],
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
    is_isolated_from_claude_config: bool,
    expected_marker_names: set[str],
) -> None:
    run_id = get_short_random_string().upper()
    markers = _build_isolation_probe_markers(run_id)
    hook_log_path = tmp_path / "foreign_hooks_ran.log"
    _write_claude_configuration(Path.home(), hook_log_path, markers)
    stub_config = GrokStubServerConfig(tolerated_proxy_targets=frozenset({_GITHUB_MARKETPLACE_PROXY_TARGET}))

    with running_grok_stub_model_server(stub_config) as stub:
        grok_config = _build_hermetic_stub_grok_config(stub, build_hermetic_grok_agent_environment)
        grok_config = grok_config.model_copy_update(
            to_update(grok_config.field_ref().isolate_from_claude_config, is_isolated_from_claude_config)
        )
        with _running_grok_agent(temp_mngr_ctx, grok_config, tmp_path / "project") as agent:
            _send_and_wait_for_turn(agent, f"{TOOL_CALL_TRIGGER} isolation check {run_id}")
            updates = _read_all_session_updates(agent)
            chat_request_bodies = [
                record.body for record in stub.get_request_records() if record.chat_reply is not None
            ]

    assert not hook_log_path.exists(), hook_log_path.read_text()
    run_names = [str(run["name"]) for run in _hook_execution_runs(updates)]
    assert any(name.startswith(f"{_MNGR_HOOK_RUN_PREFIX}user_prompt_submit") for name in run_names), run_names
    assert all(name.startswith(_MNGR_HOOK_RUN_PREFIX) for name in run_names), run_names

    request_bodies = json.dumps(chat_request_bodies)
    assert {name for name, marker in markers.items() if marker in request_bodies} == expected_marker_names


@pytest.mark.release
@pytest.mark.harness_internals(harness="grok")
@pytest.mark.tmux
def test_claude_and_cursor_hook_probes_run_once_grok_imports_their_hooks(
    pinned_grok_binary: Path,
    build_hermetic_grok_agent_environment: Callable[[str], dict[str, str]],
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """The isolation test's user configuration runs every hook probe once grok's hook import and the plugin are on."""
    run_id = get_short_random_string().upper()
    hook_log_path = tmp_path / "foreign_hooks_ran.log"
    _write_claude_configuration(Path.home(), hook_log_path, _build_isolation_probe_markers(run_id))
    stub_config = GrokStubServerConfig(tolerated_proxy_targets=frozenset({_GITHUB_MARKETPLACE_PROXY_TARGET}))
    narrowing_config = temp_mngr_ctx.config.model_copy_update(
        to_update(temp_mngr_ctx.config.field_ref().allow_settings_key_assignment_narrowing, True)
    )
    narrowing_mngr_ctx = temp_mngr_ctx.model_copy_update(to_update(temp_mngr_ctx.field_ref().config, narrowing_config))

    with running_grok_stub_model_server(stub_config) as stub:
        grok_config = _build_hermetic_stub_grok_config(stub, build_hermetic_grok_agent_environment)
        grok_config = grok_config.model_copy_update(
            to_update(grok_config.field_ref().isolate_from_claude_config, False),
            to_update(grok_config.field_ref().command, _FOREIGN_HOOKS_ENABLED_GROK_COMMAND),
            to_update(
                grok_config.field_ref().settings_overrides,
                {**grok_config.settings_overrides, "plugins": {"disabled": []}},
            ),
        )
        with _running_grok_agent(narrowing_mngr_ctx, grok_config, tmp_path / "project") as agent:
            _send_and_wait_for_turn(agent, f"{TOOL_CALL_TRIGGER} hook control {run_id}")
            updates = _read_all_session_updates(agent)

    assert set(hook_log_path.read_text().split()) == {
        _CLAUDE_SETTINGS_HOOK_PROBE_NAME,
        _CLAUDE_PLUGIN_HOOK_PROBE_NAME,
        _CURSOR_HOOK_PROBE_NAME,
    }
    run_names = [str(run["name"]) for run in _hook_execution_runs(updates)]
    assert any(not name.startswith(_MNGR_HOOK_RUN_PREFIX) for name in run_names), run_names


@pytest.mark.release
@pytest.mark.harness_internals(harness="grok")
@pytest.mark.tmux
def test_inspect_warns_only_about_two_known_keys_in_the_rendered_config(
    pinned_grok_binary: Path,
    grok_stub_server: GrokStubModelServer,
    build_hermetic_grok_agent_environment: Callable[[str], dict[str, str]],
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    hermetic_environment = build_hermetic_grok_agent_environment(grok_stub_server.get_origin_url())

    with _running_grok_agent(
        temp_mngr_ctx, build_stub_grok_config(grok_stub_server, hermetic_environment), tmp_path / "project"
    ) as agent:
        _send_and_wait_for_turn(agent, f"hello inspect {get_short_random_string()}")
        grok_home = get_grok_home(_state_dir(agent))

        inspect_environment = {
            name: value for name, value in os.environ.items() if name not in REMOVED_GROK_ENVIRONMENT_VARIABLE_NAMES
        }
        inspect_environment.update(hermetic_environment)
        inspect_environment.update(
            build_pinned_grok_environment(grok_home=grok_home, is_isolated_from_claude_config=True)
        )
        inspect_result = subprocess.run(
            [str(pinned_grok_binary), "inspect", "--json"],
            cwd=agent.work_dir,
            env=inspect_environment,
            capture_output=True,
            text=True,
            timeout=_PROCESS_TIMEOUT_SECONDS,
        )

    assert inspect_result.returncode == 0, inspect_result.stderr
    config_warnings = json.loads(inspect_result.stdout)["configWarnings"]
    assert sorted((warning["path"], warning["kind"]) for warning in config_warnings) == [
        ("claude_compat", "unknown-field"),
        ("privacy", "unknown-field"),
    ]


@pytest.mark.release
@pytest.mark.harness_internals(harness="grok")
@pytest.mark.tmux
def test_new_session_lives_in_the_computed_bucket_and_resumes_after_a_stop_before_its_first_prompt(
    pinned_grok_binary: Path,
    hermetic_stub_grok_config: GrokAgentConfig,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    local_host = get_local_host(temp_mngr_ctx)

    with _running_grok_agent(temp_mngr_ctx, hermetic_stub_grok_config, tmp_path / "work dir+ü.v2") as agent:
        session_id = str(agent.id.get_uuid())
        sessions_dir = _state_dir(agent) / GROK_SESSIONS_RELATIVE_PATH
        bucket_dir = _session_bucket_dir(agent)
        assert _list_session_dirs(sessions_dir) == [Path(bucket_dir.name, session_id)]
        assert (bucket_dir / session_id / SESSION_SUMMARY_FILENAME).is_file()

        _restart_agent(agent, local_host)
        message = f"hello after the restart {get_short_random_string()}"
        prompt_id = _send_and_wait_for_turn(agent, message)

        assert _agent_session_id(agent) == session_id
        assert _list_session_dirs(sessions_dir) == [Path(bucket_dir.name, session_id)]
        session_updates = _read_updates(bucket_dir / session_id / "updates.jsonl")
        launch_messages = _launch_log_messages(agent)

    assert len(_select_turn_completions(session_updates, prompt_id)) > 0
    assert [message for message in launch_messages if message.startswith("Launching grok")] == [
        f"Launching grok (new) with session {session_id}",
        f"Launching grok (resume) with session {session_id}",
    ]


@pytest.mark.release
@pytest.mark.harness_internals(harness="grok")
@pytest.mark.tmux
@pytest.mark.rsync
def test_fork_keeps_the_source_role_and_resumes_after_a_stop_before_its_first_prompt(
    pinned_grok_binary: Path,
    grok_stub_server: GrokStubModelServer,
    hermetic_stub_grok_config: GrokAgentConfig,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    run_id = get_short_random_string().upper()
    source_role = f"ROLEALPHA{run_id}"
    fork_role = f"ROLEBRAVO{run_id}"
    secret = f"SECRET-{run_id}"
    role_field = hermetic_stub_grok_config.field_ref().append_system_prompt
    source_config = hermetic_stub_grok_config.model_copy_update(
        to_update(role_field, (SystemPromptText(f"You are {source_role}."),))
    )
    fork_config = hermetic_stub_grok_config.model_copy_update(
        to_update(role_field, (SystemPromptText(f"You are {fork_role}."),))
    )
    local_host = get_local_host(temp_mngr_ctx)

    with _running_grok_agent(temp_mngr_ctx, source_config, tmp_path / "source_project") as source:
        _send_and_wait_for_turn(source, f"Remember this exact secret for later: {secret}. Reply with just OK.")
        source_session_id = _agent_session_id(source)

        with _running_grok_agent(
            temp_mngr_ctx, fork_config, tmp_path / "fork_project", adopt_session=(source_session_id,)
        ) as fork:
            fork_session_id = str(fork.id.get_uuid())
            assert not (_state_dir(fork) / FORK_FROM_RELATIVE_PATH).exists()
            assert (_session_bucket_dir(fork) / fork_session_id / SESSION_SUMMARY_FILENAME).is_file()

            _restart_agent(fork, local_host)
            _send_and_wait_for_turn(fork, RECALL_PROMPT)

            assert _agent_session_id(fork) == fork_session_id
            fork_launch_messages = _launch_log_messages(fork)
            fork_transcript = read_common_transcript_records(_state_dir(fork))

    assert [message for message in fork_launch_messages if message.startswith("Launching grok")] == [
        f"Launching grok (fork) with session {fork_session_id}, fork source {source_session_id}",
        f"Launching grok (resume) with session {fork_session_id}",
    ]
    assert any(record.get("source") == "agent" and secret in str(record.get("message")) for record in fork_transcript)
    recall_requests = _stub_requests_with_reply_kind(grok_stub_server, GrokStubReplyKind.RECALL)
    assert len(recall_requests) == 1
    fork_system_prompt = _system_prompt_text(recall_requests[0])
    assert source_role in fork_system_prompt
    assert fork_role not in fork_system_prompt


@pytest.mark.release
@pytest.mark.harness_internals(harness="grok")
@pytest.mark.tmux
def test_footer_first_hint_names_what_enter_does(
    pinned_grok_binary: Path,
    hermetic_stub_grok_config: GrokAgentConfig,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    with _running_grok_agent(
        temp_mngr_ctx,
        hermetic_stub_grok_config,
        tmp_path / "project",
    ) as agent:
        idle_pane = capture_tmux_pane_contents(agent.tmux_target)
        assert is_idle_and_empty(idle_pane), idle_pane

        _type_into_pane(agent, f"{SLOW_REPLY_TRIGGER} footer probe {get_short_random_string()}")
        idle_with_text_pane = _wait_for_footer_first_hint(agent, "Enter:send")
        _press_enter(agent)
        wait_for(lambda: _is_active(agent), timeout=_TURN_TIMEOUT_SECONDS, error_message="the slow turn never started")

        _type_into_pane(agent, f"queued footer probe {get_short_random_string()}")
        busy_with_text_pane = _wait_for_footer_first_hint(agent, "Enter:queue")
        _press_enter(agent)
        busy_with_queued_message_pane = _wait_for_footer_first_hint(agent, "Enter:send now")
        _type_into_pane(agent, "more text")
        busy_with_text_and_queued_message_pane = _wait_for_footer_first_hint(agent, "Enter:queue")
        assert _is_active(agent), "the slow turn ended before every footer was seen"

    for pane in (idle_with_text_pane, busy_with_text_pane, busy_with_text_and_queued_message_pane):
        assert composer_holds_text(pane), pane
        assert is_ready_for_paste(pane), pane
    assert not composer_holds_text(busy_with_queued_message_pane)
    assert is_ready_for_paste(busy_with_queued_message_pane), busy_with_queued_message_pane


@pytest.mark.release
@pytest.mark.harness_internals(harness="grok")
@pytest.mark.tmux
@pytest.mark.rsync
def test_a_consent_notice_fails_the_startup_check_before_mngr_answers_it(
    pinned_grok_binary: Path,
    grok_stub_server: GrokStubModelServer,
    build_hermetic_grok_agent_environment: Callable[[str], dict[str, str]],
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    grok_stub_server.set_scenario(
        GrokStubScenario(consent_notice=GrokStubConsentNotice(notice_id="stub-terms", version=PositiveInt(1)))
    )
    auth_provider_path = tmp_path / "grok_auth_provider.sh"
    write_stub_auth_provider_script(auth_provider_path)
    environment = {
        **build_hermetic_grok_agent_environment(grok_stub_server.get_origin_url()),
        **build_stub_auth_provider_environment(grok_stub_server.get_api_base_url(), auth_provider_path),
    }
    grok_config = GrokAgentConfig(
        version=read_pinned_grok_version(),
        auto_dismiss_dialogs_at_startup=True,
        env=tuple(f"{name}={value}" for name, value in environment.items()),
    )
    with (
        pytest.raises(AgentStartError) as error_info,
        _running_grok_agent(temp_mngr_ctx, grok_config, tmp_path / "project"),
    ):
        pass

    input_blocked_error = error_info.value.__cause__
    assert isinstance(input_blocked_error, GrokInputBlockedError)
    assert input_blocked_error.gate_screen is GrokGateScreen.CONSENT
    records = grok_stub_server.get_request_records()
    assert any(record.path.split("?", 1)[0] == "/v1/settings" for record in records)
    assert not any(record.path.split("?", 1)[0].endswith("/consent/accept") for record in records)
    assert not any(record.chat_reply is not None for record in records)


_STOP_BLOCK_FEEDBACK: Final[str] = f"{SLOW_REPLY_TRIGGER} continue once more"


def _write_blocking_stop_hook(tmp_path: Path) -> tuple[Path, Path]:
    """A Stop hook that blocks the first fire of each prompt and logs every fire with whether `active` existed."""
    log_path = tmp_path / "blocking_stop_fires.jsonl"
    script_path = tmp_path / "blocking_stop_hook.py"
    write_executable_script(
        script_path,
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        'payload = json.loads(sys.stdin.read() or "{}")\n'
        f"log_path = {str(log_path)!r}\n"
        "previous_fires = []\n"
        "if os.path.exists(log_path):\n"
        "    previous_fires = [json.loads(line) for line in open(log_path) if line.strip()]\n"
        'prompt_id = payload.get("promptId")\n'
        'is_first_fire = all(fire["prompt_id"] != prompt_id for fire in previous_fires)\n'
        "fire = {\n"
        '    "prompt_id": prompt_id,\n'
        '    "stop_hook_active": payload.get("stopHookActive"),\n'
        f'    "is_active": os.path.exists(os.path.join(os.environ["MNGR_AGENT_STATE_DIR"], {ACTIVE_MARKER_FILENAME!r})),\n'
        "}\n"
        "with open(log_path, 'a') as log_file:\n"
        "    log_file.write(json.dumps(fire) + '\\n')\n"
        'if is_first_fire and payload.get("reason") == "end_turn":\n'
        f"    sys.stderr.write({_STOP_BLOCK_FEEDBACK!r} + '\\n')\n"
        "    sys.exit(2)\n",
    )
    return script_path, log_path


@pytest.mark.release
@pytest.mark.harness_internals(harness="grok")
@pytest.mark.tmux
def test_a_blocking_stop_hook_keeps_the_agent_active_through_its_continuation(
    pinned_grok_binary: Path,
    build_hermetic_grok_agent_environment: Callable[[str], dict[str, str]],
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    agent_id = AgentId.generate()
    block_script_path, block_log_path = _write_blocking_stop_hook(tmp_path)
    block_hooks = _upload_extra_hooks_file(
        tmp_path, temp_mngr_ctx, agent_id, "block", _build_hooks_file(str(block_script_path), ("Stop",))
    )
    stub_config = GrokStubServerConfig(slow_reply_seconds=PositiveFloat(5.0))

    with (
        running_grok_stub_model_server(stub_config) as stub,
        _running_grok_agent(
            temp_mngr_ctx,
            _build_hermetic_stub_grok_config(stub, build_hermetic_grok_agent_environment),
            tmp_path / "project",
            agent_id=agent_id,
            upload_files=(block_hooks,),
        ) as agent,
    ):
        message = f"hello blocking stop {get_short_random_string()}"
        agent.send_message(message)
        prompt_id = _find_submitted_prompt_id(agent, message)
        wait_for(
            lambda: len(_stub_requests_with_reply_kind(stub, GrokStubReplyKind.SLOW)) == 1,
            timeout=_TURN_TIMEOUT_SECONDS,
            error_message="grok never continued the turn its Stop hook blocked",
        )
        is_active_during_continuation = _is_active(agent)
        turn_file_text = (_state_dir(agent) / ROOT_TURNS_RELATIVE_PATH / _agent_session_id(agent)).read_text()
        _wait_for_idle_after_turn(agent, prompt_id)

    assert is_active_during_continuation
    assert turn_file_text.split("\n", 1)[0] == prompt_id
    assert read_complete_jsonl_records(block_log_path) == [
        {"prompt_id": prompt_id, "stop_hook_active": False, "is_active": True},
        {"prompt_id": prompt_id, "stop_hook_active": True, "is_active": True},
    ]


@pytest.mark.release
@pytest.mark.harness_internals(harness="grok")
@pytest.mark.tmux
def test_an_auto_wake_turn_writes_turn_completed_under_its_task_completed_prompt_id(
    pinned_grok_binary: Path,
    hermetic_stub_grok_config: GrokAgentConfig,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    with _running_grok_agent(
        temp_mngr_ctx,
        hermetic_stub_grok_config,
        tmp_path / "project",
    ) as agent:
        _send_and_wait_for_turn(
            agent, f"{BACKGROUND_SHELL_TRIGGER} start the background job {get_short_random_string()}"
        )
        wake_completion = _wait_for_completed_wake_turn(agent, "task-completed-")
        wake_prompt_id = str(wake_completion["prompt_id"])
        submissions = (
            (_state_dir(agent) / GROK_PLUGIN_STATE_RELATIVE_PATH / SUBMISSIONS_LOG_FILENAME).read_text().splitlines()
        )
        hook_log = _wait_for_hook_log_containing(
            agent, _turn_end_log_fragment(_agent_session_id(agent), wake_prompt_id)
        )

    assert wake_completion["stop_reason"] == "end_turn"
    assert (agent.work_dir / "bg_done.txt").is_file()
    assert f"{wake_prompt_id} {AUTO_WAKE_TAIL_MARKER}" in submissions
    assert f"prompt={wake_prompt_id} turn:start-auto-wake" in hook_log


@pytest.mark.release
@pytest.mark.harness_internals(harness="grok")
@pytest.mark.tmux
def test_a_background_subagent_holds_the_agent_running_background_until_it_ends_then_running_through_its_wake_turn(
    pinned_grok_binary: Path,
    grok_stub_server: GrokStubModelServer,
    hermetic_stub_grok_config: GrokAgentConfig,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    grok_stub_server.set_scenario(GrokStubScenario(is_child_reply_slow=True))

    with _running_grok_agent(
        temp_mngr_ctx,
        hermetic_stub_grok_config,
        tmp_path / "project",
    ) as agent:
        spawn_prompt_id = _send_and_wait_for_turn(
            agent, f"{BACKGROUND_SUBAGENT_TRIGGER} please delegate in the background {get_short_random_string()}"
        )
        probe_while_subagent_runs = agent.probe_lifecycle()
        wake_completion = _wait_for_completed_wake_turn(agent, SUBAGENT_WAKE_PROMPT_ID_PREFIX)
        wake_prompt_id = str(wake_completion["prompt_id"])
        root_session_id = _agent_session_id(agent)
        hook_log = _wait_for_hook_log_containing(agent, _turn_end_log_fragment(root_session_id, wake_prompt_id))
        probe_after_wake_turn = agent.probe_lifecycle()

    subagent_id = wake_prompt_id.removeprefix(SUBAGENT_WAKE_PROMPT_ID_PREFIX)
    subagent_lease_name = WORK_LEASE_NAME_PREFIX + subagent_id
    failure_context = (
        f"pinned grok {read_pinned_grok_version()}; grok_hook.py's _handle_stop leases the backgroundTasks of the "
        f"spawning turn's Stop, which must run before its turn_completed. Hook log:\n{hook_log}"
    )
    assert _hook_outcomes_of_turn(hook_log, root_session_id, spawn_prompt_id) == [
        "turn:start active:on",
        "stop subagents-pruned=0 subagents-marked-background=1 leases=1",
        "turn:end active:off",
    ], failure_context
    assert (probe_while_subagent_runs.state, [lease.name for lease in probe_while_subagent_runs.leases]) == (
        AgentLifecycleState.RUNNING_BACKGROUND,
        [subagent_lease_name],
    ), failure_context
    assert wake_completion["stop_reason"] == "end_turn"
    (_, wake_marker_on_line) = [line for line in hook_log.splitlines() if " active:on" in line]
    assert f"event=session_end session={subagent_id} " in wake_marker_on_line
    assert wake_marker_on_line.endswith(" subagent:end wake:placeholder active:on")
    (_, wake_marker_off_line) = [line for line in hook_log.splitlines() if " active:off" in line]
    assert _turn_end_log_fragment(root_session_id, wake_prompt_id) in wake_marker_off_line
    assert (probe_after_wake_turn.state, probe_after_wake_turn.leases) == (AgentLifecycleState.WAITING, ())


@pytest.mark.release
@pytest.mark.harness_internals(harness="grok")
@pytest.mark.tmux
def test_ctrl_c_during_a_foreground_tool_cancels_the_turn_and_leaves_grok_running(
    pinned_grok_binary: Path,
    hermetic_stub_grok_config: GrokAgentConfig,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    message = f"{SLOW_TOOL_TRIGGER} run the slow tool {get_short_random_string()}"

    with _running_grok_agent(
        temp_mngr_ctx,
        hermetic_stub_grok_config,
        tmp_path / "project",
    ) as agent:
        agent.send_message(message)
        prompt_id = _find_submitted_prompt_id(agent, message)
        wait_for(
            lambda: any(update.get("sessionUpdate") == "tool_call" for update in _read_all_session_updates(agent)),
            timeout=_TURN_TIMEOUT_SECONDS,
            error_message="the slow tool call never started",
        )
        agent.press_key_chord("C-c")
        _wait_for_idle_after_turn(agent, prompt_id)
        turn_ends = _select_turn_completions(_read_all_session_updates(agent), prompt_id)
        wait_for(
            lambda: is_idle_and_empty(capture_tmux_pane_contents(agent.tmux_target)),
            timeout=_TURN_TIMEOUT_SECONDS,
            error_message="grok did not return to an idle, empty composer after the Ctrl+C",
        )
        with pytest.raises(GrokKeyRefusedError, match="grok is idle"):
            agent.press_key_chord("C-c")
        pane_after_refused_press = capture_tmux_pane_contents(agent.tmux_target)
        hook_log = (_state_dir(agent) / _HOOK_LOG_RELATIVE_PATH).read_text()

    assert [turn_end["stop_reason"] for turn_end in turn_ends] == ["cancelled"]
    assert f"event={TURN_DROPPED_FLAG} " not in hook_log
    assert is_idle_and_empty(pane_after_refused_press) and not has_press_again_hint(pane_after_refused_press)


@pytest.mark.release
@pytest.mark.harness_internals(harness="grok")
@pytest.mark.tmux
def test_ctrl_c_cancels_a_turn_without_asking_and_leaves_a_running_background_subagent_running(
    pinned_grok_binary: Path,
    grok_stub_server: GrokStubModelServer,
    hermetic_stub_grok_config: GrokAgentConfig,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    grok_stub_server.set_scenario(GrokStubScenario(is_child_reply_slow=True))
    slow_reply_message = f"{SLOW_REPLY_TRIGGER} stream a slow reply {get_short_random_string()}"

    with _running_grok_agent(
        temp_mngr_ctx,
        hermetic_stub_grok_config,
        tmp_path / "project",
    ) as agent:
        delegate_message = (
            f"{BACKGROUND_SUBAGENT_TRIGGER} please delegate in the background {get_short_random_string()}"
        )
        agent.send_message(delegate_message)
        delegate_prompt_id = _find_submitted_prompt_id(agent, delegate_message)
        wait_for(
            lambda: _is_turn_completed(agent, delegate_prompt_id),
            timeout=_TURN_TIMEOUT_SECONDS,
            error_message="the turn that spawned the background subagent did not complete",
        )
        agent.send_message(slow_reply_message)
        prompt_id = _find_submitted_prompt_id(agent, slow_reply_message)
        wait_for(
            lambda: has_cancel_hint(capture_tmux_pane_contents(agent.tmux_target)),
            timeout=_TURN_TIMEOUT_SECONDS,
            error_message="the slow reply never showed grok's cancel hint",
        )
        agent.press_key_chord("C-c")
        wait_for(
            lambda: _is_turn_completed(agent, prompt_id),
            timeout=_TURN_TIMEOUT_SECONDS,
            error_message="the Ctrl+C did not end the turn while a background subagent ran",
        )
        pane_after_cancel = capture_tmux_pane_contents(agent.tmux_target)
        is_subagent_running_at_cancel = not any(
            update.get("sessionUpdate") == "subagent_finished" for update in _read_all_session_updates(agent)
        )
        turn_ends = _select_turn_completions(_read_all_session_updates(agent), prompt_id)
        wake_completion = _wait_for_completed_wake_turn(agent, SUBAGENT_WAKE_PROMPT_ID_PREFIX)

    assert is_subagent_running_at_cancel
    assert [turn_end["stop_reason"] for turn_end in turn_ends] == ["cancelled"]
    assert "Subagents are still running" not in pane_after_cancel
    assert wake_completion["stop_reason"] == "end_turn"


def _build_one_line_held_message(run_id: str) -> str:
    return f"{HELD_REPLY_TRIGGER} hold the reply {run_id}"


def _build_pasted_held_message(run_id: str) -> str:
    """A held message of six long lines, which mngr pastes and grok puts back as a `[Pasted: 6 lines]` chip."""
    return "\n".join(
        f"{HELD_REPLY_TRIGGER} pasted line {number} {run_id} " + "the quick brown fox jumps over the lazy dog " * 4
        for number in range(1, 7)
    )


def _start_held_turn(agent: GrokAgent, held_message: str, run_id: str) -> str:
    """After one ordinary turn, send `held_message`, wait for grok's cancel hint, and return the held promptId."""
    _send_and_wait_for_turn(agent, f"hello before the held reply {run_id}")
    agent.send_message(held_message)
    held_prompt_id = _find_submitted_prompt_id(agent, held_message)
    wait_for(
        lambda: has_cancel_hint(capture_tmux_pane_contents(agent.tmux_target)),
        timeout=_TURN_TIMEOUT_SECONDS,
        error_message="the held turn never showed grok's cancel hint",
    )
    return held_prompt_id


@pytest.mark.release
@pytest.mark.harness_internals(harness="grok")
@pytest.mark.tmux
@pytest.mark.timeout(450)
def test_a_turn_a_person_cancels_before_its_first_reply_goes_unreported_and_ends_at_the_idle_notification(
    pinned_grok_binary: Path,
    hermetic_stub_grok_config: GrokAgentConfig,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    run_id = get_short_random_string()
    held_message = _build_one_line_held_message(run_id)

    with _running_grok_agent(temp_mngr_ctx, hermetic_stub_grok_config, tmp_path / "project") as agent:
        prompt_id = _start_held_turn(agent, held_message, run_id)
        _send_tmux_keys(agent, "C-c")
        wait_for(
            lambda: read_composer_text(capture_tmux_pane_contents(agent.tmux_target)) == held_message,
            timeout=_TURN_TIMEOUT_SECONDS,
            error_message="grok did not put the cancelled prompt back in the input box",
        )
        state_after_cancel = agent.get_lifecycle_state()
        wait_for(
            lambda: agent.get_lifecycle_state() == AgentLifecycleState.WAITING,
            timeout=_UNREPORTED_TURN_END_TIMEOUT_SECONDS,
            poll_interval=1.0,
            error_message="the agent never reported WAITING after grok dropped the cancelled turn",
        )
        hook_log = _wait_for_hook_log_containing(agent, " backstop:")
        turn_ends = _select_turn_completions(_read_all_session_updates(agent), prompt_id)
        composer_text_at_waiting = read_composer_text(capture_tmux_pane_contents(agent.tmux_target))

    assert state_after_cancel == AgentLifecycleState.RUNNING
    assert turn_ends == []
    prompt_log_lines = [line for line in hook_log.splitlines() if f" prompt={prompt_id} " in line]
    assert [line.split(" event=", 1)[1].split(" ", 1)[0] for line in prompt_log_lines] == ["user_prompt_submit"]
    notification_log_lines = [line for line in hook_log.splitlines() if " event=notification " in line]
    assert notification_log_lines[-1].endswith(" backstop:cleared active:off"), notification_log_lines
    assert composer_text_at_waiting == held_message


@pytest.mark.release
@pytest.mark.harness_internals(harness="grok")
@pytest.mark.tmux
@pytest.mark.parametrize(
    ("build_held_message", "min_held_message_length"),
    [
        pytest.param(_build_one_line_held_message, 1, id="one_line"),
        pytest.param(_build_pasted_held_message, LONG_MESSAGE_THRESHOLD, id="pasted_six_lines"),
    ],
)
def test_mngr_s_ctrl_c_before_the_first_reply_ends_the_dropped_turn_at_once_and_clears_its_prompt(
    pinned_grok_binary: Path,
    grok_stub_server: GrokStubModelServer,
    hermetic_stub_grok_config: GrokAgentConfig,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
    build_held_message: Callable[[str], str],
    min_held_message_length: int,
) -> None:
    run_id = get_short_random_string()
    held_message = build_held_message(run_id)
    next_message = f"next after the cancel {run_id}"
    assert len(held_message) >= min_held_message_length

    with _running_grok_agent(temp_mngr_ctx, hermetic_stub_grok_config, tmp_path / "project") as agent:
        held_prompt_id = _start_held_turn(agent, held_message, run_id)
        pressed_at = time.monotonic()
        agent.press_key_chord("C-c")
        seconds_to_press_return = time.monotonic() - pressed_at
        wait_for(
            lambda: agent.get_lifecycle_state() == AgentLifecycleState.WAITING,
            timeout=_SETTLED_CANCEL_TIMEOUT_SECONDS,
            poll_interval=0.2,
            error_message="the agent did not report WAITING soon after mngr cancelled the held turn",
        )
        seconds_to_waiting = time.monotonic() - pressed_at
        pane_at_waiting = capture_tmux_pane_contents(agent.tmux_target)
        hook_log = _wait_for_hook_log_containing(
            agent, _turn_dropped_log_fragment(_agent_session_id(agent), held_prompt_id)
        )
        held_turn_ends = _select_turn_completions(_read_all_session_updates(agent), held_prompt_id)
        _send_and_wait_for_turn(agent, next_message)
        pane_after_next_turn = capture_tmux_pane_contents(agent.tmux_target)
        state_after_next_turn = agent.get_lifecycle_state()

    assert _DROPPED_TURN_SETTLED_BOUND_SECONDS < agent.cancel_settle_timeout_seconds
    assert seconds_to_press_return < _DROPPED_TURN_SETTLED_BOUND_SECONDS, seconds_to_press_return
    assert seconds_to_waiting < _DROPPED_TURN_SETTLED_BOUND_SECONDS, seconds_to_waiting
    assert is_idle_and_empty(pane_at_waiting) and not has_press_again_hint(pane_at_waiting), pane_at_waiting
    assert read_composer_text(pane_at_waiting) == ""
    assert held_turn_ends == []
    assert " backstop:" not in hook_log
    assert is_idle_and_empty(pane_after_next_turn)
    assert state_after_next_turn == AgentLifecycleState.WAITING
    next_turn_user_texts = [
        find_last_user_text(get_chat_messages(record.body))
        for record in grok_stub_server.get_request_records()
        if record.chat_reply is not None
        and record.chat_reply.kind != GrokStubReplyKind.SIDE_CALL
        and isinstance(record.body, Mapping)
        and next_message in find_last_user_text(get_chat_messages(record.body))
    ]
    assert len(next_turn_user_texts) > 0
    assert all(held_message not in user_text for user_text in next_turn_user_texts), next_turn_user_texts


@pytest.mark.release
@pytest.mark.harness_internals(harness="grok")
@pytest.mark.tmux
def test_an_empty_model_reply_keeps_the_turn_in_flight_through_grok_s_retry(
    pinned_grok_binary: Path,
    grok_stub_server: GrokStubModelServer,
    hermetic_stub_grok_config: GrokAgentConfig,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    grok_stub_server.set_scenario(GrokStubScenario(empty_prompt_reply_attempt_count=NonNegativeInt(1)))
    message = f"{EMPTY_REPLY_TRIGGER} answer after one retry {get_short_random_string()}"

    with _running_grok_agent(
        temp_mngr_ctx,
        hermetic_stub_grok_config,
        tmp_path / "project",
    ) as agent:
        prompt_id = _send_and_wait_for_turn(agent, message)
        updates = _read_all_session_updates(agent)
        hook_log = _wait_for_hook_log_containing(agent, _turn_end_log_fragment(_agent_session_id(agent), prompt_id))

    turn_replies = [
        record.chat_reply.kind
        for record in grok_stub_server.get_request_records()
        if record.chat_reply is not None
        and record.chat_reply.kind != GrokStubReplyKind.SIDE_CALL
        and message in find_last_user_text(get_chat_messages(record.body))
    ]
    assert turn_replies == [GrokStubReplyKind.EMPTY, GrokStubReplyKind.REPLY]
    assert any(update.get("sessionUpdate") == "retry_state" for update in updates)
    turn_ends = _select_turn_completions(updates, prompt_id)
    assert [turn_end["stop_reason"] for turn_end in turn_ends] == ["end_turn"]
    assert hook_log.count(" active:on") == 1
    assert hook_log.count(" active:off") == 1


@pytest.mark.release
@pytest.mark.harness_internals(harness="grok")
@pytest.mark.tmux
def test_a_five_line_message_typed_with_send_keys_arrives_as_one_prompt(
    pinned_grok_binary: Path,
    hermetic_stub_grok_config: GrokAgentConfig,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    agent_id = AgentId.generate()
    probe_hooks, probe_log_path = _upload_payload_probe_hooks(tmp_path, temp_mngr_ctx, agent_id, ("UserPromptSubmit",))
    run_id = get_short_random_string()
    message = "\n".join(f"five line probe {run_id} line {line_number}" for line_number in range(1, 6))
    assert len(message) < LONG_MESSAGE_THRESHOLD

    with _running_grok_agent(
        temp_mngr_ctx,
        hermetic_stub_grok_config,
        tmp_path / "project",
        agent_id=agent_id,
        upload_files=(probe_hooks,),
    ) as agent:
        _send_and_wait_for_turn(agent, message)

    assert [payload["prompt"] for payload in _read_probe_payloads(probe_log_path, "user_prompt_submit")] == [message]


@pytest.mark.release
@pytest.mark.harness_internals(harness="grok")
@pytest.mark.tmux
def test_updates_carry_the_kinds_and_fields_the_converter_maps(
    pinned_grok_binary: Path,
    hermetic_stub_grok_config: GrokAgentConfig,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    with _running_grok_agent(
        temp_mngr_ctx,
        hermetic_stub_grok_config,
        tmp_path / "project",
    ) as agent:
        tool_prompt_id = _send_and_wait_for_turn(
            agent, f"{TOOL_CALL_TRIGGER} converter check {get_short_random_string()}"
        )
        _send_and_wait_for_turn(agent, f"{FOREGROUND_SUBAGENT_TRIGGER} converter check {get_short_random_string()}")
        updates = _read_all_session_updates(agent)
        common_records = read_common_transcript_records(_state_dir(agent))

    updates_by_kind: dict[str, list[dict[str, Any]]] = {}
    for update in updates:
        updates_by_kind.setdefault(str(update.get("sessionUpdate")), []).append(update)
    assert set(updates_by_kind) >= {
        "user_message_chunk",
        "agent_message_chunk",
        "tool_call",
        "tool_call_update",
        "subagent_spawned",
        "turn_completed",
    }
    assert all(isinstance(update["content"]["text"], str) for update in updates_by_kind["user_message_chunk"])
    tool_calls = updates_by_kind["tool_call"]
    assert {call["_meta"]["x.ai/tool"]["name"] for call in tool_calls} == {
        "run_terminal_command",
        common_transcript_convert._SPAWN_SUBAGENT_TOOL_NAME,
    }
    tool_call_ids = {call["toolCallId"] for call in tool_calls}
    finished_tool_call_ids = {
        update["toolCallId"]
        for update in updates_by_kind["tool_call_update"]
        if update.get("status") in common_transcript_convert._TERMINAL_TOOL_CALL_STATUSES
    }
    assert finished_tool_call_ids == tool_call_ids
    spawn_descriptions = {
        call["rawInput"]["description"]
        for call in tool_calls
        if call["_meta"]["x.ai/tool"]["name"] == common_transcript_convert._SPAWN_SUBAGENT_TOOL_NAME
    }
    assert {spawned["description"] for spawned in updates_by_kind["subagent_spawned"]} == spawn_descriptions
    assert all(isinstance(spawned["child_session_id"], str) for spawned in updates_by_kind["subagent_spawned"])
    tool_turn_completion = next(
        update for update in updates_by_kind["turn_completed"] if update["prompt_id"] == tool_prompt_id
    )
    assert tool_turn_completion["stop_reason"] == "end_turn"
    for _, grok_usage_field in common_transcript_convert._METRIC_FIELDS:
        assert isinstance(tool_turn_completion["usage"][grok_usage_field], int), tool_turn_completion["usage"]

    agent_steps = [record for record in common_records if record.get("source") == "agent"]
    assert any(
        call["function_name"] == "run_terminal_command" for step in agent_steps for call in step.get("tool_calls", [])
    )
    assert any(record["type"] == "observation" for record in common_records)
    assert any(
        step.get("extra", {}).get("is_sidechain") and "STUB_CHILD_REPLY" in step["message"] for step in agent_steps
    )
    assert any(
        step.get("extra", {}).get("finish_reason") == "end_turn" and step.get("metrics") for step in agent_steps
    )
    assert any(record.get("source") == "user" and SUBAGENT_PROMPT in record["message"] for record in common_records)


@pytest.mark.release
@pytest.mark.harness_internals(harness="grok")
@pytest.mark.tmux
def test_a_send_after_text_left_in_the_composer_is_confirmed_by_the_prompt_it_joins(
    pinned_grok_binary: Path,
    hermetic_stub_grok_config: GrokAgentConfig,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    run_id = get_short_random_string()
    draft = f"draft left by a person {run_id}"
    message = f"short send {run_id}"

    with _running_grok_agent(temp_mngr_ctx, hermetic_stub_grok_config, tmp_path / "project") as agent:
        _type_into_pane(agent, draft)
        wait_for(
            lambda: composer_holds_text(capture_tmux_pane_contents(agent.tmux_target)),
            timeout=_TURN_TIMEOUT_SECONDS,
            error_message="the draft never showed in grok's composer",
        )
        with capture_loguru(level="DEBUG") as send_log:
            agent.send_message(message)
        _wait_for_idle_after_turn(agent, _find_submitted_prompt_id(agent, f"{draft}{message}"))

    assert "(confirmed by submissions-log)" in send_log.getvalue()


@pytest.mark.release
@pytest.mark.harness_internals(harness="grok")
@pytest.mark.tmux
def test_a_send_without_ascii_letters_to_a_busy_grok_is_confirmed_by_its_queued_row(
    pinned_grok_binary: Path,
    hermetic_stub_grok_config: GrokAgentConfig,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    queued_message = "Исправь падающий тест\nи запусти его снова"
    queued_tail = normalized_prompt_tail(queued_message)

    with _running_grok_agent(temp_mngr_ctx, hermetic_stub_grok_config, tmp_path / "project") as agent:
        agent.send_message(f"{SLOW_REPLY_TRIGGER} keep busy {get_short_random_string()}")
        with capture_loguru(level="DEBUG") as send_log:
            agent.send_message(queued_message)
        tails_after_send = [line_tail for _, line_tail in _read_submissions(agent)]
        wait_for(
            lambda: any(line_tail == queued_tail for _, line_tail in _read_submissions(agent)),
            timeout=_TURN_TIMEOUT_SECONDS,
            error_message="the queued message never ran",
        )
        _wait_for_idle_after_turn(agent, _find_submitted_prompt_id(agent, queued_message))

    assert "(confirmed by queued-row)" in send_log.getvalue()
    assert queued_tail not in tails_after_send


@pytest.mark.release
@pytest.mark.harness_internals(harness="grok")
@pytest.mark.tmux
def test_a_new_command_send_is_confirmed_by_the_root_session_it_starts(
    pinned_grok_binary: Path,
    hermetic_stub_grok_config: GrokAgentConfig,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    with _running_grok_agent(temp_mngr_ctx, hermetic_stub_grok_config, tmp_path / "project") as agent:
        _send_and_wait_for_turn(agent, f"first session prompt {get_short_random_string()}")
        first_session_id = _agent_session_id(agent)
        with capture_loguru(level="DEBUG") as send_log:
            agent.send_message("/new")
        session_id_after_send = _agent_session_id(agent)

    assert "(confirmed by session-id)" in send_log.getvalue()
    assert session_id_after_send != first_session_id


@pytest.mark.release
@pytest.mark.harness_internals(harness="grok")
@pytest.mark.tmux
def test_a_tool_approved_on_its_permission_card_runs_as_running_and_takes_a_queued_message(
    pinned_grok_binary: Path,
    build_hermetic_grok_agent_environment: Callable[[str], dict[str, str]],
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    run_id = get_short_random_string()
    message = f"{SLOW_TOOL_TRIGGER} run the slow tool once approved {run_id}"
    queued_message = f"queued while the approved tool runs {run_id}"
    queued_tail = normalized_prompt_tail(queued_message)

    with running_grok_stub_model_server(GrokStubServerConfig(slow_tool_seconds=PositiveInt(30))) as stub:
        grok_config = _build_hermetic_stub_grok_config(stub, build_hermetic_grok_agent_environment)
        grok_config = grok_config.model_copy_update(
            to_update(grok_config.field_ref().auto_allow_permissions, False),
            # grok runs a plain `sleep` without asking.
            to_update(
                grok_config.field_ref().settings_overrides,
                {**grok_config.settings_overrides, "permission": {"ask": ["Bash(sleep *)"]}},
            ),
        )
        with _running_grok_agent(temp_mngr_ctx, grok_config, tmp_path / "project") as agent:
            permissions_waiting_path = _state_dir(agent) / PERMISSIONS_WAITING_FILENAME
            agent.send_message(message)
            prompt_id = _find_submitted_prompt_id(agent, message)
            wait_for(
                lambda: permissions_waiting_path.exists()
                and detect_gate_screen(capture_tmux_pane_contents(agent.tmux_target))
                is GrokGateScreen.PERMISSION_CARD,
                timeout=_TURN_TIMEOUT_SECONDS,
                error_message="grok never showed the slow tool's permission card with permissions_waiting recorded",
            )
            readings_on_card = (agent.is_blocked_on_dialog(), agent.get_lifecycle_state())
            agent.press_key_chord("Enter")
            wait_for(
                lambda: _is_busy_composer_shown(capture_tmux_pane_contents(agent.tmux_target)),
                timeout=_TURN_TIMEOUT_SECONDS,
                error_message="grok's composer did not come back while the approved tool ran",
            )
            readings_while_tool_runs = (
                permissions_waiting_path.exists(),
                agent.is_blocked_on_dialog(),
                agent.get_lifecycle_state(),
            )
            with capture_loguru(level="DEBUG") as send_log:
                agent.send_message(queued_message)
            is_tool_running_after_send = not _is_completed_tool_call_recorded(agent)
            wait_for(
                lambda: any(line_tail == queued_tail for _, line_tail in _read_submissions(agent)),
                timeout=_TURN_TIMEOUT_SECONDS,
                error_message="the message queued while the approved tool ran never ran",
            )
            _wait_for_idle_after_turn(agent, _find_submitted_prompt_id(agent, queued_message))
            is_approved_turn_completed = _is_turn_completed(agent, prompt_id)

    assert readings_on_card == (True, AgentLifecycleState.WAITING)
    assert readings_while_tool_runs == (True, False, AgentLifecycleState.RUNNING)
    assert is_tool_running_after_send
    assert "(confirmed by queued-row)" in send_log.getvalue()
    assert is_approved_turn_completed
