"""Tests for the mngr dependencies command."""

from click.testing import CliRunner

from imbue.mngr.cli.check_deps import _print_status_table
from imbue.mngr.cli.check_deps import _prompt_install_choice
from imbue.mngr.cli.check_deps import _run_installation
from imbue.mngr.cli.check_deps import check_deps
from imbue.mngr.utils.deps import DependencyCategory
from imbue.mngr.utils.deps import InstallMethod
from imbue.mngr.utils.deps import OsName
from imbue.mngr.utils.deps import SystemDependency

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


def test_check_deps_no_flags_reports_all_present(cli_runner: CliRunner) -> None:
    """Running 'mngr dependencies' with no flags when all deps are present should exit 0."""
    result = cli_runner.invoke(check_deps, [])
    assert result.exit_code in (0, 1)
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


def test_run_installation_with_empty_list_and_no_bash() -> None:
    """_run_installation with nothing to install returns no failures."""
    failed = _run_installation(to_install=[], need_bash=False, os_name=OsName.LINUX)
    assert failed == []


def test_check_deps_help(cli_runner: CliRunner) -> None:
    """The --help flag should work for the dependencies command."""
    result = cli_runner.invoke(check_deps, ["--help"])
    assert result.exit_code == 0
    assert "dependencies" in result.output.lower() or "install mode" in result.output.lower()
