"""Unit tests for deploy_utils shared utilities."""

from pathlib import Path
from typing import cast

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.providers.deploy_utils import MngrInstallMode
from imbue.mngr.providers.deploy_utils import collect_deploy_files
from imbue.mngr.providers.deploy_utils import collect_provider_profile_files
from imbue.mngr.providers.deploy_utils import detect_mngr_install_mode
from imbue.mngr.providers.deploy_utils import resolve_mngr_install_mode


class _MockHook:
    """Concrete mock for the get_files_for_deploy hook."""

    def __init__(self, results: list[dict[Path, Path | str]]) -> None:
        self._results = results

    def get_files_for_deploy(
        self,
        mngr_ctx: object,
        include_user_settings: bool,
        include_project_settings: bool,
        repo_root: Path,
    ) -> list[dict[Path, Path | str]]:
        return self._results


class _MockPluginManager:
    """Concrete mock for the plugin manager."""

    def __init__(self, hook: _MockHook) -> None:
        self.hook = hook


class _MockMngrContext:
    """Concrete mock for MngrContext with just the pm.hook needed."""

    def __init__(self, deploy_results: list[dict[Path, Path | str]]) -> None:
        self.pm = _MockPluginManager(_MockHook(deploy_results))


def _ctx(results: list[dict[Path, Path | str]]) -> MngrContext:
    """Create a mock MngrContext and cast it to the expected type."""
    return cast(MngrContext, _MockMngrContext(results))


def test_collect_deploy_files_merges_results() -> None:
    """collect_deploy_files should merge results from multiple plugins."""
    ctx = _ctx(
        [
            {Path("~/.mngr/config.toml"): Path("/local/config.toml")},
            {Path("~/.claude.json"): '{"key": "value"}'},
        ]
    )

    result = collect_deploy_files(ctx, repo_root=Path("/repo"))

    assert len(result) == 2
    assert Path("~/.mngr/config.toml") in result
    assert Path("~/.claude.json") in result


def test_collect_deploy_files_rejects_absolute_paths() -> None:
    """collect_deploy_files should reject absolute destination paths."""
    ctx = _ctx([{Path("/etc/config"): "content"}])

    with pytest.raises(MngrError, match="must be relative or start with '~'"):
        collect_deploy_files(ctx, repo_root=Path("/repo"))


def test_collect_deploy_files_allows_tilde_paths() -> None:
    """collect_deploy_files should allow paths starting with ~."""
    ctx = _ctx([{Path("~/.mngr/config.toml"): "content"}])

    result = collect_deploy_files(ctx, repo_root=Path("/repo"))
    assert Path("~/.mngr/config.toml") in result


def test_collect_deploy_files_allows_relative_paths() -> None:
    """collect_deploy_files should allow relative paths."""
    ctx = _ctx([{Path(".mngr/settings.local.toml"): "content"}])

    result = collect_deploy_files(ctx, repo_root=Path("/repo"))
    assert Path(".mngr/settings.local.toml") in result


def test_collect_deploy_files_last_plugin_wins_on_collision() -> None:
    """When multiple plugins return the same path, last one wins."""
    ctx = _ctx(
        [
            {Path("~/.mngr/config.toml"): "first"},
            {Path("~/.mngr/config.toml"): "second"},
        ]
    )

    result = collect_deploy_files(ctx, repo_root=Path("/repo"))
    assert result[Path("~/.mngr/config.toml")] == "second"


def test_collect_deploy_files_empty_results() -> None:
    """collect_deploy_files should handle no results gracefully."""
    ctx = _ctx([])

    result = collect_deploy_files(ctx, repo_root=Path("/repo"))
    assert result == {}


# --- MngrInstallMode enum tests ---


def test_mngr_install_mode_has_correct_values() -> None:
    """MngrInstallMode enum members should have uppercase string values."""
    assert MngrInstallMode.AUTO.value == "AUTO"
    assert MngrInstallMode.PACKAGE.value == "PACKAGE"
    assert MngrInstallMode.EDITABLE.value == "EDITABLE"
    assert MngrInstallMode.SKIP.value == "SKIP"


# --- detect_mngr_install_mode tests ---


def test_detect_mngr_install_mode_returns_editable_or_package() -> None:
    """detect_mngr_install_mode should return EDITABLE or PACKAGE for the current install."""
    result = detect_mngr_install_mode()
    assert result in (MngrInstallMode.EDITABLE, MngrInstallMode.PACKAGE)


def test_detect_mngr_install_mode_returns_package_for_missing_package() -> None:
    """detect_mngr_install_mode should return PACKAGE when the package is not installed."""
    result = detect_mngr_install_mode("nonexistent-package-xyz-12345")
    assert result == MngrInstallMode.PACKAGE


def test_detect_mngr_install_mode_returns_editable_for_mngr() -> None:
    """detect_mngr_install_mode for 'mngr' should return EDITABLE in a dev workspace."""
    # In a development workspace with editable install, this should return EDITABLE.
    # In a regular install, it would return PACKAGE. Either is valid.
    result = detect_mngr_install_mode("mngr")
    assert result in (MngrInstallMode.EDITABLE, MngrInstallMode.PACKAGE)


# --- resolve_mngr_install_mode tests ---


def test_detect_mngr_install_mode_returns_package_for_non_editable_package() -> None:
    """detect_mngr_install_mode should return PACKAGE for a regularly installed package."""
    # pytest is pip-installed (not editable), so it should return PACKAGE
    result = detect_mngr_install_mode("pytest")
    assert result == MngrInstallMode.PACKAGE


def test_resolve_mngr_install_mode_resolves_auto() -> None:
    """resolve_mngr_install_mode should resolve AUTO to a concrete mode."""
    result = resolve_mngr_install_mode(MngrInstallMode.AUTO)
    assert result in (MngrInstallMode.EDITABLE, MngrInstallMode.PACKAGE)


def test_resolve_mngr_install_mode_passes_through_package() -> None:
    """resolve_mngr_install_mode should pass through PACKAGE unchanged."""
    result = resolve_mngr_install_mode(MngrInstallMode.PACKAGE)
    assert result == MngrInstallMode.PACKAGE


def test_resolve_mngr_install_mode_passes_through_editable() -> None:
    """resolve_mngr_install_mode should pass through EDITABLE unchanged."""
    result = resolve_mngr_install_mode(MngrInstallMode.EDITABLE)
    assert result == MngrInstallMode.EDITABLE


def test_resolve_mngr_install_mode_passes_through_skip() -> None:
    """resolve_mngr_install_mode should pass through SKIP unchanged."""
    result = resolve_mngr_install_mode(MngrInstallMode.SKIP)
    assert result == MngrInstallMode.SKIP


# --- collect_provider_profile_files tests ---


def test_collect_provider_profile_files_returns_empty_when_dir_missing(
    temp_mngr_ctx: MngrContext,
) -> None:
    """collect_provider_profile_files should return empty dict when provider dir doesn't exist."""
    result = collect_provider_profile_files(
        mngr_ctx=temp_mngr_ctx,
        provider_name="nonexistent-provider",
        excluded_file_names=frozenset(),
    )
    assert result == {}


def test_collect_provider_profile_files_collects_files(
    temp_mngr_ctx: MngrContext,
) -> None:
    """collect_provider_profile_files should return files from the provider directory."""
    provider_dir = temp_mngr_ctx.profile_dir / "providers" / "test-provider"
    provider_dir.mkdir(parents=True, exist_ok=True)
    (provider_dir / "config.toml").write_text("test config")
    (provider_dir / "other.txt").write_text("other content")

    result = collect_provider_profile_files(
        mngr_ctx=temp_mngr_ctx,
        provider_name="test-provider",
        excluded_file_names=frozenset(),
    )

    assert len(result) == 2


def test_collect_provider_profile_files_excludes_specified_files(
    temp_mngr_ctx: MngrContext,
) -> None:
    """collect_provider_profile_files should exclude files in the excluded set."""
    provider_dir = temp_mngr_ctx.profile_dir / "providers" / "test-provider"
    provider_dir.mkdir(parents=True, exist_ok=True)
    (provider_dir / "config.toml").write_text("test config")
    (provider_dir / "id_rsa").write_text("secret key")
    (provider_dir / "known_hosts").write_text("host data")

    result = collect_provider_profile_files(
        mngr_ctx=temp_mngr_ctx,
        provider_name="test-provider",
        excluded_file_names=frozenset({"id_rsa", "known_hosts"}),
    )

    assert len(result) == 1
    # The single remaining file should be config.toml
    dest_paths = list(result.keys())
    assert any("config.toml" in str(p) for p in dest_paths)
