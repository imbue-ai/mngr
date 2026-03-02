"""Unit tests for deploy.py and verification.py pure functions."""

import json
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path

import pluggy
import pytest
from dotenv import dotenv_values

from imbue.mng import hookimpl
from imbue.mng.config.data_types import MngContext
from imbue.mng.providers.deploy_utils import MngInstallMode
from imbue.mng_schedule.data_types import ScheduleTriggerDefinition
from imbue.mng_schedule.data_types import ScheduledMngCommand
from imbue.mng_schedule.errors import ScheduleDeployError
from imbue.mng_schedule.implementations.modal.deploy import _build_full_commandline
from imbue.mng_schedule.implementations.modal.deploy import _build_package_mode_dockerfile
from imbue.mng_schedule.implementations.modal.deploy import _collect_deploy_files
from imbue.mng_schedule.implementations.modal.deploy import _resolve_timezone_from_paths
from imbue.mng_schedule.implementations.modal.deploy import _stage_consolidated_env
from imbue.mng_schedule.implementations.modal.deploy import build_deploy_config
from imbue.mng_schedule.implementations.modal.deploy import detect_mng_install_mode
from imbue.mng_schedule.implementations.modal.deploy import get_mng_dockerfile_path
from imbue.mng_schedule.implementations.modal.deploy import get_modal_app_name
from imbue.mng_schedule.implementations.modal.deploy import package_directory_as_tarball
from imbue.mng_schedule.implementations.modal.deploy import parse_upload_spec
from imbue.mng_schedule.implementations.modal.deploy import resolve_commit_hash_for_deploy
from imbue.mng_schedule.implementations.modal.deploy import stage_deploy_files
from imbue.mng_schedule.implementations.modal.deploy import try_get_repo_root
from imbue.mng_schedule.implementations.modal.verification import build_modal_run_command


def test_get_modal_app_name() -> None:
    assert get_modal_app_name("my-trigger") == "mng-schedule-my-trigger"
    assert get_modal_app_name("nightly") == "mng-schedule-nightly"


def test_build_modal_run_command() -> None:
    cmd = build_modal_run_command(
        cron_runner_path=Path("/deploy/cron_runner.py"),
        modal_env_name="test-env",
    )
    assert cmd == ["uv", "run", "modal", "run", "--env", "test-env", "/deploy/cron_runner.py"]


def test_build_deploy_config_returns_all_keys() -> None:
    trigger = ScheduleTriggerDefinition(
        name="test",
        command=ScheduledMngCommand.CREATE,
        args="--message hello",
        schedule_cron="0 3 * * *",
        provider="modal",
        is_enabled=True,
    )
    result = build_deploy_config(
        app_name="test-app",
        trigger=trigger,
        cron_schedule="0 3 * * *",
        cron_timezone="America/Los_Angeles",
        target_repo_path="/code/project",
        auto_merge_branch="main",
    )
    assert result["app_name"] == "test-app"
    assert result["cron_schedule"] == "0 3 * * *"
    assert result["cron_timezone"] == "America/Los_Angeles"
    assert result["trigger"]["name"] == "test"
    assert result["trigger"]["command"] == "CREATE"
    assert result["trigger"]["args"] == "--message hello"
    assert result["target_repo_path"] == "/code/project"
    assert result["auto_merge_branch"] == "main"


def test_resolve_timezone_reads_etc_timezone(tmp_path: Path) -> None:
    etc_timezone = tmp_path / "timezone"
    etc_timezone.write_text("America/New_York\n")
    etc_localtime = tmp_path / "localtime"

    result = _resolve_timezone_from_paths(etc_timezone, etc_localtime)
    assert result == "America/New_York"


def test_resolve_timezone_falls_back_to_localtime_symlink(tmp_path: Path) -> None:
    etc_timezone = tmp_path / "timezone"
    etc_localtime = tmp_path / "localtime"
    # Create a symlink that looks like a zoneinfo path
    zoneinfo_dir = tmp_path / "usr" / "share" / "zoneinfo" / "Europe" / "London"
    zoneinfo_dir.parent.mkdir(parents=True)
    zoneinfo_dir.touch()
    etc_localtime.symlink_to(zoneinfo_dir)

    result = _resolve_timezone_from_paths(etc_timezone, etc_localtime)
    assert result == "Europe/London"


def test_resolve_timezone_returns_utc_when_nothing_found(tmp_path: Path) -> None:
    etc_timezone = tmp_path / "timezone"
    etc_localtime = tmp_path / "localtime"

    result = _resolve_timezone_from_paths(etc_timezone, etc_localtime)
    assert result == "UTC"


def test_resolve_timezone_skips_empty_etc_timezone(tmp_path: Path) -> None:
    etc_timezone = tmp_path / "timezone"
    etc_timezone.write_text("  \n")
    etc_localtime = tmp_path / "localtime"

    result = _resolve_timezone_from_paths(etc_timezone, etc_localtime)
    assert result == "UTC"


def test_build_full_commandline_joins_argv_with_spaces() -> None:
    argv = ["uv", "run", "mng", "schedule", "add", "--command", "create"]
    result = _build_full_commandline(argv)
    assert result == "uv run mng schedule add --command create"


def test_build_full_commandline_handles_empty_argv() -> None:
    result = _build_full_commandline([])
    assert result == ""


def test_build_full_commandline_handles_single_element() -> None:
    result = _build_full_commandline(["mng"])
    assert result == "mng"


def test_build_full_commandline_shell_escapes_spaces_in_arguments() -> None:
    argv = ["mng", "schedule", "add", "--args", "hello world"]
    result = _build_full_commandline(argv)
    assert result == "mng schedule add --args 'hello world'"


# =============================================================================
# Shared test helpers
# =============================================================================


# =============================================================================
# stage_deploy_files Tests
# =============================================================================


@pytest.fixture()
def run_staging(
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
    temp_mng_ctx: MngContext,
    set_test_api_key: None,
) -> Callable[[Path | None], Path]:
    """Return a callable that runs stage_deploy_files and returns the staging dir.

    Accepts an optional repo_root (creates an empty one if not provided).
    The caller should create any files they want staged BEFORE calling this.
    """

    def _run(repo_root: Path | None = None) -> Path:
        if repo_root is None:
            repo_root = tmp_path / "repo"
            repo_root.mkdir(exist_ok=True)
        staging_dir = tmp_path / "staging"
        mng_ctx = temp_mng_ctx
        stage_deploy_files(staging_dir, mng_ctx, repo_root)
        return staging_dir

    return _run


def test_stage_deploy_files_creates_home_directory_structure(
    run_staging: Callable[[Path | None], Path],
) -> None:
    """stage_deploy_files should stage files into home/ mirroring their destination paths."""
    staging_dir = run_staging(None)

    # Files should be staged under home/ with their natural paths,
    # and claude.json should have dialog-suppression fields injected
    staged_file = staging_dir / "home" / ".claude.json"
    assert staged_file.exists()
    staged_data = json.loads(staged_file.read_text())
    assert staged_data["bypassPermissionsModeAccepted"] is True


def test_stage_deploy_files_stages_multiple_home_files(
    run_staging: Callable[[Path | None], Path],
) -> None:
    """stage_deploy_files stages multiple home files preserving directory structure."""
    mng_dir = Path.home() / ".mng"
    mng_dir.mkdir(parents=True, exist_ok=True)
    mng_config = mng_dir / "config.toml"
    mng_config.write_text("[test]\nroundtrip = true\n")

    staging_dir = run_staging(None)

    # claude.json should be staged with generated defaults (dialog-suppression fields)
    staged_claude = staging_dir / "home" / ".claude.json"
    assert staged_claude.exists()
    staged_claude_data = json.loads(staged_claude.read_text())
    assert staged_claude_data["bypassPermissionsModeAccepted"] is True
    # mng config should be staged preserving directory structure
    staged_config = staging_dir / "home" / ".mng" / "config.toml"
    assert staged_config.exists()
    assert staged_config.read_text() == "[test]\nroundtrip = true\n"


def test_stage_deploy_files_creates_secrets_dir(
    run_staging: Callable[[Path | None], Path],
) -> None:
    """stage_deploy_files should always create the secrets/ directory."""
    staging_dir = run_staging(None)

    secrets_dir = staging_dir / "secrets"
    assert secrets_dir.exists()
    assert secrets_dir.is_dir()


def test_stage_deploy_files_creates_subdirs_with_claude_defaults(
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
    temp_mng_ctx: MngContext,
    set_test_api_key: None,
) -> None:
    """stage_deploy_files should always stage generated claude defaults in home/."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    staging_dir = tmp_path / "staging"
    mng_ctx = temp_mng_ctx
    stage_deploy_files(staging_dir, mng_ctx, repo_root)

    home_dir = staging_dir / "home"
    assert home_dir.exists()
    # Claude plugin always ships generated defaults
    assert (home_dir / ".claude" / "settings.json").exists()
    assert (home_dir / ".claude.json").exists()

    project_dir = staging_dir / "project"
    assert project_dir.exists()
    assert not any(project_dir.iterdir())


def test_stage_deploy_files_stages_project_files(
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
    temp_mng_ctx: MngContext,
    set_test_api_key: None,
) -> None:
    """stage_deploy_files should stage relative paths under project/."""

    class _ProjectFilePlugin:
        @staticmethod
        @hookimpl
        def get_files_for_deploy(mng_ctx: MngContext) -> dict[Path, Path | str]:
            return {Path("config/settings.toml"): "[settings]\nkey = 1\n"}

    plugin_manager.register(_ProjectFilePlugin())
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    staging_dir = tmp_path / "staging"
    mng_ctx = temp_mng_ctx
    stage_deploy_files(staging_dir, mng_ctx, repo_root)

    staged_file = staging_dir / "project" / "config" / "settings.toml"
    assert staged_file.exists()
    assert staged_file.read_text() == "[settings]\nkey = 1\n"


# =============================================================================
# _collect_deploy_files validation Tests
# =============================================================================


def _make_mng_ctx_with_hook_returning(
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
    files: dict[Path, Path | str],
    temp_mng_ctx: MngContext,
) -> MngContext:
    """Create a MngContext with an extra plugin that returns the given files."""

    class _TestPlugin:
        @staticmethod
        @hookimpl
        def get_files_for_deploy(mng_ctx: MngContext) -> dict[Path, Path | str]:
            return files

    plugin_manager.register(_TestPlugin())
    return temp_mng_ctx


def test_collect_deploy_files_accepts_relative_path(
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
    temp_mng_ctx: MngContext,
) -> None:
    """_collect_deploy_files should accept relative paths as project files."""
    mng_ctx = _make_mng_ctx_with_hook_returning(
        plugin_manager,
        tmp_path,
        {Path("relative/config.toml"): "content"},
        temp_mng_ctx,
    )

    result = _collect_deploy_files(mng_ctx, repo_root=tmp_path)
    assert Path("relative/config.toml") in result


def test_collect_deploy_files_rejects_absolute_path(
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
    temp_mng_ctx: MngContext,
) -> None:
    """_collect_deploy_files should raise ScheduleDeployError for absolute paths."""
    mng_ctx = _make_mng_ctx_with_hook_returning(
        plugin_manager,
        tmp_path,
        {Path("/etc/config.toml"): "content"},
        temp_mng_ctx,
    )

    with pytest.raises(ScheduleDeployError, match="must be relative or start with '~'"):
        _collect_deploy_files(mng_ctx, repo_root=tmp_path)


def test_collect_deploy_files_resolves_collision(
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
    temp_mng_ctx: MngContext,
) -> None:
    """_collect_deploy_files should resolve collisions when two plugins register the same path."""

    class _PluginA:
        @staticmethod
        @hookimpl
        def get_files_for_deploy(mng_ctx: MngContext) -> dict[Path, Path | str]:
            return {Path("~/.config/test.toml"): "content-a"}

    class _PluginB:
        @staticmethod
        @hookimpl
        def get_files_for_deploy(mng_ctx: MngContext) -> dict[Path, Path | str]:
            return {Path("~/.config/test.toml"): "content-b"}

    plugin_manager.register(_PluginA())
    plugin_manager.register(_PluginB())

    mng_ctx = temp_mng_ctx
    result = _collect_deploy_files(mng_ctx, repo_root=tmp_path)

    # Should still succeed, with one entry (last one wins)
    assert Path("~/.config/test.toml") in result


# =============================================================================
# parse_upload_spec Tests
# =============================================================================


def test_parse_upload_spec_valid_file(tmp_path: Path) -> None:
    """parse_upload_spec should parse a valid SOURCE:DEST spec with an existing file."""
    source = tmp_path / "myfile.txt"
    source.write_text("content")

    result = parse_upload_spec(f"{source}:~/.config/myfile.txt")
    assert result == (source, "~/.config/myfile.txt")


def test_parse_upload_spec_valid_directory(tmp_path: Path) -> None:
    """parse_upload_spec should parse a valid SOURCE:DEST spec with an existing directory."""
    source_dir = tmp_path / "mydir"
    source_dir.mkdir()

    result = parse_upload_spec(f"{source_dir}:config/")
    assert result == (source_dir, "config/")


def test_parse_upload_spec_rejects_missing_colon() -> None:
    """parse_upload_spec should reject specs without a colon."""
    with pytest.raises(ValueError, match="SOURCE:DEST"):
        parse_upload_spec("/some/path")


def test_parse_upload_spec_rejects_nonexistent_source() -> None:
    """parse_upload_spec should reject specs where the source does not exist."""
    with pytest.raises(ValueError, match="does not exist"):
        parse_upload_spec("/nonexistent/file:dest")


def test_parse_upload_spec_rejects_absolute_dest(tmp_path: Path) -> None:
    """parse_upload_spec should reject absolute destinations."""
    source = tmp_path / "exists.txt"
    source.write_text("content")

    with pytest.raises(ValueError, match="must be relative or start with '~'"):
        parse_upload_spec(f"{source}:/absolute/path")


# =============================================================================
# _stage_consolidated_env Tests
# =============================================================================


def test_stage_consolidated_env_includes_env_files(
    tmp_path: Path,
    temp_mng_ctx: MngContext,
    set_test_api_key: None,
) -> None:
    """_stage_consolidated_env should include vars from --env-file."""
    env_file = tmp_path / "custom.env"
    env_file.write_text("CUSTOM_VAR=hello\n")

    output_dir = tmp_path / "secrets"
    output_dir.mkdir()
    mng_ctx = temp_mng_ctx
    _stage_consolidated_env(output_dir, mng_ctx=mng_ctx, env_files=[env_file])

    result = (output_dir / ".env").read_text()
    assert 'CUSTOM_VAR="hello"' in result


def test_stage_consolidated_env_includes_pass_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    temp_mng_ctx: MngContext,
    set_test_api_key: None,
) -> None:
    """_stage_consolidated_env should include vars from --pass-env."""
    monkeypatch.setenv("MY_PASS_VAR", "passed_value")

    output_dir = tmp_path / "secrets"
    output_dir.mkdir()
    mng_ctx = temp_mng_ctx
    _stage_consolidated_env(output_dir, mng_ctx=mng_ctx, pass_env=["MY_PASS_VAR"])

    result = (output_dir / ".env").read_text()
    assert 'MY_PASS_VAR="passed_value"' in result


def test_stage_consolidated_env_merges_env_files_and_pass_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    temp_mng_ctx: MngContext,
    set_test_api_key: None,
) -> None:
    """_stage_consolidated_env should merge env files and pass-env vars."""
    env_file = tmp_path / "extra.env"
    env_file.write_text("FILE_KEY=from_file\n")

    monkeypatch.setenv("SHELL_KEY", "from_shell")

    output_dir = tmp_path / "secrets"
    output_dir.mkdir()
    mng_ctx = temp_mng_ctx
    _stage_consolidated_env(
        output_dir,
        mng_ctx=mng_ctx,
        pass_env=["SHELL_KEY"],
        env_files=[env_file],
    )

    result = (output_dir / ".env").read_text()
    assert 'FILE_KEY="from_file"' in result
    assert 'SHELL_KEY="from_shell"' in result


def test_stage_consolidated_env_skips_missing_pass_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bare_temp_mng_ctx: MngContext,
) -> None:
    """_stage_consolidated_env should skip pass-env vars not in the environment."""
    monkeypatch.delenv("NONEXISTENT_VAR", raising=False)

    output_dir = tmp_path / "secrets"
    output_dir.mkdir()
    mng_ctx = bare_temp_mng_ctx
    _stage_consolidated_env(output_dir, mng_ctx=mng_ctx, pass_env=["NONEXISTENT_VAR"])

    # No .env file should be created since no env vars were found and no plugins registered
    assert not (output_dir / ".env").exists()


def test_stage_consolidated_env_creates_no_file_when_empty(
    tmp_path: Path,
    bare_temp_mng_ctx: MngContext,
) -> None:
    """_stage_consolidated_env should not create .env when no env vars are available and no plugins contribute."""
    output_dir = tmp_path / "secrets"
    output_dir.mkdir()
    mng_ctx = bare_temp_mng_ctx
    _stage_consolidated_env(output_dir, mng_ctx=mng_ctx)

    assert not (output_dir / ".env").exists()


def test_stage_consolidated_env_preserves_values_with_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bare_temp_mng_ctx: MngContext,
) -> None:
    """_stage_consolidated_env should preserve values containing ' # ' (potential inline comments)."""
    monkeypatch.setenv("PASSWORD", "abc # def")

    output_dir = tmp_path / "secrets"
    output_dir.mkdir()
    mng_ctx = bare_temp_mng_ctx
    _stage_consolidated_env(output_dir, mng_ctx=mng_ctx, pass_env=["PASSWORD"])

    # Verify the written .env file can be parsed back correctly
    parsed = dotenv_values(output_dir / ".env")
    assert parsed["PASSWORD"] == "abc # def"


# =============================================================================
# modify_env_vars_for_deploy hook Tests
# =============================================================================


def test_modify_env_vars_for_deploy_plugin_adds_vars(
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
    temp_mng_ctx: MngContext,
    set_test_api_key: None,
) -> None:
    """modify_env_vars_for_deploy plugin can add env vars by mutating the dict."""

    class _EnvPlugin:
        @staticmethod
        @hookimpl
        def modify_env_vars_for_deploy(env_vars: dict[str, str]) -> None:
            env_vars["MY_PLUGIN_VAR"] = "plugin_value"

    plugin_manager.register(_EnvPlugin())
    mng_ctx = temp_mng_ctx
    env_vars: dict[str, str] = {}
    mng_ctx.pm.hook.modify_env_vars_for_deploy(mng_ctx=mng_ctx, env_vars=env_vars)
    assert env_vars["MY_PLUGIN_VAR"] == "plugin_value"


def test_modify_env_vars_for_deploy_plugin_removes_vars(
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
    temp_mng_ctx: MngContext,
    set_test_api_key: None,
) -> None:
    """modify_env_vars_for_deploy plugin can remove env vars via pop/del."""

    class _RemovalPlugin:
        @staticmethod
        @hookimpl
        def modify_env_vars_for_deploy(env_vars: dict[str, str]) -> None:
            env_vars.pop("REMOVE_ME", None)

    plugin_manager.register(_RemovalPlugin())
    mng_ctx = temp_mng_ctx
    env_vars = {"REMOVE_ME": "old_value", "KEEP_ME": "kept"}
    mng_ctx.pm.hook.modify_env_vars_for_deploy(mng_ctx=mng_ctx, env_vars=env_vars)
    assert "REMOVE_ME" not in env_vars
    assert env_vars["KEEP_ME"] == "kept"


def test_modify_env_vars_for_deploy_plugins_see_each_others_changes(
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
    temp_mng_ctx: MngContext,
    set_test_api_key: None,
) -> None:
    """Plugins called later see mutations made by earlier plugins.

    Uses tryfirst to ensure _PluginA runs before _PluginB, demonstrating
    that plugins can control ordering via pluggy's tryfirst/trylast.
    """

    class _PluginA:
        @staticmethod
        @hookimpl(tryfirst=True)
        def modify_env_vars_for_deploy(env_vars: dict[str, str]) -> None:
            env_vars["FROM_A"] = "value_a"

    class _PluginB:
        @staticmethod
        @hookimpl
        def modify_env_vars_for_deploy(env_vars: dict[str, str]) -> None:
            # B runs after A and can see A's addition
            if "FROM_A" in env_vars:
                env_vars["SAW_A"] = "true"

    plugin_manager.register(_PluginA())
    plugin_manager.register(_PluginB())
    mng_ctx = temp_mng_ctx
    env_vars: dict[str, str] = {}
    mng_ctx.pm.hook.modify_env_vars_for_deploy(mng_ctx=mng_ctx, env_vars=env_vars)
    assert env_vars["FROM_A"] == "value_a"
    assert env_vars["SAW_A"] == "true"


# =============================================================================
# _stage_consolidated_env with plugin env vars Tests
# =============================================================================


def test_stage_consolidated_env_includes_plugin_env_vars(
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
    temp_mng_ctx: MngContext,
    set_test_api_key: None,
) -> None:
    """_stage_consolidated_env should include env vars contributed by plugins."""

    class _EnvPlugin:
        @staticmethod
        @hookimpl
        def modify_env_vars_for_deploy(env_vars: dict[str, str]) -> None:
            env_vars["PLUGIN_VAR"] = "plugin_value"

    plugin_manager.register(_EnvPlugin())
    mng_ctx = temp_mng_ctx

    output_dir = tmp_path / "secrets"
    output_dir.mkdir()
    _stage_consolidated_env(output_dir, mng_ctx=mng_ctx)

    result = (output_dir / ".env").read_text()
    assert 'PLUGIN_VAR="plugin_value"' in result


def test_stage_consolidated_env_plugin_can_remove_env_vars(
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
    temp_mng_ctx: MngContext,
    set_test_api_key: None,
) -> None:
    """_stage_consolidated_env should remove env vars when plugin deletes keys."""

    class _RemovalPlugin:
        @staticmethod
        @hookimpl
        def modify_env_vars_for_deploy(env_vars: dict[str, str]) -> None:
            env_vars.pop("REMOVE_ME", None)

    plugin_manager.register(_RemovalPlugin())
    monkeypatch.setenv("REMOVE_ME", "should_be_removed")

    mng_ctx = temp_mng_ctx
    output_dir = tmp_path / "secrets"
    output_dir.mkdir()
    _stage_consolidated_env(output_dir, mng_ctx=mng_ctx, pass_env=["REMOVE_ME"])

    # The .env file may or may not exist depending on whether other plugins
    # contribute env vars. If it exists, REMOVE_ME must not be in it.
    env_file_path = output_dir / ".env"
    assert not env_file_path.exists() or "REMOVE_ME" not in env_file_path.read_text()


def test_stage_consolidated_env_plugin_overrides_have_highest_precedence(
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
    temp_mng_ctx: MngContext,
    set_test_api_key: None,
) -> None:
    """_stage_consolidated_env plugin env vars should override pass-env and env-file vars."""
    env_file = tmp_path / "base.env"
    env_file.write_text("MY_VAR=from_file\n")

    monkeypatch.setenv("MY_VAR", "from_env")

    class _OverridePlugin:
        @staticmethod
        @hookimpl
        def modify_env_vars_for_deploy(env_vars: dict[str, str]) -> None:
            env_vars["MY_VAR"] = "from_plugin"

    plugin_manager.register(_OverridePlugin())
    mng_ctx = temp_mng_ctx

    output_dir = tmp_path / "secrets"
    output_dir.mkdir()
    _stage_consolidated_env(
        output_dir,
        mng_ctx=mng_ctx,
        pass_env=["MY_VAR"],
        env_files=[env_file],
    )

    result = (output_dir / ".env").read_text()
    assert 'MY_VAR="from_plugin"' in result
    # Should only appear once (plugin value replaces env/file values)
    assert result.count("MY_VAR=") == 1


# =============================================================================
# stage_deploy_files with uploads Tests
# =============================================================================


def test_stage_deploy_files_stages_upload_file(
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
    temp_mng_ctx: MngContext,
    set_test_api_key: None,
) -> None:
    """stage_deploy_files should stage uploaded files to the correct destination."""
    source_file = tmp_path / "local_config.toml"
    source_file.write_text("[config]\nkey = true\n")

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    staging_dir = tmp_path / "staging"
    mng_ctx = temp_mng_ctx

    stage_deploy_files(
        staging_dir,
        mng_ctx,
        repo_root,
        uploads=[(source_file, "~/.config/myapp.toml")],
    )

    staged = staging_dir / "home" / ".config" / "myapp.toml"
    assert staged.exists()
    assert staged.read_text() == "[config]\nkey = true\n"


def test_stage_deploy_files_stages_upload_directory(
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
    temp_mng_ctx: MngContext,
    set_test_api_key: None,
) -> None:
    """stage_deploy_files should stage uploaded directories recursively."""
    source_dir = tmp_path / "my_configs"
    source_dir.mkdir()
    (source_dir / "a.txt").write_text("file-a")
    sub_dir = source_dir / "sub"
    sub_dir.mkdir()
    (sub_dir / "b.txt").write_text("file-b")

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    staging_dir = tmp_path / "staging"
    mng_ctx = temp_mng_ctx

    stage_deploy_files(
        staging_dir,
        mng_ctx,
        repo_root,
        uploads=[(source_dir, "configs")],
    )

    # Relative dest should go under project/
    assert (staging_dir / "project" / "configs" / "a.txt").read_text() == "file-a"
    assert (staging_dir / "project" / "configs" / "sub" / "b.txt").read_text() == "file-b"


def test_stage_deploy_files_with_pass_env(
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
    temp_mng_ctx: MngContext,
    set_test_api_key: None,
) -> None:
    """stage_deploy_files should include --pass-env vars in the consolidated env file."""
    monkeypatch.setenv("TEST_API_KEY", "sk-test-123")

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    staging_dir = tmp_path / "staging"
    mng_ctx = temp_mng_ctx

    stage_deploy_files(
        staging_dir,
        mng_ctx,
        repo_root,
        pass_env=["TEST_API_KEY"],
    )

    staged_env = staging_dir / "secrets" / ".env"
    assert staged_env.exists()
    assert 'TEST_API_KEY="sk-test-123"' in staged_env.read_text()


def test_stage_deploy_files_with_exclude_user_settings(
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
    temp_mng_ctx: MngContext,
    set_test_api_key: None,
) -> None:
    """stage_deploy_files with include_user_settings=False should skip mng home files but still include claude defaults."""
    # Create a home file that would normally be included
    mng_dir = Path.home() / ".mng"
    mng_dir.mkdir(parents=True, exist_ok=True)
    mng_config = mng_dir / "config.toml"
    mng_config.write_text("[test]\nvalue = 1\n")

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    staging_dir = tmp_path / "staging"
    mng_ctx = temp_mng_ctx

    stage_deploy_files(
        staging_dir,
        mng_ctx,
        repo_root,
        include_user_settings=False,
    )

    home_dir = staging_dir / "home"
    # mng config should NOT be included when user settings are excluded
    assert not (home_dir / ".mng" / "config.toml").exists()
    # But claude defaults are always shipped
    assert (home_dir / ".claude" / "settings.json").exists()
    assert (home_dir / ".claude.json").exists()


# =============================================================================
# mng install mode Tests
# =============================================================================


def test_detect_mng_install_mode_returns_valid_mode() -> None:
    """detect_mng_install_mode should return either PACKAGE or EDITABLE."""
    result = detect_mng_install_mode()
    assert result in (MngInstallMode.PACKAGE, MngInstallMode.EDITABLE)


def test_stage_deploy_files_does_not_stage_mng_source(
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
    temp_mng_ctx: MngContext,
    set_test_api_key: None,
) -> None:
    """stage_deploy_files should not stage mng source (it is handled separately)."""
    monkeypatch.chdir(tmp_path)

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    staging_dir = tmp_path / "staging"
    mng_ctx = temp_mng_ctx

    stage_deploy_files(
        staging_dir,
        mng_ctx,
        repo_root,
    )

    # mng source should NOT be in the staging directory (it is staged
    # separately in deploy_schedule for better Docker layer caching)
    assert not (staging_dir / "mng_schedule_src").exists()


# =============================================================================
# get_mng_dockerfile_path Tests
# =============================================================================


def test_get_mng_dockerfile_path_editable_returns_resources_dockerfile() -> None:
    """get_mng_dockerfile_path returns the mng resources Dockerfile for EDITABLE mode."""
    result = get_mng_dockerfile_path(MngInstallMode.EDITABLE)
    assert result.exists()
    assert result.name == "Dockerfile"
    assert "resources" in str(result)


def test_get_mng_dockerfile_path_package_returns_resources_dockerfile() -> None:
    """get_mng_dockerfile_path returns the mng resources Dockerfile for PACKAGE mode."""
    result = get_mng_dockerfile_path(MngInstallMode.PACKAGE)
    assert result.exists()
    assert result.name == "Dockerfile"


def test_get_mng_dockerfile_path_skip_returns_editable_dockerfile() -> None:
    """get_mng_dockerfile_path returns the editable Dockerfile for SKIP mode."""
    result = get_mng_dockerfile_path(MngInstallMode.SKIP)
    assert result.exists()
    assert "resources" in str(result)


def test_get_mng_dockerfile_path_auto_raises() -> None:
    """get_mng_dockerfile_path raises for AUTO mode."""
    with pytest.raises(ScheduleDeployError, match="AUTO mode must be resolved"):
        get_mng_dockerfile_path(MngInstallMode.AUTO)


# =============================================================================
# _build_package_mode_dockerfile Tests
# =============================================================================


def test_build_package_mode_dockerfile_replaces_monorepo_install() -> None:
    """_build_package_mode_dockerfile replaces monorepo-specific steps with pip install."""
    mng_dockerfile = (
        "FROM python:3.11-slim\n"
        "RUN apt-get update && apt-get install -y git\n"
        "COPY . /code/\n"
        "RUN mkdir -p /code/mng/ && tar -xzf /code/current.tar.gz -C /code/mng/\n"
        "WORKDIR /code/mng/\n"
        "RUN uv sync --all-packages\n"
        "RUN uv tool install -e /code/mng/libs/mng\n"
        'CMD ["sh", "-c", "tail -f /dev/null"]\n'
    )
    result = _build_package_mode_dockerfile(mng_dockerfile)

    # Should contain the pip install replacement
    assert "uv pip install --system mng mng-schedule" in result
    # Should preserve the FROM and system deps
    assert "FROM python:3.11-slim" in result
    assert "apt-get update" in result
    # Should NOT contain monorepo-specific steps
    assert "COPY . /code/" not in result
    assert "tar -xzf" not in result
    assert "uv sync" not in result
    assert "uv tool install -e" not in result
    # Should preserve CMD
    assert "CMD" in result


def test_build_package_mode_dockerfile_preserves_env_vars() -> None:
    """_build_package_mode_dockerfile preserves ENV instructions before the install section."""
    mng_dockerfile = (
        "FROM python:3.11-slim\n"
        "ENV UV_LINK_MODE=copy\n"
        "COPY . /code/\n"
        "RUN tar -xzf /code/current.tar.gz -C /code/mng/\n"
        "WORKDIR /code/mng/\n"
        "RUN uv tool install -e /code/mng/libs/mng\n"
    )
    result = _build_package_mode_dockerfile(mng_dockerfile)
    assert "ENV UV_LINK_MODE=copy" in result


def test_build_package_mode_dockerfile_raises_on_missing_sentinel() -> None:
    """_build_package_mode_dockerfile raises if the install section end sentinel is missing."""
    mng_dockerfile = (
        "FROM python:3.11-slim\n"
        "COPY . /code/\n"
        "RUN tar -xzf /code/current.tar.gz -C /code/mng/\n"
        "WORKDIR /code/mng/\n"
        "RUN uv sync --all-packages\n"
        # Missing 'RUN uv tool install' sentinel
        'CMD ["sh", "-c", "tail -f /dev/null"]\n'
    )
    with pytest.raises(ScheduleDeployError, match="could not find the end of the monorepo install section"):
        _build_package_mode_dockerfile(mng_dockerfile)


def test_build_package_mode_dockerfile_works_with_real_dockerfile() -> None:
    """_build_package_mode_dockerfile produces valid output from the actual mng Dockerfile."""
    dockerfile_path = get_mng_dockerfile_path(MngInstallMode.EDITABLE)
    mng_dockerfile_content = dockerfile_path.read_text()
    result = _build_package_mode_dockerfile(mng_dockerfile_content)

    # Must contain pip install replacement
    assert "uv pip install --system mng mng-schedule" in result
    # Must NOT contain monorepo-specific steps
    assert "COPY . /code/" not in result
    assert "uv sync" not in result
    assert "uv tool install -e" not in result
    # Must preserve the FROM instruction
    assert "FROM" in result
    # Must preserve system deps
    assert "apt-get" in result


# =============================================================================
# resolve_commit_hash_for_deploy Tests
# =============================================================================


def test_resolve_commit_hash_reads_cached_file(tmp_path: Path) -> None:
    """resolve_commit_hash_for_deploy returns the cached hash when the file exists."""
    commit_hash_file = tmp_path / "commit_hash"
    commit_hash_file.write_text("abc123def456")

    result = resolve_commit_hash_for_deploy(commit_hash_file, repo_root=tmp_path)
    assert result == "abc123def456"


def test_resolve_commit_hash_ignores_empty_cached_file(tmp_path: Path) -> None:
    """resolve_commit_hash_for_deploy ignores an empty cached file and resolves fresh."""
    commit_hash_file = tmp_path / "commit_hash"
    commit_hash_file.write_text("   \n")

    # Will fail because tmp_path is not a git repo, proving it tried to resolve fresh
    with pytest.raises(ScheduleDeployError):
        resolve_commit_hash_for_deploy(commit_hash_file, repo_root=tmp_path)


# =============================================================================
# try_get_repo_root Tests
# =============================================================================


def test_try_get_repo_root_returns_path_in_git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """try_get_repo_root returns a Path when inside a git repository."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
    monkeypatch.chdir(repo_dir)

    result = try_get_repo_root()
    assert result is not None
    assert result.is_dir()
    assert (result / ".git").exists()


def test_try_get_repo_root_returns_none_outside_git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """try_get_repo_root returns None when not inside a git repository."""
    monkeypatch.chdir(tmp_path)
    result = try_get_repo_root()
    assert result is None


# =============================================================================
# package_directory_as_tarball Tests
# =============================================================================


def test_package_directory_as_tarball_creates_tarball(tmp_path: Path) -> None:
    """package_directory_as_tarball creates a current.tar.gz from the source directory."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "file1.txt").write_text("hello")
    (source_dir / "file2.txt").write_text("world")
    sub_dir = source_dir / "subdir"
    sub_dir.mkdir()
    (sub_dir / "nested.txt").write_text("nested content")

    dest_dir = tmp_path / "dest"
    package_directory_as_tarball(source_dir, dest_dir)

    tarball = dest_dir / "current.tar.gz"
    assert tarball.exists()
    assert tarball.stat().st_size > 0


def test_package_directory_as_tarball_contents_extractable(tmp_path: Path) -> None:
    """package_directory_as_tarball produces a tarball that extracts correctly."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "hello.txt").write_text("hello world")
    sub_dir = source_dir / "sub"
    sub_dir.mkdir()
    (sub_dir / "nested.txt").write_text("nested")

    dest_dir = tmp_path / "dest"
    package_directory_as_tarball(source_dir, dest_dir)

    # Extract and verify contents
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    with tarfile.open(dest_dir / "current.tar.gz", "r:gz") as tf:
        tf.extractall(extract_dir)

    assert (extract_dir / "hello.txt").read_text() == "hello world"
    assert (extract_dir / "sub" / "nested.txt").read_text() == "nested"


def test_package_directory_as_tarball_creates_dest_dir(tmp_path: Path) -> None:
    """package_directory_as_tarball creates the destination directory if it does not exist."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "file.txt").write_text("content")

    dest_dir = tmp_path / "nonexistent" / "nested" / "dest"
    assert not dest_dir.exists()

    package_directory_as_tarball(source_dir, dest_dir)

    assert dest_dir.exists()
    assert (dest_dir / "current.tar.gz").exists()
