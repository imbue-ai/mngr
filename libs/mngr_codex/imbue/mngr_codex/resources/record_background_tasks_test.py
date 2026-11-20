"""Unit tests for record_background_tasks.py: which records codex's listing yields, and how they are written."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import pytest

from imbue.mngr.hosts.background_tasks import BACKGROUND_TASK_DIR_NAME
from imbue.mngr.hosts.background_tasks import parse_background_tasks
from imbue.mngr.primitives import BackgroundTaskKind
from imbue.mngr_codex.codex_config import get_codex_app_server_socket_path
from imbue.mngr_codex.codex_config import get_codex_home
from imbue.mngr_codex.resources.record_background_tasks import CodexThread
from imbue.mngr_codex.resources.record_background_tasks import RECORD_DIR_NAME
from imbue.mngr_codex.resources.record_background_tasks import app_server_socket_path
from imbue.mngr_codex.resources.record_background_tasks import plan_records
from imbue.mngr_codex.resources.record_background_tasks import write_records

_ROOT: Final[str] = "01a1f2b2-6d9c-7b93-87cc-4beb530e40ef"
_TITLE_THREAD: Final[str] = "01a1f2b4-0062-7d10-ab9e-7c3b52147701"
_SUBAGENT: Final[str] = "01a1f2b8-4a8a-7060-9962-1bb9cb835f6e"
_TERMINAL: Final[str] = "exec-44abb3ff-4033-431a-a745-8f7cfbd78908"
_SHELL: Final[tuple[str, str]] = ("SHELL", "backgroundTerminals")
_WORKING_SUBAGENT: Final[tuple[str, str]] = ("SUBAGENT", "subagent")


def _thread(
    thread_id: str,
    is_turn_open: bool = False,
    parent_thread_id: str | None = None,
    terminal_ids: Sequence[str] = (),
    thread_source: str | None = None,
) -> CodexThread:
    return CodexThread(
        thread_id=thread_id,
        parent_thread_id=parent_thread_id,
        thread_source=thread_source,
        is_turn_open=is_turn_open,
        background_terminal_ids=tuple(terminal_ids),
    )


def _working_subagent(terminal_ids: Sequence[str] = ()) -> CodexThread:
    return _thread(
        _SUBAGENT, is_turn_open=True, parent_thread_id=_ROOT, terminal_ids=terminal_ids, thread_source="subagent"
    )


def _root_with_its_turn_open() -> CodexThread:
    return _thread(_ROOT, is_turn_open=True, terminal_ids=[_TERMINAL])


@pytest.mark.parametrize(
    ("threads", "expected_records"),
    [
        pytest.param([_thread(_ROOT)], {}, id="nothing_listed"),
        pytest.param(
            [_thread(_ROOT, terminal_ids=[_TERMINAL])], {f"task-{_TERMINAL}": _SHELL}, id="idle_root_terminal"
        ),
        pytest.param(
            [_thread(_ROOT), _working_subagent()], {f"task-{_SUBAGENT}": _WORKING_SUBAGENT}, id="working_subagent"
        ),
        pytest.param(
            [_thread(_ROOT), _working_subagent(terminal_ids=[_TERMINAL])],
            {f"task-{_SUBAGENT}": _WORKING_SUBAGENT},
            id="working_subagent_covers_its_own_commands",
        ),
        pytest.param(
            [_thread(_ROOT), _thread(_SUBAGENT, parent_thread_id=_ROOT, terminal_ids=[_TERMINAL])],
            {f"task-{_TERMINAL}": _SHELL},
            id="idle_subagent_terminal",
        ),
        pytest.param(
            [_thread(_ROOT), _thread(_SUBAGENT, is_turn_open=True, parent_thread_id=_ROOT)],
            {f"task-{_SUBAGENT}": ("SUBAGENT", "")},
            id="subagent_without_a_thread_source",
        ),
        pytest.param(
            [_thread(_ROOT, terminal_ids=["exec/1 2"])], {"task-exec_1_2": _SHELL}, id="id_outside_the_name_alphabet"
        ),
        pytest.param([_root_with_its_turn_open()], {}, id="open_turn_adds_no_terminal"),
        pytest.param(
            [_root_with_its_turn_open(), _working_subagent()],
            {f"task-{_SUBAGENT}": _WORKING_SUBAGENT},
            id="open_turn_with_a_working_subagent",
        ),
        pytest.param(
            [_thread(_ROOT, terminal_ids=[_TERMINAL]), _thread(_TITLE_THREAD, is_turn_open=True)],
            {f"task-{_TERMINAL}": _SHELL},
            id="another_top_level_threads_open_turn",
        ),
    ],
)
def test_plan_records_names_the_background_work_codex_lists(
    threads: list[CodexThread], expected_records: dict[str, tuple[str, str]]
) -> None:
    assert plan_records(threads, frozenset()) == expected_records


def test_a_top_level_thread_with_its_turn_open_keeps_the_terminal_records_already_written() -> None:
    records = plan_records([_root_with_its_turn_open()], frozenset({f"task-{_TERMINAL}", "task-gone"}))

    assert records == {f"task-{_TERMINAL}": _SHELL}


def test_the_thread_whose_turn_is_ending_has_its_terminals_recorded_though_codex_reports_its_turn_open() -> None:
    threads = [_root_with_its_turn_open(), _thread(_TITLE_THREAD, is_turn_open=True)]

    assert plan_records(threads, frozenset(), ended_thread_id=_ROOT) == {f"task-{_TERMINAL}": _SHELL}


def test_write_records_writes_each_line_then_removes_only_this_writers_absent_records(tmp_path: Path) -> None:
    record_dir = tmp_path / RECORD_DIR_NAME
    record_dir.mkdir()
    (record_dir / "task-ended").write_text("seen:1795099000 kind:SHELL native_type:backgroundTerminals\n")
    (record_dir / "lease-other-writer").write_text("seen:1795099000\n")
    (record_dir / f"task-{_SUBAGENT}").write_text("seen:1795099000 kind:SUBAGENT native_type:subagent\n")

    write_records(
        record_dir,
        {
            f"task-{_TERMINAL}": _SHELL,
            f"task-{_SUBAGENT}": _WORKING_SUBAGENT,
            "task-review": ("SUBAGENT", "sub agent/x"),
        },
        1795100000,
    )

    assert {path.name: path.read_text() for path in record_dir.iterdir()} == {
        f"task-{_TERMINAL}": "seen:1795100000 kind:SHELL native_type:backgroundTerminals\n",
        f"task-{_SUBAGENT}": "seen:1795100000 kind:SUBAGENT native_type:subagent\n",
        "task-review": "seen:1795100000 kind:SUBAGENT native_type:sub%20agent%2Fx\n",
        "lease-other-writer": "seen:1795099000\n",
    }


def test_written_records_read_back_as_the_kind_and_native_type_the_writer_meant(tmp_path: Path) -> None:
    record_dir = tmp_path / BACKGROUND_TASK_DIR_NAME
    write_records(
        record_dir,
        {f"task-{_TERMINAL}": _SHELL, f"task-{_SUBAGENT}": _WORKING_SUBAGENT, "task-x": ("SUBAGENT", "")},
        1795100000,
    )

    raw = "\n".join(f"{path.name}\t{path.read_text().rstrip()}" for path in sorted(record_dir.iterdir()))
    tasks = {task.name: (task.kind, task.native_type, task.last_seen_at) for task in parse_background_tasks(raw)}

    assert {name: (kind, native_type) for name, (kind, native_type, _) in tasks.items()} == {
        f"task-{_TERMINAL}": (BackgroundTaskKind.SHELL, "backgroundTerminals"),
        f"task-{_SUBAGENT}": (BackgroundTaskKind.SUBAGENT, "subagent"),
        "task-x": (BackgroundTaskKind.SUBAGENT, ""),
    }
    assert {int(seen.timestamp()) for _, _, seen in tasks.values() if seen is not None} == {1795100000}


def test_write_records_with_nothing_listed_removes_this_writers_records(tmp_path: Path) -> None:
    record_dir = tmp_path / RECORD_DIR_NAME
    record_dir.mkdir()
    (record_dir / "task-ended").write_text("seen:1795099000\n")

    write_records(record_dir, {}, 1795100000)

    assert list(record_dir.iterdir()) == []


def test_write_records_with_nothing_listed_and_no_record_dir_creates_none(tmp_path: Path) -> None:
    write_records(tmp_path / RECORD_DIR_NAME, {}, 1795100000)

    assert not (tmp_path / RECORD_DIR_NAME).exists()


def test_the_record_dir_is_the_one_mngr_reads() -> None:
    assert RECORD_DIR_NAME == BACKGROUND_TASK_DIR_NAME


def test_the_socket_is_the_one_mngr_launches_the_app_server_on(tmp_path: Path) -> None:
    state_dir = tmp_path / "agents" / "agent-0f2c4c9e1d8b4f6aa1b3c5d7e9f01234"

    assert app_server_socket_path(state_dir) == get_codex_app_server_socket_path(get_codex_home(state_dir))
