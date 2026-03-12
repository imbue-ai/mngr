"""Test utilities for resource guards."""

import pytest

import imbue.resource_guards.resource_guards as resource_guards


def isolate_guard_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset all resource guard module-level state for test isolation.

    Call this from an isolated_guard_state fixture in each package's conftest.py.
    """
    monkeypatch.setattr(resource_guards, "_guard_wrapper_dir", None)
    monkeypatch.setattr(resource_guards, "_owns_guard_wrapper_dir", False)
    monkeypatch.setattr(resource_guards, "_session_env_patcher", None)
    monkeypatch.setattr(resource_guards, "_guarded_resources", [])
    monkeypatch.setattr(resource_guards, "_registered_sdk_guards", [])
    monkeypatch.delenv("_PYTEST_GUARD_WRAPPER_DIR", raising=False)
