import json
import os
import subprocess
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.event_utils import ReadOnlyEvent
from imbue.concurrency_group.subprocess_utils import FinishedProcess

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


def make_finished_process(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    command: tuple[str, ...] = ("mng",),
) -> FinishedProcess:
    """Create a FinishedProcess for use in tests with fake ConcurrencyGroups."""
    return FinishedProcess(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        command=command,
        is_output_already_logged=False,
    )


class FakeConcurrencyGroup(ConcurrencyGroup):
    """ConcurrencyGroup subclass that returns pre-configured results instead of running processes.

    Records the commands that were run in order. Use the factory function
    make_fake_concurrency_group() to create instances with pre-configured results.

    Do not instantiate directly -- use make_fake_concurrency_group() instead.
    """

    _fake_results: dict[str, FinishedProcess]
    _commands_run: list[list[str]]

    @property
    def commands_run(self) -> list[list[str]]:
        return self._commands_run

    @property
    def subcommands_run(self) -> list[str]:
        """Extract the mng subcommand names (second element) from all recorded commands."""
        return [cmd[1] for cmd in self._commands_run if len(cmd) > 1]

    def run_process_to_completion(
        self,
        command: Sequence[str],
        timeout: float | None = None,
        is_checked_after: bool = True,
        on_output: Callable[[str, bool], None] | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        shutdown_event: ReadOnlyEvent | None = None,
    ) -> FinishedProcess:
        cmd_list = list(command)
        self._commands_run.append(cmd_list)

        # Try to match by the mng subcommand (second element, e.g. "snapshot", "stop")
        if len(cmd_list) > 1 and cmd_list[1] in self._fake_results:
            return self._fake_results[cmd_list[1]]

        # Default: success
        return make_finished_process(command=tuple(cmd_list))


@contextmanager
def capture_loguru_messages() -> Generator[list[str], None, None]:
    """Context manager that captures loguru messages into a list.

    Adds a temporary loguru handler that appends all messages to a list,
    and removes it on exit. Yields the list so callers can assert on
    the captured output after calling functions that log via loguru.
    """
    messages: list[str] = []
    handler_id = logger.add(lambda m: messages.append(str(m)), level="TRACE")
    try:
        yield messages
    finally:
        logger.remove(handler_id)


def make_fake_concurrency_group(
    results: dict[str, FinishedProcess] | None = None,
) -> FakeConcurrencyGroup:
    """Create a FakeConcurrencyGroup with pre-configured results.

    The results dict maps mng subcommand names (e.g. "list", "create") to
    FinishedProcess objects. Commands not in the dict return success by default.
    """
    cg = FakeConcurrencyGroup.__new__(FakeConcurrencyGroup)
    ConcurrencyGroup.__init__(cg, name="fake-cg")
    cg._fake_results = results or {}
    cg._commands_run = []
    return cg


# ---------------------------------------------------------------------------
# End-to-end test helpers (for real mng/changeling subprocess calls)
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


def run_mng(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    """Run a `uv run mng` command and return the result."""
    return subprocess.run(
        ["uv", "run", "mng", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_clean_env(),
    )


def run_changeling(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    """Run a `uv run changeling` command and return the result."""
    return subprocess.run(
        ["uv", "run", "changeling", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_clean_env(),
    )


def parse_mng_list_json(stdout: str) -> list[dict[str, object]]:
    """Extract agent records from mng list --json stdout.

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
        "--json",
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
