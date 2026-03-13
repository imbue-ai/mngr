import json
import os
import subprocess
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup

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


# ---------------------------------------------------------------------------
# End-to-end test helpers (for real mng/mind subprocess calls)
# ---------------------------------------------------------------------------


def _clean_env() -> dict[str, str]:
    """Build an environment dict for subprocesses that strips pytest markers.

    mng refuses to run when PYTEST_CURRENT_TEST is set (safety check to
    prevent tests from accidentally using real mng state). We strip it
    so that our end-to-end subprocess calls work against the real system.
    """
    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    return env


def run_mng(*args: str, timeout: float = 60.0, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a `uv run mng` command and return the result."""
    return subprocess.run(
        ["uv", "run", "mng", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_clean_env(),
        cwd=cwd,
    )


def parse_mng_list_json(stdout: str) -> list[dict[str, object]]:
    """Extract agent records from mng list --format json stdout.

    The stdout may contain non-JSON lines (e.g. SSH error tracebacks)
    mixed with the JSON. We find the first line starting with '{' and
    parse from there.
    """
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                data = json.loads(stripped)
                return list(data.get("agents", []))
            except json.JSONDecodeError:
                logger.trace("Failed to parse JSON from mng list output line: {}", stripped[:200])
                continue
    return []


def find_agent(agent_name: str) -> dict[str, object] | None:
    """Find an agent by name, returning its full record or None."""
    result = run_mng(
        "list",
        "--include",
        f'name == "{agent_name}"',
        "--format=json",
        "--provider",
        "local",
    )
    if result.returncode != 0:
        logger.debug("mng list failed (rc={}): {}", result.returncode, result.stderr[:200])
        return None
    agents = parse_mng_list_json(result.stdout)
    if agents:
        return agents[0]
    return None


def extract_response(exec_result: subprocess.CompletedProcess[str]) -> str:
    """Extract the model response from mng exec output.

    Filters out mng's "Command succeeded/failed" status lines,
    returning only the first line of actual model output.
    """
    response_lines = [
        line for line in exec_result.stdout.strip().splitlines() if line and not line.startswith("Command ")
    ]
    if not response_lines:
        raise AssertionError(f"No response from model: {exec_result.stdout!r}")
    return response_lines[0]
