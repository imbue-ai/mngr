"""Test fixtures for mngr-schedule.

Uses shared plugin test fixtures from mngr to avoid duplicating common
fixture code across plugin libraries.
"""

from collections.abc import Callable
from pathlib import Path

import pluggy
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mngr_schedule.data_types import ScheduleTriggerDefinition
from imbue.mngr_schedule.data_types import ScheduledMngrCommand

register_plugin_test_fixtures(globals())


@pytest.fixture()
def set_test_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set a dummy ANTHROPIC_API_KEY for tests that exercise deploy hooks.

    The claude plugin's modify_env_vars_for_deploy hook requires an API key.
    Tests that invoke stage_deploy_files, _stage_consolidated_env, or the
    modify_env_vars_for_deploy hook with temp_mngr_ctx should request this
    fixture to avoid UserInputError.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key-for-tests")


@pytest.fixture()
def bare_plugin_manager() -> pluggy.PluginManager:
    """Create a plugin manager with hookspecs only, no plugins registered."""
    from imbue.mngr.plugins import hookspecs

    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    return pm


def _build_mngr_ctx(pm: pluggy.PluginManager, tmp_path: Path) -> MngrContext:
    """Build a MngrContext for testing."""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(exist_ok=True)
    config = MngrConfig(default_host_dir=tmp_path / ".mngr")
    return MngrContext(
        config=config,
        pm=pm,
        profile_dir=profile_dir,
        concurrency_group=ConcurrencyGroup(name="test"),
    )


@pytest.fixture()
def temp_mngr_ctx(
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
) -> MngrContext:
    """MngrContext with all plugins loaded (via plugin_manager fixture)."""
    return _build_mngr_ctx(plugin_manager, tmp_path)


@pytest.fixture()
def bare_temp_mngr_ctx(
    tmp_path: Path,
    bare_plugin_manager: pluggy.PluginManager,
) -> MngrContext:
    """MngrContext with no plugins loaded (bare hookspecs only)."""
    return _build_mngr_ctx(bare_plugin_manager, tmp_path)


def _build_test_trigger(name: str = "test-trigger") -> ScheduleTriggerDefinition:
    """Build a minimal local-provider CREATE trigger for unit tests.

    Module-level so the ``make_test_trigger`` fixture can expose it directly
    without defining an inline closure (which the inline-function ratchet
    disallows).
    """
    return ScheduleTriggerDefinition(
        name=name,
        command=ScheduledMngrCommand.CREATE,
        args="--message hello",
        schedule_cron="0 2 * * *",
        provider="local",
    )


@pytest.fixture()
def make_test_trigger() -> Callable[..., ScheduleTriggerDefinition]:
    """Factory fixture returning ``_build_test_trigger``.

    Callers invoke ``make_test_trigger()`` for the default name or
    ``make_test_trigger("custom-name")`` to override it. Used across schedule
    unit tests so a change to the trigger shape only needs to be made in one
    place.
    """
    return _build_test_trigger


@pytest.fixture()
def monorepo_root() -> Path:
    """Get the monorepo root from this file's location.

    mngr schedule add needs to package the repo, so the subprocess must run
    from the monorepo root. We can't use cwd because isolate_home() chdir's
    to a temp directory.

    The path is derived from this file's location
    (libs/mngr_schedule/imbue/mngr_schedule/conftest.py), mirroring the
    pattern used in libs/mngr/imbue/mngr/conftest.py and other sibling
    modules. Avoiding a git subprocess keeps fixtures fast and makes the
    fixture work in non-git checkouts.
    """
    return Path(__file__).resolve().parents[4]
