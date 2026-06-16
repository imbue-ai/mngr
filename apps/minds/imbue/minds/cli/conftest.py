from pathlib import Path
from typing import Generator
from uuid import uuid4

import pytest

from imbue.mngr.utils.testing import isolate_git
from imbue.mngr.utils.testing import isolate_home
from imbue.mngr.utils.testing import isolate_tmux_server


@pytest.fixture(autouse=True)
def isolate_mind_tests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    """Isolate mind CLI tests from the real mngr environment.

    Sets HOME, MNGR_HOST_DIR, and MNGR_PREFIX to temp/unique values so that
    tests do not create agents in the real ~/.mngr or pollute the real tmux
    server. Uses the shared isolate_tmux_server() for tmux isolation and
    isolate_git() to populate a .gitconfig with default user info so any
    test that shells out to git finds a complete config.
    """
    test_id = uuid4().hex
    host_dir = tmp_path / ".mngr"
    host_dir.mkdir(exist_ok=True)

    isolate_home(tmp_path, monkeypatch)
    monkeypatch.setenv("MNGR_HOST_DIR", str(host_dir))
    monkeypatch.setenv("MNGR_PREFIX", "mngr_{}-".format(test_id))
    monkeypatch.setenv("MNGR_ROOT_NAME", "mngr-test-{}".format(test_id))
    monkeypatch.setenv("MNGR_COMPLETION_CACHE_DIR", str(host_dir))

    with isolate_git(monkeypatch), isolate_tmux_server(monkeypatch):
        yield


@pytest.fixture
def isolated_activation_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Strip every activation env var so tests opt in to a specific env explicitly.

    The autouse ``isolate_mind_tests`` already points HOME (and the cwd) at
    ``tmp_path``; this fixture layers on top by clearing the activation vars
    that leak in from the operator's shell (``MINDS_ROOT_NAME``,
    ``MODAL_PROFILE``) plus ``MODAL_CONFIG_PATH``, which would otherwise
    redirect deploy-mode validation away from the test's ``~/.modal.toml``.
    Returns ``tmp_path`` (== HOME) for tests that need to drop fixture files there.
    """
    monkeypatch.delenv("MINDS_ROOT_NAME", raising=False)
    monkeypatch.delenv("MODAL_PROFILE", raising=False)
    monkeypatch.delenv("MODAL_CONFIG_PATH", raising=False)
    return tmp_path
