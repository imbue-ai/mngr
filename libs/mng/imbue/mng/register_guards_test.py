import pytest

import imbue.imbue_common.resource_guards as rg
from imbue.mng.register_guards import register_docker_cli_guard
from imbue.mng.register_guards import register_docker_sdk_guard
from imbue.mng.register_guards import register_modal_guard


@pytest.fixture()
def isolated_guard_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate resource guard module state for register_guards tests."""
    monkeypatch.setattr(rg, "_guard_wrapper_dir", None)
    monkeypatch.setattr(rg, "_owns_guard_wrapper_dir", False)
    monkeypatch.setattr(rg, "_session_env_patcher", None)
    monkeypatch.setattr(rg, "_guarded_resources", [])
    monkeypatch.setattr(rg, "_registered_sdk_guards", [])


def test_register_modal_guard_adds_modal(
    isolated_guard_state: None,
) -> None:
    register_modal_guard()

    registered_names = [entry[0] for entry in rg._registered_sdk_guards]
    assert "modal" in registered_names


def test_register_docker_sdk_guard_adds_docker_sdk(
    isolated_guard_state: None,
) -> None:
    register_docker_sdk_guard()

    registered_names = [entry[0] for entry in rg._registered_sdk_guards]
    assert "docker_sdk" in registered_names


def test_register_docker_cli_guard_adds_docker_binary(
    isolated_guard_state: None,
) -> None:
    register_docker_cli_guard()

    assert "docker" in rg._guarded_resources


def test_register_modal_guard_deduplicates_on_repeated_calls(
    isolated_guard_state: None,
) -> None:
    register_modal_guard()
    register_modal_guard()

    registered_names = [entry[0] for entry in rg._registered_sdk_guards]
    assert registered_names.count("modal") == 1


def test_create_sdk_resource_guards_populates_guarded_resources(
    isolated_guard_state: None,
) -> None:
    register_modal_guard()
    register_docker_sdk_guard()
    rg.create_sdk_resource_guards()

    assert "modal" in rg._guarded_resources
    assert "docker_sdk" in rg._guarded_resources
