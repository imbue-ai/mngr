"""Unit tests for mngr_recursive provisioning logic."""

from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from inline_snapshot import snapshot

from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.providers.deploy_utils import MngrInstallMode
from imbue.mngr_recursive.data_types import RecursivePluginConfig
from imbue.mngr_recursive.plugin import on_host_created
from imbue.mngr_recursive.provisioning import _build_editable_install_command
from imbue.mngr_recursive.provisioning import _build_package_install_command
from imbue.mngr_recursive.provisioning import _build_uv_env_prefix
from imbue.mngr_recursive.provisioning import _ensure_uv_available
from imbue.mngr_recursive.provisioning import _get_installed_mngr_packages
from imbue.mngr_recursive.provisioning import _get_mngr_repo_root
from imbue.mngr_recursive.provisioning import _install_mngr_package_mode
from imbue.mngr_recursive.provisioning import _resolve_editable_source_dir
from imbue.mngr_recursive.provisioning import _upload_deploy_files
from imbue.mngr_recursive.provisioning import provision_mngr_for_agent
from imbue.mngr_recursive.provisioning import provision_mngr_on_host


def _make_command_result(success: bool, stdout: str = "", stderr: str = "") -> CommandResult:
    """Create a CommandResult for testing."""
    return CommandResult(
        success=success,
        stdout=stdout,
        stderr=stderr,
    )


def _make_mock_host(is_local: bool = False, host_dir: Path | None = None) -> MagicMock:
    """Create a mock OnlineHostInterface.

    Only the methods provisioning actually calls (execute_idempotent_command,
    write_file, write_text_file) are configured. provisioning never uses
    execute_stateful_command, so it is left unconfigured rather than given a
    misleading canned return value.
    """
    host = MagicMock()
    host.is_local = is_local
    host.host_dir = host_dir or Path("/tmp/mngr-test/host")
    host.execute_idempotent_command.return_value = _make_command_result(True, stdout="/home/testuser\n")
    host.write_file.return_value = None
    host.write_text_file.return_value = None
    return host


def _issued_commands(host: MagicMock) -> list[str]:
    """Return the command strings passed to ``execute_idempotent_command``, in order.

    Reads the exact first positional argument of each recorded call, so assertions
    test the real command issued rather than the repr of the whole mock call.
    """
    return [call.args[0] for call in host.execute_idempotent_command.call_args_list]


def _make_mock_mngr_ctx(
    plugin_config: RecursivePluginConfig | None = None,
) -> MagicMock:
    """Create a mock MngrContext."""
    ctx = MagicMock()
    resolved_config = plugin_config if plugin_config is not None else RecursivePluginConfig()
    ctx.get_plugin_config.return_value = resolved_config
    ctx.pm.hook.get_files_for_deploy.return_value = []
    return ctx


def _make_mock_agent(agent_id: str = "agent-123", mngr_ctx: MagicMock | None = None) -> MagicMock:
    """Create a mock AgentInterface."""
    agent = MagicMock()
    agent.id = agent_id
    agent.name = "test-agent"
    agent.mngr_ctx = mngr_ctx or _make_mock_mngr_ctx()
    return agent


# --- Staging / upload tests ---


def test_upload_deploy_files_uses_single_rsync(tmp_path: Path) -> None:
    """All deploy files are transferred via one copy_local_directory (rsync) call, not per-file writes.

    This is the regression guard for the Modal "SSH connection reset / banner" bug: the old
    implementation opened one SFTP channel per file (~0.7s/file over a Modal tunnel), so a few
    hundred plugin files timed out or reset the connection. The fix must transfer the whole set
    in a single rsync regardless of file count.
    """
    host = _make_mock_host(is_local=False)
    source_file = tmp_path / "a.txt"
    source_file.write_text("aaa")
    deploy_files: dict[Path, Path | str] = {
        Path("~/.claude/a.txt"): source_file,
        Path("~/.mngr/b.toml"): "bbb",
    }

    # Snapshot the staging tree at copy_local_directory time, since the temp dir is
    # cleaned up before _upload_deploy_files returns.
    staged_snapshot: dict[str, str] = {}

    def _record(source_path: Path, target_path: Path, extra_args: object) -> None:
        for staged in Path(source_path).rglob("*"):
            if staged.is_file():
                staged_snapshot[staged.relative_to(source_path).as_posix()] = staged.read_text()

    host.copy_local_directory.side_effect = _record

    count = _upload_deploy_files(host, deploy_files, "/home/testuser")

    assert count == 2
    host.copy_local_directory.assert_called_once()
    # Deploy files must be transferred via a single rsync, never per-file writes.
    host.write_file.assert_not_called()
    host.write_text_file.assert_not_called()
    call_args = host.copy_local_directory.call_args.args
    # rsync targets the tightest common ancestor of the destinations, not "/".
    assert call_args[1] == Path("/home/testuser")
    assert staged_snapshot == {
        ".claude/a.txt": "aaa",
        ".mngr/b.toml": "bbb",
    }


# --- Host provisioning tests ---


def test_local_host_ensures_uv_available() -> None:
    """provision_mngr_on_host on a local host should check for uv availability."""
    host = _make_mock_host(is_local=True)
    ctx = _make_mock_mngr_ctx()

    provision_mngr_on_host(host=host, mngr_ctx=ctx)

    commands = _issued_commands(host)
    # Should have checked for uv exactly once.
    assert [c for c in commands if "command -v uv" in c] == ["command -v uv"]
    # Should NOT have tried to get the remote home dir (that is a remote-only path).
    assert not [c for c in commands if "echo $HOME" in c]


def test_remote_host_uploads_deploy_files_and_ensures_uv() -> None:
    """provision_mngr_on_host on a remote host should upload files and check uv."""
    host = _make_mock_host(is_local=False)
    ctx = _make_mock_mngr_ctx()
    ctx.pm.hook.get_files_for_deploy.return_value = []

    provision_mngr_on_host(host=host, mngr_ctx=ctx)

    commands = _issued_commands(host)
    # Should have checked for the remote home dir (remote path resolution) exactly once.
    assert [c for c in commands if "echo $HOME" in c] == ["echo $HOME"]
    # Should have checked for uv exactly once.
    assert [c for c in commands if "command -v uv" in c] == ["command -v uv"]


def test_skip_when_install_mode_is_skip() -> None:
    """provision_mngr_on_host should skip when install_mode is SKIP."""
    host = _make_mock_host(is_local=False)
    ctx = _make_mock_mngr_ctx(
        plugin_config=RecursivePluginConfig(install_mode=MngrInstallMode.SKIP),
    )

    provision_mngr_on_host(host=host, mngr_ctx=ctx)

    # Should not execute any commands (no home dir lookup, no file uploads, etc.)
    host.execute_idempotent_command.assert_not_called()


def test_get_installed_mngr_packages_finds_mngr() -> None:
    """Should find at least the mngr package itself.

    This is intentionally coupled to the test environment, which installs the
    mngr workspace (so ``imbue-mngr`` is present). It also asserts the *shape*
    of every returned entry -- the only names returned are the base package or
    its ``imbue-mngr-`` plugins -- which holds regardless of which plugins are
    installed.
    """
    packages = _get_installed_mngr_packages()
    package_names = [name for name, _ in packages]
    assert "imbue-mngr" in package_names
    assert all(name == "imbue-mngr" or name.startswith("imbue-mngr-") for name in package_names)


# --- Error handling ---


def test_errors_fatal_raises_on_failure() -> None:
    """When is_errors_fatal=True, errors should raise MngrError."""
    host = _make_mock_host(is_local=False)
    # Make echo $HOME fail
    host.execute_idempotent_command.return_value = _make_command_result(False, stderr="connection refused")
    ctx = _make_mock_mngr_ctx(
        plugin_config=RecursivePluginConfig(is_errors_fatal=True, install_mode=MngrInstallMode.PACKAGE),
    )

    with pytest.raises(MngrError, match="Failed to determine remote home directory"):
        provision_mngr_on_host(host=host, mngr_ctx=ctx)


def test_errors_non_fatal_warns_on_failure(log_warnings: list[str]) -> None:
    """When is_errors_fatal=False, MngrErrors should be logged as a warning instead of raising."""
    host = _make_mock_host(is_local=False)
    # Make echo $HOME fail so _get_remote_home raises MngrError
    host.execute_idempotent_command.return_value = _make_command_result(False, stderr="connection refused")
    ctx = _make_mock_mngr_ctx(
        plugin_config=RecursivePluginConfig(is_errors_fatal=False, install_mode=MngrInstallMode.PACKAGE),
    )

    # Should not raise (MngrError is caught and logged as a warning).
    provision_mngr_on_host(host=host, mngr_ctx=ctx)

    # The non-fatal path's entire contract is that it warns rather than raising.
    assert any("Failed to provision mngr prerequisites on host" in m for m in log_warnings), log_warnings


# --- Per-agent mngr installation ---


def test_agent_package_mode_builds_correct_command() -> None:
    """Package mode should build a uv tool install command with UV_TOOL_DIR and UV_TOOL_BIN_DIR."""
    host_dir = Path("/tmp/mngr-test/host")
    host = _make_mock_host(is_local=False, host_dir=host_dir)
    host.execute_idempotent_command.return_value = _make_command_result(True)
    ctx = _make_mock_mngr_ctx(
        plugin_config=RecursivePluginConfig(install_mode=MngrInstallMode.PACKAGE),
    )
    agent = _make_mock_agent(mngr_ctx=ctx)

    with patch("imbue.mngr_recursive.provisioning._get_installed_mngr_packages") as mock_packages:
        mock_packages.return_value = [("imbue-mngr", "0.1.4"), ("imbue-mngr-pair", "0.1.0")]
        provision_mngr_for_agent(agent=agent, host=host, mngr_ctx=ctx)

    # The install command issued should be exactly what the package-mode builder
    # produces for the agent's per-agent tool/bin dirs and the discovered packages.
    # (The command string itself is pinned by test_build_package_install_command;
    # here we verify provision_mngr_for_agent wires the right inputs into it.)
    agent_state_dir = host_dir / "agents" / "agent-123"
    expected_cmd = _build_package_install_command(
        ("imbue-mngr", "0.1.4"),
        [("imbue-mngr-pair", "0.1.0")],
        agent_state_dir / "tools",
        agent_state_dir / "bin",
    )
    install_commands = [c for c in _issued_commands(host) if "uv tool install" in c]
    assert install_commands == [expected_cmd]


def test_agent_editable_local_mode_builds_correct_command(tmp_path: Path) -> None:
    """Editable local mode should install from the source tree with per-agent UV_TOOL_DIR/UV_TOOL_BIN_DIR."""
    # Set up a fake monorepo structure
    repo_root = tmp_path / "monorepo"
    libs_dir = repo_root / "libs"
    (libs_dir / "mngr").mkdir(parents=True)
    (libs_dir / "mngr_recursive").mkdir(parents=True)
    (libs_dir / "mngr_pair").mkdir(parents=True)
    (libs_dir / "imbue_common").mkdir(parents=True)

    host_dir = Path("/tmp/mngr-test/host")
    host = _make_mock_host(is_local=True, host_dir=host_dir)
    host.execute_idempotent_command.return_value = _make_command_result(True)
    ctx = _make_mock_mngr_ctx(
        plugin_config=RecursivePluginConfig(install_mode=MngrInstallMode.EDITABLE),
    )
    agent = _make_mock_agent(mngr_ctx=ctx)

    with patch("imbue.mngr_recursive.provisioning._get_mngr_repo_root") as mock_root:
        mock_root.return_value = repo_root
        provision_mngr_for_agent(agent=agent, host=host, mngr_ctx=ctx)

    install_commands = [c for c in _issued_commands(host) if "uv tool install" in c]
    assert len(install_commands) == 1
    install_cmd = install_commands[0]

    # Editable install from the source tree, cd'ing into the (quoted) repo root.
    assert f"cd {repo_root} && uv tool install -e libs/mngr" in install_cmd
    # mngr_-prefixed plugins are added as --with-editable; non-mngr libs are not.
    assert "--with-editable libs/mngr_recursive" in install_cmd
    assert "--with-editable libs/mngr_pair" in install_cmd
    assert "imbue_common" not in install_cmd
    # Per-agent UV_TOOL_DIR / UV_TOOL_BIN_DIR are set in the env prefix.
    agent_state_dir = host_dir / "agents" / "agent-123"
    assert f"UV_TOOL_DIR={agent_state_dir / 'tools'}" in install_cmd
    assert f"UV_TOOL_BIN_DIR={agent_state_dir / 'bin'}" in install_cmd


def test_agent_skip_mode_does_nothing() -> None:
    """provision_mngr_for_agent should skip when install_mode is SKIP."""
    host = _make_mock_host()
    ctx = _make_mock_mngr_ctx(
        plugin_config=RecursivePluginConfig(install_mode=MngrInstallMode.SKIP),
    )
    agent = _make_mock_agent(mngr_ctx=ctx)

    provision_mngr_for_agent(agent=agent, host=host, mngr_ctx=ctx)

    host.execute_idempotent_command.assert_not_called()


def test_agent_errors_fatal_raises() -> None:
    """When is_errors_fatal=True, agent-level mngr install failures should raise."""
    host = _make_mock_host()
    host.execute_idempotent_command.return_value = _make_command_result(False, stderr="mkdir failed")
    ctx = _make_mock_mngr_ctx(
        plugin_config=RecursivePluginConfig(is_errors_fatal=True, install_mode=MngrInstallMode.PACKAGE),
    )
    agent = _make_mock_agent(mngr_ctx=ctx)

    with pytest.raises(MngrError, match="Failed to create directory"):
        provision_mngr_for_agent(agent=agent, host=host, mngr_ctx=ctx)


def test_agent_errors_non_fatal_warns(log_warnings: list[str]) -> None:
    """When is_errors_fatal=False, agent-level mngr install failures should warn, not raise."""
    host = _make_mock_host()
    host.execute_idempotent_command.return_value = _make_command_result(False, stderr="mkdir failed")
    ctx = _make_mock_mngr_ctx(
        plugin_config=RecursivePluginConfig(is_errors_fatal=False, install_mode=MngrInstallMode.PACKAGE),
    )
    agent = _make_mock_agent(mngr_ctx=ctx)

    # Should not raise.
    provision_mngr_for_agent(agent=agent, host=host, mngr_ctx=ctx)

    assert any("Failed to install mngr for agent" in m for m in log_warnings), log_warnings


def test_agent_creates_tool_and_bin_dirs() -> None:
    """provision_mngr_for_agent should create the tools/ and bin/ directories."""
    host_dir = Path("/tmp/mngr-test/host")
    host = _make_mock_host(host_dir=host_dir)
    host.execute_idempotent_command.return_value = _make_command_result(True)
    ctx = _make_mock_mngr_ctx(
        plugin_config=RecursivePluginConfig(install_mode=MngrInstallMode.PACKAGE),
    )
    agent = _make_mock_agent(mngr_ctx=ctx)

    with patch("imbue.mngr_recursive.provisioning._get_installed_mngr_packages") as mock_packages:
        mock_packages.return_value = [("imbue-mngr", "0.1.4")]
        provision_mngr_for_agent(agent=agent, host=host, mngr_ctx=ctx)

    agent_state_dir = host_dir / "agents" / "agent-123"
    mkdir_commands = [cmd for cmd in _issued_commands(host) if cmd.startswith("mkdir -p ")]
    assert mkdir_commands == [
        f"mkdir -p {agent_state_dir / 'tools'}",
        f"mkdir -p {agent_state_dir / 'bin'}",
    ]


# --- uv installation ---


def test_uv_installed_when_missing() -> None:
    """When uv is not available, it should be installed via curl."""
    host = _make_mock_host(is_local=False)
    host.execute_idempotent_command.side_effect = [
        # echo $HOME
        _make_command_result(True, stdout="/home/testuser\n"),
        # command -v uv (uv NOT available)
        _make_command_result(False),
        # curl install uv
        _make_command_result(True),
        # source env
        _make_command_result(True),
    ]
    ctx = _make_mock_mngr_ctx(
        plugin_config=RecursivePluginConfig(install_mode=MngrInstallMode.PACKAGE),
    )
    ctx.pm.hook.get_files_for_deploy.return_value = []

    provision_mngr_on_host(host=host, mngr_ctx=ctx)

    # uv should have been installed via the astral.sh installer exactly once.
    curl_commands = [c for c in _issued_commands(host) if "astral.sh/uv" in c]
    assert curl_commands == ["curl -LsSf https://astral.sh/uv/install.sh | sh"]


# --- Plugin hook tests ---


def test_on_host_created_calls_provision() -> None:
    """on_host_created should delegate to provision_mngr_on_host.

    Asserting the concrete observable effect of that delegation for a local host
    (exactly one ``command -v uv`` check, and no remote-only ``echo $HOME``)
    distinguishes "called provision_mngr_on_host" from "issued some command",
    mirroring test_local_host_ensures_uv_available.
    """
    host = _make_mock_host(is_local=True)
    ctx = _make_mock_mngr_ctx()

    on_host_created(host=host, mngr_ctx=ctx)

    commands = _issued_commands(host)
    assert [c for c in commands if "command -v uv" in c] == ["command -v uv"]
    assert not [c for c in commands if "echo $HOME" in c]


# --- Data types tests ---


def test_recursive_plugin_config_merge_with_explicit_fields_win() -> None:
    """Fields the override layer explicitly set win over the base layer."""
    base = RecursivePluginConfig(is_errors_fatal=False, install_mode=MngrInstallMode.AUTO)
    override = RecursivePluginConfig(is_errors_fatal=True, install_mode=MngrInstallMode.PACKAGE)
    merged = base.merge_with(override)
    assert merged.is_errors_fatal is True
    assert merged.install_mode == MngrInstallMode.PACKAGE


def test_recursive_plugin_config_merge_with_unset_fields_inherit_base() -> None:
    """Fields the override layer never set are inherited from the base layer.

    This is the regression test for the merge bug: an override layer that touches
    only install_mode must NOT reset is_errors_fatal back to its default. The
    merge keys off model_fields_set, not off the field values, so the base
    layer's explicit is_errors_fatal=True survives.
    """
    base = RecursivePluginConfig(is_errors_fatal=True, install_mode=MngrInstallMode.AUTO)
    override = RecursivePluginConfig(install_mode=MngrInstallMode.PACKAGE)
    assert "is_errors_fatal" not in override.model_fields_set

    merged = base.merge_with(override)
    assert merged.is_errors_fatal is True
    assert merged.install_mode == MngrInstallMode.PACKAGE
    assert isinstance(merged, RecursivePluginConfig)


def test_recursive_plugin_config_merge_with_empty_override_keeps_base() -> None:
    """An override layer that set nothing leaves the base config unchanged."""
    base = RecursivePluginConfig(is_errors_fatal=True, install_mode=MngrInstallMode.EDITABLE)
    merged = base.merge_with(RecursivePluginConfig())
    assert merged.is_errors_fatal is True
    assert merged.install_mode == MngrInstallMode.EDITABLE


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
    host.execute_idempotent_command.side_effect = [
        _make_command_result(False),
        _make_command_result(False, stderr="curl failed"),
    ]
    with pytest.raises(MngrError, match="Failed to install uv"):
        _ensure_uv_available(host)


def test_ensure_uv_raises_when_not_on_path_after_install() -> None:
    """_ensure_uv_available should raise when uv is installed but not findable."""
    host = _make_mock_host()
    host.execute_idempotent_command.side_effect = [
        _make_command_result(False),
        _make_command_result(True),
        _make_command_result(False),
    ]
    with pytest.raises(MngrError, match="cannot be found on PATH"):
        _ensure_uv_available(host)


# --- _install_mngr_package_mode tests ---


def test_install_package_mode_raises_when_no_mngr_package() -> None:
    """_install_mngr_package_mode should raise when mngr is not in packages list."""
    host = _make_mock_host()
    with pytest.raises(MngrError, match="mngr package not found"):
        _install_mngr_package_mode(host, [("imbue-mngr-pair", "0.1.0")], Path("/tools"), Path("/bin"))


def test_install_package_mode_retries_with_force_reinstall() -> None:
    """_install_mngr_package_mode should retry with --force-reinstall on failure."""
    host = _make_mock_host()
    host.execute_idempotent_command.side_effect = [
        _make_command_result(False, stderr="already installed"),
        _make_command_result(True),
    ]
    _install_mngr_package_mode(host, [("imbue-mngr", "0.1.4")], Path("/tools"), Path("/bin"))
    commands = _issued_commands(host)
    # First attempt, then a retry that appends --force-reinstall to the same command.
    base_cmd = _build_package_install_command(("imbue-mngr", "0.1.4"), [], Path("/tools"), Path("/bin"))
    assert commands == [base_cmd, base_cmd + " --force-reinstall"]


# --- provision_mngr_for_agent when no packages found ---


def test_agent_package_mode_warns_when_no_packages(log_warnings: list[str]) -> None:
    """provision_mngr_for_agent should warn and skip installation when no packages are found."""
    host = _make_mock_host()
    host.execute_idempotent_command.return_value = _make_command_result(True)
    ctx = _make_mock_mngr_ctx(
        plugin_config=RecursivePluginConfig(install_mode=MngrInstallMode.PACKAGE),
    )
    agent = _make_mock_agent(mngr_ctx=ctx)

    with patch("imbue.mngr_recursive.provisioning._get_installed_mngr_packages") as mock_packages:
        mock_packages.return_value = []
        provision_mngr_for_agent(agent=agent, host=host, mngr_ctx=ctx)

    # It should warn...
    assert any("No mngr packages found locally" in m for m in log_warnings), log_warnings
    # ...and skip installation entirely: only the tool/bin mkdir -p calls, no uv install.
    commands = _issued_commands(host)
    assert all(cmd.startswith("mkdir -p ") for cmd in commands), commands
    assert not [c for c in commands if "uv tool install" in c]


# --- _upload_deploy_files rsync failure ---


def test_upload_deploy_files_propagates_rsync_failure() -> None:
    """_upload_deploy_files should propagate an rsync (copy_local_directory) failure."""
    host = _make_mock_host(is_local=False)
    host.copy_local_directory.side_effect = MngrError("rsync failed: connection reset")
    deploy_files: dict[Path, Path | str] = {
        Path("~/.mngr/config.toml"): "content",
    }
    with pytest.raises(MngrError, match="rsync failed"):
        _upload_deploy_files(host, deploy_files, "/home/testuser")


def test_install_package_mode_raises_when_force_reinstall_also_fails() -> None:
    """_install_mngr_package_mode should raise when both install and force-reinstall fail."""
    host = _make_mock_host()
    host.execute_idempotent_command.side_effect = [
        _make_command_result(False, stderr="install failed"),
        _make_command_result(False, stderr="reinstall also failed"),
    ]
    with pytest.raises(MngrError, match="Failed to install mngr"):
        _install_mngr_package_mode(host, [("imbue-mngr", "0.1.4")], Path("/tools"), Path("/bin"))


def test_agent_editable_mode_dispatches_and_retries_force_reinstall(tmp_path: Path) -> None:
    """Editable local mode should dispatch to install and retry with --force-reinstall on failure."""
    repo_root = tmp_path / "monorepo"
    libs_dir = repo_root / "libs"
    (libs_dir / "mngr").mkdir(parents=True)

    host_dir = tmp_path / "host"
    host_dir.mkdir()
    host = _make_mock_host(is_local=True, host_dir=host_dir)

    def execute_side_effect(cmd: str, **kwargs: object) -> CommandResult:
        if "uv tool install" in cmd and "--force-reinstall" not in cmd:
            return _make_command_result(False, stderr="already installed")
        return _make_command_result(True)

    host.execute_idempotent_command.side_effect = execute_side_effect
    ctx = _make_mock_mngr_ctx(
        plugin_config=RecursivePluginConfig(install_mode=MngrInstallMode.EDITABLE),
    )
    agent = _make_mock_agent(mngr_ctx=ctx)

    with patch("imbue.mngr_recursive.provisioning._get_mngr_repo_root") as mock_root:
        mock_root.return_value = repo_root
        provision_mngr_for_agent(agent=agent, host=host, mngr_ctx=ctx)

    # First editable install fails, so it retries with the same command plus --force-reinstall.
    install_commands = [c for c in _issued_commands(host) if "uv tool install" in c]
    assert len(install_commands) == 2
    assert not install_commands[0].endswith("--force-reinstall")
    assert install_commands[1] == install_commands[0] + " --force-reinstall"


def test_provision_on_host_handles_deploy_file_errors(log_warnings: list[str]) -> None:
    """provision_mngr_on_host should warn (not raise) on collect_deploy_files errors when not fatal."""
    host = _make_mock_host(is_local=False)
    ctx = _make_mock_mngr_ctx(
        plugin_config=RecursivePluginConfig(is_errors_fatal=False, install_mode=MngrInstallMode.PACKAGE),
    )

    with patch("imbue.mngr_recursive.provisioning.collect_deploy_files") as mock_collect:
        mock_collect.side_effect = MngrError("absolute path not allowed")
        provision_mngr_on_host(host=host, mngr_ctx=ctx)

    assert any("Failed to provision mngr prerequisites on host" in m for m in log_warnings), log_warnings


# --- _get_mngr_repo_root tests ---


def test_get_mngr_repo_root_returns_repo_root() -> None:
    """_get_mngr_repo_root should return the git repo root of the mngr monorepo.

    This is an integration check that requires mngr to be installed editable from
    a git checkout (true in the dev/test environment). The parsing logic it
    depends on is unit-tested separately via _resolve_editable_source_dir, so a
    parsing regression is caught even where this environment assumption does not
    hold.
    """
    result = _get_mngr_repo_root()
    assert result.is_dir()
    assert (result / ".git").exists()


def test_resolve_editable_source_dir_parses_file_url() -> None:
    """A file:// direct_url should resolve to the local source directory."""
    result = _resolve_editable_source_dir('{"url": "file:///home/dev/mngr", "dir_info": {"editable": true}}')
    assert result == Path("/home/dev/mngr")


def test_resolve_editable_source_dir_raises_when_not_editable() -> None:
    """A missing direct_url.json (None) means the package is not editable."""
    with pytest.raises(MngrError, match="not installed in editable mode"):
        _resolve_editable_source_dir(None)


def test_resolve_editable_source_dir_raises_on_malformed_json() -> None:
    """Malformed direct_url.json should raise a clear parse error."""
    with pytest.raises(MngrError, match="Failed to parse direct_url.json"):
        _resolve_editable_source_dir("this is not json {{{")


def test_resolve_editable_source_dir_raises_on_non_file_url() -> None:
    """A non-file:// URL (e.g. a wheel) is not an editable source tree."""
    with pytest.raises(MngrError, match="Unexpected direct_url format"):
        _resolve_editable_source_dir('{"url": "https://pypi.org/imbue-mngr.whl"}')


# --- command-builder tests ---


def test_build_package_install_command() -> None:
    """The package-mode install command pins the env prefix, install, and --with plugins."""
    cmd = _build_package_install_command(
        ("imbue-mngr", "0.1.4"),
        [("imbue-mngr-pair", "0.1.0")],
        Path("/state/agent-123/tools"),
        Path("/state/agent-123/bin"),
    )
    assert cmd == snapshot(
        'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH" && export UV_TOOL_DIR=/state/agent-123/tools'
        " && export UV_TOOL_BIN_DIR=/state/agent-123/bin && uv tool install imbue-mngr==0.1.4"
        " --with imbue-mngr-pair==0.1.0"
    )


def test_build_editable_install_command_includes_only_mngr_plugins() -> None:
    """The editable install command installs the base lib and only mngr_-prefixed plugins."""
    cmd = _build_editable_install_command(
        "/repo",
        ["mngr", "mngr_recursive", "mngr_pair", "imbue_common"],
        "export UV_TOOL_DIR=/state/tools && export UV_TOOL_BIN_DIR=/state/bin && ",
    )
    assert cmd == snapshot(
        'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH" && export UV_TOOL_DIR=/state/tools'
        " && export UV_TOOL_BIN_DIR=/state/bin && cd /repo && uv tool install -e libs/mngr"
        " --with-editable libs/mngr_recursive --with-editable libs/mngr_pair"
    )


def test_editable_local_raises_when_force_reinstall_also_fails(tmp_path: Path) -> None:
    """Editable local mode should raise when both install and force-reinstall fail."""
    repo_root = tmp_path / "monorepo"
    libs_dir = repo_root / "libs"
    (libs_dir / "mngr").mkdir(parents=True)

    host_dir = tmp_path / "host"
    host_dir.mkdir()
    host = _make_mock_host(is_local=True, host_dir=host_dir)
    host.execute_idempotent_command.side_effect = [
        _make_command_result(True),
        _make_command_result(True),
        _make_command_result(False, stderr="install failed"),
        _make_command_result(False, stderr="reinstall also failed"),
    ]

    ctx = _make_mock_mngr_ctx(
        plugin_config=RecursivePluginConfig(is_errors_fatal=True, install_mode=MngrInstallMode.EDITABLE),
    )
    agent = _make_mock_agent(mngr_ctx=ctx)

    with patch("imbue.mngr_recursive.provisioning._get_mngr_repo_root") as mock_root:
        mock_root.return_value = repo_root
        with pytest.raises(MngrError, match="Failed to install mngr in editable mode"):
            provision_mngr_for_agent(agent=agent, host=host, mngr_ctx=ctx)
