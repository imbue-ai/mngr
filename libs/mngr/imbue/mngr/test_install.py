"""Tests that mngr works correctly when installed fresh into an isolated venv.

These tests install mngr into a clean venv (separate from the dev workspace)
and exercise basic CLI commands. This catches issues that only manifest in a
real install: broken entry points, missing dependencies, accidental eager
imports of optional plugins, etc.
"""

import json

import pytest

from imbue.mngr.conftest import MinimalInstallEnv


@pytest.mark.release
@pytest.mark.timeout(60)
def test_help(minimal_install_env: MinimalInstallEnv) -> None:
    """In a fresh install, `mngr --help` exits 0 and prints the usage banner along with the
    core subcommands (`create`, `list`), proving the CLI entry point and command tree load."""
    result = minimal_install_env.run_mngr(["--help"])

    assert result.returncode == 0, (
        f"mngr --help failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "Usage" in result.stdout
    assert "create" in result.stdout
    assert "list" in result.stdout


@pytest.mark.release
@pytest.mark.timeout(60)
def test_create_help(minimal_install_env: MinimalInstallEnv) -> None:
    """In a fresh install, `mngr create --help` exits 0 and documents the `--type` and
    `--no-connect` options, proving the create subcommand and its flags are wired up."""
    result = minimal_install_env.run_mngr(["create", "--help"])

    assert result.returncode == 0, (
        f"mngr create --help failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "--type" in result.stdout
    assert "--no-connect" in result.stdout


@pytest.mark.release
@pytest.mark.timeout(60)
def test_list(minimal_install_env: MinimalInstallEnv) -> None:
    """In a fresh install with no agents, `mngr list` exits 0 and reports "No agents found",
    proving the command runs end-to-end against an empty state rather than crashing."""
    result = minimal_install_env.run_mngr(["list"])

    assert result.returncode == 0, (
        f"mngr list failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "No agents found" in result.stdout


@pytest.mark.release
@pytest.mark.timeout(60)
def test_list_json(minimal_install_env: MinimalInstallEnv) -> None:
    """In a fresh install, `mngr list --format json` exits 0 and emits parseable JSON whose
    `agents` and `errors` arrays are both empty, proving the JSON formatter produces a
    well-formed, machine-readable payload for the empty state."""
    result = minimal_install_env.run_mngr(["list", "--format", "json"])

    assert result.returncode == 0, (
        f"mngr list --format json failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    parsed = json.loads(result.stdout)
    assert parsed["agents"] == []
    assert parsed["errors"] == []


@pytest.mark.release
@pytest.mark.timeout(60)
def test_no_eager_plugin_imports(minimal_install_env: MinimalInstallEnv) -> None:
    """Importing mngr's main module does not eagerly import optional plugin modules.

    This catches accidental top-level imports that would cause ImportError
    for users who haven't installed optional plugins like modal.
    """
    check_script = (
        "import imbue.mngr.main; import sys; "
        "plugin_mods = [m for m in sys.modules if m.startswith('imbue.mngr_')]; "
        "optional_3p = [m for m in ['modal'] if m in sys.modules]; "
        "imported = sorted(set(plugin_mods + optional_3p)); "
        "assert not imported, f'Unexpected eager imports: {imported}'"
    )
    result = minimal_install_env.run_python(check_script)

    assert result.returncode == 0, (
        f"Optional plugin modules were eagerly imported:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.release
@pytest.mark.timeout(60)
def test_plugin_list_in_fresh_install(minimal_install_env: MinimalInstallEnv) -> None:
    """In a fresh install with no optional plugins installed, `mngr plugin list` exits 0,
    proving the plugin-discovery path handles the empty case without erroring."""
    result = minimal_install_env.run_mngr(["plugin", "list"])

    assert result.returncode == 0, (
        f"mngr plugin list failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.release
@pytest.mark.timeout(60)
def test_plugin_help_in_fresh_install(minimal_install_env: MinimalInstallEnv) -> None:
    """In a fresh install, `mngr plugin --help` exits 0 and documents the `list`, `enable`,
    and `disable` subcommands, proving the plugin command group is fully registered."""
    result = minimal_install_env.run_mngr(["plugin", "--help"])

    assert result.returncode == 0, (
        f"mngr plugin --help failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "list" in result.stdout
    assert "enable" in result.stdout
    assert "disable" in result.stdout


@pytest.mark.release
@pytest.mark.timeout(60)
def test_config_get_in_fresh_install(minimal_install_env: MinimalInstallEnv) -> None:
    """In a fresh install with no settings file, `mngr config get headless` exits 0,
    proving config lookup of a known key falls back to its default without erroring."""
    result = minimal_install_env.run_mngr(["config", "get", "headless"])

    assert result.returncode == 0, (
        f"mngr config get failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.release
@pytest.mark.timeout(60)
def test_config_set_roundtrip_in_fresh_install(minimal_install_env: MinimalInstallEnv) -> None:
    """In a fresh install, `mngr config set headless true` persists the value so a subsequent
    `mngr config get headless` reads back "true", proving config writes round-trip to disk."""
    set_result = minimal_install_env.run_mngr(["config", "set", "headless", "true"])
    assert set_result.returncode == 0, (
        f"mngr config set failed (exit {set_result.returncode}):\nstdout: {set_result.stdout}\nstderr: {set_result.stderr}"
    )

    get_result = minimal_install_env.run_mngr(["config", "get", "headless"])
    assert get_result.returncode == 0, (
        f"mngr config get failed (exit {get_result.returncode}):\nstdout: {get_result.stdout}\nstderr: {get_result.stderr}"
    )
    assert "true" in get_result.stdout.lower()


@pytest.mark.release
@pytest.mark.timeout(60)
def test_version_output(minimal_install_env: MinimalInstallEnv) -> None:
    """mngr --version prints a version string in a fresh install.

    The version option is registered with package_name="imbue-mngr"
    (matching the installed distribution), so --version must succeed and
    print the program name.
    """
    result = minimal_install_env.run_mngr(["--version"])

    assert result.returncode == 0, (
        f"mngr --version failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "mngr" in result.stdout
