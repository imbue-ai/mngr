from collections.abc import Sequence
from pathlib import Path
from subprocess import TimeoutExpired
from typing import Any

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.mngr.utils.testing import run_git_command
from imbue.mngr_kanpan.data_source import FIELD_COMMITS_AHEAD
from imbue.mngr_kanpan.data_sources.git_info import CommitsAheadField
from imbue.mngr_kanpan.data_sources.git_info import GitInfoDataSource
from imbue.mngr_kanpan.data_sources.git_info import _get_all_commits_ahead
from imbue.mngr_kanpan.testing import make_agent_details
from imbue.mngr_kanpan.testing import make_mngr_ctx_with_cg

# These hand-rolled fakes intentionally couple to the launch/wait seam of
# ConcurrencyGroup -- run_process_in_background returning a process whose
# .wait()/.returncode/.read_stdout() we drive. There is no way to force a
# launch failure or a non-integer rev-list output through a real git repo, so
# the error/parse branches in _get_all_commits_ahead are only reachable via a
# fake. We use small concrete fakes (not unittest.mock) so the substituted
# behaviour is explicit and the no-unittest.mock style rule is honored.


class _FakeProc:
    """A fake RunningProcess exposing only what _get_all_commits_ahead reads."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        wait_error: Exception | None = None,
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._wait_error = wait_error

    def wait(self, timeout: float | None = None) -> int:
        if self._wait_error is not None:
            raise self._wait_error
        return self.returncode

    def read_stdout(self) -> str:
        return self._stdout


class _FakeConcurrencyGroup:
    """A fake ConcurrencyGroup that returns a preset proc (or raises on launch)."""

    def __init__(
        self,
        *,
        proc: _FakeProc | None = None,
        launch_error: Exception | None = None,
    ) -> None:
        self._proc = proc
        self._launch_error = launch_error

    def run_process_in_background(self, command: Sequence[str], **kwargs: Any) -> _FakeProc:
        if self._launch_error is not None:
            raise self._launch_error
        assert self._proc is not None
        return self._proc


# === _get_all_commits_ahead ===


def test_get_all_commits_ahead_empty(test_cg: ConcurrencyGroup) -> None:
    result = _get_all_commits_ahead([], test_cg)
    assert result == {}


def test_get_all_commits_ahead_with_upstream(temp_git_repo: Path, test_cg: ConcurrencyGroup) -> None:
    """Real git repo with an upstream tracking branch."""
    run_git_command(temp_git_repo, "checkout", "-b", "feature")
    run_git_command(temp_git_repo, "branch", "--set-upstream-to=main")
    # Should be 0 ahead before any new commits
    result = _get_all_commits_ahead([temp_git_repo], test_cg)
    assert result[temp_git_repo] == 0
    # Make one commit ahead
    (temp_git_repo / "file.txt").write_text("new content")
    run_git_command(temp_git_repo, "add", "file.txt")
    run_git_command(temp_git_repo, "commit", "-m", "ahead commit")
    result = _get_all_commits_ahead([temp_git_repo], test_cg)
    assert result[temp_git_repo] == 1


def test_get_all_commits_ahead_no_upstream(temp_git_repo: Path, test_cg: ConcurrencyGroup) -> None:
    """Real git repo without an upstream -- returns None."""
    result = _get_all_commits_ahead([temp_git_repo], test_cg)
    assert result[temp_git_repo] is None


def test_get_all_commits_ahead_nonexistent_dir(tmp_path: Path, test_cg: ConcurrencyGroup) -> None:
    """Non-existent directory -- returns None."""
    missing = tmp_path / "does_not_exist"
    result = _get_all_commits_ahead([missing], test_cg)
    assert result[missing] is None


def test_get_all_commits_ahead_launch_error(tmp_path: Path) -> None:
    """ConcurrencyGroupError on process launch -- returns None (launch seam)."""
    cg = _FakeConcurrencyGroup(launch_error=ConcurrencyGroupError("failed"))
    result = _get_all_commits_ahead([tmp_path], cg)  # ty: ignore[invalid-argument-type]
    assert result[tmp_path] is None


def test_get_all_commits_ahead_wait_timeout(tmp_path: Path) -> None:
    """TimeoutExpired on process wait -- returns None (wait seam)."""
    proc = _FakeProc(wait_error=TimeoutExpired(["git"], 10.0))
    cg = _FakeConcurrencyGroup(proc=proc)
    result = _get_all_commits_ahead([tmp_path], cg)  # ty: ignore[invalid-argument-type]
    assert result[tmp_path] is None


def test_get_all_commits_ahead_non_integer_stdout(tmp_path: Path) -> None:
    """returncode 0 but non-integer stdout hits the ValueError branch -- returns None."""
    proc = _FakeProc(returncode=0, stdout="garbage")
    cg = _FakeConcurrencyGroup(proc=proc)
    result = _get_all_commits_ahead([tmp_path], cg)  # ty: ignore[invalid-argument-type]
    assert result[tmp_path] is None


# === compute ===


def test_compute_local_agent_with_upstream(temp_git_repo: Path, test_cg: ConcurrencyGroup) -> None:
    """Local agent with a real git repo that has an upstream."""
    run_git_command(temp_git_repo, "checkout", "-b", "feature")
    run_git_command(temp_git_repo, "branch", "--set-upstream-to=main")

    ds = GitInfoDataSource()
    agent = make_agent_details(name="agent-1", provider_name="local", work_dir=temp_git_repo)
    ctx = make_mngr_ctx_with_cg(test_cg)
    fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert errors == []
    assert agent.name in fields
    ca = fields[agent.name][FIELD_COMMITS_AHEAD]
    assert isinstance(ca, CommitsAheadField)
    assert ca.has_work_dir is True
    assert ca.count == 0


def test_compute_remote_agent_no_work_dir(test_cg: ConcurrencyGroup) -> None:
    ds = GitInfoDataSource()
    agent = make_agent_details(name="agent-1", provider_name="modal")
    ctx = make_mngr_ctx_with_cg(test_cg)
    fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert errors == []
    assert agent.name in fields
    ca = fields[agent.name][FIELD_COMMITS_AHEAD]
    assert isinstance(ca, CommitsAheadField)
    assert ca.has_work_dir is False
    assert ca.count is None


def test_compute_nonexistent_work_dir(test_cg: ConcurrencyGroup) -> None:
    ds = GitInfoDataSource()
    agent = make_agent_details(
        name="agent-1",
        provider_name="local",
        work_dir=Path("/nonexistent/dir/that/does/not/exist"),
    )
    ctx = make_mngr_ctx_with_cg(test_cg)
    fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert errors == []
    assert agent.name in fields
    ca = fields[agent.name][FIELD_COMMITS_AHEAD]
    assert isinstance(ca, CommitsAheadField)
    assert ca.has_work_dir is False
