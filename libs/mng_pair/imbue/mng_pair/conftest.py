"""Test fixtures for mng-pair.

Uses shared plugin test fixtures from mng for common setup (plugin manager,
environment isolation, etc.) and defines pair-specific fixtures below.
"""

from pathlib import Path
from typing import Generator

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mng.utils.testing import init_git_repo

register_plugin_test_fixtures(globals())


@pytest.fixture
def cg() -> Generator[ConcurrencyGroup, None, None]:
    """Provide a ConcurrencyGroup for tests that need to run processes."""
    with ConcurrencyGroup(name="test") as group:
        yield group


@pytest.fixture
def setup_git_config(tmp_path: Path) -> None:
    """Create a .gitconfig in the fake HOME so git commands work."""
    gitconfig = tmp_path / ".gitconfig"
    if not gitconfig.exists():
        gitconfig.write_text("[user]\n\tname = Test User\n\temail = test@test.com\n")


@pytest.fixture
def temp_git_repo(tmp_path: Path, setup_git_config: None) -> Path:
    """Create a temporary git repository with an initial commit."""
    repo_dir = tmp_path / "git_repo"
    repo_dir.mkdir()
    init_git_repo(repo_dir)
    return repo_dir
