from pathlib import Path
from subprocess import TimeoutExpired
from types import SimpleNamespace
from unittest.mock import MagicMock

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.utils.testing import run_git_command
from imbue.mngr_kanpan.data_source import FIELD_COMMITS_AHEAD
from imbue.mngr_kanpan.data_sources.git_info import CommitsAheadField
from imbue.mngr_kanpan.data_sources.git_info import GitInfoDataSource
from imbue.mngr_kanpan.data_sources.git_info import _get_all_commits_ahead
from imbue.mngr_kanpan.testing import make_agent_details

# === _get_all_commits_ahead ===


def test_get_all_commits_ahead_empty() -> None:
    with ConcurrencyGroup(name="test") as cg:
        result = _get_all_commits_ahead([], cg)
    assert result == {}


def test_get_all_commits_ahead_with_upstream(temp_git_repo: Path) -> None:
    """Real git repo with an upstream tracking branch."""
    run_git_command(temp_git_repo, "checkout", "-b", "feature")
    run_git_command(temp_git_repo, "branch", "--set-upstream-to=main")
    # Should be 0 ahead before any new commits
    with ConcurrencyGroup(name="test") as cg:
        result = _get_all_commits_ahead([temp_git_repo], cg)
    assert result[temp_git_repo] == 0
    # Make one commit ahead
    (temp_git_repo / "file.txt").write_text("new content")
    run_git_command(temp_git_repo, "add", "file.txt")
    run_git_command(temp_git_repo, "commit", "-m", "ahead commit")
    with ConcurrencyGroup(name="test") as cg:
        result = _get_all_commits_ahead([temp_git_repo], cg)
    assert result[temp_git_repo] == 1


def test_get_all_commits_ahead_no_upstream(temp_git_repo: Path) -> None:
    """Real git repo without an upstream -- returns None."""
    with ConcurrencyGroup(name="test") as cg:
        result = _get_all_commits_ahead([temp_git_repo], cg)
    assert result[temp_git_repo] is None


def test_get_all_commits_ahead_nonexistent_dir(tmp_path: Path) -> None:
    """Non-existent directory -- returns None."""
    missing = tmp_path / "does_not_exist"
    with ConcurrencyGroup(name="test") as cg:
        result = _get_all_commits_ahead([missing], cg)
    assert result[missing] is None


def test_get_all_commits_ahead_launch_error(tmp_path: Path) -> None:
    """ConcurrencyGroupError on process launch -- returns None."""
    cg = MagicMock()
    cg.run_process_in_background.side_effect = ConcurrencyGroupError("failed")
    result = _get_all_commits_ahead([tmp_path], cg)
    assert result[tmp_path] is None


def test_get_all_commits_ahead_wait_timeout(tmp_path: Path) -> None:
    """TimeoutExpired on process wait -- returns None."""
    proc = MagicMock()
    proc.wait.side_effect = TimeoutExpired(["git"], 10.0)
    cg = MagicMock()
    cg.run_process_in_background.return_value = proc
    result = _get_all_commits_ahead([tmp_path], cg)
    assert result[tmp_path] is None


# === compute ===


def test_compute_local_agent_with_upstream(temp_git_repo: Path) -> None:
    """Local agent with a real git repo that has an upstream."""
    run_git_command(temp_git_repo, "checkout", "-b", "feature")
    run_git_command(temp_git_repo, "branch", "--set-upstream-to=main")

    ds = GitInfoDataSource()
    agent = make_agent_details(name="agent-1", provider_name="local", work_dir=temp_git_repo)
    with ConcurrencyGroup(name="test") as cg:
        ctx: MngrContext = SimpleNamespace(concurrency_group=cg)  # ty: ignore[invalid-assignment]
        fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert errors == []
    assert agent.name in fields
    ca = fields[agent.name][FIELD_COMMITS_AHEAD]
    assert isinstance(ca, CommitsAheadField)
    assert ca.has_work_dir is True
    assert ca.count == 0


def test_compute_remote_agent_no_work_dir() -> None:
    ds = GitInfoDataSource()
    agent = make_agent_details(name="agent-1", provider_name="modal")
    with ConcurrencyGroup(name="test") as cg:
        ctx: MngrContext = SimpleNamespace(concurrency_group=cg)  # ty: ignore[invalid-assignment]
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
    with ConcurrencyGroup(name="test") as cg:
        ctx: MngrContext = SimpleNamespace(concurrency_group=cg)  # ty: ignore[invalid-assignment]
        fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert errors == []
    assert agent.name in fields
    ca = fields[agent.name][FIELD_COMMITS_AHEAD]
    assert isinstance(ca, CommitsAheadField)
    assert ca.has_work_dir is False
