from pathlib import Path

import pytest
from docker.api.client import APIClient

import imbue.resource_guards.resource_guards as resource_guards
from imbue.resource_guards.resource_guards import ResourceGuardViolation
from imbue.resource_guards_docker.guards import _cleanup_docker_sdk_guards
from imbue.resource_guards_docker.guards import _docker_originals
from imbue.resource_guards_docker.guards import _guarded_docker_send
from imbue.resource_guards_docker.guards import _install_docker_sdk_guards
from imbue.resource_guards_docker.guards import register_docker_cli_guard
from imbue.resource_guards_docker.guards import register_docker_sdk_guard


def test_register_docker_sdk_guard_adds_docker_sdk(
    isolated_guard_state: None,
) -> None:
    register_docker_sdk_guard()

    registered_names = [entry[0] for entry in resource_guards._registered_sdk_guards]
    assert "docker_sdk" in registered_names


def test_register_docker_cli_guard_adds_docker_binary(
    isolated_guard_state: None,
) -> None:
    register_docker_cli_guard()

    assert "docker" in resource_guards._guarded_resources


def test_create_sdk_resource_guards_populates_guarded_resources_docker(
    isolated_guard_state: None,
) -> None:
    register_docker_sdk_guard()
    resource_guards.create_sdk_resource_guards()

    assert "docker_sdk" in resource_guards._guarded_resources


def test_install_docker_sdk_guards_patches_apiclient_send(
    isolated_guard_state: None,
) -> None:
    """install records the original send method and patches APIClient.send."""
    # Clean up any existing patches so we can install fresh
    _cleanup_docker_sdk_guards()

    _install_docker_sdk_guards()

    assert "send_original_resolved" in _docker_originals
    assert "send_existed" in _docker_originals
    assert APIClient.send is _guarded_docker_send


def test_cleanup_docker_sdk_guards_restores_original(
    isolated_guard_state: None,
) -> None:
    """cleanup restores the original APIClient.send after install."""
    _cleanup_docker_sdk_guards()

    original_send = APIClient.send
    _install_docker_sdk_guards()
    _cleanup_docker_sdk_guards()

    assert APIClient.send is original_send
    assert len(_docker_originals) == 0


def test_cleanup_docker_sdk_guards_is_idempotent(
    isolated_guard_state: None,
) -> None:
    """Calling cleanup without install is safe (no-op)."""
    _cleanup_docker_sdk_guards()


def test_guarded_docker_send_delegates_to_original(
    isolated_guard_state: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guarded send function delegates to the original when the guard allows it."""
    monkeypatch.delenv("_PYTEST_GUARD_PHASE", raising=False)

    sentinel = object()
    _docker_originals["send_original_resolved"] = lambda self, *a, **kw: sentinel

    result = _guarded_docker_send(None)

    assert result is sentinel
    _docker_originals.clear()


def test_guarded_docker_send_enforces_guard(
    isolated_guard_state: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The guarded send raises ResourceGuardViolation when blocked."""
    monkeypatch.setenv("_PYTEST_GUARD_PHASE", "call")
    monkeypatch.setenv("_PYTEST_GUARD_DOCKER_SDK", "block")
    monkeypatch.setenv("_PYTEST_GUARD_TRACKING_DIR", str(tmp_path))

    _docker_originals["send_original_resolved"] = lambda self, *a, **kw: None

    with pytest.raises(ResourceGuardViolation, match="without @pytest.mark.docker_sdk"):
        _guarded_docker_send(None)

    _docker_originals.clear()


def test_install_when_apiclient_has_own_send(
    isolated_guard_state: None,
) -> None:
    """install/cleanup round-trips correctly when APIClient already has send in __dict__."""
    _cleanup_docker_sdk_guards()

    def original_own_send(self, *a, **kw):
        pass

    APIClient.send = original_own_send  # ty: ignore[invalid-assignment]

    _install_docker_sdk_guards()

    assert _docker_originals["send_existed"] is True
    assert _docker_originals["send_original"] is original_own_send
    assert APIClient.send is _guarded_docker_send

    _cleanup_docker_sdk_guards()

    assert APIClient.__dict__.get("send") is original_own_send
    # Clean up: remove our test attribute so we don't leak state
    del APIClient.send


def test_cleanup_when_send_already_removed(
    isolated_guard_state: None,
) -> None:
    """Cleanup handles the case where another path already removed the patched send."""
    _cleanup_docker_sdk_guards()

    _install_docker_sdk_guards()

    # Simulate another cleanup path removing our patch before we clean up
    if "send" in APIClient.__dict__:
        del APIClient.send

    # Should not raise
    _cleanup_docker_sdk_guards()
