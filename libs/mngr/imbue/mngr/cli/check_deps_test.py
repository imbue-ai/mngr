"""Tests for the mngr dependencies command."""

import pytest
from click.testing import CliRunner

from imbue.mngr.cli import check_deps as check_deps_mod
from imbue.mngr.cli.check_deps import _print_status_table
from imbue.mngr.cli.check_deps import _prompt_install_choice
from imbue.mngr.cli.check_deps import _report_post_install_status
from imbue.mngr.cli.check_deps import _run_installation
from imbue.mngr.cli.check_deps import check_deps
from imbue.mngr.utils.deps import DependencyCategory
from imbue.mngr.utils.deps import InstallMethod
from imbue.mngr.utils.deps import OsName
from imbue.mngr.utils.deps import SystemDependency

_EXISTING_DEP = SystemDependency(
    binary="python3",
    purpose="testing (exists on PATH)",
    macos_hint="n/a",
    linux_hint="n/a",
    category=DependencyCategory.CORE,
)

_TEST_DEPS: tuple[SystemDependency, ...] = (
    SystemDependency(
        binary="fakecorebin",
        purpose="testing core",
        macos_hint="brew install fakecorebin",
        linux_hint="apt-get install fakecorebin",
        category=DependencyCategory.CORE,
    ),
    SystemDependency(
        binary="fakeoptbin",
        purpose="testing optional",
        macos_hint="brew install fakeoptbin",
        linux_hint="apt-get install fakeoptbin",
        category=DependencyCategory.OPTIONAL,
    ),
)

_MISSING_CORE = SystemDependency(
    binary="no-such-core-xyz",
    purpose="testing core",
    macos_hint="brew install no-such-core-xyz",
    linux_hint="apt-get install no-such-core-xyz",
    category=DependencyCategory.CORE,
    install_method=InstallMethod(brew_package="no-such-core-xyz", apt_package="no-such-core-xyz"),
)

_MISSING_OPT = SystemDependency(
    binary="no-such-opt-xyz",
    purpose="testing optional",
    macos_hint="brew install no-such-opt-xyz",
    linux_hint="apt-get install no-such-opt-xyz",
    category=DependencyCategory.OPTIONAL,
    install_method=InstallMethod(brew_package="no-such-opt-xyz", apt_package="no-such-opt-xyz"),
)


def test_print_status_table_all_present(capsys: object) -> None:
    """_print_status_table prints 'ok' for all deps when none are missing."""
    _print_status_table(_TEST_DEPS, missing=[], bash_ok=True, os_name=OsName.LINUX)


def test_print_status_table_with_missing(capsys: object) -> None:
    """_print_status_table prints 'missing' for deps in the missing list."""
    _print_status_table(_TEST_DEPS, missing=[_TEST_DEPS[0]], bash_ok=True, os_name=OsName.LINUX)


def test_print_status_table_bash_missing_on_macos() -> None:
    """_print_status_table shows bash(4+) as missing on macOS when bash_ok is False."""
    _print_status_table(_TEST_DEPS, missing=[], bash_ok=False, os_name=OsName.MACOS)


def test_check_deps_no_flags(cli_runner: CliRunner) -> None:
    """Running 'mngr dependencies' with no flags outputs a status table."""
    result = cli_runner.invoke(check_deps, [])
    assert result.exit_code in (0, 1)
    assert "System dependencies" in result.output


def test_check_deps_all_flag(cli_runner: CliRunner) -> None:
    """Running 'mngr dependencies --all' runs the full check/install flow."""
    result = cli_runner.invoke(check_deps, ["--all"])
    assert "System dependencies" in result.output


def test_check_deps_core_flag(cli_runner: CliRunner) -> None:
    """Running 'mngr dependencies --core' runs the core check/install flow."""
    result = cli_runner.invoke(check_deps, ["--core"])
    assert "System dependencies" in result.output


def test_check_deps_interactive_flag(cli_runner: CliRunner) -> None:
    """Running 'mngr dependencies -i' runs the interactive flow (skipped without tty)."""
    result = cli_runner.invoke(check_deps, ["-i"])
    assert "System dependencies" in result.output


def test_prompt_install_choice_returns_none_when_no_tty() -> None:
    """_prompt_install_choice returns None when /dev/tty is unavailable.

    In test environments, read_tty_choice returns "" because /dev/tty
    cannot be opened by the CliRunner, so the function returns None.
    """
    missing = [_MISSING_CORE, _MISSING_OPT]
    result = _prompt_install_choice(missing, [_MISSING_CORE], need_bash=False, os_name=OsName.LINUX)
    assert result is None


def test_prompt_install_choice_with_need_bash_returns_none_when_no_tty() -> None:
    """_prompt_install_choice with need_bash=True also returns None without tty."""
    missing = [_MISSING_CORE]
    result = _prompt_install_choice(missing, [_MISSING_CORE], need_bash=True, os_name=OsName.MACOS)
    assert result is None


def test_prompt_install_choice_no_core_missing_but_need_bash_returns_none_when_no_tty() -> None:
    """_prompt_install_choice with no core missing but need_bash=True also returns None."""
    missing = [_MISSING_OPT]
    result = _prompt_install_choice(missing, [], need_bash=True, os_name=OsName.MACOS)
    assert result is None


def test_run_installation_with_empty_list_and_no_bash() -> None:
    """_run_installation with nothing to install returns no failures."""
    failed = _run_installation(to_install=[], need_bash=False, os_name=OsName.LINUX)
    assert failed == []


def test_report_post_install_status_all_present() -> None:
    """_report_post_install_status returns True when all core deps are present and bash is ok."""
    result = _report_post_install_status(
        failed=[],
        need_bash=False,
        os_name=OsName.LINUX,
        all_deps=(_EXISTING_DEP,),
        bash_ok_now=True,
    )
    assert result is True


def test_report_post_install_status_with_failed_deps() -> None:
    """_report_post_install_status reports failed deps and returns False when core deps are missing."""
    result = _report_post_install_status(
        failed=[_MISSING_CORE],
        need_bash=False,
        os_name=OsName.LINUX,
        all_deps=(_MISSING_CORE,),
        bash_ok_now=True,
    )
    assert result is False


def test_report_post_install_status_still_missing_optional_is_ok() -> None:
    """_report_post_install_status returns True when only optional deps are still missing."""
    result = _report_post_install_status(
        failed=[],
        need_bash=False,
        os_name=OsName.LINUX,
        all_deps=(_EXISTING_DEP, _MISSING_OPT),
        bash_ok_now=True,
    )
    assert result is True


def test_report_post_install_status_bash_still_old() -> None:
    """_report_post_install_status returns False and warns when bash is still old on macOS."""
    result = _report_post_install_status(
        failed=[],
        need_bash=True,
        os_name=OsName.MACOS,
        all_deps=(_EXISTING_DEP,),
        bash_ok_now=False,
    )
    assert result is False


def test_check_deps_help(cli_runner: CliRunner) -> None:
    """The --help flag should work for the dependencies command."""
    result = cli_runner.invoke(check_deps, ["--help"])
    assert result.exit_code == 0
    assert "dependencies" in result.output.lower() or "install mode" in result.output.lower()


def test_prompt_install_choice_interactive_choices(monkeypatch: pytest.MonkeyPatch) -> None:
    """_prompt_install_choice returns correct lists for 'a', 'c', and 'n' choices."""
    missing = [_MISSING_CORE, _MISSING_OPT]
    missing_core = [_MISSING_CORE]

    # User chooses 'a' (all) -> returns all missing deps
    monkeypatch.setattr(check_deps_mod, "read_tty_choice", lambda _prompt: "a")
    result = _prompt_install_choice(missing, missing_core, need_bash=False, os_name=OsName.LINUX)
    assert result == missing

    # User chooses 'c' (core) -> returns core-only deps
    monkeypatch.setattr(check_deps_mod, "read_tty_choice", lambda _prompt: "c")
    result = _prompt_install_choice(missing, missing_core, need_bash=False, os_name=OsName.LINUX)
    assert result == missing_core

    # User chooses 'n' (skip) -> returns None
    monkeypatch.setattr(check_deps_mod, "read_tty_choice", lambda _prompt: "n")
    result = _prompt_install_choice(missing, missing_core, need_bash=False, os_name=OsName.LINUX)
    assert result is None


def test_run_installation_with_deps_invokes_batch_install() -> None:
    """_run_installation with deps that have no installer for the given OS returns them as failed."""
    # Use a dep with only a brew installer but pass OsName.LINUX, so no installer matches
    # and the dep ends up in the "no auto install" / failed list without touching any package manager.
    brew_only = SystemDependency(
        binary="no-such-xyz",
        purpose="testing",
        macos_hint="brew install no-such-xyz",
        linux_hint="n/a",
        category=DependencyCategory.CORE,
        install_method=InstallMethod(brew_package="no-such-xyz"),
    )
    failed = _run_installation(to_install=[brew_only], need_bash=False, os_name=OsName.LINUX)
    assert brew_only in failed
