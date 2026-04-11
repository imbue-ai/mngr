from pathlib import Path
from subprocess import TimeoutExpired
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_kanpan.data_source import CommitsAheadField
from imbue.mngr_kanpan.data_source import FIELD_COMMITS_AHEAD
from imbue.mngr_kanpan.data_sources.git_info import GitInfoDataSource
from imbue.mngr_kanpan.data_sources.git_info import _get_all_commits_ahead
from imbue.mngr_kanpan.testing import make_agent_details


def test_git_info_data_source_name() -> None:
    ds = GitInfoDataSource()
    assert ds.name == "git_info"


def test_git_info_columns() -> None:
    ds = GitInfoDataSource()
    assert ds.columns == {FIELD_COMMITS_AHEAD: "GIT"}


def test_git_info_field_types() -> None:
    ds = GitInfoDataSource()
    assert ds.field_types == {FIELD_COMMITS_AHEAD: CommitsAheadField}


# === _get_all_commits_ahead ===


def _make_mock_proc(stdout: str = "", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.read_stdout.return_value = stdout
    proc.returncode = returncode
    proc.wait.return_value = returncode
    return proc


def test_get_all_commits_ahead_empty() -> None:
    cg = MagicMock()
    result = _get_all_commits_ahead([], cg)
    assert result == {}


def test_get_all_commits_ahead_success(tmp_path: Path) -> None:
    proc = _make_mock_proc(stdout="3\n", returncode=0)
    cg = MagicMock()
    cg.run_process_in_background.return_value = proc
    result = _get_all_commits_ahead([tmp_path], cg)
    assert result[tmp_path] == 3


def test_get_all_commits_ahead_nonzero_exit(tmp_path: Path) -> None:
    proc = _make_mock_proc(stdout="", returncode=1)
    cg = MagicMock()
    cg.run_process_in_background.return_value = proc
    result = _get_all_commits_ahead([tmp_path], cg)
    assert result[tmp_path] is None


def test_get_all_commits_ahead_invalid_output(tmp_path: Path) -> None:
    proc = _make_mock_proc(stdout="not-a-number\n", returncode=0)
    cg = MagicMock()
    cg.run_process_in_background.return_value = proc
    result = _get_all_commits_ahead([tmp_path], cg)
    assert result[tmp_path] is None


def test_get_all_commits_ahead_launch_error(tmp_path: Path) -> None:
    cg = MagicMock()
    cg.run_process_in_background.side_effect = ConcurrencyGroupError("failed")
    result = _get_all_commits_ahead([tmp_path], cg)
    assert result[tmp_path] is None


def test_get_all_commits_ahead_wait_timeout(tmp_path: Path) -> None:
    proc = MagicMock()
    proc.wait.side_effect = TimeoutExpired(["git"], 10.0)
    cg = MagicMock()
    cg.run_process_in_background.return_value = proc
    result = _get_all_commits_ahead([tmp_path], cg)
    assert result[tmp_path] is None


# === compute ===


def test_compute_local_agent_with_work_dir(tmp_path: Path) -> None:
    ds = GitInfoDataSource()
    agent = make_agent_details(name="agent-1", provider_name="local", work_dir=tmp_path)
    proc = _make_mock_proc(stdout="5\n", returncode=0)
    cg = MagicMock()
    cg.run_process_in_background.return_value = proc
    ctx = cast(MngrContext, SimpleNamespace(concurrency_group=cg))
    fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert errors == []
    assert agent.name in fields
    ca = fields[agent.name][FIELD_COMMITS_AHEAD]
    assert isinstance(ca, CommitsAheadField)
    assert ca.has_work_dir is True
    assert ca.count == 5


def test_compute_remote_agent_no_work_dir() -> None:
    ds = GitInfoDataSource()
    agent = make_agent_details(name="agent-1", provider_name="modal")
    cg = MagicMock()
    ctx = cast(MngrContext, SimpleNamespace(concurrency_group=cg))
    fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert errors == []
    assert agent.name in fields
    ca = fields[agent.name][FIELD_COMMITS_AHEAD]
    assert isinstance(ca, CommitsAheadField)
    assert ca.has_work_dir is False
    assert ca.count is None


def test_compute_nonexistent_work_dir() -> None:
    ds = GitInfoDataSource()
    agent = make_agent_details(
        name="agent-1",
        provider_name="local",
        work_dir=Path("/nonexistent/dir/that/does/not/exist"),
    )
    cg = MagicMock()
    ctx = cast(MngrContext, SimpleNamespace(concurrency_group=cg))
    fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert errors == []
    # Work dir doesn't exist, so treated as no work dir
    assert agent.name in fields
    ca = fields[agent.name][FIELD_COMMITS_AHEAD]
    assert isinstance(ca, CommitsAheadField)
    assert ca.has_work_dir is False
