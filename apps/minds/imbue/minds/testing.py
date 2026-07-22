import subprocess
from pathlib import Path
from typing import Final

import pytest
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.bootstrap import mngr_host_dir_for

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


# -- Backup-service test helpers (shared by the workspace-script unit tests
# and the snapshot-resume backup tests) --

_BACKUP_TEST_GIT_IDENTITY: Final[tuple[str, ...]] = (
    "-c",
    "user.name=test",
    "-c",
    "user.email=test@example.com",
)


def run_git_for_backup_test(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *_BACKUP_TEST_GIT_IDENTITY, *args], cwd=repo, capture_output=True, text=True, check=True, timeout=60
    )
    return result.stdout


def write_stub_supervisorctl(stub_bin: Path, *, is_restart_ok: bool = True) -> Path:
    """Write a stub ``supervisorctl`` into ``stub_bin`` and return its path.

    The healthy variant always reports ``host-backup`` RUNNING; the
    ``is_restart_ok=False`` variant fails ``restart`` and reports the program
    STOPPED, for exercising the update script's rollback path.
    """
    stub = stub_bin / "supervisorctl"
    if is_restart_ok:
        stub.write_text('#!/bin/bash\necho "host-backup RUNNING pid 123, uptime 0:00:01"\nexit 0\n')
    else:
        stub.write_text(
            '#!/bin/bash\nif [ "$1" = "restart" ]; then echo "failed" >&2; exit 1; fi\n'
            'echo "host-backup STOPPED"\nexit 0\n'
        )
    stub.chmod(0o755)
    return stub


def tag_newer_release_content(repo: Path, *, removed_file: str | None = None) -> None:
    """Commit newer backup code on a side branch and tag it ``minds-v2.0.0``.

    HEAD (main) then reads as *outdated* relative to the tag. ``removed_file``
    additionally deletes that path inside the release commit, for exercising
    convergence onto a tag that removed a file.
    """
    run_git_for_backup_test(repo, "checkout", "-q", "-b", "release")
    if removed_file is not None:
        run_git_for_backup_test(repo, "rm", "-q", removed_file)
    (repo / "libs" / "host_backup" / "service.py").write_text("VERSION = 2\n")
    run_git_for_backup_test(repo, "add", "-A")
    run_git_for_backup_test(repo, "commit", "-q", "-m", "release content")
    run_git_for_backup_test(repo, "tag", "minds-v2.0.0")
    run_git_for_backup_test(repo, "checkout", "-q", "main")


# -- Workspace-sync e2e (snapshot sandbox + real connector env) ---------------
#
# The sync e2e release tests (apps/minds/test_sync_e2e.py) run in the
# minds-snapshot offload sandbox against a real per-run CI connector env.
# The env's coordinates + admin secrets are forwarded into the sandbox as
# env vars (only on run_minds_release_tests CI runs); these helpers hold the
# contract in one place so the conftest fixture and any future consumer agree.

SYNC_E2E_CONNECTOR_URL_ENV: Final[str] = "MINDS_SYNC_E2E_CONNECTOR_URL"
SYNC_E2E_LITELLM_URL_ENV: Final[str] = "MINDS_SYNC_E2E_LITELLM_URL"
SYNC_E2E_SUPERTOKENS_URI_ENV: Final[str] = "MINDS_SYNC_E2E_SUPERTOKENS_CONNECTION_URI"
SYNC_E2E_SUPERTOKENS_API_KEY_ENV: Final[str] = "MINDS_SYNC_E2E_SUPERTOKENS_API_KEY"


class SyncE2EEnv(FrozenModel):
    """Coordinates + admin secrets of the real connector env the sync e2e tests target."""

    connector_url: str = Field(description="Base URL of the deployed remote_service_connector")
    litellm_proxy_url: str = Field(description="Base URL of the deployed litellm proxy")
    supertokens_connection_uri: SecretStr = Field(description="SuperTokens core URI for admin user provisioning")
    supertokens_api_key: SecretStr = Field(description="SuperTokens core admin api-key")


class SyncE2EAccount(FrozenModel):
    """A per-test, pre-verified, paid account on the sync e2e connector env."""

    email: str = Field(description="Unique per-test address under the env's seeded paid domain")
    password: SecretStr = Field(description="The account's sign-in password (typed into the real UI)")
    user_id: str = Field(description="SuperTokens user id (used for teardown and record assertions)")
    access_token: SecretStr = Field(description="A session JWT for read-only connector polling from the test")
