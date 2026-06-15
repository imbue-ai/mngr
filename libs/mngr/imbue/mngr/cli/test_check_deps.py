"""Release tests for the check_deps (mngr dependencies) command.

These tests actually invoke package managers to install missing dependencies,
so they are slow and require network access.
"""

import pytest
from click.testing import CliRunner

from imbue.mngr.cli.check_deps import check_deps
from imbue.mngr.utils.deps import ALL_DEPS
from imbue.mngr.utils.deps import DependencyCategory
from imbue.mngr.utils.deps import OsName
from imbue.mngr.utils.deps import check_bash_version
from imbue.mngr.utils.deps import detect_os


@pytest.mark.release
@pytest.mark.timeout(120)
def test_check_deps_install_auto(cli_runner: CliRunner) -> None:
    """Running 'mngr dependencies --install auto' runs the full check/install flow.

    After the install attempt, every core dependency should be present, so the
    command exits 0 and reports no still-missing core deps. (Exit 1 is allowed
    only if a core dep genuinely could not be installed, in which case the
    "Still missing:" line must name it.)
    """
    os_name = detect_os()
    result = cli_runner.invoke(check_deps, ["--install", "auto"], catch_exceptions=False)
    assert f"System dependencies ({os_name})" in result.output

    missing_core_after = [
        dep for dep in ALL_DEPS if dep.category == DependencyCategory.CORE and not dep.is_available()
    ]
    bash_ok_after = check_bash_version() if os_name == OsName.MACOS else True

    if not missing_core_after and bash_ok_after:
        assert result.exit_code == 0
        # Either everything was already present, or the install succeeded and no
        # core dep remains missing.
        if not any(not dep.is_available() for dep in ALL_DEPS):
            assert "All system dependencies are present." in result.output
        else:
            assert "Still missing:" not in result.output
    else:
        # A core dep could not be installed: the command must report it and fail.
        assert result.exit_code == 1
        assert "Still missing:" in result.output
        for dep in missing_core_after:
            assert dep.binary in result.output
