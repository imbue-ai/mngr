"""Unit tests for mng_recursive provisioning logic."""

from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from imbue.mng.errors import MngError
from imbue.mng.interfaces.data_types import CommandResult
from imbue.mng.providers.deploy_utils import MngInstallMode
from imbue.mng_recursive.data_types import RecursivePluginConfig
from imbue.mng_recursive.plugin import on_host_created
from imbue.mng_recursive.provisioning import _build_uv_env_prefix
from imbue.mng_recursive.provisioning import _ensure_uv_available
from imbue.mng_recursive.provisioning import _get_installed_mng_packages
from imbue.mng_recursive.provisioning import _get_mng_repo_root
from imbue.mng_recursive.provisioning import _install_mng_package_mode
from imbue.mng_recursive.provisioning import _resolve_remote_path
from imbue.mng_recursive.provisioning import _upload_deploy_files
from imbue.mng_recursive.provisioning import provision_mng_for_agent
from imbue.mng_recursive.provisioning import provision_mng_on_host


def _make_command_result(success: bool, stdout: str = "", stderr: str = "") -> CommandResult:
    """Create a CommandResult for testing."""
    return CommandResult(
        success=success,
        stdout=stdout,
        stderr=stderr,
    )


def _make_mock_host(is_local: bool = False, host_dir: Path | None = None) -> MagicMock:
    """Create a mock OnlineHostInterface."""
    host = MagicMock()
    host.is_local = is_local
    host.host_dir = host_dir or Path("/tmp/mng-test/host")
    host.execute_command.return_value = _make_command_result(True, stdout="/home/testuser\n")
    host.write_file.return_value = None
    host.write_text_file.return_value = None
    return host


def _make_mock_mng_ctx(
    plugin_config: RecursivePluginConfig | None = None,
) -> MagicMock:
    """Create a mock MngContext."""
    ctx = MagicMock()
    resolved_config = plugin_config if plugin_config is not None else RecursivePluginConfig()
    ctx.get_plugin_config.return_value = resolved_config
    ctx.pm.hook.get_files_for_deploy.return_value = []
    return ctx


def _make_mock_agent(agent_id: str = "agent-123", mng_ctx: MagicMock | None = None) -> MagicMock:
    """Create a mock AgentInterface."""
    agent = MagicMock()
    agent.id = agent_id
    agent.name = "test-agent"
    agent.mng_ctx = mng_ctx or _make_mock_mng_ctx()
    return agent


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
    ctx = _make_mock_mng_ctx()
    source_file = tmp_path / "config.toml"
    source_file.write_text("key = 'value'")

    deploy_files: dict[Path, Path | str] = {
        Path("~/.mng/config.toml"): source_file,
    }

    count = _upload_deploy_files(host, deploy_files, "/home/testuser", ctx)

    assert count == 1
    host.execute_command.assert_called()
    host.write_file.assert_called_once_with(
        path=Path("/home/testuser/.mng/config.toml"),
        content=source_file.read_bytes(),
    )


def test_upload_deploy_files_with_string_source() -> None:
    """Files with string sources should be uploaded directly."""
    host = _make_mock_host()
    ctx = _make_mock_mng_ctx()
    deploy_files: dict[Path, Path | str] = {
        Path("~/.mng/config.toml"): 'key = "value"',
    }

    count = _upload_deploy_files(host, deploy_files, "/home/testuser", ctx)

    assert count == 1
    host.write_text_file.assert_called_once_with(
        path=Path("/home/testuser/.mng/config.toml"),
        content='key = "value"',
    )


def test_upload_deploy_files_skips_missing_path(tmp_path: Path) -> None:
    """Missing Path source files should be skipped."""
    host = _make_mock_host()
    ctx = _make_mock_mng_ctx()
    deploy_files: dict[Path, Path | str] = {
        Path("~/.mng/config.toml"): tmp_path / "nonexistent.toml",
    }

    count = _upload_deploy_files(host, deploy_files, "/home/testuser", ctx)

    assert count == 0
    host.write_file.assert_not_called()
    host.write_text_file.assert_not_called()


def test_upload_deploy_files_creates_parent_dirs() -> None:
    """Parent directories should be created before uploading."""
    host = _make_mock_host()
    ctx = _make_mock_mng_ctx()
    deploy_files: dict[Path, Path | str] = {
        Path("~/.mng/profiles/abc/settings.toml"): "content",
    }

    _upload_deploy_files(host, deploy_files, "/home/testuser", ctx)

    # Check that mkdir -p was called for the parent directory
    mkdir_calls = [call for call in host.execute_command.call_args_list if "mkdir -p" in str(call)]
    assert len(mkdir_calls) == 1


# --- Host provisioning tests ---


def test_local_host_ensures_uv_available() -> None:
    """provision_mng_on_host on a local host should check for uv availability."""
    host = _make_mock_host(is_local=True)
    ctx = _make_mock_mng_ctx()

    provision_mng_on_host(host=host, mng_ctx=ctx)

    # Should have checked for uv (command -v uv)
    uv_checks = [call for call in host.execute_command.call_args_list if "command -v uv" in str(call)]
    assert len(uv_checks) == 1

    # Should NOT have tried to get home dir or upload deploy files
    home_checks = [call for call in host.execute_command.call_args_list if "echo $HOME" in str(call)]
    assert len(home_checks) == 0


def test_remote_host_uploads_deploy_files_and_ensures_uv() -> None:
    """provision_mng_on_host on a remote host should upload files and check uv."""
    host = _make_mock_host(is_local=False)
    ctx = _make_mock_mng_ctx()
    ctx.pm.hook.get_files_for_deploy.return_value = []

    provision_mng_on_host(host=host, mng_ctx=ctx)

    # Should have checked for home dir (remote path resolution)
    home_checks = [call for call in host.execute_command.call_args_list if "echo $HOME" in str(call)]
    assert len(home_checks) == 1

    # Should have checked for uv
    uv_checks = [call for call in host.execute_command.call_args_list if "command -v uv" in str(call)]
    assert len(uv_checks) == 1


def test_skip_when_install_mode_is_skip() -> None:
    """provision_mng_on_host should skip when install_mode is SKIP."""
    host = _make_mock_host(is_local=False)
    ctx = _make_mock_mng_ctx(
        plugin_config=RecursivePluginConfig(install_mode=MngInstallMode.SKIP),
    )

    provision_mng_on_host(host=host, mng_ctx=ctx)

    # Should not execute any commands (no home dir lookup, no file uploads, etc.)
    host.execute_command.assert_not_called()


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


# --- Per-agent mng installation ---


def test_agent_package_mode_builds_correct_command() -> None:
    """Package mode should build a uv tool install command with UV_TOOL_DIR and UV_TOOL_BIN_DIR."""
    host_dir = Path("/tmp/mng-test/host")
    host = _make_mock_host(is_local=False, host_dir=host_dir)
    host.execute_command.return_value = _make_command_result(True)
    ctx = _make_mock_mng_ctx(
        plugin_config=RecursivePluginConfig(install_mode=MngInstallMode.PACKAGE),
    )
    agent = _make_mock_agent(mng_ctx=ctx)

    with patch("imbue.mng_recursive.provisioning._get_installed_mng_packages") as mock_packages:
        mock_packages.return_value = [("mng", "0.1.4"), ("mng-pair", "0.1.0")]
        provision_mng_for_agent(agent=agent, host=host, mng_ctx=ctx)

    # Find the uv tool install call
    install_calls = [call for call in host.execute_command.call_args_list if "uv tool install" in str(call)]
    assert len(install_calls) >= 1
    install_cmd = str(install_calls[0])
    assert "mng==0.1.4" in install_cmd
    assert "--with mng-pair==0.1.0" in install_cmd

    # Verify UV_TOOL_DIR and UV_TOOL_BIN_DIR are set to agent-specific paths
    agent_state_dir = host_dir / "agents" / "agent-123"
    assert str(agent_state_dir / "tools") in install_cmd
    assert str(agent_state_dir / "bin") in install_cmd


def test_agent_editable_local_mode_builds_correct_command(tmp_path: Path) -> None:
    """Editable local mode should install from the source tree with per-agent UV_TOOL_DIR/UV_TOOL_BIN_DIR."""
    # Set up a fake monorepo structure
    repo_root = tmp_path / "monorepo"
    libs_dir = repo_root / "libs"
    (libs_dir / "mng").mkdir(parents=True)
    (libs_dir / "mng_recursive").mkdir(parents=True)
    (libs_dir / "mng_pair").mkdir(parents=True)
    (libs_dir / "imbue_common").mkdir(parents=True)

    host_dir = Path("/tmp/mng-test/host")
    host = _make_mock_host(is_local=True, host_dir=host_dir)
    host.execute_command.return_value = _make_command_result(True)
    ctx = _make_mock_mng_ctx(
        plugin_config=RecursivePluginConfig(install_mode=MngInstallMode.EDITABLE),
    )
    agent = _make_mock_agent(mng_ctx=ctx)

    with patch("imbue.mng_recursive.provisioning._get_mng_repo_root") as mock_root:
        mock_root.return_value = repo_root
        provision_mng_for_agent(agent=agent, host=host, mng_ctx=ctx)

    # Find the uv tool install call
    install_calls = [call for call in host.execute_command.call_args_list if "uv tool install" in str(call)]
    assert len(install_calls) >= 1
    install_cmd = str(install_calls[0])

    # Should use editable install from the source tree
    assert "-e libs/mng" in install_cmd

    # Should include mng_ prefixed plugins as --with-editable
    assert "--with-editable libs/mng_recursive" in install_cmd
    assert "--with-editable libs/mng_pair" in install_cmd

    # Should NOT include non-mng libs (like imbue_common)
    assert "imbue_common" not in install_cmd

    # Should have per-agent UV_TOOL_DIR and UV_TOOL_BIN_DIR
    agent_state_dir = host_dir / "agents" / "agent-123"
    assert str(agent_state_dir / "tools") in install_cmd
    assert str(agent_state_dir / "bin") in install_cmd

    # Should cd to the repo root
    assert str(repo_root) in install_cmd


def test_agent_skip_mode_does_nothing() -> None:
    """provision_mng_for_agent should skip when install_mode is SKIP."""
    host = _make_mock_host()
    ctx = _make_mock_mng_ctx(
        plugin_config=RecursivePluginConfig(install_mode=MngInstallMode.SKIP),
    )
    agent = _make_mock_agent(mng_ctx=ctx)

    provision_mng_for_agent(agent=agent, host=host, mng_ctx=ctx)

    host.execute_command.assert_not_called()


def test_agent_errors_fatal_raises() -> None:
    """When is_errors_fatal=True, agent-level mng install failures should raise."""
    host = _make_mock_host()
    host.execute_command.return_value = _make_command_result(False, stderr="mkdir failed")
    ctx = _make_mock_mng_ctx(
        plugin_config=RecursivePluginConfig(is_errors_fatal=True, install_mode=MngInstallMode.PACKAGE),
    )
    agent = _make_mock_agent(mng_ctx=ctx)

    with pytest.raises(MngError, match="Failed to create directory"):
        provision_mng_for_agent(agent=agent, host=host, mng_ctx=ctx)


def test_agent_errors_non_fatal_warns() -> None:
    """When is_errors_fatal=False, agent-level mng install failures should warn."""
    host = _make_mock_host()
    host.execute_command.return_value = _make_command_result(False, stderr="mkdir failed")
    ctx = _make_mock_mng_ctx(
        plugin_config=RecursivePluginConfig(is_errors_fatal=False, install_mode=MngInstallMode.PACKAGE),
    )
    agent = _make_mock_agent(mng_ctx=ctx)

    # Should not raise
    provision_mng_for_agent(agent=agent, host=host, mng_ctx=ctx)


def test_agent_creates_tool_and_bin_dirs() -> None:
    """provision_mng_for_agent should create the tools/ and bin/ directories."""
    host_dir = Path("/tmp/mng-test/host")
    host = _make_mock_host(host_dir=host_dir)
    host.execute_command.return_value = _make_command_result(True)
    ctx = _make_mock_mng_ctx(
        plugin_config=RecursivePluginConfig(install_mode=MngInstallMode.PACKAGE),
    )
    agent = _make_mock_agent(mng_ctx=ctx)

    with patch("imbue.mng_recursive.provisioning._get_installed_mng_packages") as mock_packages:
        mock_packages.return_value = [("mng", "0.1.4")]
        provision_mng_for_agent(agent=agent, host=host, mng_ctx=ctx)

    agent_state_dir = host_dir / "agents" / "agent-123"
    mkdir_calls = [str(call) for call in host.execute_command.call_args_list if "mkdir -p" in str(call)]
    assert any(str(agent_state_dir / "tools") in c for c in mkdir_calls)
    assert any(str(agent_state_dir / "bin") in c for c in mkdir_calls)


# --- uv installation ---


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
    ]
    ctx = _make_mock_mng_ctx(
        plugin_config=RecursivePluginConfig(install_mode=MngInstallMode.PACKAGE),
    )
    ctx.pm.hook.get_files_for_deploy.return_value = []

    provision_mng_on_host(host=host, mng_ctx=ctx)

    # Find the curl call
    curl_calls = [call for call in host.execute_command.call_args_list if "astral.sh/uv" in str(call)]
    assert len(curl_calls) == 1


# --- Plugin hook tests ---


def test_on_host_created_calls_provision() -> None:
    """on_host_created hook should call provision_mng_on_host."""
    host = _make_mock_host(is_local=True)
    ctx = _make_mock_mng_ctx()
    on_host_created(host=host, mng_ctx=ctx)
    host.execute_command.assert_called()


# --- Data types tests ---


def test_recursive_plugin_config_merge_with() -> None:
    """merge_with should let override values win over base values."""
    base = RecursivePluginConfig(is_errors_fatal=False, install_mode=MngInstallMode.AUTO)
    override = RecursivePluginConfig(is_errors_fatal=True, install_mode=MngInstallMode.PACKAGE)
    merged = base.merge_with(override)
    assert merged.is_errors_fatal is True
    assert merged.install_mode == MngInstallMode.PACKAGE


# --- _resolve_remote_path bare tilde test ---


def test_resolve_remote_path_bare_tilde() -> None:
    """A bare '~' should resolve to the remote home directory."""
    result = _resolve_remote_path(Path("~"), "/home/testuser")
    assert result == Path("/home/testuser")


# --- _build_uv_env_prefix test ---


def test_build_uv_env_prefix_sets_tool_and_bin_dirs() -> None:
    """_build_uv_env_prefix should export UV_TOOL_DIR and UV_TOOL_BIN_DIR."""
    result = _build_uv_env_prefix(Path("/tools"), Path("/bin"))
    assert "UV_TOOL_DIR=" in result
    assert "UV_TOOL_BIN_DIR=" in result
    assert "/tools" in result
    assert "/bin" in result


# --- _ensure_uv_available error tests ---


def test_ensure_uv_raises_on_install_failure() -> None:
    """_ensure_uv_available should raise when installation fails."""
    host = _make_mock_host()
    host.execute_command.side_effect = [
        _make_command_result(False),
        _make_command_result(False, stderr="curl failed"),
    ]
    with pytest.raises(MngError, match="Failed to install uv"):
        _ensure_uv_available(host)


def test_ensure_uv_raises_when_not_on_path_after_install() -> None:
    """_ensure_uv_available should raise when uv is installed but not findable."""
    host = _make_mock_host()
    host.execute_command.side_effect = [
        _make_command_result(False),
        _make_command_result(True),
        _make_command_result(False),
    ]
    with pytest.raises(MngError, match="cannot be found on PATH"):
        _ensure_uv_available(host)


# --- _install_mng_package_mode tests ---


def test_install_package_mode_raises_when_no_mng_package() -> None:
    """_install_mng_package_mode should raise when mng is not in packages list."""
    host = _make_mock_host()
    with pytest.raises(MngError, match="mng package not found"):
        _install_mng_package_mode(host, [("mng-pair", "0.1.0")], Path("/tools"), Path("/bin"))


def test_install_package_mode_retries_with_force_reinstall() -> None:
    """_install_mng_package_mode should retry with --force-reinstall on failure."""
    host = _make_mock_host()
    host.execute_command.side_effect = [
        _make_command_result(False, stderr="already installed"),
        _make_command_result(True),
    ]
    _install_mng_package_mode(host, [("mng", "0.1.4")], Path("/tools"), Path("/bin"))
    assert len(host.execute_command.call_args_list) == 2
    second_call = str(host.execute_command.call_args_list[1])
    assert "--force-reinstall" in second_call


# --- provision_mng_for_agent when no packages found ---


def test_agent_package_mode_warns_when_no_packages() -> None:
    """provision_mng_for_agent should warn when no mng packages are found locally."""
    host = _make_mock_host()
    host.execute_command.return_value = _make_command_result(True)
    ctx = _make_mock_mng_ctx(
        plugin_config=RecursivePluginConfig(install_mode=MngInstallMode.PACKAGE),
    )
    agent = _make_mock_agent(mng_ctx=ctx)

    with patch("imbue.mng_recursive.provisioning._get_installed_mng_packages") as mock_packages:
        mock_packages.return_value = []
        provision_mng_for_agent(agent=agent, host=host, mng_ctx=ctx)


# --- _upload_deploy_files mkdir failure ---


def test_upload_deploy_files_raises_on_mkdir_failure() -> None:
    """_upload_deploy_files should raise when mkdir -p fails."""
    host = _make_mock_host()
    host.execute_command.return_value = _make_command_result(False, stderr="permission denied")
    ctx = _make_mock_mng_ctx()
    deploy_files: dict[Path, Path | str] = {
        Path("~/.mng/config.toml"): "content",
    }
    with pytest.raises(MngError, match="Failed to create director"):
        _upload_deploy_files(host, deploy_files, "/home/testuser", ctx)


def test_install_package_mode_raises_when_force_reinstall_also_fails() -> None:
    """_install_mng_package_mode should raise when both install and force-reinstall fail."""
    host = _make_mock_host()
    host.execute_command.side_effect = [
        _make_command_result(False, stderr="install failed"),
        _make_command_result(False, stderr="reinstall also failed"),
    ]
    with pytest.raises(MngError, match="Failed to install mng"):
        _install_mng_package_mode(host, [("mng", "0.1.4")], Path("/tools"), Path("/bin"))


def test_agent_editable_mode_dispatches_and_retries_force_reinstall(tmp_path: Path) -> None:
    """Editable local mode should dispatch to install and retry with --force-reinstall on failure."""
    repo_root = tmp_path / "monorepo"
    libs_dir = repo_root / "libs"
    (libs_dir / "mng").mkdir(parents=True)

    host_dir = tmp_path / "host"
    host_dir.mkdir()
    host = _make_mock_host(is_local=True, host_dir=host_dir)

    def execute_side_effect(cmd: str, **kwargs: object) -> CommandResult:
        if "uv tool install" in cmd and "--force-reinstall" not in cmd:
            return _make_command_result(False, stderr="already installed")
        return _make_command_result(True)

    host.execute_command.side_effect = execute_side_effect
    ctx = _make_mock_mng_ctx(
        plugin_config=RecursivePluginConfig(install_mode=MngInstallMode.EDITABLE),
    )
    agent = _make_mock_agent(mng_ctx=ctx)

    with patch("imbue.mng_recursive.provisioning._get_mng_repo_root") as mock_root:
        mock_root.return_value = repo_root
        provision_mng_for_agent(agent=agent, host=host, mng_ctx=ctx)

    install_calls = [call for call in host.execute_command.call_args_list if "uv tool install" in str(call)]
    assert len(install_calls) >= 1
    force_calls = [call for call in host.execute_command.call_args_list if "--force-reinstall" in str(call)]
    assert len(force_calls) >= 1


def test_provision_on_host_handles_deploy_file_errors() -> None:
    """provision_mng_on_host should catch errors from collect_deploy_files when not fatal."""
    host = _make_mock_host(is_local=False)
    ctx = _make_mock_mng_ctx(
        plugin_config=RecursivePluginConfig(is_errors_fatal=False, install_mode=MngInstallMode.PACKAGE),
    )

    with patch("imbue.mng_recursive.provisioning.collect_deploy_files") as mock_collect:
        mock_collect.side_effect = MngError("absolute path not allowed")
        provision_mng_on_host(host=host, mng_ctx=ctx)


# --- _get_mng_repo_root tests ---


def test_get_mng_repo_root_returns_repo_root() -> None:
    """_get_mng_repo_root should return the git repo root of the mng monorepo."""
    result = _get_mng_repo_root()
    assert result.is_dir()
    assert (result / ".git").exists()


def test_editable_local_raises_when_force_reinstall_also_fails(tmp_path: Path) -> None:
    """Editable local mode should raise when both install and force-reinstall fail."""
    repo_root = tmp_path / "monorepo"
    libs_dir = repo_root / "libs"
    (libs_dir / "mng").mkdir(parents=True)

    host_dir = tmp_path / "host"
    host_dir.mkdir()
    host = _make_mock_host(is_local=True, host_dir=host_dir)
    host.execute_command.side_effect = [
        _make_command_result(True),
        _make_command_result(True),
        _make_command_result(False, stderr="install failed"),
        _make_command_result(False, stderr="reinstall also failed"),
    ]

    ctx = _make_mock_mng_ctx(
        plugin_config=RecursivePluginConfig(is_errors_fatal=True, install_mode=MngInstallMode.EDITABLE),
    )
    agent = _make_mock_agent(mng_ctx=ctx)

    with patch("imbue.mng_recursive.provisioning._get_mng_repo_root") as mock_root:
        mock_root.return_value = repo_root
        with pytest.raises(MngError, match="Failed to install mng in editable mode"):
            provision_mng_for_agent(agent=agent, host=host, mng_ctx=ctx)
