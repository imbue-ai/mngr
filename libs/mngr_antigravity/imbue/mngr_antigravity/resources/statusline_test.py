"""Tests for statusline.sh, the agy statusLine command that owns agent lifecycle.

agy invokes this on every agent-state change, piping a JSON payload on stdin
(`agent_state`, `conversation_id`, `model`, ...). The script maintains the
`active` marker BaseAgent reads for RUNNING/WAITING (active iff `agent_state` is
a busy state -- a denylist excluding idle/initializing/authenticating/empty),
records the root `conversation_id` for resume, touches `antigravity_ready` once
`agent_state` is past startup, rewrites the background-task
records from `task_count` and `subagents`, and fires the tmux submission
signal when busy. mngr's statusLine is lifecycle-only and prints nothing of its
own (agy already shows working/idle), so stdout is empty unless a user statusLine
is composed, in which case the row is that command's output verbatim.
`tmux wait-for` is `|| true`-guarded, so it is a no-op without a usable TMUX in
tests.
"""

from __future__ import annotations

import importlib.resources
import json
import os
import re
import shutil
import subprocess
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Final

import pytest

from imbue.mngr import resources as mngr_resources
from imbue.mngr.primitives import BackgroundTaskKind
from imbue.mngr.utils.testing import read_background_task_records_in
from imbue.mngr_antigravity.resources.testing import STATUSLINE_PAYLOAD_FIELD_PATHS
from imbue.mngr_antigravity.resources.testing import agent_env
from imbue.mngr_antigravity.resources.testing import run_statusline as _run

_SCRIPT_PATH = Path(__file__).parent / "statusline.sh"

_ROOT_CONV = "2005e9cc-93d7-4685-b96c-8da612ab8165"
_OTHER_CONV = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_MODEL = "Claude Sonnet 4.6 (Thinking)"
_CLOCK_SLACK: Final[timedelta] = timedelta(seconds=5)


def _payload(*, agent_state: str | None, conversation_id: str | None = _ROOT_CONV, model: str | None = _MODEL) -> str:
    """A statusLine payload shaped like the real one (verified live, agy 1.0.6/1.0.7)."""
    fields = []
    if agent_state is not None:
        fields.append(f'"agent_state":"{agent_state}"')
    if conversation_id is not None:
        fields.append(f'"conversation_id":"{conversation_id}"')
    if model is not None:
        fields.append(f'"model":"{model}"')
    fields.append('"context_window":{"total_input_tokens":173}')
    return "{" + ",".join(fields) + "}"


def _marker(state_dir: Path) -> Path:
    return state_dir / "active"


def _root_file(state_dir: Path) -> Path:
    return state_dir / "root_conversation"


def test_working_sets_marker_and_records_root(tmp_path: Path) -> None:
    result = _run(tmp_path, _payload(agent_state="working"))
    assert _marker(tmp_path).exists()
    assert _root_file(tmp_path).read_text() == _ROOT_CONV
    # mngr is lifecycle-only and prints nothing of its own (agy shows working/idle
    # itself), so with no user statusLine the rendered row is empty.
    assert result.stdout == ""


def test_idle_clears_marker(tmp_path: Path) -> None:
    """`idle` is the canonical done state -> WAITING."""
    _marker(tmp_path).touch()
    _run(tmp_path, _payload(agent_state="idle"))
    assert not _marker(tmp_path).exists()


def test_initializing_clears_marker(tmp_path: Path) -> None:
    """`initializing` is a not-yet-working state -> WAITING (denylist member)."""
    _marker(tmp_path).touch()
    _run(tmp_path, _payload(agent_state="initializing"))
    assert not _marker(tmp_path).exists()


def test_authenticating_clears_marker(tmp_path: Path) -> None:
    """`authenticating` is a not-yet-working state -> WAITING (denylist member)."""
    _marker(tmp_path).touch()
    _run(tmp_path, _payload(agent_state="authenticating"))
    assert not _marker(tmp_path).exists()


def test_unknown_busy_state_sets_marker(tmp_path: Path) -> None:
    """The denylist means any state outside {idle, initializing, authenticating, ""}
    counts as RUNNING -- a future busy state (e.g. "responding") keeps the marker."""
    _run(tmp_path, _payload(agent_state="responding"))
    assert _marker(tmp_path).exists()


def test_empty_agent_state_clears_marker(tmp_path: Path) -> None:
    """An empty/absent agent_state is treated as not-working (never falsely RUNNING)."""
    _marker(tmp_path).touch()
    _run(tmp_path, _payload(agent_state=None))
    assert not _marker(tmp_path).exists()


@pytest.mark.parametrize(
    ("agent_state", "is_ready_expected"),
    [("initializing", False), ("authenticating", False), (None, False), ("idle", True), ("working", True)],
)
def test_ready_marker_is_touched_once_agy_reports_a_state_past_startup(
    tmp_path: Path, agent_state: str | None, is_ready_expected: bool
) -> None:
    _run(tmp_path, _payload(agent_state=agent_state))
    assert (tmp_path / "antigravity_ready").exists() == is_ready_expected


def test_empty_payload_does_not_clobber_recorded_root(tmp_path: Path) -> None:
    """A later payload without a conversation_id must not erase the recorded root."""
    _run(tmp_path, _payload(agent_state="working"))
    assert _root_file(tmp_path).read_text() == _ROOT_CONV
    _run(tmp_path, _payload(agent_state="idle", conversation_id=None))
    assert _root_file(tmp_path).read_text() == _ROOT_CONV


def test_root_conversation_updates_to_latest_root(tmp_path: Path) -> None:
    """agy always reports the root id here, so recording the latest keeps resume
    correct across /clear, /fork, /switch, and resume."""
    _run(tmp_path, _payload(agent_state="working", conversation_id=_ROOT_CONV))
    _run(tmp_path, _payload(agent_state="working", conversation_id=_OTHER_CONV))
    assert _root_file(tmp_path).read_text() == _OTHER_CONV


def test_garbage_stdin_records_no_root_and_clears_marker(tmp_path: Path) -> None:
    """Non-JSON stdin parses no state (-> not-working) and no id (-> no root)."""
    _marker(tmp_path).touch()
    result = _run(tmp_path, "not json at all\n")
    assert not _marker(tmp_path).exists()
    assert not _root_file(tmp_path).exists()
    # mngr prints nothing of its own, and no user statusLine here, so stdout is empty.
    assert result.stdout == ""


def test_missing_state_dir_fails_loudly(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(_SCRIPT_PATH)],
        input=_payload(agent_state="working"),
        env={k: v for k, v in os.environ.items() if k != "MNGR_AGENT_STATE_DIR"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "MNGR_AGENT_STATE_DIR" in result.stderr


def _user_cmd_file(state_dir: Path) -> Path:
    return state_dir / "user_statusline_command"


def test_composes_user_statusline_output(tmp_path: Path) -> None:
    """When a user statusLine command is recorded, the row is exactly its output.

    mngr prints nothing of its own, so the rendered row is the user's command
    output verbatim (no glyph, no separator). The user command receives the same
    payload on stdin that agy delivers; here it just echoes a literal.
    """
    _user_cmd_file(tmp_path).write_text("echo USER-PART")
    result = _run(tmp_path, _payload(agent_state="working"))
    assert result.stdout.strip() == "USER-PART"


def test_user_statusline_receives_payload_on_stdin(tmp_path: Path) -> None:
    """The composed user command gets the agy payload on stdin (so it can render from it)."""
    # `cat` echoes whatever it received on stdin -> proves the payload was piped through.
    _user_cmd_file(tmp_path).write_text("cat")
    result = _run(tmp_path, _payload(agent_state="working"))
    assert _ROOT_CONV in result.stdout


def test_empty_user_statusline_file_renders_empty_row(tmp_path: Path) -> None:
    """An empty user_statusline_command file (no command recorded) renders nothing."""
    _user_cmd_file(tmp_path).write_text("")
    result = _run(tmp_path, _payload(agent_state="working"))
    assert result.stdout == ""


def test_failing_user_statusline_does_not_break_side_effects(tmp_path: Path) -> None:
    """A user command that exits non-zero / errors can't break mngr's side-effects.

    The marker (the lifecycle source of truth, read by BaseAgent) is still
    maintained, and the failing command emits no row.
    """
    _user_cmd_file(tmp_path).write_text("this-command-does-not-exist-xyz")
    result = _run(tmp_path, _payload(agent_state="working"))
    assert _marker(tmp_path).exists()
    assert result.stdout == ""


def _install_flush_stubs(state_dir: Path) -> Path:
    """Install the real shared common-transcript lib (so statusline.sh's source
    succeeds and mngr_common_transcript_flush is defined) plus stub stream/common
    transcript scripts that record that they ran (and whether the active marker
    was still present at that moment, to pin the flush-before-clear ordering).
    Returns the sentinel path the stubs append to."""
    commands = state_dir / "commands"
    commands.mkdir(parents=True, exist_ok=True)
    (commands / "mngr_common_transcript_lib.sh").write_text(
        importlib.resources.files(mngr_resources).joinpath("mngr_common_transcript_lib.sh").read_text()
    )
    sentinel = state_dir / "flush_sentinel"
    for name in ("stream_transcript.sh", "common_transcript.sh"):
        script = commands / name
        script.write_text(
            "#!/usr/bin/env bash\n"
            f'echo "{name}:marker=$([ -e "$MNGR_AGENT_STATE_DIR/active" ] && echo present || echo gone)" '
            '>> "$MNGR_AGENT_STATE_DIR/flush_sentinel"\n'
        )
        script.chmod(0o755)
    return sentinel


def test_busy_to_idle_edge_flushes_before_clearing_marker(tmp_path: Path) -> None:
    """On the busy->idle edge the transcript converters are flushed (single-pass)
    BEFORE the active marker is cleared, so a WAITING-signal consumer that reads
    the common transcript can't outrun the converter."""
    sentinel = _install_flush_stubs(tmp_path)
    # A prior busy sample left the marker present; this idle is the busy->idle edge.
    _marker(tmp_path).touch()

    _run(tmp_path, _payload(agent_state="idle"))

    assert not _marker(tmp_path).exists()
    # Both converters ran (marker still present at flush time -> flush precedes
    # the clear), in pipeline order: raw streamer first, then common converter.
    assert sentinel.read_text().splitlines() == [
        "stream_transcript.sh:marker=present",
        "common_transcript.sh:marker=present",
    ]


def test_idle_without_prior_busy_does_not_flush(tmp_path: Path) -> None:
    """Idle/startup samples with no marker present (not a busy->idle edge) must
    not pay for a synchronous flush."""
    sentinel = _install_flush_stubs(tmp_path)
    _run(tmp_path, _payload(agent_state="idle"))
    assert not _marker(tmp_path).exists()
    assert not sentinel.exists()


def test_working_does_not_flush(tmp_path: Path) -> None:
    """Entering a busy state sets the marker and never flushes."""
    sentinel = _install_flush_stubs(tmp_path)
    _run(tmp_path, _payload(agent_state="working"))
    assert _marker(tmp_path).exists()
    assert not sentinel.exists()


@pytest.mark.parametrize(
    ("model_object", "expected_model_state"),
    [
        # An adjustable model: the effort is its own axis and its tier leaves the name.
        (
            '{"id":"Gemini 3.8 Flash (High)","display_name":"Gemini 3.8 Flash (High)","effort":"high"}',
            {"model": "Gemini 3.8 Flash", "effort": "high"},
        ),
        (
            '{"id":"Gemini 3.8 Flash (Low)","display_name":"Gemini 3.8 Flash (Low)","effort":"low"}',
            {"model": "Gemini 3.8 Flash", "effort": "low"},
        ),
        # A fixed-effort model reports no effort, so its name is kept whole, tier and all.
        ('{"id":"GPT-OSS 120B (Medium)","display_name":"GPT-OSS 120B (Medium)"}', {"model": "GPT-OSS 120B (Medium)"}),
        (
            '{"id":"Claude Sonnet 4.6 (Thinking)","display_name":"Claude Sonnet 4.6 (Thinking)"}',
            {"model": "Claude Sonnet 4.6 (Thinking)"},
        ),
        # The tier is matched whatever case agy reports the effort in.
        ('{"id":"Gemini 3.8 Flash (High)","effort":"High"}', {"model": "Gemini 3.8 Flash", "effort": "High"}),
        # A name that does not end in the reported tier is left alone rather than cut.
        ('{"id":"Gemini 3.8 Flash","effort":"xhigh"}', {"model": "Gemini 3.8 Flash", "effort": "xhigh"}),
    ],
)
def test_model_state_records_model_and_effort(
    tmp_path: Path, model_object: str, expected_model_state: dict[str, str]
) -> None:
    payload = '{"agent_state":"idle","model":' + model_object + "}"
    _run(tmp_path, payload)
    assert json.loads((tmp_path / "model_state.json").read_text()) == expected_model_state


def test_effort_change_rewrites_model_state(tmp_path: Path) -> None:
    """A later payload at another effort replaces the recorded effort, keeping the model."""
    _run(tmp_path, '{"agent_state":"idle","model":{"id":"Gemini 3.8 Flash (High)","effort":"high"}}')
    _run(tmp_path, '{"agent_state":"idle","model":{"id":"Gemini 3.8 Flash (Medium)","effort":"medium"}}')
    assert json.loads((tmp_path / "model_state.json").read_text()) == {"model": "Gemini 3.8 Flash", "effort": "medium"}


def test_switching_to_a_fixed_effort_model_drops_the_recorded_effort(tmp_path: Path) -> None:
    """Moving to a model with no effort axis writes no effort, rather than keeping the old one."""
    _run(tmp_path, '{"agent_state":"idle","model":{"id":"Gemini 3.8 Flash (High)","effort":"high"}}')
    _run(tmp_path, '{"agent_state":"idle","model":{"id":"Claude Opus 4.6 (Thinking)"}}')
    assert json.loads((tmp_path / "model_state.json").read_text()) == {"model": "Claude Opus 4.6 (Thinking)"}


def _install_background_task_lib(state_dir: Path) -> None:
    commands = state_dir / "commands"
    commands.mkdir(parents=True, exist_ok=True)
    library = importlib.resources.files(mngr_resources).joinpath("mngr_background_task_lib.sh").read_text()
    (commands / "mngr_background_task_lib.sh").write_text(library)


def _agy_payload(
    agent_state: str, *, task_count: int = 0, subagents: Sequence[dict[str, str]] = (), role: str = "Command Runner"
) -> str:
    """A statusLine payload in agy 1.5.9's shape: compact, `task_count` present only while above zero."""
    payload: dict[str, object] = {"conversation_id": _ROOT_CONV, "agent_state": agent_state}
    if subagents:
        payload["subagents"] = [{"name": "self", "role": role, "model": _MODEL, **subagent} for subagent in subagents]
    payload["plan_tier"] = "Antigravity Starter Quota"
    if task_count:
        payload["task_count"] = task_count
    payload["terminal_width"] = 200
    return json.dumps(payload, separators=(",", ":"))


_RUNNING: Final[dict[str, str]] = {"status": "running"}
_COMPLETED: Final[dict[str, str]] = {"status": "completed"}


def _records(state_dir: Path) -> list[tuple[str, BackgroundTaskKind, str]]:
    return [
        (background_task.name, background_task.kind, background_task.native_type)
        for background_task in read_background_task_records_in(state_dir / "background_tasks")
    ]


def test_each_task_agy_counts_gets_a_shell_record_seen_now(tmp_path: Path) -> None:
    _install_background_task_lib(tmp_path)
    reported_at = datetime.now(timezone.utc)

    _run(tmp_path, _agy_payload("idle", task_count=2))

    assert _records(tmp_path) == [
        ("agy-task-1", BackgroundTaskKind.SHELL, "task_count"),
        ("agy-task-2", BackgroundTaskKind.SHELL, "task_count"),
    ]
    for background_task in read_background_task_records_in(tmp_path / "background_tasks"):
        assert background_task.last_seen_at is not None
        assert abs(background_task.last_seen_at - reported_at) <= _CLOCK_SLACK


def test_each_running_subagent_gets_a_subagent_record_and_a_completed_one_none(tmp_path: Path) -> None:
    _install_background_task_lib(tmp_path)

    _run(tmp_path, _agy_payload("idle", subagents=(_RUNNING, _COMPLETED, _RUNNING)))

    assert _records(tmp_path) == [
        ("agy-subagent-1", BackgroundTaskKind.SUBAGENT, "subagents"),
        ("agy-subagent-2", BackgroundTaskKind.SUBAGENT, "subagents"),
    ]


def test_a_lower_count_removes_the_highest_numbered_records(tmp_path: Path) -> None:
    _install_background_task_lib(tmp_path)
    _run(tmp_path, _agy_payload("idle", task_count=3, subagents=(_RUNNING, _RUNNING)))

    _run(tmp_path, _agy_payload("idle", task_count=1, subagents=(_RUNNING, _COMPLETED)))

    assert [name for name, _, _ in _records(tmp_path)] == ["agy-subagent-1", "agy-task-1"]


@pytest.mark.parametrize(
    "payload",
    [
        # agy's first payload after a restart: it does not restart tasks.
        pytest.param(_agy_payload("authenticating"), id="restart"),
        pytest.param(_agy_payload("idle", subagents=(_COMPLETED,)), id="ended_while_idle"),
        pytest.param(_agy_payload("working", subagents=(_COMPLETED,)), id="ended_and_woke_the_agent"),
    ],
)
def test_a_payload_reporting_no_task_and_no_running_subagent_clears_the_records(tmp_path: Path, payload: str) -> None:
    _install_background_task_lib(tmp_path)
    _run(tmp_path, _agy_payload("idle", task_count=2, subagents=(_RUNNING,)))
    assert len(_records(tmp_path)) == 3

    _run(tmp_path, payload)

    assert _records(tmp_path) == []


@pytest.mark.parametrize(
    "payload",
    [pytest.param("not json at all\n", id="garbage"), pytest.param(_payload(agent_state=None), id="no_agent_state")],
)
def test_a_payload_without_an_agent_state_leaves_the_records_as_they_were(tmp_path: Path, payload: str) -> None:
    _install_background_task_lib(tmp_path)
    _run(tmp_path, _agy_payload("idle", task_count=1, subagents=(_RUNNING,)))

    _run(tmp_path, payload)

    assert [name for name, _, _ in _records(tmp_path)] == ["agy-subagent-1", "agy-task-1"]


def test_records_another_writer_owns_are_left_alone(tmp_path: Path) -> None:
    _install_background_task_lib(tmp_path)
    (tmp_path / "background_tasks").mkdir()
    (tmp_path / "background_tasks" / "task-b09b409g6").write_text("seen:1794966000 kind:SHELL native_type:shell\n")
    _run(tmp_path, _agy_payload("idle", task_count=1))
    assert [name for name, _, _ in _records(tmp_path)] == ["agy-task-1", "task-b09b409g6"]

    _run(tmp_path, _agy_payload("idle"))

    assert [name for name, _, _ in _records(tmp_path)] == ["task-b09b409g6"]


def test_text_in_a_subagent_role_neither_adds_nor_hides_a_running_subagent(tmp_path: Path) -> None:
    _install_background_task_lib(tmp_path)
    role_quoting_the_payload = 'Fix [a] and [b], then print {"status":"running"}]'

    _run(tmp_path, _agy_payload("idle", subagents=(_COMPLETED, _RUNNING), role=role_quoting_the_payload))

    assert [name for name, _, _ in _records(tmp_path)] == ["agy-subagent-1"]


# Runs the real rm, then logs whether the turn marker and any record are left after it.
_REMOVAL_WITNESS_SCRIPT: Final[str] = """#!/bin/sh
{real_rm} "$@"
removal_status=$?
active=absent
[ -e "$MNGR_AGENT_STATE_DIR/active" ] && active=present
records=$(ls "$MNGR_AGENT_STATE_DIR/background_tasks" 2>/dev/null | wc -l | tr -d ' ')
echo "active:$active records:$records" >> "$REMOVAL_WITNESS_LOG"
exit $removal_status
"""


@pytest.mark.parametrize(
    "payloads",
    [
        pytest.param(
            (
                _agy_payload("working"),
                _agy_payload("working", task_count=1),
                _agy_payload("idle", task_count=1),
                _agy_payload("working"),
            ),
            id="task",
        ),
        pytest.param(
            (_agy_payload("working"), _agy_payload("idle", task_count=1), _agy_payload("working")),
            id="task_first_reported_at_the_turn_end",
        ),
        pytest.param(
            (
                _agy_payload("working"),
                _agy_payload("working", subagents=(_RUNNING,)),
                _agy_payload("idle", subagents=(_RUNNING,)),
                _agy_payload("working", subagents=(_RUNNING,)),
                _agy_payload("working", subagents=(_COMPLETED,)),
            ),
            id="subagent",
        ),
    ],
)
def test_no_removal_leaves_the_agent_reading_waiting_from_a_turn_end_through_the_wake_turn(
    tmp_path: Path, payloads: tuple[str, ...]
) -> None:
    """On agy 1.5.9 and 1.5.12 the turn ends on the old count and the lowered one first arrives in the wake's `working` call."""
    state_dir = tmp_path / "state"
    _install_background_task_lib(state_dir)
    witness_dir = tmp_path / "bin"
    witness_dir.mkdir()
    real_rm = shutil.which("rm")
    assert real_rm is not None
    (witness_dir / "rm").write_text(_REMOVAL_WITNESS_SCRIPT.format(real_rm=real_rm))
    (witness_dir / "rm").chmod(0o755)
    removal_log = tmp_path / "removals.log"
    env = {
        **agent_env(state_dir),
        "PATH": f"{witness_dir}{os.pathsep}{os.environ['PATH']}",
        "REMOVAL_WITNESS_LOG": str(removal_log),
    }

    for payload in payloads:
        subprocess.run(["bash", str(_SCRIPT_PATH)], input=payload, env=env, capture_output=True, text=True, check=True)

    assert removal_log.read_text().splitlines() == ["active:absent records:1", "active:present records:0"]


def test_every_payload_key_the_script_reads_is_on_a_pinned_path() -> None:
    keys_read = set(re.findall(r'"(\w+)"\[\[:space:\]\]\*:', _SCRIPT_PATH.read_text()))

    assert keys_read == {key for path in STATUSLINE_PAYLOAD_FIELD_PATHS for key in path}, (
        "statusline.sh reads a statusLine payload key that STATUSLINE_PAYLOAD_FIELD_PATHS does not list, or the "
        "reverse; the release test that checks the agy binary pins only the listed paths"
    )
