import pytest

from imbue.resource_guards.testing import isolate_guard_state


@pytest.fixture()
def isolated_guard_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate resource guard module state so create/cleanup don't affect the session."""
    isolate_guard_state(monkeypatch)
