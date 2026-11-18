import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Create a temporary git repository isolated from host git config.

    Returns the repo directory path. The repo has git init and local
    user.email/user.name configured, but no initial commit -- tests
    create their own files and commits.

    HOME is redirected to a temp directory so that the host's global
    gitconfig (e.g. commit.gpgsign) does not leak into tests.
    """
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    repo_dir = tmp_path / "test_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    yield repo_dir


@pytest.fixture
def offline_uv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forbid uv from reaching a package index, and let it pick the project's own .venv over the test run's."""
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
