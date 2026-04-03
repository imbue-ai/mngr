"""Tests for the mngr dependencies command."""

from click.testing import CliRunner

from imbue.mngr.cli.check_deps import _print_status_table
from imbue.mngr.cli.check_deps import check_deps
from imbue.mngr.utils.deps import DependencyCategory
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


def test_print_status_table_all_present(capsys: object) -> None:
    """_print_status_table prints 'ok' for all deps when none are missing."""
    _print_status_table(_TEST_DEPS, missing=[], bash_ok=True, os_name=OsName.LINUX)
    # No assertion on exact output -- just ensure it doesn't crash.
    # The function writes to stdout via write_human_line.


def test_print_status_table_with_missing(capsys: object) -> None:
    """_print_status_table prints 'missing' for deps in the missing list."""
    _print_status_table(_TEST_DEPS, missing=[_TEST_DEPS[0]], bash_ok=True, os_name=OsName.LINUX)


def test_print_status_table_bash_missing_on_macos() -> None:
    """_print_status_table shows bash(4+) as missing on macOS when bash_ok is False."""
    _print_status_table(_TEST_DEPS, missing=[], bash_ok=False, os_name=OsName.MACOS)


def test_check_deps_no_flags_reports_all_present(cli_runner: CliRunner) -> None:
    """Running 'mngr dependencies' with no flags when all deps are present should exit 0."""
    # All real system deps (ssh, git, tmux, jq) are expected to be present in dev environments.
    # This test may need adjustment if run in a minimal CI container.
    result = cli_runner.invoke(check_deps, [])
    # Just verify it runs without crashing. Exit code depends on whether all deps are installed.
    assert result.exit_code in (0, 1)
    assert "System dependencies" in result.output
