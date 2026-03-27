from pathlib import Path
from typing import Generator
from uuid import uuid4

import pytest

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
    server. Uses the shared isolate_tmux_server() for tmux isolation.
    """
    test_id = uuid4().hex
    host_dir = tmp_path / ".mngr"
    host_dir.mkdir(exist_ok=True)

    isolate_home(tmp_path, monkeypatch)
    monkeypatch.setenv("MNGR_HOST_DIR", str(host_dir))
    monkeypatch.setenv("MNGR_PREFIX", "mngr_{}-".format(test_id))
    monkeypatch.setenv("MNGR_ROOT_NAME", "mngr-test-{}".format(test_id))
    monkeypatch.setenv("MNGR_COMPLETION_CACHE_DIR", str(host_dir))

    # Create .gitconfig so git commands work in the temp HOME
    gitconfig = tmp_path / ".gitconfig"
    if not gitconfig.exists():
        gitconfig.write_text("[user]\n\tname = Test User\n\temail = test@test.com\n")

    with isolate_tmux_server(monkeypatch):
        yield
