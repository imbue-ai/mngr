"""record_background_tasks.py, and the turn hook that runs it, against a fake codex app-server on a unix socket."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait
from pathlib import Path
from typing import Any
from typing import Final

import pytest

from imbue.mngr_codex.codex_config import ACTIVE_MARKER_FILENAME
from imbue.mngr_codex.codex_config import APP_SERVER_THREAD_FILENAME
from imbue.mngr_codex.codex_config import RECORD_BACKGROUND_TASKS_SCRIPT_NAME
from imbue.mngr_codex.codex_config import TURN_HOOK_SCRIPT_NAME
from imbue.mngr_codex.resources.record_background_tasks import RECORD_DIR_NAME
from imbue.mngr_codex.resources.record_background_tasks import pass_lock_path
from imbue.mngr_codex.resources.record_background_tasks import sync_records
from imbue.mngr_codex.resources.testing import FakeCodexAppServer
from imbue.mngr_codex.resources.testing import codex_background_terminal
from imbue.mngr_codex.resources.testing import codex_thread
from imbue.mngr_codex.resources.testing import provision_commands_dir
from imbue.mngr_codex.resources.testing import serving_fake_codex_app_server
from imbue.mngr_codex.resources.testing import serving_fake_codex_app_server_for_agent

_ROOT: Final[str] = "01a1f2b2-6d9c-7b93-87cc-4beb530e40ef"
_TITLE_THREAD: Final[str] = "01a1f2b4-0062-7d10-ab9e-7c3b52147701"
_SUBAGENT: Final[str] = "01a1f2b8-4a8a-7060-9962-1bb9cb835f6e"
_TERMINAL: Final[str] = "exec-44abb3ff-4033-431a-a745-8f7cfbd78908"
_SECOND_TERMINAL: Final[str] = "exec-6e1f0b3a-91d2-4c7e-b0a4-2d5f8e9c1b70"
_TURN: Final[str] = "01a1f3aa-2f4b-73f1-bd7d-8062a3beb1f2"
_NOW: Final[int] = 1795100000
_EARLIER_RECORDS: Final[dict[str, str]] = {
    "task-exec-ended": "seen:1795099000 kind:SHELL native_type:backgroundTerminals\n",
    "lease-other-writer": "seen:1795099000\n",
}

_THREADS: Final[tuple[dict[str, Any], ...]] = (
    codex_thread(_ROOT),
    codex_thread(_TITLE_THREAD, thread_source="thread_title"),
    codex_thread(_SUBAGENT, status_type="active", parent_thread_id=_ROOT, thread_source="subagent"),
)


def _idle_root_with_a_terminal_and_a_working_subagent(
    threads: tuple[dict[str, Any], ...] = _THREADS,
    replies_by_method: dict[str, dict[str, Any]] | None = None,
    page_size: int | None = None,
    is_framing_varied: bool = False,
) -> FakeCodexAppServer:
    """The root idle with a background terminal left running, and a subagent working on its own foreground command."""
    return FakeCodexAppServer(
        threads=threads,
        background_terminals={
            _ROOT: (codex_background_terminal(_TERMINAL),),
            _SUBAGENT: (codex_background_terminal(_SECOND_TERMINAL, command="sleep 60"),),
        },
        replies_by_method=replies_by_method or {},
        page_size=page_size,
        is_framing_varied=is_framing_varied,
    )


_EXPECTED_RECORDS: Final[dict[str, str]] = {
    f"task-{_TERMINAL}": f"seen:{_NOW} kind:SHELL native_type:backgroundTerminals\n",
    f"task-{_SUBAGENT}": f"seen:{_NOW} kind:SUBAGENT native_type:subagent\n",
    "lease-other-writer": "seen:1795099000\n",
}


def _state_dir_with_earlier_records(tmp_path: Path) -> Path:
    record_dir = tmp_path / RECORD_DIR_NAME
    record_dir.mkdir(parents=True)
    for name, line in _EARLIER_RECORDS.items():
        (record_dir / name).write_text(line)
    return tmp_path


def _records(state_dir: Path) -> dict[str, str]:
    return {path.name: path.read_text() for path in (state_dir / RECORD_DIR_NAME).iterdir()}


@pytest.mark.parametrize(
    "server",
    [
        pytest.param(_idle_root_with_a_terminal_and_a_working_subagent(), id="plain"),
        pytest.param(_idle_root_with_a_terminal_and_a_working_subagent(page_size=1), id="paged"),
        pytest.param(
            _idle_root_with_a_terminal_and_a_working_subagent(
                threads=({**codex_thread(_ROOT), "preview": "x" * 210_000}, *_THREADS[1:]), is_framing_varied=True
            ),
            id="pings_fragments_and_long_frames",
        ),
    ],
)
def test_a_pass_rewrites_the_records_from_what_the_app_server_lists(
    server: FakeCodexAppServer, short_socket_path: Path, tmp_path: Path
) -> None:
    state_dir = _state_dir_with_earlier_records(tmp_path)

    with serving_fake_codex_app_server(short_socket_path, server):
        outcome = sync_records(state_dir, short_socket_path, _NOW)

    assert _records(state_dir) == _EXPECTED_RECORDS
    assert outcome == f"recorded: task-{_SUBAGENT} task-{_TERMINAL}"


_UNREADABLE_REPLIES: Final[dict[str, dict[str, Any]]] = {
    "error": {"thread/backgroundTerminals/list": {"error": {"code": -32600, "message": "requires experimentalApi"}}},
    "unknown_list_shape": {"thread/backgroundTerminals/list": {"result": {"data": "none", "nextCursor": None}}},
    "unknown_terminal_shape": {"thread/backgroundTerminals/list": {"result": {"data": [{}], "nextCursor": None}}},
    "unknown_thread_shape": {"thread/read": {"result": {"thread": {"id": _ROOT, "status": "idle"}}}},
    "no_result": {"thread/loaded/list": {"id_only": True}},
}


@pytest.mark.parametrize("replies", list(_UNREADABLE_REPLIES.values()), ids=list(_UNREADABLE_REPLIES))
def test_a_failed_read_leaves_the_records_as_they_were(
    replies: dict[str, dict[str, Any]], short_socket_path: Path, tmp_path: Path
) -> None:
    state_dir = _state_dir_with_earlier_records(tmp_path)
    server = _idle_root_with_a_terminal_and_a_working_subagent(replies_by_method=replies)

    with serving_fake_codex_app_server(short_socket_path, server):
        outcome = sync_records(state_dir, short_socket_path, _NOW)

    assert _records(state_dir) == _EARLIER_RECORDS
    assert outcome.startswith("unchanged: ")


def test_an_app_server_that_never_answers_leaves_the_records_as_they_were(
    short_socket_path: Path, tmp_path: Path
) -> None:
    state_dir = _state_dir_with_earlier_records(tmp_path)

    with serving_fake_codex_app_server(short_socket_path, FakeCodexAppServer(is_silent=True)):
        outcome = sync_records(state_dir, short_socket_path, _NOW, time_limit_seconds=0.5)

    assert _records(state_dir) == _EARLIER_RECORDS
    assert outcome.startswith("unchanged: ")


def test_with_no_socket_this_writers_records_are_removed(short_socket_path: Path, tmp_path: Path) -> None:
    state_dir = _state_dir_with_earlier_records(tmp_path)

    outcome = sync_records(state_dir, short_socket_path, _NOW)

    assert _records(state_dir) == {"lease-other-writer": "seen:1795099000\n"}
    assert outcome.startswith("cleared: ")


def test_with_nothing_listening_on_the_socket_this_writers_records_are_removed(
    short_socket_path: Path, tmp_path: Path
) -> None:
    state_dir = _state_dir_with_earlier_records(tmp_path)
    abandoned = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    abandoned.bind(str(short_socket_path))
    abandoned.close()

    outcome = sync_records(state_dir, short_socket_path, _NOW)

    assert _records(state_dir) == {"lease-other-writer": "seen:1795099000\n"}
    assert outcome.startswith("cleared: ")


def test_a_subagent_waiting_on_an_approval_is_recorded_as_working(short_socket_path: Path, tmp_path: Path) -> None:
    server = FakeCodexAppServer(
        threads=(
            codex_thread(_ROOT),
            codex_thread(
                _SUBAGENT,
                status_type="active",
                active_flags=["waitingOnApproval"],
                parent_thread_id=_ROOT,
                thread_source="subagent",
            ),
        )
    )

    with serving_fake_codex_app_server(short_socket_path, server):
        sync_records(tmp_path, short_socket_path, _NOW)

    assert _records(tmp_path) == {f"task-{_SUBAGENT}": f"seen:{_NOW} kind:SUBAGENT native_type:subagent\n"}


def test_a_pass_waits_for_the_pass_holding_the_lock(short_socket_path: Path, tmp_path: Path) -> None:
    state_dir = _state_dir_with_earlier_records(tmp_path)
    lock_path = pass_lock_path(state_dir)
    lock_path.parent.mkdir(parents=True)

    with serving_fake_codex_app_server(short_socket_path, _idle_root_with_a_terminal_and_a_working_subagent()):
        with ThreadPoolExecutor(max_workers=1) as executor:
            with lock_path.open("a") as held_lock:
                fcntl.flock(held_lock, fcntl.LOCK_EX)
                waiting_pass = executor.submit(sync_records, state_dir, short_socket_path, _NOW)
                done, _ = wait([waiting_pass], timeout=0.5)
                records_while_held = _records(state_dir)
            outcome = waiting_pass.result(timeout=10)

    assert not done
    assert records_while_held == _EARLIER_RECORDS
    assert _records(state_dir) == _EXPECTED_RECORDS
    assert outcome.startswith("recorded: ")


def _agent_with_its_turn_open(tmp_path: Path) -> Path:
    """An agent state dir holding the turn hook and the record writer, with `active` naming ``_TURN``."""
    state_dir = tmp_path / "agent"
    provision_commands_dir(state_dir, [TURN_HOOK_SCRIPT_NAME, RECORD_BACKGROUND_TASKS_SCRIPT_NAME])
    (state_dir / APP_SERVER_THREAD_FILENAME).write_text(_ROOT)
    (state_dir / ACTIVE_MARKER_FILENAME).write_text(f"{_TURN}\n")
    return state_dir


def _root_turn_open_with_terminals(*terminal_ids: str) -> FakeCodexAppServer:
    """codex as its root-turn hooks see it: the root's turn still open, with ``terminal_ids`` running."""
    return FakeCodexAppServer(
        threads=(
            codex_thread(_ROOT, status_type="active"),
            codex_thread(_TITLE_THREAD, status_type="active", thread_source="thread_title"),
        ),
        background_terminals={_ROOT: tuple(codex_background_terminal(terminal_id) for terminal_id in terminal_ids)},
    )


def _run_turn_hook(state_dir: Path, event: str, **fields: Any) -> subprocess.CompletedProcess[str]:
    payload = {"session_id": _ROOT, "turn_id": _TURN, "hook_event_name": event, **fields}
    return subprocess.run(
        ["bash", str(state_dir / "commands" / TURN_HOOK_SCRIPT_NAME), event],
        input=json.dumps(payload, separators=(",", ":")),
        env={**os.environ, "MNGR_AGENT_STATE_DIR": str(state_dir)},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


def _record_kinds(state_dir: Path) -> dict[str, str]:
    return {name: line.split(" ", 1)[1] for name, line in _records(state_dir).items()}


def _active(state_dir: Path) -> str | None:
    active = state_dir / ACTIVE_MARKER_FILENAME
    return active.read_text() if active.exists() else None


@pytest.mark.parametrize(
    ("event", "active_after"),
    [pytest.param("Stop", f"{_TURN}\n", id="Stop"), pytest.param("Interrupt", None, id="Interrupt")],
)
def test_a_root_turn_end_records_the_terminal_its_turn_left_running(
    event: str, active_after: str | None, tmp_path: Path
) -> None:
    state_dir = _agent_with_its_turn_open(tmp_path)

    with serving_fake_codex_app_server_for_agent(state_dir, _root_turn_open_with_terminals(_TERMINAL)):
        result = _run_turn_hook(state_dir, event)

    assert result.stdout == ""
    assert _active(state_dir) == active_after
    assert _record_kinds(state_dir) == {f"task-{_TERMINAL}": "kind:SHELL native_type:backgroundTerminals\n"}


def test_the_stop_after_another_stop_hook_blocked_rewrites_the_records(tmp_path: Path) -> None:
    """The first Stop's terminal ended during the continuation, which left another running."""
    state_dir = _agent_with_its_turn_open(tmp_path)

    with serving_fake_codex_app_server_for_agent(state_dir, _root_turn_open_with_terminals(_TERMINAL)):
        _run_turn_hook(state_dir, "Stop", stop_hook_active=False)
    records_while_continuing = _record_kinds(state_dir)
    with serving_fake_codex_app_server_for_agent(state_dir, _root_turn_open_with_terminals(_SECOND_TERMINAL)):
        _run_turn_hook(state_dir, "Stop", stop_hook_active=True)

    assert records_while_continuing == {f"task-{_TERMINAL}": "kind:SHELL native_type:backgroundTerminals\n"}
    assert _active(state_dir) == f"{_TURN}\n"
    assert _record_kinds(state_dir) == {f"task-{_SECOND_TERMINAL}": "kind:SHELL native_type:backgroundTerminals\n"}
