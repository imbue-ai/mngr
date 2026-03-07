import os
from pathlib import Path
from typing import Generator
from uuid import uuid4

import pytest

from imbue.mng.utils.testing import isolate_home
from imbue.mng.utils.testing import isolate_tmux_server


@pytest.fixture(autouse=True)
def fake_mng_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Place a fake mng binary first on PATH so deploy tests don't spawn real agents.

    The CLI deploy tests verify prompting behavior and argument parsing, not
    actual deployment. The real ``mng create`` spawns tmux sessions, provisions
    agents, etc. and takes >10s, causing timeouts. This fake binary exits
    immediately with success.
    """
    bin_dir = tmp_path / "fake_mng_bin"
    bin_dir.mkdir(exist_ok=True)
    fake_mng = bin_dir / "mng"
    fake_mng.write_text("#!/bin/bash\nexit 0\n")
    fake_mng.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")


@pytest.fixture(autouse=True)
def isolate_changeling_tests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    """Isolate changeling CLI tests from the real mng environment.

    Sets HOME, MNG_HOST_DIR, and MNG_PREFIX to temp/unique values so that
    tests do not create agents in the real ~/.mng or pollute the real tmux
    server. Uses the shared isolate_tmux_server() for tmux isolation.
    """
    test_id = uuid4().hex
    host_dir = tmp_path / ".mng"
    host_dir.mkdir()

    isolate_home(tmp_path, monkeypatch)
    monkeypatch.setenv("MNG_HOST_DIR", str(host_dir))
    monkeypatch.setenv("MNG_PREFIX", "mng_{}-".format(test_id))
    monkeypatch.setenv("MNG_ROOT_NAME", "mng-test-{}".format(test_id))
    monkeypatch.setenv("MNG_COMPLETION_CACHE_DIR", str(host_dir))

    # Create .gitconfig so git commands work in the temp HOME
    gitconfig = tmp_path / ".gitconfig"
    if not gitconfig.exists():
        gitconfig.write_text("[user]\n\tname = Test User\n\temail = test@test.com\n")

    with isolate_tmux_server(monkeypatch):
        yield
