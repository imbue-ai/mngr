"""Unit tests for mng_recursive provisioning logic."""

from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from imbue.mng.errors import MngError
from imbue.mng.interfaces.data_types import CommandResult
from imbue.mng.primitives import PluginName
from imbue.mng.providers.deploy_utils import MngInstallMode
from imbue.mng.providers.deploy_utils import detect_mng_install_mode
from imbue.mng.providers.deploy_utils import resolve_mng_install_mode
from imbue.mng_recursive.data_types import RecursivePluginConfig
from imbue.mng_recursive.provisioning import _get_installed_mng_packages
from imbue.mng_recursive.provisioning import _resolve_remote_path
from imbue.mng_recursive.provisioning import _upload_deploy_files
from imbue.mng_recursive.provisioning import provision_mng_on_host


def _make_command_result(success: bool, stdout: str = "", stderr: str = "") -> CommandResult:
    """Create a CommandResult for testing."""
    return CommandResult(
        success=success,
        stdout=stdout,
        stderr=stderr,
    )


def _make_mock_host(is_local: bool = False) -> MagicMock:
    """Create a mock OnlineHostInterface."""
    host = MagicMock()
    host.is_local = is_local
    host.execute_command.return_value = _make_command_result(True, stdout="/home/testuser\n")
    host.write_file.return_value = None
    host.write_text_file.return_value = None
    return host


def _make_mock_mng_ctx(
    plugin_config: RecursivePluginConfig | None = None,
) -> MagicMock:
    """Create a mock MngContext."""
    ctx = MagicMock()
    plugins: dict[PluginName, RecursivePluginConfig] = {}
    if plugin_config is not None:
        plugins[PluginName("recursive")] = plugin_config
    ctx.config.plugins = plugins
    ctx.pm.hook.get_files_for_deploy.return_value = []
    return ctx


# --- Path resolution tests ---


def test_resolve_remote_path_with_tilde() -> None:
    """Paths starting with ~ should resolve relative to the remote home."""
    result = _resolve_remote_path(Path("~/.mng/config.toml"), "/home/testuser")
    assert result == Path("/home/testuser/.mng/config.toml")


def test_resolve_remote_path_with_tilde_nested() -> None:
    """Nested tilde paths should resolve correctly."""
    result = _resolve_remote_path(Path("~/.mng/profiles/abc/settings.toml"), "/home/testuser")
    assert result == Path("/home/testuser/.mng/profiles/abc/settings.toml")


def test_resolve_remote_path_relative() -> None:
    """Relative paths should pass through unchanged."""
    result = _resolve_remote_path(Path(".mng/settings.local.toml"), "/home/testuser")
    assert result == Path(".mng/settings.local.toml")


# --- Upload tests ---


def test_upload_deploy_files_with_path_source(tmp_path: Path) -> None:
    """Files with Path sources should be read and uploaded."""
    host = _make_mock_host()
    source_file = tmp_path / "config.toml"
    source_file.write_text("key = 'value'")

    deploy_files: dict[Path, Path | str] = {
        Path("~/.mng/config.toml"): source_file,
    }

    count = _upload_deploy_files(host, deploy_files, "/home/testuser")

    assert count == 1
    host.execute_command.assert_called()
    host.write_file.assert_called_once_with(
        Path("/home/testuser/.mng/config.toml"),
        source_file.read_bytes(),
    )


def test_upload_deploy_files_with_string_source() -> None:
    """Files with string sources should be uploaded directly."""
    host = _make_mock_host()
    deploy_files: dict[Path, Path | str] = {
        Path("~/.mng/config.toml"): 'key = "value"',
    }

    count = _upload_deploy_files(host, deploy_files, "/home/testuser")

    assert count == 1
    host.write_text_file.assert_called_once_with(
        Path("/home/testuser/.mng/config.toml"),
        'key = "value"',
    )


def test_upload_deploy_files_skips_missing_path(tmp_path: Path) -> None:
    """Missing Path source files should be skipped."""
    host = _make_mock_host()
    deploy_files: dict[Path, Path | str] = {
        Path("~/.mng/config.toml"): tmp_path / "nonexistent.toml",
    }

    count = _upload_deploy_files(host, deploy_files, "/home/testuser")

    assert count == 0
    host.write_file.assert_not_called()
    host.write_text_file.assert_not_called()


def test_upload_deploy_files_creates_parent_dirs() -> None:
    """Parent directories should be created before uploading."""
    host = _make_mock_host()
    deploy_files: dict[Path, Path | str] = {
        Path("~/.mng/profiles/abc/settings.toml"): "content",
    }

    _upload_deploy_files(host, deploy_files, "/home/testuser")

    # Check that mkdir -p was called for the parent directory
    mkdir_calls = [call for call in host.execute_command.call_args_list if "mkdir -p" in str(call)]
    assert len(mkdir_calls) == 1


# --- Local host skip tests ---


def test_skip_local_host() -> None:
    """provision_mng_on_host should be a no-op for local hosts."""
    host = _make_mock_host(is_local=True)
    ctx = _make_mock_mng_ctx()

    provision_mng_on_host(host=host, mng_ctx=ctx)

    # Should not execute any commands
    host.execute_command.assert_not_called()
    host.write_file.assert_not_called()


def test_skip_when_install_mode_is_skip() -> None:
    """provision_mng_on_host should skip when install_mode is SKIP."""
    host = _make_mock_host(is_local=False)
    ctx = _make_mock_mng_ctx(
        plugin_config=RecursivePluginConfig(install_mode=MngInstallMode.SKIP),
    )

    provision_mng_on_host(host=host, mng_ctx=ctx)

    # Should not execute any commands (no home dir lookup, no file uploads, etc.)
    host.execute_command.assert_not_called()


# --- Install mode detection ---


def testresolve_mng_install_mode_auto() -> None:
    """AUTO mode should resolve to a concrete mode."""
    with patch("imbue.mng.providers.deploy_utils.detect_mng_install_mode") as mock_detect:
        mock_detect.return_value = MngInstallMode.PACKAGE
        result = resolve_mng_install_mode(MngInstallMode.AUTO)
        assert result == MngInstallMode.PACKAGE
        mock_detect.assert_called_once()


def testresolve_mng_install_mode_explicit() -> None:
    """Explicit modes should pass through unchanged."""
    assert resolve_mng_install_mode(MngInstallMode.PACKAGE) == MngInstallMode.PACKAGE
    assert resolve_mng_install_mode(MngInstallMode.EDITABLE) == MngInstallMode.EDITABLE
    assert resolve_mng_install_mode(MngInstallMode.SKIP) == MngInstallMode.SKIP


def testdetect_mng_install_mode_returns_valid_mode() -> None:
    """detect_local_install_mode should return either PACKAGE or EDITABLE."""
    result = detect_mng_install_mode()
    assert result in (MngInstallMode.PACKAGE, MngInstallMode.EDITABLE)


def test_get_installed_mng_packages_finds_mng() -> None:
    """Should find at least the mng package itself."""
    packages = _get_installed_mng_packages()
    package_names = [name for name, _ in packages]
    assert "mng" in package_names


# --- Error handling ---


def test_errors_fatal_raises_on_failure() -> None:
    """When is_errors_fatal=True, errors should raise MngError."""
    host = _make_mock_host(is_local=False)
    # Make echo $HOME fail
    host.execute_command.return_value = _make_command_result(False, stderr="connection refused")
    ctx = _make_mock_mng_ctx(
        plugin_config=RecursivePluginConfig(is_errors_fatal=True, install_mode=MngInstallMode.PACKAGE),
    )

    with pytest.raises(MngError, match="Failed to determine remote home directory"):
        provision_mng_on_host(host=host, mng_ctx=ctx)


def test_errors_non_fatal_warns_on_failure() -> None:
    """When is_errors_fatal=False, MngErrors should log warnings instead of raising."""
    host = _make_mock_host(is_local=False)
    # Make echo $HOME fail so _get_remote_home raises MngError
    host.execute_command.return_value = _make_command_result(False, stderr="connection refused")
    ctx = _make_mock_mng_ctx(
        plugin_config=RecursivePluginConfig(is_errors_fatal=False, install_mode=MngInstallMode.PACKAGE),
    )

    # Should not raise (MngError is caught and logged as warning)
    provision_mng_on_host(host=host, mng_ctx=ctx)


# --- Package mode installation ---


def test_package_mode_builds_correct_command() -> None:
    """Package mode should build a uv tool install command with --with for plugins."""
    host = _make_mock_host(is_local=False)
    host.execute_command.side_effect = [
        # echo $HOME
        _make_command_result(True, stdout="/home/testuser\n"),
        # command -v uv (uv is available)
        _make_command_result(True),
        # uv tool install command
        _make_command_result(True),
    ]
    ctx = _make_mock_mng_ctx(
        plugin_config=RecursivePluginConfig(install_mode=MngInstallMode.PACKAGE),
    )
    ctx.pm.hook.get_files_for_deploy.return_value = []

    with patch("imbue.mng_recursive.provisioning._get_installed_mng_packages") as mock_packages:
        mock_packages.return_value = [("mng", "0.1.4"), ("mng-pair", "0.1.0")]
        provision_mng_on_host(host=host, mng_ctx=ctx)

    # Find the uv tool install call
    install_calls = [call for call in host.execute_command.call_args_list if "uv tool install" in str(call)]
    assert len(install_calls) >= 1
    install_cmd = str(install_calls[0])
    assert "mng==0.1.4" in install_cmd
    assert "--with mng-pair==0.1.0" in install_cmd


def test_uv_installed_when_missing() -> None:
    """When uv is not available, it should be installed via curl."""
    host = _make_mock_host(is_local=False)
    host.execute_command.side_effect = [
        # echo $HOME
        _make_command_result(True, stdout="/home/testuser\n"),
        # command -v uv (uv NOT available)
        _make_command_result(False),
        # curl install uv
        _make_command_result(True),
        # source env
        _make_command_result(True),
        # uv tool install
        _make_command_result(True),
    ]
    ctx = _make_mock_mng_ctx(
        plugin_config=RecursivePluginConfig(install_mode=MngInstallMode.PACKAGE),
    )
    ctx.pm.hook.get_files_for_deploy.return_value = []

    with patch("imbue.mng_recursive.provisioning._get_installed_mng_packages") as mock_packages:
        mock_packages.return_value = [("mng", "0.1.4")]
        provision_mng_on_host(host=host, mng_ctx=ctx)

    # Find the curl call
    curl_calls = [call for call in host.execute_command.call_args_list if "astral.sh/uv" in str(call)]
    assert len(curl_calls) == 1
