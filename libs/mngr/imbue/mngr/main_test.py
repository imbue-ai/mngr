"""Unit tests for create_plugin_manager."""

import os
from pathlib import Path

import click
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.mngr.errors import UserInputError
from imbue.mngr.main import _unwrap_user_facing_error
from imbue.mngr.main import create_plugin_manager
from imbue.mngr.utils.env_utils import parse_bool_env


def test_create_plugin_manager_blocks_disabled_plugins(
    project_config_dir: Path,
    temp_git_repo_cwd: Path,
) -> None:
    """create_plugin_manager should block plugins disabled in config files."""
    # MNGR_LOAD_ALL_PLUGINS disables config-based blocking, so if it is set it would
    # silently mask this test. It must never be set during a normal test run, so treat
    # its presence as a leak and fail loudly -- some other test or imported module set
    # it process-wide (e.g. importing scripts/make_cli_docs, which sets it at import
    # time and is expected to pop it again). Surface the leak so it gets fixed at the
    # source rather than papered over here.
    assert not parse_bool_env(os.environ.get("MNGR_LOAD_ALL_PLUGINS", "")), (
        "MNGR_LOAD_ALL_PLUGINS is set in the test environment, which disables plugin "
        "blocking and would mask this test. It leaked into the process from another "
        "test or an imported module (e.g. an importer of scripts/make_cli_docs that "
        "failed to pop it). Find and contain the leak at its source."
    )
    (project_config_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n\n[plugins.modal]\nenabled = false\n"
    )

    pm = create_plugin_manager()

    assert pm.is_blocked("modal")


def test_create_plugin_manager_skips_blocking_when_load_all_plugins_set(
    project_config_dir: Path,
    temp_git_repo_cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_plugin_manager should skip blocking when MNGR_LOAD_ALL_PLUGINS is truthy."""
    (project_config_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n\n[plugins.modal]\nenabled = false\n"
    )
    monkeypatch.setenv("MNGR_LOAD_ALL_PLUGINS", "1")

    pm = create_plugin_manager()

    assert not pm.is_blocked("modal")


def test_unwrap_user_facing_error_returns_bare_user_facing_error() -> None:
    """A ClickException/MngrError that is not wrapped is returned as-is."""
    error = UserInputError("bad input")
    assert _unwrap_user_facing_error(error) is error

    click_error = click.ClickException("boom")
    assert _unwrap_user_facing_error(click_error) is click_error


def test_unwrap_user_facing_error_unwraps_single_wrapped_error() -> None:
    """A ConcurrencyExceptionGroup wrapping a single user-facing error is unwrapped.

    This is the provision_agent case: a UserInputError raised inside the concurrency
    group's `with` block is re-raised wrapped in a ConcurrencyExceptionGroup.
    """
    inner = UserInputError("session not found")
    group = ConcurrencyExceptionGroup("wrapped", (inner,), main_exception=inner)
    assert _unwrap_user_facing_error(group) is inner


def test_unwrap_user_facing_error_unwraps_nested_groups() -> None:
    """Nested single-exception ConcurrencyExceptionGroups are unwrapped recursively."""
    inner = UserInputError("session not found")
    nested = ConcurrencyExceptionGroup("inner", (inner,), main_exception=inner)
    outer = ConcurrencyExceptionGroup("outer", (nested,), main_exception=nested)
    assert _unwrap_user_facing_error(outer) is inner


def test_unwrap_user_facing_error_returns_none_for_plain_exception() -> None:
    """A non-user-facing error (a genuine bug) is not unwrapped, so it stays unexpected."""
    assert _unwrap_user_facing_error(RuntimeError("boom")) is None


def test_unwrap_user_facing_error_returns_none_for_multiple_wrapped_errors() -> None:
    """A group carrying multiple distinct failures is not unwrapped to a single error."""
    group = ConcurrencyExceptionGroup("two failures", (UserInputError("a"), UserInputError("b")))
    assert _unwrap_user_facing_error(group) is None


def test_unwrap_user_facing_error_returns_none_for_wrapped_plain_exception() -> None:
    """A group wrapping a single non-user-facing error stays unexpected (full traceback)."""
    group = ConcurrencyExceptionGroup("wrapped bug", (RuntimeError("boom"),))
    assert _unwrap_user_facing_error(group) is None
