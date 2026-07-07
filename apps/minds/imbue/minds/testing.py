import os
import subprocess
from pathlib import Path
from typing import Final

import pytest
from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.bootstrap import mngr_host_dir_for
from imbue.minds.config.data_types import parse_agents_from_mngr_output
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.env_utils import TEST_ENV_PREFIX

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
# End-to-end test helpers (for real mngr subprocess calls)
# ---------------------------------------------------------------------------


def clean_env() -> dict[str, str]:
    """Build an environment dict for subprocesses.

    Returns a copy of os.environ. Relies on the shared plugin test fixtures
    (registered in apps/minds/conftest.py via register_plugin_test_fixtures)
    having set MNGR_HOST_DIR / MNGR_PREFIX / MNGR_ROOT_NAME to per-test
    tmp values, so the subprocess inherits proper isolation. With
    MNGR_ROOT_NAME set to `mngr-test-<id>`, the subprocess does not load
    the repo's .mngr/settings.toml, so the is_allowed_in_pytest=false
    guard there does not fire and no explicit opt-in is needed.

    Asserts that the expected isolation is in place so a future test that
    forgets to register the shared fixtures gets a loud failure instead of
    a silent orphan-env leak.
    """
    host_dir = os.environ.get("MNGR_HOST_DIR")
    prefix = os.environ.get("MNGR_PREFIX", "")
    root_name = os.environ.get("MNGR_ROOT_NAME", "")
    assert host_dir is not None, (
        "clean_env() requires MNGR_HOST_DIR to be set -- expected the shared plugin "
        "test fixtures (register_plugin_test_fixtures in apps/minds/conftest.py) to "
        "have populated it via the autouse setup_test_mngr_env fixture."
    )
    assert prefix.startswith(TEST_ENV_PREFIX), (
        f"clean_env() requires MNGR_PREFIX to start with {TEST_ENV_PREFIX!r} so any "
        f"leaked Modal env is visible to the CI cleanup script; got {prefix!r}. The "
        f"apps/minds/conftest.py override points this at generate_test_environment_name()."
    )
    assert root_name.startswith("mngr-test-"), (
        f"clean_env() requires MNGR_ROOT_NAME to start with 'mngr-test-' so the "
        f"subprocess mngr does not load the repo's .mngr/settings.toml; got "
        f"{root_name!r}. The autouse setup_test_mngr_env fixture sets this."
    )
    return dict(os.environ)


def run_mngr(*args: str, timeout: float = 60.0, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a `uv run mngr` command and return the result."""
    return subprocess.run(
        ["uv", "run", "mngr", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=clean_env(),
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


def stub_mngr_host_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, root_name: str) -> Path:
    """Redirect ``Path.home()`` to ``tmp_path`` and seed a minimal mngr profile.

    Returns the active ``settings.toml`` path (the file itself may not exist
    on return -- callers populate it as needed). The bootstrap helpers refuse
    to write anything until ``config.toml`` and the matching profile dir
    exist, so we materialize them up front. ``Path.home()`` consults ``$HOME``
    on Linux/macOS, so swapping that in via ``monkeypatch.setenv`` is enough
    to redirect the helpers without touching ``Path`` itself.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    mngr_host_dir = mngr_host_dir_for(root_name)
    mngr_host_dir.mkdir(parents=True, exist_ok=True)
    profile_id = "testprofile"
    (mngr_host_dir / "config.toml").write_text(f'profile = "{profile_id}"\n')
    settings_dir = mngr_host_dir / "profiles" / profile_id
    settings_dir.mkdir(parents=True, exist_ok=True)
    return settings_dir / "settings.toml"


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


# -- Backup-service release-test helpers (see apps/minds/conftest.py's
# ``backup_release_workspace`` fixture and test_backup_service_release.py) --

_BACKUP_TEST_GIT_IDENTITY: Final[tuple[str, ...]] = (
    "-c",
    "user.name=test",
    "-c",
    "user.email=test@example.com",
)


class BackupReleaseWorkspace(FrozenModel):
    """Handle for the backup release tests' real local workspace agent."""

    agent_id: AgentId = Field(description="The created agent's id")
    work_dir: Path = Field(description="The agent's work_dir (an FCT-shaped git repo)")
    agent_name: str = Field(description="The created agent's name")


def run_git_for_backup_test(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *_BACKUP_TEST_GIT_IDENTITY, *args], cwd=repo, capture_output=True, text=True, check=True, timeout=60
    )
    return result.stdout


def make_fct_shaped_repo(tmp_path: Path) -> Path:
    """A committed git repo shaped like a workspace checkout.

    Includes a minimal pyproject so ``uv run mngr ...`` inside the work_dir
    resolves (uv falls back to PATH for commands that are not project
    scripts), plus libs/host_backup content and a newer tagged version on a
    side branch (so HEAD reads as *outdated* relative to the tag).
    """
    repo = tmp_path / "fake-workspace"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True, timeout=60)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "fake-workspace"\nversion = "0.0.1"\nrequires-python = ">=3.11"\n'
    )
    # A committed lockfile so `uv run` / `uv sync` inside the work_dir succeed
    # even under a UV_FROZEN=1 environment (as the test harness sets).
    lock_env = {key: value for key, value in os.environ.items() if key != "UV_FROZEN"}
    subprocess.run(["uv", "lock"], cwd=repo, check=True, capture_output=True, timeout=120, env=lock_env)
    backup_dir = repo / "libs" / "host_backup"
    backup_dir.mkdir(parents=True)
    (backup_dir / "service.py").write_text("VERSION = 1\n")
    run_git_for_backup_test(repo, "add", "-A")
    run_git_for_backup_test(repo, "commit", "-q", "-m", "initial")
    # Tagged newer backup code on a side branch: HEAD (main) is outdated.
    run_git_for_backup_test(repo, "checkout", "-q", "-b", "release")
    (backup_dir / "service.py").write_text("VERSION = 2\n")
    run_git_for_backup_test(repo, "add", "-A")
    run_git_for_backup_test(repo, "commit", "-q", "-m", "release content")
    run_git_for_backup_test(repo, "tag", "minds-v2.0.0")
    run_git_for_backup_test(repo, "checkout", "-q", "main")
    return repo
