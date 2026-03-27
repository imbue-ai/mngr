import os
import subprocess
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import parse_agents_from_mngr_output

_GIT_TEST_ENV_KEYS: Final[dict[str, str]] = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@test",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@test",
}


def _git_test_env(tmp_path: Path) -> dict[str, str]:
    """Build an environment dict for git commands in tests.

    Uses deterministic author/committer info and a minimal PATH so that
    git operations are reproducible and don't depend on the user's config.
    """
    return {
        **_GIT_TEST_ENV_KEYS,
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }


def init_and_commit_git_repo(repo_dir: Path, tmp_path: Path, allow_empty: bool = False) -> None:
    """Initialize a git repo and commit all files in repo_dir.

    If allow_empty is True, creates an empty commit even when there are no
    staged files. Otherwise, all files in the directory are staged and committed.
    """
    cg = ConcurrencyGroup(name="test-git-init")
    with cg:
        cg.run_process_to_completion(command=["git", "init"], cwd=repo_dir)
        cg.run_process_to_completion(command=["git", "add", "."], cwd=repo_dir)

        commit_cmd = ["git", "commit", "-m", "init"]
        if allow_empty:
            commit_cmd.append("--allow-empty")

        cg.run_process_to_completion(
            command=commit_cmd,
            cwd=repo_dir,
            env=_git_test_env(tmp_path),
        )


def make_git_repo(tmp_path: Path, name: str = "repo") -> Path:
    """Create a minimal git repo with a committed file.

    Shared helper for tests that need a local git repo to operate on.
    Creates a directory under tmp_path with a single ``hello.txt`` file,
    initializes a git repo, and commits the file.
    """
    repo = tmp_path / name
    repo.mkdir()
    (repo / "hello.txt").write_text("hello")
    init_and_commit_git_repo(repo, tmp_path)
    return repo


def add_and_commit_git_repo(repo_dir: Path, tmp_path: Path, message: str = "update") -> None:
    """Stage all changes and commit in an existing git repo.

    Unlike init_and_commit_git_repo, this does not run ``git init`` and is
    intended for adding follow-up commits to an already-initialized repo.
    """
    cg = ConcurrencyGroup(name="test-git-commit")
    with cg:
        cg.run_process_to_completion(command=["git", "add", "."], cwd=repo_dir)
        cg.run_process_to_completion(
            command=["git", "commit", "-m", message],
            cwd=repo_dir,
            env=_git_test_env(tmp_path),
        )


# ---------------------------------------------------------------------------
# End-to-end test helpers (for real mngr/mind subprocess calls)
# ---------------------------------------------------------------------------


def _clean_env() -> dict[str, str]:
    """Build an environment dict for subprocesses that strips pytest markers.

    mngr refuses to run when PYTEST_CURRENT_TEST is set (safety check to
    prevent tests from accidentally using real mngr state). We strip it
    so that our end-to-end subprocess calls work against the real system.
    """
    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    return env


def run_mngr(*args: str, timeout: float = 60.0, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a `uv run mngr` command and return the result."""
    return subprocess.run(
        ["uv", "run", "mngr", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_clean_env(),
        cwd=cwd,
    )


def parse_mngr_list_json(stdout: str) -> list[dict[str, object]]:
    """Extract agent records from mngr list --format json stdout.

    Delegates to the shared implementation in config.data_types. Kept here
    for backward compatibility with existing test callers.
    """
    return parse_agents_from_mngr_output(stdout)


def find_agent(agent_name: str) -> dict[str, object] | None:
    """Find an agent by name, returning its full record or None."""
    result = run_mngr(
        "list",
        "--include",
        f'name == "{agent_name}"',
        "--format=json",
        "--provider",
        "local",
    )
    if result.returncode != 0:
        logger.debug("mngr list failed (rc={}): {}", result.returncode, result.stderr[:200])
        return None
    agents = parse_mngr_list_json(result.stdout)
    if agents:
        return agents[0]
    return None


def extract_response(exec_result: subprocess.CompletedProcess[str]) -> str:
    """Extract the model response from mngr exec output.

    Filters out mngr's "Command succeeded/failed" status lines,
    returning only the first line of actual model output.
    """
    response_lines = [
        line for line in exec_result.stdout.strip().splitlines() if line and not line.startswith("Command ")
    ]
    if not response_lines:
        raise AssertionError(f"No response from model: {exec_result.stdout!r}")
    return response_lines[0]
