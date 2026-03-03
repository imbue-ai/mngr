"""Test fixtures for mng-schedule.

Uses shared plugin test fixtures from mng to avoid duplicating common
fixture code across plugin libraries.
"""

from pathlib import Path

import pluggy
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng.config.data_types import MngConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.testing.plugin_testing import register_plugin_test_fixtures

register_plugin_test_fixtures(globals())


@pytest.fixture()
def set_test_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set a dummy ANTHROPIC_API_KEY for tests that exercise deploy hooks.

    The claude plugin's modify_env_vars_for_deploy hook requires an API key.
    Tests that invoke stage_deploy_files, _stage_consolidated_env, or the
    modify_env_vars_for_deploy hook with temp_mng_ctx should request this
    fixture to avoid UserInputError.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key-for-tests")


@pytest.fixture()
def bare_plugin_manager() -> pluggy.PluginManager:
    """Create a plugin manager with hookspecs only, no plugins registered."""
    from imbue.mng.plugins import hookspecs

    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)
    return pm


def _build_mng_ctx(pm: pluggy.PluginManager, tmp_path: Path) -> MngContext:
    """Build a MngContext for testing."""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(exist_ok=True)
    config = MngConfig(default_host_dir=tmp_path / ".mng")
    return MngContext(
        config=config,
        pm=pm,
        profile_dir=profile_dir,
        concurrency_group=ConcurrencyGroup(name="test"),
    )


@pytest.fixture()
def temp_mng_ctx(
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
) -> MngContext:
    """MngContext with all plugins loaded (via plugin_manager fixture)."""
    return _build_mng_ctx(plugin_manager, tmp_path)


@pytest.fixture()
def bare_temp_mng_ctx(
    tmp_path: Path,
    bare_plugin_manager: pluggy.PluginManager,
) -> MngContext:
    """MngContext with no plugins loaded (bare hookspecs only)."""
    return _build_mng_ctx(bare_plugin_manager, tmp_path)
