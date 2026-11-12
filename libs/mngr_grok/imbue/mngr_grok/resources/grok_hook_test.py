import io
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

import pytest
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.agents.tui_utils import NORMALIZED_PROBE_MAX_LENGTH
from imbue.mngr.agents.tui_utils import build_normalized_message_probe
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.work_leases import DEFAULT_WORK_LEASE_TTL_SECONDS
from imbue.mngr.hosts.work_leases import WORK_LEASE_TTL_ENV_VAR
from imbue.mngr.hosts.work_leases import parse_work_leases
from imbue.mngr.utils.testing import file_lock_held_by_another_process
from imbue.mngr_grok.resources import grok_hook
from imbue.mngr_grok.resources.testing import HOOK_FIXTURES_DIR
from imbue.mngr_grok.resources.testing import read_complete_jsonl_records
from imbue.mngr_grok.resources.testing import read_files_under

_SCRIPT_PATH: Final[Path] = Path(__file__).parent / "grok_hook.py"
_EVENT_DETAIL_KEY_BY_EVENT: Final[Mapping[str, str]] = {
    "session_start": "source",
    "stop": "reason",
    "session_end": "reason",
    "notification": "notificationType",
    "pre_tool_use": "toolName",
    "post_tool_use": "toolName",
}
_TIMESTAMP_PATTERN: Final[re.Pattern[str]] = re.compile(r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{9}Z\Z")
# How much later than the test's own clock the hook may date a lease from
_LEASE_TTL_SLACK_SECONDS: Final[float] = 30.0
_HOLD_LOCK_SCRIPT: Final[str] = (
    "import fcntl, sys\n"
    "lock_file = open(sys.argv[1], 'a')\n"
    "fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)\n"
    "print('locked', flush=True)\n"
    "sys.stdin.read()\n"
)


class _HookFixture(FrozenModel):
    """A captured hook sequence and the short labels its step descriptions use."""

    label_by_id: dict[str, str] = Field(description="Short labels for the session, subagent and prompt ids")
    steps: tuple[dict[str, Any], ...] = Field(description="Hook payloads and turn_completed records in order")


class _HookTarget(FrozenModel):
    """The agent state directory and host directory one replay writes to."""

    state_dir: Path = Field(description="The agent's MNGR_AGENT_STATE_DIR")
    host_dir: Path = Field(description="The host's MNGR_HOST_DIR")


def _load_fixture(name: str) -> _HookFixture:
    records = read_complete_jsonl_records(HOOK_FIXTURES_DIR / f"{name}.jsonl")
    header = records[0]
    assert (header["kind"], header["binary"]) == ("labels", "grok 1.0.62")
    return _HookFixture(label_by_id={**header["sessions"], **header["prompts"]}, steps=tuple(records[1:]))


def _make_target(temp_host_dir: Path) -> _HookTarget:
    state_dir = temp_host_dir / "agents" / f"agent-{uuid4().hex}"
    state_dir.mkdir(parents=True)
    return _HookTarget(state_dir=state_dir, host_dir=temp_host_dir)


def _plugin_dir(target: _HookTarget) -> Path:
    return target.state_dir / "plugin" / "grok"


def _handler_environ(target: _HookTarget, extra_env: Mapping[str, str]) -> dict[str, str]:
    return {"MNGR_AGENT_STATE_DIR": str(target.state_dir), "MNGR_HOST_DIR": str(target.host_dir), **extra_env}


def _run_handler(target: _HookTarget, arguments: Sequence[str], stdin_text: str, extra_env: Mapping[str, str]) -> None:
    """Run one invocation in-process and require that it logged no error, since main() swallows every exception."""
    log_line_count_before = len(_hook_log_lines(target))
    exit_code = grok_hook.main(
        ["grok_hook.py", *arguments], io.BytesIO(stdin_text.encode()), _handler_environ(target, extra_env), ()
    )
    assert exit_code == 0
    added_log_lines = _hook_log_lines(target)[log_line_count_before:]
    assert not any(" error:" in line for line in added_log_lines), added_log_lines


def _run_handler_process(
    target: _HookTarget, arguments: Sequence[str], stdin_text: str, extra_env: Mapping[str, str]
) -> float:
    """Run the script as grok runs it; returns the wall time of the process."""
    started_at = time.monotonic()
    result = subprocess.run(
        [str(_SCRIPT_PATH), *arguments],
        input=stdin_text,
        env={**os.environ, **_handler_environ(target, extra_env)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    elapsed_seconds = time.monotonic() - started_at
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    return elapsed_seconds


def _hook_step(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {"kind": "hook", "payload": dict(payload)}


def _turn_completed_step(session_id: str, prompt_id: str) -> dict[str, Any]:
    return {"kind": "turn_completed", "sessionId": session_id, "promptId": prompt_id}


def _run_step(target: _HookTarget, step: Mapping[str, Any]) -> None:
    if step["kind"] == "turn_completed":
        _run_handler(target, ["--turn-completed", step["sessionId"], step["promptId"]], "", {})
    else:
        _run_handler(target, [], json.dumps(step["payload"]), {})


def _label(value: str, fixture: _HookFixture) -> str:
    if value.startswith(grok_hook.PENDING_WAKE_PROMPT_ID_PREFIX):
        return grok_hook.PENDING_WAKE_PROMPT_ID_PREFIX + _label(
            value[len(grok_hook.PENDING_WAKE_PROMPT_ID_PREFIX) :], fixture
        )
    return fixture.label_by_id.get(value, value)


def _describe_step(step: Mapping[str, Any], fixture: _HookFixture) -> str:
    if step["kind"] == "turn_completed":
        return f"{_label(step['sessionId'], fixture)} --turn-completed {_label(step['promptId'], fixture)}"
    payload = step["payload"]
    event = payload["hookEventName"]
    words = [_label(payload["sessionId"], fixture), event]
    detail_key = _EVENT_DETAIL_KEY_BY_EVENT.get(event)
    if event == "subagent_start":
        words.append(_label(payload["subagentId"], fixture))
    elif detail_key is not None and detail_key in payload:
        words.append(payload[detail_key])
    else:
        pass
    if "promptId" in payload:
        words.append(_label(payload["promptId"], fixture))
    if payload.get("stopHookActive") is True:
        words.append("again")
    return " ".join(words)


def _visible_names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.glob("*") if not path.name.startswith("."))


def _file_lines(path: Path) -> list[str]:
    return path.read_text().splitlines()


def _describe_marker_state(target: _HookTarget, fixture: _HookFixture) -> str:
    plugin_dir = _plugin_dir(target)
    turns = [
        f"{_label(name, fixture)}:{_label(_file_lines(plugin_dir / 'turns' / name)[0], fixture)}"
        for name in _visible_names(plugin_dir / "turns")
    ]
    subagents = [
        _label(name, fixture) + ("(bg)" if "background" in _file_lines(plugin_dir / "subagents" / name)[1:] else "")
        for name in _visible_names(plugin_dir / "subagents")
    ]
    pending_wakes = [
        f"{_label(name, fixture)}<{_label(_file_lines(plugin_dir / 'pending_wake' / name)[0], fixture)}"
        for name in _visible_names(plugin_dir / "pending_wake")
    ]
    leases = ["task-" + _label(name[len("task-") :], fixture) for name in _visible_names(target.state_dir / "leases")]
    words = ["active" if (target.state_dir / "active").exists() else "idle"]
    words.extend([f"turns={','.join(turns)}"] if turns else [])
    words.extend([f"subagents={','.join(subagents)}"] if subagents else [])
    words.extend([f"pending-wake={','.join(pending_wakes)}"] if pending_wakes else [])
    words.extend([f"leases={','.join(leases)}"] if leases else [])
    words.extend(["permissions-waiting"] if (target.state_dir / "permissions_waiting").exists() else [])
    return " ".join(words)


def _step_index(fixture: _HookFixture, description: str) -> int:
    return next(i for i, step in enumerate(fixture.steps) if _describe_step(step, fixture) == description)


def _replay(target: _HookTarget, fixture: _HookFixture, steps: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
    observed: list[tuple[str, str]] = []
    for step in steps:
        _run_step(target, step)
        observed.append((_describe_step(step, fixture), _describe_marker_state(target, fixture)))
    return observed


def _count_active_flips(states: Sequence[str]) -> int:
    activity = ["idle"] + [state.split(" ")[0] for state in states]
    return sum(1 for previous, current in zip(activity, activity[1:], strict=False) if previous != current)


def _activity_events(target: _HookTarget) -> list[dict[str, Any]]:
    return read_complete_jsonl_records(target.host_dir / "events" / "mngr" / "activity" / "events.jsonl")


def _hook_log_lines(target: _HookTarget) -> list[str]:
    log_path = target.state_dir / "logs" / "grok_hooks.log"
    return log_path.read_text().splitlines() if log_path.exists() else []


def _steps_without_turn_completed_calls(fixture: _HookFixture) -> list[dict[str, Any]]:
    return [step for step in fixture.steps if step["kind"] != "turn_completed"]


def _steps_in_order(fixture: _HookFixture, step_indices: Sequence[int]) -> list[dict[str, Any]]:
    return [fixture.steps[step_idx] for step_idx in step_indices]


def _payload_of(fixture: _HookFixture, description: str) -> dict[str, Any]:
    for step in fixture.steps:
        if step["kind"] == "hook" and _describe_step(step, fixture) == description:
            return dict(step["payload"])
    raise AssertionError(f"no step {description!r} in the fixture")


def _id_of(fixture: _HookFixture, label: str) -> str:
    return next(value for value, value_label in fixture.label_by_id.items() if value_label == label)


def _without_keys(payload: Mapping[str, Any], removed_keys: Sequence[str]) -> dict[str, Any]:
    return {name: value for name, value in payload.items() if name not in removed_keys}


_SESSION_ID_KEYS: Final = ("sessionId", "session_id")
_PROMPT_ID_KEYS: Final = ("promptId", "prompt_id")
_FROM_A_SUBAGENT: Final = {"subagentType": "general-purpose", "subagent_type": "general-purpose"}


def _as_subagent_payload(payload: Mapping[str, Any], subagent_id: str) -> dict[str, Any]:
    """The payload as the subagent session `subagent_id` sends it."""
    return {**payload, "sessionId": subagent_id, "session_id": subagent_id, **_FROM_A_SUBAGENT}


# Replays of the captured sequences, with the streamer's --turn-completed call where grok wrote turn_completed.

_PLAIN_EXPECTED: Final = (
    ("R session_start new", "idle"),
    ("R user_prompt_submit p1", "active turns=R:p1"),
    ("R stop end_turn p1", "active turns=R:p1"),
    ("R --turn-completed p1", "idle"),
    ("R notification idle_prompt", "idle"),
    ("R session_end shutdown", "idle"),
    ("R stop shutdown", "idle"),
)

_TOOL_EXPECTED: Final = (
    ("R session_start new", "idle"),
    ("R user_prompt_submit p1", "active turns=R:p1"),
    ("R pre_tool_use run_terminal_command", "active turns=R:p1"),
    ("R post_tool_use run_terminal_command", "active turns=R:p1"),
    ("R stop end_turn p1", "active turns=R:p1"),
    ("R --turn-completed p1", "idle"),
    ("R session_end shutdown", "idle"),
    ("R stop shutdown", "idle"),
)

_FG_SUBAGENT_EXPECTED: Final = (
    ("R session_start new", "idle"),
    ("R user_prompt_submit p1", "active turns=R:p1"),
    ("R stop end_turn p1", "active turns=R:p1"),
    ("R --turn-completed p1", "idle"),
    ("R notification idle_prompt", "idle"),
    ("R user_prompt_submit p2", "active turns=R:p2"),
    ("R pre_tool_use run_terminal_command", "active turns=R:p2"),
    ("R post_tool_use run_terminal_command", "active turns=R:p2"),
    ("R stop end_turn p2", "active turns=R:p2"),
    ("R --turn-completed p2", "idle"),
    ("R user_prompt_submit p3", "active turns=R:p3"),
    ("R pre_tool_use run_terminal_command", "active turns=R:p3"),
    ("R post_tool_use run_terminal_command", "active turns=R:p3"),
    ("R stop end_turn p3", "active turns=R:p3"),
    ("R --turn-completed p3", "idle"),
    ("R user_prompt_submit p4", "active turns=R:p4"),
    ("R pre_tool_use spawn_subagent", "active turns=R:p4"),
    ("R subagent_start C1", "active turns=R:p4 subagents=C1"),
    ("C1 user_prompt_submit C1.p1", "active turns=R:p4 subagents=C1"),
    ("C1 subagent_stop C1.p1", "active turns=R:p4 subagents=C1"),
    ("C1 --turn-completed C1.p1", "active turns=R:p4 subagents=C1"),
    ("C1 session_end shutdown", "active turns=R:p4"),
    ("R post_tool_use spawn_subagent", "active turns=R:p4"),
    ("R stop end_turn p4", "active turns=R:p4"),
    ("R --turn-completed p4", "idle"),
    ("R session_end shutdown", "idle"),
    ("R stop shutdown", "idle"),
    ("R session_start load", "idle"),
    ("R user_prompt_submit p5", "active turns=R:p5"),
    ("R stop end_turn p5", "active turns=R:p5"),
    ("R --turn-completed p5", "idle"),
    ("R session_end shutdown", "idle"),
    ("R stop shutdown", "idle"),
)

_BGSUBAGENT_EXPECTED: Final = (
    ("R session_start new", "idle"),
    ("R user_prompt_submit p1", "active turns=R:p1"),
    ("R pre_tool_use spawn_subagent", "active turns=R:p1"),
    ("R post_tool_use spawn_subagent", "active turns=R:p1"),
    ("R subagent_start C1", "active turns=R:p1 subagents=C1"),
    ("C1 user_prompt_submit C1.p1", "active turns=R:p1 subagents=C1"),
    ("R stop end_turn p1", "active turns=R:p1 subagents=C1(bg) leases=task-C1"),
    ("R --turn-completed p1", "idle subagents=C1(bg) leases=task-C1"),
    ("R notification idle_prompt", "idle subagents=C1(bg) leases=task-C1"),
    ("C1 subagent_stop C1.p1", "idle subagents=C1(bg) leases=task-C1"),
    ("C1 --turn-completed C1.p1", "idle subagents=C1(bg) leases=task-C1"),
    ("C1 session_end shutdown", "active turns=R:pending-wake:C1"),
    ("R user_prompt_submit wake-C1", "active turns=R:wake-C1"),
    ("R stop end_turn wake-C1", "active turns=R:wake-C1"),
    ("R --turn-completed wake-C1", "idle"),
    ("R notification idle_prompt", "idle"),
    ("R session_end shutdown", "idle"),
    ("R stop shutdown", "idle"),
)

_BGSUBAGENT_BUSY_ROOT_EXPECTED: Final = (
    ("R session_start new", "idle"),
    ("R user_prompt_submit p1", "active turns=R:p1"),
    ("R pre_tool_use spawn_subagent", "active turns=R:p1"),
    ("R post_tool_use spawn_subagent", "active turns=R:p1"),
    ("R subagent_start C1", "active turns=R:p1 subagents=C1"),
    ("C1 user_prompt_submit C1.p1", "active turns=R:p1 subagents=C1"),
    ("R stop end_turn p1", "active turns=R:p1 subagents=C1(bg) leases=task-C1"),
    ("R --turn-completed p1", "idle subagents=C1(bg) leases=task-C1"),
    ("R user_prompt_submit p2", "active turns=R:p2 subagents=C1(bg) leases=task-C1"),
    ("C1 subagent_stop C1.p1", "active turns=R:p2 subagents=C1(bg) leases=task-C1"),
    ("C1 --turn-completed C1.p1", "active turns=R:p2 subagents=C1(bg) leases=task-C1"),
    ("C1 session_end shutdown", "active turns=R:p2 pending-wake=R<C1"),
    ("R stop end_turn p2", "active turns=R:p2 pending-wake=R<C1"),
    ("R --turn-completed p2", "active turns=R:pending-wake:C1 pending-wake=R<C1"),
    ("R user_prompt_submit wake-C1", "active turns=R:wake-C1"),
    ("R stop end_turn wake-C1", "active turns=R:wake-C1"),
    ("R --turn-completed wake-C1", "idle"),
    ("R session_end shutdown", "idle"),
    ("R stop shutdown", "idle"),
)

_BGSHELL_EXPECTED: Final = (
    ("R session_start new", "idle"),
    ("R user_prompt_submit p1", "active turns=R:p1"),
    ("R pre_tool_use run_terminal_command", "active turns=R:p1"),
    ("R post_tool_use run_terminal_command", "active turns=R:p1"),
    ("R stop end_turn p1", "active turns=R:p1 leases=task-01a156da-5fa8-7e01-bed9-b3e2112a053a"),
    ("R --turn-completed p1", "idle leases=task-01a156da-5fa8-7e01-bed9-b3e2112a053a"),
    ("R notification idle_prompt", "idle leases=task-01a156da-5fa8-7e01-bed9-b3e2112a053a"),
    ("R notification task_complete", "idle leases=task-01a156da-5fa8-7e01-bed9-b3e2112a053a"),
    ("R user_prompt_submit wake-T1", "active turns=R:wake-T1 leases=task-01a156da-5fa8-7e01-bed9-b3e2112a053a"),
    ("R stop end_turn wake-T1", "active turns=R:wake-T1"),
    ("R --turn-completed wake-T1", "idle"),
    ("R notification idle_prompt", "idle"),
    ("R session_end shutdown", "idle"),
    ("R stop shutdown", "idle"),
)

_INTERRUPT_EXPECTED: Final = (
    ("R session_start new", "idle"),
    ("R user_prompt_submit p1", "active turns=R:p1"),
    ("R --turn-completed p1", "idle"),
    ("R stop_cancelled p1", "idle"),
    ("R user_prompt_submit p2", "active turns=R:p2"),
    ("R pre_tool_use run_terminal_command", "active turns=R:p2"),
    ("R --turn-completed p2", "idle"),
    ("R stop_cancelled p2", "idle"),
    ("R user_prompt_submit p3", "active turns=R:p3"),
    ("R stop end_turn p3", "active turns=R:p3"),
    ("R --turn-completed p3", "idle"),
    ("R session_end shutdown", "idle"),
    ("R stop shutdown", "idle"),
)

_SEND_NOW_EXPECTED: Final = (
    ("R session_start new", "idle"),
    ("R user_prompt_submit p1", "active turns=R:p1"),
    ("R --turn-completed p1", "idle"),
    ("R user_prompt_submit p2", "active turns=R:p2"),
    ("R stop end_turn p2", "active turns=R:p2"),
    ("R --turn-completed p2", "idle"),
    ("R session_end shutdown", "idle"),
    ("R stop shutdown", "idle"),
)

_STOP_BLOCK_EXPECTED: Final = (
    ("R session_start new", "idle"),
    ("R user_prompt_submit p1", "active turns=R:p1"),
    ("R stop end_turn p1", "active turns=R:p1"),
    ("R stop end_turn p1 again", "active turns=R:p1"),
    ("R --turn-completed p1", "idle"),
    ("R user_prompt_submit p2", "active turns=R:p2"),
    ("R pre_tool_use run_terminal_command", "active turns=R:p2"),
    ("R post_tool_use run_terminal_command", "active turns=R:p2"),
    ("R stop end_turn p2", "active turns=R:p2 leases=task-01a156df-7eed-7d61-b833-461a1546cea2"),
    ("R stop end_turn p2 again", "active turns=R:p2 leases=task-01a156df-7eed-7d61-b833-461a1546cea2"),
    ("R --turn-completed p2", "idle leases=task-01a156df-7eed-7d61-b833-461a1546cea2"),
    ("R notification task_complete", "idle leases=task-01a156df-7eed-7d61-b833-461a1546cea2"),
    ("R user_prompt_submit wake-T1", "active turns=R:wake-T1 leases=task-01a156df-7eed-7d61-b833-461a1546cea2"),
    ("R stop end_turn wake-T1", "active turns=R:wake-T1"),
    ("R stop end_turn wake-T1 again", "active turns=R:wake-T1"),
    ("R --turn-completed wake-T1", "idle"),
    ("R session_end shutdown", "idle"),
    ("R stop shutdown", "idle"),
)

_IDLE_PROMPT_EXPECTED: Final = (
    ("R session_start new", "idle"),
    ("R user_prompt_submit p1", "active turns=R:p1"),
    ("R stop end_turn p1", "active turns=R:p1"),
    ("R --turn-completed p1", "idle"),
    ("R notification idle_prompt", "idle"),
    ("R user_prompt_submit p2", "active turns=R:p2"),
    ("R stop end_turn p2", "active turns=R:p2"),
    ("R --turn-completed p2", "idle"),
    ("R user_prompt_submit p3", "active turns=R:p3"),
    ("R stop end_turn p3", "active turns=R:p3"),
    ("R --turn-completed p3", "idle"),
    ("R notification idle_prompt", "idle"),
    ("R user_prompt_submit p4", "active turns=R:p4"),
    ("R --turn-completed p4", "idle"),
    ("R stop_cancelled p4", "idle"),
    ("R notification idle_prompt", "idle"),
    ("R session_end shutdown", "idle"),
    ("R stop shutdown", "idle"),
)

_SILENT_WAKE_EXHAUST_EXPECTED: Final = (
    ("R session_start new", "idle"),
    ("R user_prompt_submit p1", "active turns=R:p1"),
    ("R pre_tool_use run_terminal_command", "active turns=R:p1"),
    ("R post_tool_use run_terminal_command", "active turns=R:p1"),
    ("R stop end_turn p1", "active turns=R:p1 leases=task-01a156de-a2e8-7f11-9db4-63f72b8f05ac"),
    ("R --turn-completed p1", "idle leases=task-01a156de-a2e8-7f11-9db4-63f72b8f05ac"),
    ("R notification task_complete", "idle leases=task-01a156de-a2e8-7f11-9db4-63f72b8f05ac"),
    ("R user_prompt_submit wake-T1", "active turns=R:wake-T1 leases=task-01a156de-a2e8-7f11-9db4-63f72b8f05ac"),
    ("R notification agent_error", "active turns=R:wake-T1 leases=task-01a156de-a2e8-7f11-9db4-63f72b8f05ac"),
    ("R stop_failure wake-T1", "active turns=R:wake-T1 leases=task-01a156de-a2e8-7f11-9db4-63f72b8f05ac"),
    ("R --turn-completed wake-T1", "idle leases=task-01a156de-a2e8-7f11-9db4-63f72b8f05ac"),
    ("R session_end shutdown", "idle"),
    ("R stop shutdown", "idle"),
)

_NEW_SESSION_EXPECTED: Final = (
    ("R session_start new", "idle"),
    ("R user_prompt_submit p1", "active turns=R:p1"),
    ("R stop end_turn p1", "active turns=R:p1"),
    ("R --turn-completed p1", "idle"),
    ("R2 session_start new", "idle"),
    ("R2 user_prompt_submit p2", "active turns=R2:p2"),
    ("R2 stop end_turn p2", "active turns=R2:p2"),
    ("R2 --turn-completed p2", "idle"),
    ("R session_end shutdown", "idle"),
    ("R2 session_end shutdown", "idle"),
    ("R2 stop shutdown", "idle"),
    ("R stop shutdown", "idle"),
    ("R session_start load", "idle"),
    ("R user_prompt_submit p3", "active turns=R:p3"),
    ("R stop end_turn p3", "active turns=R:p3"),
    ("R --turn-completed p3", "idle"),
    ("R session_end shutdown", "idle"),
    ("R stop shutdown", "idle"),
)

_UNKNOWN_SESSION_EXPECTED: Final = (
    ("R session_end shutdown", "idle"),
    ("R stop shutdown", "idle"),
)


@pytest.mark.parametrize(
    ("fixture_name", "expected"),
    [
        pytest.param("plain", _PLAIN_EXPECTED, id="plain"),
        pytest.param("tool", _TOOL_EXPECTED, id="tool"),
        pytest.param("fg_subagent", _FG_SUBAGENT_EXPECTED, id="foreground_subagent"),
        pytest.param("bgsubagent", _BGSUBAGENT_EXPECTED, id="background_subagent_idle_parent"),
        pytest.param("bgsubagent_busy_root", _BGSUBAGENT_BUSY_ROOT_EXPECTED, id="background_subagent_busy_parent"),
        pytest.param("bgshell", _BGSHELL_EXPECTED, id="background_shell"),
        pytest.param("interrupt", _INTERRUPT_EXPECTED, id="interrupt"),
        pytest.param("send_now", _SEND_NOW_EXPECTED, id="send_now"),
        pytest.param("stop_block", _STOP_BLOCK_EXPECTED, id="blocking_stop"),
        pytest.param("idle_prompt", _IDLE_PROMPT_EXPECTED, id="idle_prompt"),
        pytest.param("silent_wake_exhaust", _SILENT_WAKE_EXHAUST_EXPECTED, id="failed_wake_turn"),
        pytest.param("new_session", _NEW_SESSION_EXPECTED, id="new_session"),
        pytest.param("unknown_session", _UNKNOWN_SESSION_EXPECTED, id="unknown_session"),
    ],
)
def test_replayed_capture_leaves_the_expected_markers_after_every_event(
    fixture_name: str, expected: Sequence[tuple[str, str]], temp_host_dir: Path
) -> None:
    fixture = _load_fixture(fixture_name)
    target = _make_target(temp_host_dir)

    observed = _replay(target, fixture, fixture.steps)

    assert observed == list(expected)
    assert len(_activity_events(target)) == _count_active_flips([state for _, state in observed])
    assert len(_hook_log_lines(target)) == len(fixture.steps)


# Replays with the transcript streamer dead: only the hooks' backstops end turns.

_DEAD_STREAMER_PLAIN_EXPECTED: Final = (
    ("R session_start new", "idle"),
    ("R user_prompt_submit p1", "active turns=R:p1"),
    ("R stop end_turn p1", "active turns=R:p1"),
    ("R notification idle_prompt", "idle"),
    ("R session_end shutdown", "idle"),
    ("R stop shutdown", "idle"),
)

_DEAD_STREAMER_BGSHELL_EXPECTED: Final = (
    ("R session_start new", "idle"),
    ("R user_prompt_submit p1", "active turns=R:p1"),
    ("R pre_tool_use run_terminal_command", "active turns=R:p1"),
    ("R post_tool_use run_terminal_command", "active turns=R:p1"),
    ("R stop end_turn p1", "active turns=R:p1 leases=task-01a156da-5fa8-7e01-bed9-b3e2112a053a"),
    ("R notification idle_prompt", "idle leases=task-01a156da-5fa8-7e01-bed9-b3e2112a053a"),
    ("R notification task_complete", "idle leases=task-01a156da-5fa8-7e01-bed9-b3e2112a053a"),
    ("R user_prompt_submit wake-T1", "active turns=R:wake-T1 leases=task-01a156da-5fa8-7e01-bed9-b3e2112a053a"),
    ("R stop end_turn wake-T1", "active turns=R:wake-T1"),
    ("R notification idle_prompt", "idle"),
    ("R session_end shutdown", "idle"),
    ("R stop shutdown", "idle"),
)

_DEAD_STREAMER_INTERRUPT_EXPECTED: Final = (
    ("R session_start new", "idle"),
    ("R user_prompt_submit p1", "active turns=R:p1"),
    ("R stop_cancelled p1", "idle"),
    ("R user_prompt_submit p2", "active turns=R:p2"),
    ("R pre_tool_use run_terminal_command", "active turns=R:p2"),
    ("R stop_cancelled p2", "idle"),
    ("R user_prompt_submit p3", "active turns=R:p3"),
    ("R stop end_turn p3", "active turns=R:p3"),
    ("R session_end shutdown", "idle"),
    ("R stop shutdown", "idle"),
)

_DEAD_STREAMER_BGSUBAGENT_EXPECTED: Final = (
    ("R session_start new", "idle"),
    ("R user_prompt_submit p1", "active turns=R:p1"),
    ("R pre_tool_use spawn_subagent", "active turns=R:p1"),
    ("R post_tool_use spawn_subagent", "active turns=R:p1"),
    ("R subagent_start C1", "active turns=R:p1 subagents=C1"),
    ("C1 user_prompt_submit C1.p1", "active turns=R:p1 subagents=C1"),
    ("R stop end_turn p1", "active turns=R:p1 subagents=C1(bg) leases=task-C1"),
    ("R notification idle_prompt", "idle subagents=C1(bg) leases=task-C1"),
    ("C1 subagent_stop C1.p1", "idle subagents=C1(bg) leases=task-C1"),
    ("C1 session_end shutdown", "active turns=R:pending-wake:C1"),
    ("R user_prompt_submit wake-C1", "active turns=R:wake-C1"),
    ("R stop end_turn wake-C1", "active turns=R:wake-C1"),
    ("R notification idle_prompt", "idle"),
    ("R session_end shutdown", "idle"),
    ("R stop shutdown", "idle"),
)


@pytest.mark.parametrize(
    ("fixture_name", "expected"),
    [
        pytest.param("plain", _DEAD_STREAMER_PLAIN_EXPECTED, id="plain"),
        pytest.param("bgshell", _DEAD_STREAMER_BGSHELL_EXPECTED, id="background_shell"),
        pytest.param("interrupt", _DEAD_STREAMER_INTERRUPT_EXPECTED, id="interrupt"),
        pytest.param("bgsubagent", _DEAD_STREAMER_BGSUBAGENT_EXPECTED, id="background_subagent"),
    ],
)
def test_dead_streamer_turns_end_through_the_hook_backstops(
    fixture_name: str, expected: Sequence[tuple[str, str]], temp_host_dir: Path
) -> None:
    fixture = _load_fixture(fixture_name)
    target = _make_target(temp_host_dir)

    observed = _replay(target, fixture, _steps_without_turn_completed_calls(fixture))

    assert observed == list(expected)


def test_dead_streamer_failed_turn_runs_until_the_idle_prompt_backstop(temp_host_dir: Path) -> None:
    fixture = _load_fixture("silent_wake_exhaust")
    target = _make_target(temp_host_dir)
    agent_error = _payload_of(fixture, "R notification agent_error")
    idle_prompt = {**agent_error, "notificationType": "idle_prompt", "notification_type": "idle_prompt"}
    steps = [
        _hook_step(_payload_of(fixture, "R session_start new")),
        _hook_step(_payload_of(fixture, "R user_prompt_submit wake-T1")),
        _hook_step(agent_error),
        _hook_step(_payload_of(fixture, "R stop_failure wake-T1")),
        _hook_step(idle_prompt),
    ]

    observed = [state for _, state in _replay(target, fixture, steps)]

    assert observed == [
        "idle",
        "active turns=R:wake-T1",
        "active turns=R:wake-T1",
        "active turns=R:wake-T1",
        "idle",
    ]
    assert _hook_log_lines(target)[-1].endswith(" backstop:cleared active:off")


def test_a_turn_grok_drops_without_a_report_ends_at_the_idle_prompt(temp_host_dir: Path) -> None:
    fixture = _load_fixture("idle_prompt")
    target = _make_target(temp_host_dir)
    dropped_turn_steps = _steps_in_order(fixture, [0, 1, 2, 3, 5, 4])

    observed = _replay(target, fixture, dropped_turn_steps)

    assert observed == [
        ("R session_start new", "idle"),
        ("R user_prompt_submit p1", "active turns=R:p1"),
        ("R stop end_turn p1", "active turns=R:p1"),
        ("R --turn-completed p1", "idle"),
        ("R user_prompt_submit p2", "active turns=R:p2"),
        ("R notification idle_prompt", "idle"),
    ]
    assert _hook_log_lines(target)[-1].endswith(" backstop:cleared active:off")


def test_turn_dropped_ends_the_session_s_recorded_turn_and_ignores_any_other(temp_host_dir: Path) -> None:
    fixture = _load_fixture("idle_prompt")
    target = _make_target(temp_host_dir)
    _replay(target, fixture, _steps_in_order(fixture, [0, 1, 2, 3, 5]))
    root_id = _id_of(fixture, "R")
    first_prompt_id = _id_of(fixture, "p1")
    recorded_prompt_id = _id_of(fixture, "p2")

    _run_handler(target, [grok_hook.TURN_DROPPED_FLAG, root_id, first_prompt_id], "", {})
    state_after_dropping_another_turn = _describe_marker_state(target, fixture)
    _run_handler(target, [grok_hook.TURN_DROPPED_FLAG, root_id, recorded_prompt_id], "", {})

    assert state_after_dropping_another_turn == "active turns=R:p2"
    assert _describe_marker_state(target, fixture) == "idle"
    assert _hook_log_lines(target)[-2].endswith(
        f" event=--turn-dropped session={root_id} prompt={first_prompt_id} ignored:not-the-current-turn"
    )
    assert _hook_log_lines(target)[-1].endswith(
        f" event=--turn-dropped session={root_id} prompt={recorded_prompt_id} turn:end active:off"
    )


# Replays with the streamer's --turn-completed call arriving after grok's next hooks.


def test_idle_prompt_landing_after_the_next_prompt_s_submission_reads_that_turn_as_waiting(
    temp_host_dir: Path,
) -> None:
    fixture = _load_fixture("idle_prompt")
    target = _make_target(temp_host_dir)
    raced_steps = _steps_in_order(fixture, [0, 1, 2, 5, 4, 3, 6, 7])

    observed = _replay(target, fixture, raced_steps)

    assert observed == [
        ("R session_start new", "idle"),
        ("R user_prompt_submit p1", "active turns=R:p1"),
        ("R stop end_turn p1", "active turns=R:p1"),
        ("R user_prompt_submit p2", "active turns=R:p2"),
        ("R notification idle_prompt", "idle"),
        ("R --turn-completed p1", "idle"),
        ("R stop end_turn p2", "idle"),
        ("R --turn-completed p2", "idle"),
    ]
    assert len(_activity_events(target)) == 2


def test_late_turn_completed_after_send_now_keeps_the_queued_turn_running(temp_host_dir: Path) -> None:
    fixture = _load_fixture("send_now")
    target = _make_target(temp_host_dir)

    observed = _replay(target, fixture, _steps_in_order(fixture, [0, 1, 3, 2, 4, 5]))

    assert observed == [
        ("R session_start new", "idle"),
        ("R user_prompt_submit p1", "active turns=R:p1"),
        ("R user_prompt_submit p2", "active turns=R:p2"),
        ("R --turn-completed p1", "active turns=R:p2"),
        ("R stop end_turn p2", "active turns=R:p2"),
        ("R --turn-completed p2", "idle"),
    ]
    assert len(_activity_events(target)) == 2


def test_wake_turn_starting_before_the_late_turn_completed_consumes_the_pending_wake(temp_host_dir: Path) -> None:
    fixture = _load_fixture("bgsubagent_busy_root")
    target = _make_target(temp_host_dir)

    observed = _replay(target, fixture, _steps_in_order(fixture, [0, 1, 4, 6, 7, 8, 11, 12, 14, 13, 15, 16]))

    assert observed == [
        ("R session_start new", "idle"),
        ("R user_prompt_submit p1", "active turns=R:p1"),
        ("R subagent_start C1", "active turns=R:p1 subagents=C1"),
        ("R stop end_turn p1", "active turns=R:p1 subagents=C1(bg) leases=task-C1"),
        ("R --turn-completed p1", "idle subagents=C1(bg) leases=task-C1"),
        ("R user_prompt_submit p2", "active turns=R:p2 subagents=C1(bg) leases=task-C1"),
        ("C1 session_end shutdown", "active turns=R:p2 pending-wake=R<C1"),
        ("R stop end_turn p2", "active turns=R:p2 pending-wake=R<C1"),
        ("R user_prompt_submit wake-C1", "active turns=R:wake-C1"),
        ("R --turn-completed p2", "active turns=R:wake-C1"),
        ("R stop end_turn wake-C1", "active turns=R:wake-C1"),
        ("R --turn-completed wake-C1", "idle"),
    ]


# Subagent edge cases, built from captured payloads.


def test_stop_snapshot_prunes_a_lost_subagent_and_never_recreates_an_ended_one(temp_host_dir: Path) -> None:
    fixture = _load_fixture("bgsubagent")
    target = _make_target(temp_host_dir)
    root_id = _id_of(fixture, "R")
    stop_listing_child = _payload_of(fixture, "R stop end_turn p1")
    stop_with_empty_snapshot = {**stop_listing_child, "backgroundTasks": []}
    stale_snapshot_wake_stop = {
        **_payload_of(fixture, "R stop end_turn wake-C1"),
        "backgroundTasks": stop_listing_child["backgroundTasks"],
    }
    steps = [
        _hook_step(_payload_of(fixture, "R session_start new")),
        _hook_step(_payload_of(fixture, "R user_prompt_submit p1")),
        _hook_step(_payload_of(fixture, "R subagent_start C1")),
        _hook_step(stop_with_empty_snapshot),
        _hook_step(_payload_of(fixture, "R subagent_start C1")),
        _hook_step(stop_listing_child),
        _turn_completed_step(root_id, _id_of(fixture, "p1")),
        _hook_step(_payload_of(fixture, "C1 session_end shutdown")),
        _hook_step(_payload_of(fixture, "R user_prompt_submit wake-C1")),
        _hook_step(stale_snapshot_wake_stop),
        _turn_completed_step(root_id, _id_of(fixture, "wake-C1")),
    ]

    observed = [state for _, state in _replay(target, fixture, steps)]

    assert observed == [
        "idle",
        "active turns=R:p1",
        "active turns=R:p1 subagents=C1",
        "active turns=R:p1",
        "active turns=R:p1 subagents=C1",
        "active turns=R:p1 subagents=C1(bg) leases=task-C1",
        "idle subagents=C1(bg) leases=task-C1",
        "active turns=R:pending-wake:C1",
        "active turns=R:wake-C1",
        "active turns=R:wake-C1 leases=task-C1",
        "idle leases=task-C1",
    ]


def test_cancelled_foreground_subagent_of_a_cancelled_turn_leaves_no_placeholder(temp_host_dir: Path) -> None:
    fixture = _load_fixture("fg_subagent")
    target = _make_target(temp_host_dir)
    child_session_end = _payload_of(fixture, "C1 session_end shutdown")
    child_stop_cancelled = {
        **child_session_end,
        "hookEventName": "stop_cancelled",
        "hook_event_name": "StopCancelled",
        "reason": "user_interrupt",
    }
    steps = [
        _hook_step(_payload_of(fixture, "R session_start new")),
        _hook_step(_payload_of(fixture, "R user_prompt_submit p4")),
        _hook_step(_payload_of(fixture, "R subagent_start C1")),
        _turn_completed_step(_id_of(fixture, "R"), _id_of(fixture, "p4")),
        _hook_step(child_stop_cancelled),
        _hook_step(child_session_end),
    ]

    observed = [state for _, state in _replay(target, fixture, steps)]

    assert observed == [
        "idle",
        "active turns=R:p4",
        "active turns=R:p4 subagents=C1",
        "active subagents=C1",
        "idle",
        "idle",
    ]


def test_nested_subagent_ending_never_writes_a_turn_for_its_subagent_parent(temp_host_dir: Path) -> None:
    fixture = _load_fixture("bgsubagent")
    target = _make_target(temp_host_dir)
    child_id = _id_of(fixture, "C1")
    grandchild_id = f"grandchild-{uuid4().hex}"
    grandchild_start = {
        **_payload_of(fixture, "R subagent_start C1"),
        "sessionId": child_id,
        "subagentId": grandchild_id,
    }
    grandchild_end = {**_payload_of(fixture, "C1 session_end shutdown"), "sessionId": grandchild_id}
    steps = [
        _hook_step(_payload_of(fixture, "R session_start new")),
        _hook_step(_payload_of(fixture, "R user_prompt_submit p1")),
        _hook_step(_payload_of(fixture, "R subagent_start C1")),
        _hook_step(grandchild_start),
        _hook_step(grandchild_end),
    ]

    observed = [state for _, state in _replay(target, fixture, steps)]

    assert observed == [
        "idle",
        "active turns=R:p1",
        "active turns=R:p1 subagents=C1",
        f"active turns=R:p1 subagents=C1,{grandchild_id}",
        "active turns=R:p1 subagents=C1",
    ]


def test_root_session_end_drops_its_running_subagents(temp_host_dir: Path) -> None:
    fixture = _load_fixture("bgsubagent")
    target = _make_target(temp_host_dir)
    steps = [step for step in fixture.steps[:8]] + [_hook_step(_payload_of(fixture, "R session_end shutdown"))]

    observed = [state for _, state in _replay(target, fixture, steps)]

    assert observed[-2:] == ["idle subagents=C1(bg) leases=task-C1", "idle"]


def test_a_leased_grok_task_parses_the_way_mngr_reads_it(temp_host_dir: Path) -> None:
    """The lease written here and the reader in imbue.mngr are two halves of one format.

    grok_hook.py cannot import that reader: it runs from the agent's hook directory against the
    standard library alone. Nothing but this test holds the two sides together.
    """
    fixture = _load_fixture("bgshell")
    target = _make_target(temp_host_dir)
    through_the_turn_end = [
        step
        for step in fixture.steps[: 1 + _step_index(fixture, "R stop end_turn p1")]
        if step["kind"] != "turn_completed"
    ]

    _replay(target, fixture, through_the_turn_end)

    lease_dir = target.state_dir / "leases"
    lease_names = _visible_names(lease_dir)
    assert len(lease_names) == 1, lease_names
    first_line = (lease_dir / lease_names[0]).read_text().strip()
    leases = parse_work_leases(f"{lease_names[0]}\t{first_line}")
    assert [lease.name for lease in leases] == [lease_names[0]]
    assert leases[0].expires_at is not None
    assert leases[0].expires_at > datetime.now(timezone.utc)


def _lease_ttl_seconds_at_turn_end(temp_host_dir: Path) -> float:
    """How far ahead the hook dates the lease of the task the bgshell capture leaves running.

    Measured from this test's clock before the replay, and read back through the parser the
    probe uses.
    """
    started_at = datetime.now(timezone.utc)
    fixture = _load_fixture("bgshell")
    target = _make_target(temp_host_dir)
    through_the_turn_end = [
        step
        for step in fixture.steps[: 1 + _step_index(fixture, "R stop end_turn p1")]
        if step["kind"] != "turn_completed"
    ]

    _replay(target, fixture, through_the_turn_end)

    lease_dir = target.state_dir / "leases"
    lease_names = _visible_names(lease_dir)
    assert len(lease_names) == 1, lease_names
    leases = parse_work_leases(f"{lease_names[0]}\t{(lease_dir / lease_names[0]).read_text().strip()}")
    assert len(leases) == 1
    expires_at = leases[0].expires_at
    assert expires_at is not None
    return (expires_at - started_at).total_seconds()


def _assert_lease_ttl_is(observed_ttl_seconds: float, expected_ttl_seconds: int) -> None:
    """The hook dates a lease from its own clock, truncated to a whole second."""
    assert expected_ttl_seconds - 1 <= observed_ttl_seconds <= expected_ttl_seconds + _LEASE_TTL_SLACK_SECONDS, (
        observed_ttl_seconds
    )


def test_a_grok_lease_is_dated_by_the_default_ttl(temp_host_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bound an agent that sets nothing gets, which is the one mngr documents for every writer."""
    monkeypatch.delenv(WORK_LEASE_TTL_ENV_VAR, raising=False)

    _assert_lease_ttl_is(_lease_ttl_seconds_at_turn_end(temp_host_dir), DEFAULT_WORK_LEASE_TTL_SECONDS)


def test_a_grok_lease_is_dated_by_the_agents_own_ttl(temp_host_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent whose work is short can say so, rather than holding its host for the default.

    The hook reads the setting from the process it runs in, which is the agent's environment.
    """
    monkeypatch.setenv(WORK_LEASE_TTL_ENV_VAR, "600")

    _assert_lease_ttl_is(_lease_ttl_seconds_at_turn_end(temp_host_dir), 600)


@pytest.mark.parametrize("override", ["", " ", "0", "007", "4h", "-600", "1.5"])
def test_a_grok_lease_ignores_a_ttl_that_is_not_a_positive_count_of_seconds(
    temp_host_dir: Path, monkeypatch: pytest.MonkeyPatch, override: str
) -> None:
    """A lease dated by one of these would be expired on arrival, which reads as work that ended."""
    monkeypatch.setenv(WORK_LEASE_TTL_ENV_VAR, override)

    _assert_lease_ttl_is(_lease_ttl_seconds_at_turn_end(temp_host_dir), DEFAULT_WORK_LEASE_TTL_SECONDS)


def test_repeated_subagent_start_and_stop_listing_keep_one_background_subagent(temp_host_dir: Path) -> None:
    fixture = _load_fixture("bgsubagent")
    target = _make_target(temp_host_dir)
    stop_listing_child = _payload_of(fixture, "R stop end_turn p1")
    steps = [
        _hook_step(_payload_of(fixture, "R user_prompt_submit p1")),
        _hook_step(_payload_of(fixture, "R subagent_start C1")),
        _hook_step(_payload_of(fixture, "R subagent_start C1")),
        _hook_step(stop_listing_child),
        _hook_step({**stop_listing_child, "stopHookActive": True, "stop_hook_active": True}),
    ]

    observed = [state for _, state in _replay(target, fixture, steps)]

    assert observed == [
        "active turns=R:p1",
        "active turns=R:p1 subagents=C1",
        "active turns=R:p1 subagents=C1",
        "active turns=R:p1 subagents=C1(bg) leases=task-C1",
        "active turns=R:p1 subagents=C1(bg) leases=task-C1",
    ]
    log_lines = _hook_log_lines(target)
    assert log_lines[2].endswith(" subagent:known")
    assert log_lines[3].endswith(" stop subagents-pruned=0 subagents-marked-background=1 leases=1")
    assert log_lines[4].endswith(" stop subagents-pruned=0 subagents-marked-background=0 leases=1")


def _steps_leaving_two_background_subagents_running(
    fixture: _HookFixture, second_child_id: str
) -> list[dict[str, Any]]:
    """The bgsubagent capture's root turn, with a second background subagent beside C1, up to that turn's end."""
    stop_listing_child = _payload_of(fixture, "R stop end_turn p1")
    stop_listing_both_children = {
        **stop_listing_child,
        "backgroundTasks": [
            *stop_listing_child["backgroundTasks"],
            {**stop_listing_child["backgroundTasks"][0], "id": second_child_id},
        ],
    }
    return [
        _hook_step(_payload_of(fixture, "R session_start new")),
        _hook_step(_payload_of(fixture, "R user_prompt_submit p1")),
        _hook_step(_payload_of(fixture, "R subagent_start C1")),
        _hook_step({**_payload_of(fixture, "R subagent_start C1"), "subagentId": second_child_id}),
        _hook_step(stop_listing_both_children),
        _turn_completed_step(_id_of(fixture, "R"), _id_of(fixture, "p1")),
    ]


def test_a_second_background_subagent_ending_while_idle_replaces_the_wake_placeholder(temp_host_dir: Path) -> None:
    fixture = _load_fixture("bgsubagent")
    target = _make_target(temp_host_dir)
    second_child_id = f"second-child-{uuid4().hex}"
    steps = [
        *_steps_leaving_two_background_subagents_running(fixture, second_child_id),
        _hook_step(_payload_of(fixture, "C1 session_end shutdown")),
        _hook_step({**_payload_of(fixture, "C1 session_end shutdown"), "sessionId": second_child_id}),
    ]

    observed = [state for _, state in _replay(target, fixture, steps)]

    assert observed[-3:] == [
        f"idle subagents=C1(bg),{second_child_id}(bg) leases=task-C1,task-{second_child_id}",
        f"active turns=R:pending-wake:C1 subagents={second_child_id}(bg) leases=task-{second_child_id}",
        f"active turns=R:pending-wake:{second_child_id}",
    ]


def test_a_wake_prompt_that_beats_its_subagent_session_end_leaves_no_wake_pending(temp_host_dir: Path) -> None:
    fixture = _load_fixture("bgsubagent")
    target = _make_target(temp_host_dir)
    root_id = _id_of(fixture, "R")
    steps = [
        _hook_step(_payload_of(fixture, "R session_start new")),
        _hook_step(_payload_of(fixture, "R user_prompt_submit p1")),
        _hook_step(_payload_of(fixture, "R subagent_start C1")),
        _hook_step(_payload_of(fixture, "R stop end_turn p1")),
        _turn_completed_step(root_id, _id_of(fixture, "p1")),
        _hook_step(_payload_of(fixture, "R user_prompt_submit wake-C1")),
        _hook_step(_payload_of(fixture, "C1 session_end shutdown")),
        _turn_completed_step(root_id, _id_of(fixture, "wake-C1")),
    ]

    observed = [state for _, state in _replay(target, fixture, steps)]

    assert observed[-3:] == [
        "active turns=R:wake-C1 subagents=C1(bg) leases=task-C1",
        "active turns=R:wake-C1",
        "idle",
    ]
    assert " subagent:end wake:none-already-woken" in _hook_log_lines(target)[-2]


def test_a_turn_ending_into_a_pending_wake_clears_a_stranded_permissions_marker(temp_host_dir: Path) -> None:
    fixture = _load_fixture("bgsubagent")
    target = _make_target(temp_host_dir)
    late_child_notification = _as_subagent_payload(
        _notification_of_type(fixture, "permission_prompt"), _id_of(fixture, "C1")
    )
    steps = [
        _hook_step(_payload_of(fixture, "R session_start new")),
        _hook_step(_payload_of(fixture, "R user_prompt_submit p1")),
        _hook_step(_payload_of(fixture, "R subagent_start C1")),
        _hook_step(_payload_of(fixture, "R stop end_turn p1")),
        _hook_step(_payload_of(fixture, "C1 session_end shutdown")),
        _hook_step(late_child_notification),
        _turn_completed_step(_id_of(fixture, "R"), _id_of(fixture, "p1")),
    ]

    observed = [state for _, state in _replay(target, fixture, steps)]

    assert observed[-3:] == [
        "active turns=R:p1 pending-wake=R<C1",
        "active turns=R:p1 pending-wake=R<C1 permissions-waiting",
        "active turns=R:pending-wake:C1 pending-wake=R<C1",
    ]


def test_stop_snapshot_leaves_the_subagents_of_other_sessions_alone(temp_host_dir: Path) -> None:
    fixture = _load_fixture("bgsubagent")
    target = _make_target(temp_host_dir)
    other_root_id = f"other-root-{uuid4().hex}"
    other_root_child_start = {
        **_payload_of(fixture, "R subagent_start C1"),
        "sessionId": other_root_id,
        "session_id": other_root_id,
    }
    steps = [
        _hook_step(_payload_of(fixture, "R user_prompt_submit p1")),
        _hook_step(other_root_child_start),
        _hook_step({**_payload_of(fixture, "R stop end_turn p1"), "backgroundTasks": []}),
    ]

    observed = [state for _, state in _replay(target, fixture, steps)]

    assert observed[-1] == "active turns=R:p1 subagents=C1"
    assert _file_lines(_plugin_dir(target) / "subagents" / _id_of(fixture, "C1")) == [other_root_id]


# Hook-only rules and invocation modes.


def _notification_of_type(fixture: _HookFixture, notification_type: str) -> dict[str, Any]:
    idle_notification = _payload_of(fixture, "R notification idle_prompt")
    return {**idle_notification, "notificationType": notification_type, "notification_type": notification_type}


def test_permission_dialogs_set_permissions_waiting_until_the_tool_resolves(temp_host_dir: Path) -> None:
    fixture = _load_fixture("bgsubagent")
    target = _make_target(temp_host_dir)
    post_tool_use = _payload_of(fixture, "R post_tool_use spawn_subagent")
    permission_denied = {**post_tool_use, "hookEventName": "permission_denied", "hook_event_name": "PermissionDenied"}
    steps = [
        _hook_step(_payload_of(fixture, "R session_start new")),
        _hook_step(_payload_of(fixture, "R user_prompt_submit p1")),
        _hook_step(_notification_of_type(fixture, "permission_prompt")),
        _hook_step(post_tool_use),
        _hook_step(_notification_of_type(fixture, "elicitation_dialog")),
        _hook_step(permission_denied),
        _hook_step(_notification_of_type(fixture, "permission_prompt")),
        _hook_step(_payload_of(fixture, "R subagent_start C1")),
        _turn_completed_step(_id_of(fixture, "R"), _id_of(fixture, "p1")),
        _hook_step(_payload_of(fixture, "R session_end shutdown")),
    ]

    observed = [state for _, state in _replay(target, fixture, steps)]

    assert observed == [
        "idle",
        "active turns=R:p1",
        "active turns=R:p1 permissions-waiting",
        "active turns=R:p1",
        "active turns=R:p1 permissions-waiting",
        "active turns=R:p1",
        "active turns=R:p1 permissions-waiting",
        "active turns=R:p1 subagents=C1 permissions-waiting",
        "active subagents=C1",
        "idle",
    ]


def test_a_permission_dialog_is_cleared_only_by_its_own_session(temp_host_dir: Path) -> None:
    fixture = _load_fixture("bgsubagent")
    target = _make_target(temp_host_dir)
    root_post_tool_use = _payload_of(fixture, "R post_tool_use spawn_subagent")
    child_id = _id_of(fixture, "C1")
    child_post_tool_use = _as_subagent_payload(root_post_tool_use, child_id)
    for step in (
        _hook_step(_payload_of(fixture, "R session_start new")),
        _hook_step(_payload_of(fixture, "R user_prompt_submit p1")),
        _hook_step(_payload_of(fixture, "R subagent_start C1")),
        _hook_step(_notification_of_type(fixture, "permission_prompt")),
    ):
        _run_step(target, step)
    waiting_path = target.state_dir / "permissions_waiting"

    _run_step(target, _hook_step(child_post_tool_use))
    lines_after_child_activity = waiting_path.read_text().splitlines()
    _run_step(target, _hook_step(root_post_tool_use))

    assert lines_after_child_activity == [_id_of(fixture, "R")]
    assert not waiting_path.exists()
    assert [line.rsplit(" ", 1)[-1] for line in _hook_log_lines(target)[-2:]] == [
        "permissions:kept-other-session",
        "permissions:cleared",
    ]


def test_a_permission_dialog_of_each_session_is_cleared_by_that_session(temp_host_dir: Path) -> None:
    fixture = _load_fixture("bgsubagent")
    target = _make_target(temp_host_dir)
    child_id = _id_of(fixture, "C1")
    root_notification = _notification_of_type(fixture, "permission_prompt")
    child_notification = _as_subagent_payload(root_notification, child_id)
    child_post_tool_use = _as_subagent_payload(_payload_of(fixture, "R post_tool_use spawn_subagent"), child_id)
    for step in (
        _hook_step(_payload_of(fixture, "R user_prompt_submit p1")),
        _hook_step(root_notification),
        _hook_step(child_notification),
        _hook_step(child_notification),
    ):
        _run_step(target, step)
    waiting_path = target.state_dir / "permissions_waiting"
    lines_with_both_open = waiting_path.read_text().splitlines()

    _run_step(target, _hook_step(child_post_tool_use))

    assert lines_with_both_open == [_id_of(fixture, "R"), child_id]
    assert waiting_path.read_text().splitlines() == [_id_of(fixture, "R")]


@pytest.mark.parametrize(
    ("ending_event_name", "ending_event_pascal_case_name", "is_wake_expected"),
    [
        pytest.param("session_end", "SessionEnd", True, id="session_end"),
        pytest.param("stop_cancelled", "StopCancelled", False, id="stop_cancelled"),
        pytest.param("stop_failure", "StopFailure", False, id="stop_failure"),
    ],
)
def test_a_subagent_ending_clears_its_own_permission_dialog_while_other_work_runs(
    ending_event_name: str, ending_event_pascal_case_name: str, is_wake_expected: bool, temp_host_dir: Path
) -> None:
    fixture = _load_fixture("bgsubagent")
    target = _make_target(temp_host_dir)
    child_id = _id_of(fixture, "C1")
    second_child_id = f"second-child-{uuid4().hex}"
    child_notification = _as_subagent_payload(_notification_of_type(fixture, "permission_prompt"), child_id)
    child_ending = {
        **_payload_of(fixture, "C1 session_end shutdown"),
        "hookEventName": ending_event_name,
        "hook_event_name": ending_event_pascal_case_name,
    }
    steps = [
        *_steps_leaving_two_background_subagents_running(fixture, second_child_id),
        _hook_step(child_notification),
        _hook_step(child_ending),
    ]

    observed = [state for _, state in _replay(target, fixture, steps)]

    activity = "active turns=R:pending-wake:C1" if is_wake_expected else "idle"
    assert observed[-2:] == [
        f"idle subagents=C1(bg),{second_child_id}(bg) leases=task-C1,task-{second_child_id} permissions-waiting",
        f"{activity} subagents={second_child_id}(bg) leases=task-{second_child_id}",
    ]


@pytest.mark.parametrize(
    ("is_cleared_by_the_turn_end", "expected_state"),
    [
        pytest.param(
            False, "active turns=R:p2 subagents=C1(bg) leases=task-C1 permissions-waiting", id="next_root_prompt"
        ),
        pytest.param(True, "idle subagents=C1(bg) leases=task-C1 permissions-waiting", id="root_turn_end"),
    ],
)
def test_a_root_prompt_or_turn_end_clears_only_the_root_permission_dialog(
    is_cleared_by_the_turn_end: bool, expected_state: str, temp_host_dir: Path
) -> None:
    fixture = _load_fixture("bgsubagent_busy_root")
    target = _make_target(temp_host_dir)
    root_id = _id_of(fixture, "R")
    child_id = _id_of(fixture, "C1")
    root_notification = {
        "hookEventName": "notification",
        "sessionId": root_id,
        "notificationType": "permission_prompt",
    }
    child_notification = _as_subagent_payload(root_notification, child_id)
    clearing_step = (
        _turn_completed_step(root_id, _id_of(fixture, "p1"))
        if is_cleared_by_the_turn_end
        else _hook_step(_payload_of(fixture, "R user_prompt_submit p2"))
    )
    for step in (
        _hook_step(_payload_of(fixture, "R user_prompt_submit p1")),
        _hook_step(_payload_of(fixture, "R subagent_start C1")),
        _hook_step(root_notification),
        _hook_step(child_notification),
        _hook_step(_payload_of(fixture, "R stop end_turn p1")),
        clearing_step,
    ):
        _run_step(target, step)

    assert (target.state_dir / "permissions_waiting").read_text().splitlines() == [child_id]
    assert _describe_marker_state(target, fixture) == expected_state


def test_stray_permissions_waiting_without_work_is_cleared_by_the_next_recompute(temp_host_dir: Path) -> None:
    fixture = _load_fixture("plain")
    target = _make_target(temp_host_dir)
    steps = [
        _hook_step(_notification_of_type(fixture, "permission_prompt")),
        _hook_step(_payload_of(fixture, "R notification idle_prompt")),
    ]

    observed = [state for _, state in _replay(target, fixture, steps)]

    assert observed == ["idle permissions-waiting", "idle"]


def test_a_permission_dialog_without_a_session_id_is_cleared_by_any_session(temp_host_dir: Path) -> None:
    fixture = _load_fixture("bgsubagent")
    target = _make_target(temp_host_dir)
    waiting_path = target.state_dir / "permissions_waiting"
    _run_step(target, _hook_step(_payload_of(fixture, "R user_prompt_submit p1")))
    _run_step(target, _hook_step(_without_keys(_notification_of_type(fixture, "permission_prompt"), _SESSION_ID_KEYS)))
    marker_text_with_the_dialog_open = waiting_path.read_text()

    _run_step(target, _hook_step(_payload_of(fixture, "R post_tool_use spawn_subagent")))

    assert marker_text_with_the_dialog_open == ""
    assert not waiting_path.exists()
    assert _hook_log_lines(target)[-1].endswith(" permissions:cleared")


@pytest.mark.parametrize(
    ("resolving_event_name", "resolving_event_pascal_case_name"),
    [
        pytest.param("post_tool_use", "PostToolUse", id="post_tool_use"),
        pytest.param("permission_denied", "PermissionDenied", id="permission_denied"),
    ],
)
def test_a_resolved_tool_without_a_session_id_clears_every_session_s_permission_dialog(
    resolving_event_name: str, resolving_event_pascal_case_name: str, temp_host_dir: Path
) -> None:
    fixture = _load_fixture("bgsubagent")
    target = _make_target(temp_host_dir)
    root_notification = _notification_of_type(fixture, "permission_prompt")
    child_id = _id_of(fixture, "C1")
    resolving_event = {
        **_without_keys(_payload_of(fixture, "R post_tool_use spawn_subagent"), _SESSION_ID_KEYS),
        "hookEventName": resolving_event_name,
        "hook_event_name": resolving_event_pascal_case_name,
    }
    waiting_path = target.state_dir / "permissions_waiting"
    for step in (
        _hook_step(_payload_of(fixture, "R user_prompt_submit p1")),
        _hook_step(_payload_of(fixture, "R subagent_start C1")),
        _hook_step(root_notification),
        _hook_step(_as_subagent_payload(root_notification, child_id)),
    ):
        _run_step(target, step)
    lines_with_both_open = waiting_path.read_text().splitlines()

    _run_step(target, _hook_step(resolving_event))

    assert lines_with_both_open == [_id_of(fixture, "R"), child_id]
    assert not waiting_path.exists()
    assert _hook_log_lines(target)[-1].endswith(" permissions:cleared")


def test_a_timer_running_before_the_handler_is_still_running_after_it(temp_host_dir: Path) -> None:
    fixture = _load_fixture("plain")
    target = _make_target(temp_host_dir)
    alarm_handler_before = signal.getsignal(signal.SIGALRM)
    previous_delay, previous_interval = signal.setitimer(signal.ITIMER_REAL, 30.0)
    started_at = time.monotonic()
    try:
        _run_handler(target, [], json.dumps(_payload_of(fixture, "R user_prompt_submit p1")), {})

        remaining_delay, _ = signal.getitimer(signal.ITIMER_REAL)
        alarm_handler_after = signal.getsignal(signal.SIGALRM)
    finally:
        elapsed_seconds = time.monotonic() - started_at
        restored_delay = max(previous_delay - elapsed_seconds, 0.001) if previous_delay > 0 else 0.0
        signal.setitimer(signal.ITIMER_REAL, restored_delay, previous_interval)
    assert 25.0 < remaining_delay <= 30.0
    assert alarm_handler_after is alarm_handler_before
    assert _describe_marker_state(target, fixture) == "active turns=R:p1"


def test_submissions_log_and_turn_files_carry_the_tail_or_the_auto_wake_marker(temp_host_dir: Path) -> None:
    fixture = _load_fixture("bgshell")
    target = _make_target(temp_host_dir)
    user_prompt = _payload_of(fixture, "R user_prompt_submit p1")
    wake_prompt = _payload_of(fixture, "R user_prompt_submit wake-T1")

    _run_step(target, _hook_step(user_prompt))
    user_turn_lines = _file_lines(_plugin_dir(target) / "turns" / user_prompt["sessionId"])
    _run_step(target, _hook_step(wake_prompt))
    wake_turn_lines = _file_lines(_plugin_dir(target) / "turns" / wake_prompt["sessionId"])

    user_tail = build_normalized_message_probe(user_prompt["prompt"])
    assert user_tail == "bgtestrunsomethinginthebackground"
    assert user_turn_lines == [user_prompt["promptId"], user_tail]
    assert wake_turn_lines == [wake_prompt["promptId"], grok_hook.AUTO_WAKE_TAIL_MARKER]
    assert _file_lines(_plugin_dir(target) / "submissions.log") == [
        f"{user_prompt['promptId']} {user_tail}",
        f"{wake_prompt['promptId']} {grok_hook.AUTO_WAKE_TAIL_MARKER}",
    ]
    assert re.fullmatch(r"[a-z0-9]*", grok_hook.AUTO_WAKE_TAIL_MARKER) is None


@pytest.mark.parametrize(
    "prompt",
    [
        "",
        "!!!",
        "Hello, World!",
        "  multi\nline\ttext with   spaces  ",
        "ÉCOLE café naïve",
        "temperature 300 \u212a and \u0130stanbul",
        "[Pasted: 4 lines]",
        "emoji \U0001f680 rocket",
        "0123456789" * 10,
        "x" * 100 + " The Tail End",
    ],
)
def test_prompt_tail_normalization_matches_mngr_message_probe(prompt: str) -> None:
    assert grok_hook.normalized_prompt_tail(prompt) == build_normalized_message_probe(prompt)


def test_prompt_tail_length_matches_mngr_probe_length() -> None:
    assert grok_hook.NORMALIZED_PROBE_MAX_LENGTH == NORMALIZED_PROBE_MAX_LENGTH


def test_active_flips_emit_activity_events_and_idle_since(temp_host_dir: Path) -> None:
    fixture = _load_fixture("plain")
    target = _make_target(temp_host_dir)
    root_id = _id_of(fixture, "R")
    prompt_id = _id_of(fixture, "p1")

    _run_step(target, _hook_step(_payload_of(fixture, "R user_prompt_submit p1")))
    is_idle_since_present_while_active = (target.state_dir / "idle_since").exists()
    _run_step(target, _turn_completed_step(root_id, prompt_id))
    _run_step(target, _turn_completed_step(root_id, prompt_id))

    events = _activity_events(target)
    assert is_idle_since_present_while_active is False
    assert [list(event) for event in events] == [["source", "type", "event_id", "timestamp"]] * 2
    assert {(event["source"], event["type"]) for event in events} == {("mngr/activity", "activity")}
    assert all(re.fullmatch(r"evt-[0-9a-f]{32}", event["event_id"]) for event in events)
    assert events[0]["event_id"] != events[1]["event_id"]
    assert all(_TIMESTAMP_PATTERN.match(event["timestamp"]) for event in events)
    assert _TIMESTAMP_PATTERN.match((target.state_dir / "idle_since").read_text().strip())

    _run_step(target, _hook_step(_payload_of(fixture, "R user_prompt_submit p1")))

    assert not (target.state_dir / "idle_since").exists()
    assert len(_activity_events(target)) == 3


def test_session_start_records_the_resume_target_and_each_root_once(temp_host_dir: Path) -> None:
    fixture = _load_fixture("new_session")
    target = _make_target(temp_host_dir)
    _plugin_dir(target).mkdir(parents=True)
    (_plugin_dir(target) / "fork_from").write_text("source-session\n")

    session_id_labels = []
    for step in fixture.steps:
        _run_step(target, step)
        if step["kind"] == "hook" and step["payload"]["hookEventName"] == "session_start":
            session_id_labels.append(_label(_file_lines(_plugin_dir(target) / "session_id")[0], fixture))

    assert session_id_labels == ["R", "R2", "R"]
    assert [_label(line, fixture) for line in _file_lines(_plugin_dir(target) / "roots")] == ["R", "R2"]
    assert not (_plugin_dir(target) / "fork_from").exists()
    assert (target.state_dir / "grok_session_started").exists()


def test_stop_records_the_background_tasks_snapshot(temp_host_dir: Path) -> None:
    fixture = _load_fixture("bgshell")
    target = _make_target(temp_host_dir)
    stop = _payload_of(fixture, "R stop end_turn p1")

    _run_step(target, _hook_step(stop))

    assert json.loads((_plugin_dir(target) / "background_tasks.json").read_text()) == stop["backgroundTasks"]


def test_nested_grok_hooks_are_ignored_but_streamer_calls_are_not(temp_host_dir: Path) -> None:
    fixture = _load_fixture("plain")
    target = _make_target(temp_host_dir)
    user_prompt = _payload_of(fixture, "R user_prompt_submit p1")

    _run_handler_process(target, [], json.dumps(user_prompt), {"GROK_AGENT": "1"})

    assert sorted(path.name for path in target.state_dir.iterdir()) == []
    _run_handler_process(target, [], json.dumps(user_prompt), {})
    assert _describe_marker_state(target, fixture) == "active turns=R:p1"
    _run_handler_process(
        target, ["--turn-completed", user_prompt["sessionId"], user_prompt["promptId"]], "", {"GROK_AGENT": "1"}
    )
    assert _describe_marker_state(target, fixture) == "idle"


def test_event_name_falls_back_to_the_pascal_case_alias_and_the_environment(temp_host_dir: Path) -> None:
    fixture = _load_fixture("plain")
    pascal_case_target = _make_target(temp_host_dir)
    environment_target = _make_target(temp_host_dir)
    user_prompt = _payload_of(fixture, "R user_prompt_submit p1")
    pascal_case_only = {name: value for name, value in user_prompt.items() if name != "hookEventName"}
    nameless = {name: value for name, value in pascal_case_only.items() if name != "hook_event_name"}

    _run_handler(pascal_case_target, [], json.dumps(pascal_case_only), {})
    _run_handler(environment_target, [], json.dumps(nameless), {"GROK_HOOK_EVENT": "user_prompt_submit"})

    assert _describe_marker_state(pascal_case_target, fixture) == "active turns=R:p1"
    assert _describe_marker_state(environment_target, fixture) == "active turns=R:p1"


@pytest.mark.parametrize(
    "event_name",
    ["pre_compact", "post_compact", "subagent_stop", "pre_tool_use", "a_future_event"],
)
def test_events_without_marker_rules_are_logged_no_ops(event_name: str, temp_host_dir: Path) -> None:
    fixture = _load_fixture("plain")
    target = _make_target(temp_host_dir)
    payload = {**_payload_of(fixture, "R user_prompt_submit p1"), "hookEventName": event_name}

    _run_handler(target, [], json.dumps(payload), {})

    assert _describe_marker_state(target, fixture) == "idle"
    assert not _plugin_dir(target).exists()
    assert len(_hook_log_lines(target)) == 1
    assert f"event={event_name} " in _hook_log_lines(target)[0]


def test_unknown_reasons_notification_types_and_session_ids_are_no_ops(temp_host_dir: Path) -> None:
    fixture = _load_fixture("bgsubagent")
    target = _make_target(temp_host_dir)
    unknown_session_id = f"unknown-{uuid4().hex}"
    seed_steps = [
        _hook_step(_payload_of(fixture, "R user_prompt_submit p1")),
        _hook_step(_payload_of(fixture, "R subagent_start C1")),
    ]
    steps = [
        _hook_step({**_payload_of(fixture, "R stop shutdown"), "reason": "channel_closed"}),
        _hook_step(_notification_of_type(fixture, "agent_error")),
        _hook_step({**_payload_of(fixture, "R notification idle_prompt"), "sessionId": unknown_session_id}),
        _hook_step({**_payload_of(fixture, "C1 session_end shutdown"), "sessionId": unknown_session_id}),
        _hook_step({**_payload_of(fixture, "R session_end shutdown"), "sessionId": unknown_session_id}),
        _hook_step({**_payload_of(fixture, "C1 subagent_stop C1.p1"), "sessionId": unknown_session_id}),
        _turn_completed_step(unknown_session_id, _id_of(fixture, "p1")),
        _hook_step({**_payload_of(fixture, "R user_prompt_submit p1"), "sessionId": "../escaped"}),
        _hook_step({**_payload_of(fixture, "R subagent_start C1"), "subagentId": "nested/child"}),
    ]
    for step in seed_steps:
        _run_step(target, step)
    plugin_files_before = read_files_under(_plugin_dir(target))

    observed = [state for _, state in _replay(target, fixture, steps)]

    assert observed == ["active turns=R:p1 subagents=C1"] * len(steps)
    assert read_files_under(_plugin_dir(target)) == plugin_files_before
    assert len(_hook_log_lines(target)) == len(seed_steps) + len(steps)


@pytest.mark.parametrize("reason", ["shutdown", "channel_closed"])
def test_a_stop_that_is_not_end_turn_leaves_the_running_subagents_alone(reason: str, temp_host_dir: Path) -> None:
    fixture = _load_fixture("bgsubagent")
    target = _make_target(temp_host_dir)
    for description in ("R user_prompt_submit p1", "R subagent_start C1"):
        _run_step(target, _hook_step(_payload_of(fixture, description)))
    plugin_files_before = read_files_under(_plugin_dir(target))
    stop_payload = {**_without_keys(_payload_of(fixture, "R stop shutdown"), ("backgroundTasks",)), "reason": reason}

    _run_step(target, _hook_step(stop_payload))

    assert read_files_under(_plugin_dir(target)) == plugin_files_before
    assert _describe_marker_state(target, fixture) == "active turns=R:p1 subagents=C1"
    assert _hook_log_lines(target)[-1].endswith(f" ignored:reason={reason}")


@pytest.mark.parametrize(
    ("description", "removed_keys", "added_fields", "expected_outcome"),
    [
        pytest.param("R session_start new", _SESSION_ID_KEYS, {}, "ignored:no-session-id", id="session_start_no_id"),
        pytest.param(
            "R session_start new", (), _FROM_A_SUBAGENT, "ignored:subagent-session", id="session_start_of_subagent"
        ),
        pytest.param("R stop end_turn p1", (), _FROM_A_SUBAGENT, "ignored:not-a-root-session", id="stop_of_subagent"),
        pytest.param(
            "R stop end_turn p1",
            _SESSION_ID_KEYS,
            {"hookEventName": "stop_failure"},
            "ignored:no-session-id",
            id="stop_failure_no_id",
        ),
        pytest.param(
            "R stop end_turn p1",
            _PROMPT_ID_KEYS,
            {"hookEventName": "stop_cancelled"},
            "ignored:no-prompt-id",
            id="stop_cancelled_no_prompt_id",
        ),
        pytest.param("R session_end shutdown", _SESSION_ID_KEYS, {}, "ignored:no-session-id", id="session_end_no_id"),
        pytest.param(
            "R notification idle_prompt",
            (),
            _FROM_A_SUBAGENT,
            "ignored:not-a-root-session",
            id="idle_prompt_of_subagent",
        ),
    ],
)
def test_payloads_without_a_usable_root_session_or_prompt_leave_the_markers_unchanged(
    description: str,
    removed_keys: Sequence[str],
    added_fields: Mapping[str, str],
    expected_outcome: str,
    temp_host_dir: Path,
) -> None:
    fixture = _load_fixture("plain")
    target = _make_target(temp_host_dir)
    _run_step(target, _hook_step(_payload_of(fixture, "R user_prompt_submit p1")))
    plugin_files_before = read_files_under(_plugin_dir(target))

    _run_step(target, _hook_step({**_without_keys(_payload_of(fixture, description), removed_keys), **added_fields}))

    assert read_files_under(_plugin_dir(target)) == plugin_files_before
    assert _describe_marker_state(target, fixture) == "active turns=R:p1"
    assert _hook_log_lines(target)[-1].endswith(" " + expected_outcome)


@pytest.mark.parametrize("stdin_bytes", [b"", b"not json", b"[1, 2]", b"\xff\xfe"])
def test_malformed_payloads_are_logged_and_change_nothing(stdin_bytes: bytes, temp_host_dir: Path) -> None:
    target = _make_target(temp_host_dir)

    exit_code = grok_hook.main(["grok_hook.py"], io.BytesIO(stdin_bytes), _handler_environ(target, {}), ())

    assert exit_code == 0
    log_lines = _hook_log_lines(target)
    assert len(log_lines) == 1
    assert "event=hook session=- prompt=- error:MalformedHookPayloadError: hook payload is" in log_lines[0]
    assert not _plugin_dir(target).exists()


def test_internal_failure_is_logged_and_still_exits_zero_silently(temp_host_dir: Path) -> None:
    fixture = _load_fixture("plain")
    target = _make_target(temp_host_dir)
    (target.state_dir / "plugin").mkdir()
    (target.state_dir / "plugin" / "grok").write_text("a file where the plugin directory belongs\n")

    _run_handler_process(target, [], json.dumps(_payload_of(fixture, "R user_prompt_submit p1")), {})

    log_lines = _hook_log_lines(target)
    assert len(log_lines) == 1
    assert "error:FileExistsError" in log_lines[0]


def test_missing_state_dir_exits_zero_without_touching_anything(tmp_path: Path) -> None:
    fixture = _load_fixture("plain")
    env = {name: value for name, value in os.environ.items() if name != "MNGR_AGENT_STATE_DIR"}
    work_dir = tmp_path / f"work-{uuid4().hex}"
    work_dir.mkdir()

    result = subprocess.run(
        [str(_SCRIPT_PATH)],
        input=json.dumps(_payload_of(fixture, "R user_prompt_submit p1")),
        env=env,
        cwd=work_dir,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert list(work_dir.iterdir()) == []


@pytest.mark.parametrize(
    ("arguments", "expected_outcome"),
    [
        pytest.param(["--turn-completed", "only-a-session-id"], "ignored:unknown-arguments", id="missing_prompt_id"),
        pytest.param(["--unknown-flag", "x"], "ignored:unknown-arguments", id="unknown_flag"),
        pytest.param(["--turn-completed", "../escape", "p1"], "ignored:invalid-arguments", id="unsafe_session_id"),
    ],
)
def test_unknown_or_invalid_arguments_are_logged_no_ops(
    arguments: list[str], expected_outcome: str, temp_host_dir: Path
) -> None:
    target = _make_target(temp_host_dir)
    waiting_path = target.state_dir / "permissions_waiting"
    waiting_path.write_text("other-session\n")

    _run_handler(target, arguments, "", {})

    assert len(_hook_log_lines(target)) == 1
    assert expected_outcome in _hook_log_lines(target)[0]
    assert not _plugin_dir(target).joinpath("turns").exists()
    assert waiting_path.read_text() == "other-session\n"


@pytest.mark.parametrize(
    "arguments", [pytest.param([], id="hook"), pytest.param(["--turn-completed", "s1", "p1"], id="turn_completed")]
)
def test_invocation_without_a_state_dir_writes_nothing(
    arguments: list[str], temp_host_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _load_fixture("plain")
    work_dir = tmp_path / f"work-{uuid4().hex}"
    work_dir.mkdir()
    monkeypatch.chdir(work_dir)
    host_files_before = read_files_under(temp_host_dir)
    stdin = io.BytesIO(json.dumps(_payload_of(fixture, "R user_prompt_submit p1")).encode())

    exit_code = grok_hook.main(["grok_hook.py", *arguments], stdin, {"MNGR_HOST_DIR": str(temp_host_dir)}, ())

    assert exit_code == 0
    assert list(work_dir.iterdir()) == []
    assert read_files_under(temp_host_dir) == host_files_before


def test_turn_without_a_host_dir_still_flips_active_and_logs_the_missing_event(temp_host_dir: Path) -> None:
    fixture = _load_fixture("plain")
    target = _make_target(temp_host_dir)
    stdin = io.BytesIO(json.dumps(_payload_of(fixture, "R user_prompt_submit p1")).encode())

    grok_hook.main(["grok_hook.py"], stdin, {"MNGR_AGENT_STATE_DIR": str(target.state_dir)}, ())

    assert _describe_marker_state(target, fixture) == "active turns=R:p1"
    assert _hook_log_lines(target)[0].endswith(" turn:start active:on activity-event:no-host-dir")
    assert _activity_events(target) == []


def test_silenced_descriptors_are_pointed_at_dev_null(temp_host_dir: Path) -> None:
    target = _make_target(temp_host_dir)
    read_end, write_end = os.pipe()

    grok_hook.main(
        ["grok_hook.py", "--turn-completed", "s1", "p1"], io.BytesIO(b""), _handler_environ(target, {}), [write_end]
    )
    os.write(write_end, b"written after silencing")
    os.close(write_end)

    with os.fdopen(read_end, "rb") as pipe_reader:
        assert pipe_reader.read() == b""
    assert len(_hook_log_lines(target)) == 1


def test_an_unwritable_hook_log_does_not_stop_the_marker_update(temp_host_dir: Path) -> None:
    fixture = _load_fixture("plain")
    target = _make_target(temp_host_dir)
    (target.state_dir / "logs").write_text("a file where the log directory belongs\n")

    _run_step(target, _hook_step(_payload_of(fixture, "R user_prompt_submit p1")))

    assert _describe_marker_state(target, fixture) == "active turns=R:p1"
    assert (target.state_dir / "logs").read_text() == "a file where the log directory belongs\n"


# Lock holders and leftovers from a killed grok.


def test_lock_holder_blocks_with_a_bounded_wait_and_its_death_frees_the_lock(
    temp_host_dir: Path, temp_mngr_ctx: MngrContext
) -> None:
    fixture = _load_fixture("plain")
    target = _make_target(temp_host_dir)
    user_prompt = json.dumps(_payload_of(fixture, "R user_prompt_submit p1"))

    with file_lock_held_by_another_process(_plugin_dir(target) / "state.lock", temp_mngr_ctx.concurrency_group):
        started_at = time.monotonic()
        _run_handler(target, [], user_prompt, {})
        blocked_seconds = time.monotonic() - started_at
        state_while_held = _describe_marker_state(target, fixture)

    _run_handler(target, [], user_prompt, {})

    assert grok_hook.STATE_LOCK_TIMEOUT_SECONDS <= blocked_seconds < 5
    assert state_while_held == "idle"
    assert "skipped:state-lock-timeout" in _hook_log_lines(target)[0]
    assert _describe_marker_state(target, fixture) == "active turns=R:p1"


def test_a_streamer_call_waits_past_the_hook_lock_timeout_for_the_state_lock(temp_host_dir: Path) -> None:
    fixture = _load_fixture("plain")
    target = _make_target(temp_host_dir)
    _run_step(target, _hook_step(_payload_of(fixture, "R user_prompt_submit p1")))
    lock_path = _plugin_dir(target) / "state.lock"
    hold_seconds = 2 * grok_hook.STATE_LOCK_TIMEOUT_SECONDS

    with subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK_SCRIPT, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as holder:
        assert holder.stdin is not None and holder.stdout is not None
        assert holder.stdout.readline() == "locked\n"
        release_timer = threading.Timer(hold_seconds, holder.stdin.close)
        release_timer.start()
        started_at = time.monotonic()
        try:
            _run_handler(target, ["--turn-completed", _id_of(fixture, "R"), _id_of(fixture, "p1")], "", {})
            waited_seconds = time.monotonic() - started_at
        finally:
            release_timer.join()
        holder.wait(timeout=10)

    assert waited_seconds >= grok_hook.STATE_LOCK_TIMEOUT_SECONDS
    assert _describe_marker_state(target, fixture) == "idle"
    assert _hook_log_lines(target)[-1].endswith("turn:end active:off")


def _write_leftovers_of_a_killed_turn(target: _HookTarget, fixture: _HookFixture) -> None:
    plugin_dir = _plugin_dir(target)
    root_id = _id_of(fixture, "R")
    for relative_path, content in (
        (f"turns/{root_id}", "stale-prompt-id\nstaletail\n"),
        (f"turns/.{root_id}.4242.tmp", "half-written\n"),
        ("subagents/stale-subagent-id", f"{root_id}\nbackground\n"),
        (f"pending_wake/{root_id}", "stale-subagent-id\n"),
        ("state.lock", ""),
        ("roots", f"{root_id}\n"),
    ):
        (plugin_dir / relative_path).parent.mkdir(parents=True, exist_ok=True)
        (plugin_dir / relative_path).write_text(content)
    (target.state_dir / "active").touch()
    (target.state_dir / "permissions_waiting").touch()


def test_a_temp_file_left_in_turns_by_a_killed_handler_is_not_a_turn(temp_host_dir: Path) -> None:
    fixture = _load_fixture("plain")
    target = _make_target(temp_host_dir)
    stray_temp_path = _plugin_dir(target) / "turns" / f".{_id_of(fixture, 'R')}.4242.tmp"
    stray_temp_path.parent.mkdir(parents=True)
    stray_temp_path.write_text("half-written\n")

    observed = _replay(target, fixture, fixture.steps)

    assert observed == list(_PLAIN_EXPECTED)
    assert stray_temp_path.read_text() == "half-written\n"


def test_leftovers_without_the_launch_reset_heal_through_stop_and_the_backstop(temp_host_dir: Path) -> None:
    fixture = _load_fixture("plain")
    target = _make_target(temp_host_dir)
    _write_leftovers_of_a_killed_turn(target, fixture)

    observed = _replay(target, fixture, fixture.steps)

    assert observed == [
        (
            "R session_start new",
            "active turns=R:stale-prompt-id subagents=stale-subagent-id(bg) pending-wake=R<stale-subagent-id "
            "permissions-waiting",
        ),
        (
            "R user_prompt_submit p1",
            "active turns=R:p1 subagents=stale-subagent-id(bg) pending-wake=R<stale-subagent-id",
        ),
        ("R stop end_turn p1", "active turns=R:p1 pending-wake=R<stale-subagent-id"),
        ("R --turn-completed p1", "active turns=R:pending-wake:stale-subagent-id pending-wake=R<stale-subagent-id"),
        ("R notification idle_prompt", "idle"),
        ("R session_end shutdown", "idle"),
        ("R stop shutdown", "idle"),
    ]


# The script itself.


def test_handler_is_an_executable_python3_script() -> None:
    source = _SCRIPT_PATH.read_text()

    assert source.startswith("#!/usr/bin/env python3\n")
    assert os.access(_SCRIPT_PATH, os.X_OK)


def test_one_invocation_runs_well_under_200_ms(temp_host_dir: Path) -> None:
    fixture = _load_fixture("plain")
    user_prompt = json.dumps(_payload_of(fixture, "R user_prompt_submit p1"))

    fastest_seconds = min(_run_handler_process(_make_target(temp_host_dir), [], user_prompt, {}) for _ in range(3))

    assert fastest_seconds < 0.2
