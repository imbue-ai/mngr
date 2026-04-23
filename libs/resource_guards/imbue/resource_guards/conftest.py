import os

import pytest

from imbue.resource_guards.testing import isolate_guard_state


@pytest.fixture()
def isolated_guard_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate resource guard module state so create/cleanup don't affect the session."""
    isolate_guard_state(monkeypatch)


@pytest.fixture()
def clean_guard_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove outer guard-wrapper state from env + PATH before a pytester subprocess runs.

    Pytester subprocesses inherit env + PATH from the parent. Without this fixture,
    the child would see the parent's _PYTEST_GUARD_WRAPPER_DIR and its wrapper scripts
    first on PATH, which shadows any wrappers the child creates.
    """
    monkeypatch.delenv("_PYTEST_GUARD_DIAG_LOG", raising=False)
    outer_dir = os.environ.get("_PYTEST_GUARD_WRAPPER_DIR")
    if outer_dir is None:
        return
    monkeypatch.delenv("_PYTEST_GUARD_WRAPPER_DIR")
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if p != outer_dir),
    )
