"""Unit tests for deploy_utils shared utilities."""

from pathlib import Path
from typing import cast

import pytest

from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import MngError
from imbue.mng.providers.deploy_utils import collect_deploy_files


class _MockHook:
    """Concrete mock for the get_files_for_deploy hook."""

    def __init__(self, results: list[dict[Path, Path | str]]) -> None:
        self._results = results

    def get_files_for_deploy(
        self,
        mng_ctx: object,
        include_user_settings: bool,
        include_project_settings: bool,
        repo_root: Path,
    ) -> list[dict[Path, Path | str]]:
        return self._results


class _MockPluginManager:
    """Concrete mock for the plugin manager."""

    def __init__(self, hook: _MockHook) -> None:
        self.hook = hook


class _MockMngContext:
    """Concrete mock for MngContext with just the pm.hook needed."""

    def __init__(self, deploy_results: list[dict[Path, Path | str]]) -> None:
        self.pm = _MockPluginManager(_MockHook(deploy_results))


def _ctx(results: list[dict[Path, Path | str]]) -> MngContext:
    """Create a mock MngContext and cast it to the expected type."""
    return cast(MngContext, _MockMngContext(results))


def test_collect_deploy_files_merges_results() -> None:
    """collect_deploy_files should merge results from multiple plugins."""
    ctx = _ctx(
        [
            {Path("~/.mng/config.toml"): Path("/local/config.toml")},
            {Path("~/.claude.json"): '{"key": "value"}'},
        ]
    )

    result = collect_deploy_files(ctx, repo_root=Path("/repo"))

    assert len(result) == 2
    assert Path("~/.mng/config.toml") in result
    assert Path("~/.claude.json") in result


def test_collect_deploy_files_rejects_absolute_paths() -> None:
    """collect_deploy_files should reject absolute destination paths."""
    ctx = _ctx([{Path("/etc/config"): "content"}])

    with pytest.raises(MngError, match="must be relative or start with '~'"):
        collect_deploy_files(ctx, repo_root=Path("/repo"))


def test_collect_deploy_files_allows_tilde_paths() -> None:
    """collect_deploy_files should allow paths starting with ~."""
    ctx = _ctx([{Path("~/.mng/config.toml"): "content"}])

    result = collect_deploy_files(ctx, repo_root=Path("/repo"))
    assert Path("~/.mng/config.toml") in result


def test_collect_deploy_files_allows_relative_paths() -> None:
    """collect_deploy_files should allow relative paths."""
    ctx = _ctx([{Path(".mng/settings.local.toml"): "content"}])

    result = collect_deploy_files(ctx, repo_root=Path("/repo"))
    assert Path(".mng/settings.local.toml") in result


def test_collect_deploy_files_last_plugin_wins_on_collision() -> None:
    """When multiple plugins return the same path, last one wins."""
    ctx = _ctx(
        [
            {Path("~/.mng/config.toml"): "first"},
            {Path("~/.mng/config.toml"): "second"},
        ]
    )

    result = collect_deploy_files(ctx, repo_root=Path("/repo"))
    assert result[Path("~/.mng/config.toml")] == "second"


def test_collect_deploy_files_empty_results() -> None:
    """collect_deploy_files should handle no results gracefully."""
    ctx = _ctx([])

    result = collect_deploy_files(ctx, repo_root=Path("/repo"))
    assert result == {}
