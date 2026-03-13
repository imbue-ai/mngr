import shutil

import pytest


@pytest.fixture
def noop_binary() -> str:
    """A cross-platform path to a no-op binary that accepts any arguments.

    Use this as a fake mng_binary for AgentObserver tests. On macOS /bin/true
    does not exist (it lives at /usr/bin/true), so shutil.which() finds the
    correct path on any platform.
    """
    path = shutil.which("true")
    assert path is not None, "Could not find 'true' binary on this system"
    return path
