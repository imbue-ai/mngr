"""Tests that mngr works correctly when installed without optional plugin packages.

These tests install mngr into an isolated venv that contains only the core package
and its direct dependencies -- no plugin packages like mngr_modal, mngr_claude, etc.
"""

import json

import pytest

from imbue.mngr.e2e.conftest import MinimalInstallEnv


@pytest.mark.release
@pytest.mark.timeout(60)
def test_help_without_plugins(minimal_install_env: MinimalInstallEnv) -> None:
    """mngr --help works in a clean install without any plugin packages."""
    result = minimal_install_env.run_mngr(["--help"])

    assert result.returncode == 0, (
        f"mngr --help failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "Usage" in result.stdout
    assert "create" in result.stdout
    assert "list" in result.stdout


@pytest.mark.release
@pytest.mark.timeout(60)
def test_create_help_without_plugins(minimal_install_env: MinimalInstallEnv) -> None:
    """mngr create --help works without plugin packages."""
    result = minimal_install_env.run_mngr(["create", "--help"])

    assert result.returncode == 0, (
        f"mngr create --help failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "--command" in result.stdout
    assert "--no-connect" in result.stdout


@pytest.mark.release
@pytest.mark.timeout(60)
def test_list_without_plugins(minimal_install_env: MinimalInstallEnv) -> None:
    """mngr list works in a clean install and returns no agents."""
    result = minimal_install_env.run_mngr(["list"])

    assert result.returncode == 0, (
        f"mngr list failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "No agents found" in result.stdout


@pytest.mark.release
@pytest.mark.timeout(60)
def test_list_json_without_plugins(minimal_install_env: MinimalInstallEnv) -> None:
    """mngr list --format json works and returns valid JSON with empty agents."""
    result = minimal_install_env.run_mngr(["list", "--format", "json"])

    assert result.returncode == 0, (
        f"mngr list --format json failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    parsed = json.loads(result.stdout)
    assert parsed["agents"] == []


@pytest.mark.release
@pytest.mark.timeout(60)
def test_no_eager_plugin_imports(minimal_install_env: MinimalInstallEnv) -> None:
    """Importing mngr's main module does not eagerly import optional plugin modules.

    This is a defense against accidental top-level imports that would cause
    ImportError for users who haven't installed optional plugins like modal.
    """
    check_script = (
        "import imbue.mngr.main; import sys; "
        "optional = ['modal', 'imbue.mngr_modal', 'imbue.mngr_claude']; "
        "imported = [m for m in optional if m in sys.modules]; "
        "assert not imported, f'Unexpected eager imports: {imported}'"
    )
    result = minimal_install_env.run_python(check_script)

    assert result.returncode == 0, (
        f"Optional plugin modules were eagerly imported:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
