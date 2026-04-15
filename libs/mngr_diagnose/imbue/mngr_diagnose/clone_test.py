from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.mngr.errors import UserInputError
from imbue.mngr_diagnose.clone import ensure_mngr_clone


def _finished(stdout: str = "", command: tuple[str, ...] = ("fake",)) -> FinishedProcess:
    return FinishedProcess(
        returncode=0,
        stdout=stdout,
        stderr="",
        command=command,
        is_output_already_logged=False,
    )


def test_fresh_clone(tmp_path: Path, cg: ConcurrencyGroup, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh clone when directory does not exist."""
    clone_dir = tmp_path / "mngr-clone"

    calls: list[list[str]] = []

    def fake_run(self: object, cmd: list[str] | tuple[str, ...], timeout: float = 30.0, **kwargs: object) -> FinishedProcess:
        cmd_list = list(cmd)
        calls.append(cmd_list)
        if "clone" in cmd_list:
            clone_dir.mkdir(parents=True, exist_ok=True)
        return _finished(command=tuple(cmd_list))

    monkeypatch.setattr(ConcurrencyGroup, "run_process_to_completion", fake_run)

    ensure_mngr_clone(clone_dir, cg)

    assert len(calls) == 1
    assert "clone" in calls[0]
    assert "--depth" in calls[0]
    assert str(clone_dir) in calls[0]


def test_existing_clone_pulls(tmp_path: Path, cg: ConcurrencyGroup, monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing clone on main triggers a pull."""
    clone_dir = tmp_path / "mngr-clone"
    clone_dir.mkdir()

    calls: list[list[str]] = []

    def fake_run(self: object, cmd: list[str] | tuple[str, ...], timeout: float = 30.0, **kwargs: object) -> FinishedProcess:
        cmd_list = list(cmd)
        calls.append(cmd_list)
        if "rev-parse" in cmd_list:
            return _finished(stdout="main\n", command=tuple(cmd_list))
        return _finished(command=tuple(cmd_list))

    monkeypatch.setattr(ConcurrencyGroup, "run_process_to_completion", fake_run)

    ensure_mngr_clone(clone_dir, cg)

    assert len(calls) == 2
    assert "rev-parse" in calls[0]
    assert "pull" in calls[1]


def test_existing_clone_wrong_branch(tmp_path: Path, cg: ConcurrencyGroup, monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing clone on wrong branch raises UserInputError."""
    clone_dir = tmp_path / "mngr-clone"
    clone_dir.mkdir()

    def fake_run(self: object, cmd: list[str] | tuple[str, ...], timeout: float = 30.0, **kwargs: object) -> FinishedProcess:
        cmd_list = list(cmd)
        if "rev-parse" in cmd_list:
            return _finished(stdout="feature-branch\n", command=tuple(cmd_list))
        return _finished(command=tuple(cmd_list))

    monkeypatch.setattr(ConcurrencyGroup, "run_process_to_completion", fake_run)

    with pytest.raises(UserInputError, match="feature-branch"):
        ensure_mngr_clone(clone_dir, cg)
