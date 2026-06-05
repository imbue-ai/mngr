import json
from pathlib import Path

import pluggy
import pytest

from imbue.mngr.config.completion_cache import COMPLETION_CACHE_FILENAME
from imbue.mngr.config.completion_cache import CompletionCacheData
from imbue.mngr.plugins import hookspecs
from imbue.mngr.providers.registry import load_all_registries


@pytest.fixture
def completion_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set MNGR_COMPLETION_CACHE_DIR to a temporary directory."""
    monkeypatch.setenv("MNGR_COMPLETION_CACHE_DIR", str(tmp_path))
    return tmp_path


def read_completion_cache(cache_dir: Path) -> CompletionCacheData:
    """Read the completion cache JSON from ``cache_dir`` as typed CompletionCacheData.

    Unknown keys are dropped so the helper stays robust to additive schema
    changes in the on-disk format. Shared by the unit and integration completion
    tests so both read the cache through one typed accessor.
    """
    data = json.loads((cache_dir / COMPLETION_CACHE_FILENAME).read_text())
    return CompletionCacheData(**{k: v for k, v in data.items() if k in CompletionCacheData._fields})


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
