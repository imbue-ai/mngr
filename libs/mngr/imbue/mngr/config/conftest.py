from pathlib import Path

import pluggy
import pytest

from imbue.mngr.plugins import hookspecs
from imbue.mngr.providers.registry import load_all_registries


@pytest.fixture
def completion_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set MNGR_COMPLETION_CACHE_DIR to a temporary directory."""
    monkeypatch.setenv("MNGR_COMPLETION_CACHE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def isolated_load_config_pm(monkeypatch: pytest.MonkeyPatch) -> pluggy.PluginManager:
    """A plugin manager wired for ``load_config`` tests, with the env isolated.

    Builds a fresh ``PluginManager``, registers the hookspecs, and runs
    ``load_all_registries`` (the standard three-line ``load_config`` setup), then
    undoes the autouse ``setup_test_mngr_env`` fixture's ``MNGR_*`` settings so
    ``load_config`` resolves ``~/.mngr`` to ``tmp_path/.mngr`` and ``root_name``
    collapses to ``"mngr"`` (mirroring the test file's ``_isolate_load_config_env``
    and the narrowing tests' ``_setup_layered_test_env``).

    HOME is already pointed at ``tmp_path`` by the autouse fixture (via
    ``isolate_home``). Tests pair this with ``temp_git_repo_cwd`` and apply any
    extra env tweaks (MNGR_HEADLESS, MNGR_ALLOW_UNKNOWN_CONFIG, MNGR__*,
    PYTEST_CURRENT_TEST, etc.) inline after consuming the fixture.
    """
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)
    monkeypatch.delenv("MNGR_PREFIX", raising=False)
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    monkeypatch.delenv("MNGR_ROOT_NAME", raising=False)
    return pm
