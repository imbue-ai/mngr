"""Tests that mngr works correctly when installed fresh into an isolated venv.

These tests install mngr into a clean venv (separate from the dev workspace)
and exercise basic CLI commands. This catches issues that only manifest in a
real install: broken entry points, missing dependencies, accidental eager
imports of optional plugins, etc.
"""

import json
from pathlib import Path
import re

import pytest

from imbue.mngr.e2e.conftest import MinimalInstallEnv


@pytest.mark.release
@pytest.mark.timeout(60)
def test_help(minimal_install_env: MinimalInstallEnv) -> None:
    """mngr --help works in a fresh install."""
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
    """mngr create --help works in a fresh install."""
    result = minimal_install_env.run_mngr(["create", "--help"])

    assert result.returncode == 0, (
        f"mngr create --help failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # No warnings or errors should leak to stderr when rendering --help.
    assert result.stderr == "", f"mngr create --help produced unexpected stderr:\n{result.stderr}"
    assert "--type" in result.stdout
    assert "--no-connect" in result.stdout
    # The custom man-page-style help renders EXAMPLES so users see real usage.
    assert "EXAMPLES" in result.stdout


@pytest.mark.release
@pytest.mark.timeout(60)
def test_list(minimal_install_env: MinimalInstallEnv) -> None:
    """mngr list works in a fresh install and returns no agents."""
    result = minimal_install_env.run_mngr(["list"])

    assert result.returncode == 0, (
        f"mngr list failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "No agents found" in result.stdout, (
        f"Expected 'No agents found' in stdout:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.release
@pytest.mark.timeout(60)
def test_list_json(minimal_install_env: MinimalInstallEnv) -> None:
    """mngr list --format json returns valid JSON in a fresh install."""
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
    for users who haven't installed optional plugins like modal. The third-party
    package names are import names (e.g. ``dockerfile_parse``), not PyPI names.
    """
    check_script = """
import sys
import imbue.mngr.main

plugin_mods = [m for m in sys.modules if m.startswith('imbue.mngr_')]
optional_3p = [m for m in ('modal', 'modal_proxy', 'dockerfile_parse') if m in sys.modules]
imported = sorted(set(plugin_mods + optional_3p))
assert not imported, f'Unexpected eager imports: {imported}'
"""
    result = minimal_install_env.run_python(check_script)

    assert result.returncode == 0, (
        f"Optional plugin modules were eagerly imported:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


_BUILTIN_PLUGINS = frozenset({"codex", "command", "docker", "headless_command", "local", "ssh"})


@pytest.mark.release
@pytest.mark.timeout(60)
def test_plugin_list_in_fresh_install(minimal_install_env: MinimalInstallEnv) -> None:
    """mngr plugin list works in a fresh install and shows the built-in plugins.

    A fresh imbue-mngr install registers no external entry points, but the
    built-in agents (codex/command/headless_command) and built-in provider
    backends (local/ssh/docker) are always registered programmatically. This
    asserts that the command produces the expected baseline output rather
    than crashing or returning a partial table.
    """
    result = minimal_install_env.run_mngr(["plugin", "list"])

    assert result.returncode == 0, (
        f"mngr plugin list failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    for plugin_name in _BUILTIN_PLUGINS:
        assert plugin_name in result.stdout, (
            f"Expected built-in plugin '{plugin_name}' in output, got:\nstdout: {result.stdout}"
        )
    assert "NAME" in result.stdout and "ENABLED" in result.stdout, (
        f"Expected table headers in output, got:\nstdout: {result.stdout}"
    )


@pytest.mark.release
@pytest.mark.timeout(60)
def test_plugin_list_json_in_fresh_install(minimal_install_env: MinimalInstallEnv) -> None:
    """mngr plugin list --format json returns the built-in plugins as JSON in a fresh install."""
    result = minimal_install_env.run_mngr(["plugin", "list", "--format", "json"])

    assert result.returncode == 0, (
        f"mngr plugin list --format json failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    parsed = json.loads(result.stdout)
    assert set(parsed.keys()) == {"plugins"}, f"Expected top-level 'plugins' key only, got: {parsed}"
    names = {p["name"] for p in parsed["plugins"]}
    assert names == _BUILTIN_PLUGINS, f"Expected built-in plugins {_BUILTIN_PLUGINS}, got: {names}"
    # All built-ins are enabled by default in a fresh install with no overrides
    for plugin in parsed["plugins"]:
        assert plugin["enabled"] == "true", f"Plugin {plugin['name']} unexpectedly disabled: {plugin}"


@pytest.mark.release
@pytest.mark.timeout(60)
def test_plugin_list_active_in_fresh_install(minimal_install_env: MinimalInstallEnv) -> None:
    """mngr plugin list --active works in a fresh install and lists the active built-ins."""
    result = minimal_install_env.run_mngr(["plugin", "list", "--active"])

    assert result.returncode == 0, (
        f"mngr plugin list --active failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # All built-in plugins are active by default
    for plugin_name in _BUILTIN_PLUGINS:
        assert plugin_name in result.stdout, (
            f"Expected built-in plugin '{plugin_name}' in --active output, got:\nstdout: {result.stdout}"
        )


@pytest.mark.release
@pytest.mark.timeout(60)
def test_plugin_help_in_fresh_install(minimal_install_env: MinimalInstallEnv) -> None:
    """mngr plugin --help works in a fresh install."""
    result = minimal_install_env.run_mngr(["plugin", "--help"])

    assert result.returncode == 0, (
        f"mngr plugin --help failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # All five plugin subcommands should be advertised by the help text.
    for subcommand in ("list", "add", "remove", "enable", "disable"):
        assert subcommand in result.stdout, f"missing subcommand {subcommand!r} in help output:\n{result.stdout}"
    # Confirm the output is actually the plugin help (not, e.g., an error
    # that happens to include some of the words above).
    assert "Manage" in result.stdout, f"expected plugin description in help output:\n{result.stdout}"


@pytest.mark.release
@pytest.mark.timeout(60)
def test_plugin_no_args_shows_help_in_fresh_install(minimal_install_env: MinimalInstallEnv) -> None:
    """mngr plugin with no subcommand falls back to showing the plugin help."""
    result = minimal_install_env.run_mngr(["plugin"])

    assert result.returncode == 0, (
        f"mngr plugin failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # The fallback should advertise the same subcommands as --help.
    for subcommand in ("list", "add", "remove", "enable", "disable"):
        assert subcommand in result.stdout, f"missing subcommand {subcommand!r} in fallback help:\n{result.stdout}"


@pytest.mark.release
@pytest.mark.timeout(60)
def test_config_get_in_fresh_install(minimal_install_env: MinimalInstallEnv) -> None:
    """mngr config get returns a default value in a fresh install.

    With no config files written, reading a key must return the schema's
    default (``headless`` defaults to ``False``) -- this exercises the
    merged-config path and confirms defaults load without any user/project
    config present.
    """
    result = minimal_install_env.run_mngr(["config", "get", "headless"])

    assert result.returncode == 0, (
        f"mngr config get failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.stdout.strip().lower() == "false", (
        f"expected default headless value 'false', got stdout: {result.stdout!r}"
    )


@pytest.mark.release
@pytest.mark.timeout(60)
def test_config_get_unknown_key_in_fresh_install(minimal_install_env: MinimalInstallEnv) -> None:
    """mngr config get on an unknown key fails with a clear error in a fresh install."""
    result = minimal_install_env.run_mngr(["config", "get", "this.key.does.not.exist"])

    assert result.returncode != 0, (
        f"mngr config get unknown key should have failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    combined = (result.stdout + result.stderr).lower()
    assert "not found" in combined, (
        f"expected 'not found' in output, got stdout: {result.stdout!r} stderr: {result.stderr!r}"
    )


@pytest.mark.release
@pytest.mark.timeout(60)
def test_config_set_roundtrip_in_fresh_install(minimal_install_env: MinimalInstallEnv) -> None:
    """mngr config set then config get returns the set value, and roundtrips back."""
    # Confirm the default before mutating: otherwise a set-to-default would pass trivially.
    initial = minimal_install_env.run_mngr(["config", "get", "headless", "--format", "json"])
    assert initial.returncode == 0, (
        f"mngr config get failed (exit {initial.returncode}):\nstdout: {initial.stdout}\nstderr: {initial.stderr}"
    )
    assert json.loads(initial.stdout)["value"] is False

    set_result = minimal_install_env.run_mngr(["config", "set", "headless", "true", "--format", "json"])
    assert set_result.returncode == 0, (
        f"mngr config set failed (exit {set_result.returncode}):\nstdout: {set_result.stdout}\nstderr: {set_result.stderr}"
    )
    set_payload = json.loads(set_result.stdout)
    assert set_payload["key"] == "headless"
    assert set_payload["value"] is True
    # Verify the actual on-disk effect: set should have written a config file.
    assert Path(set_payload["path"]).exists(), f"Config file not written at {set_payload['path']}"

    get_result = minimal_install_env.run_mngr(["config", "get", "headless", "--format", "json"])
    assert get_result.returncode == 0, (
        f"mngr config get failed (exit {get_result.returncode}):\nstdout: {get_result.stdout}\nstderr: {get_result.stderr}"
    )
    assert json.loads(get_result.stdout)["value"] is True

    # Round-trip: set back to the default and confirm.
    revert_result = minimal_install_env.run_mngr(["config", "set", "headless", "false", "--format", "json"])
    assert revert_result.returncode == 0, (
        f"mngr config set failed (exit {revert_result.returncode}):\nstdout: {revert_result.stdout}\nstderr: {revert_result.stderr}"
    )
    final = minimal_install_env.run_mngr(["config", "get", "headless", "--format", "json"])
    assert final.returncode == 0, (
        f"mngr config get failed (exit {final.returncode}):\nstdout: {final.stdout}\nstderr: {final.stderr}"
    )
    assert json.loads(final.stdout)["value"] is False


@pytest.mark.release
@pytest.mark.timeout(60)
def test_version_output(minimal_install_env: MinimalInstallEnv) -> None:
    """mngr --version prints the installed package version in a fresh install.

    Guards against the `package_name`/distribution-name mismatch that previously
    caused click to raise `RuntimeError("'mngr' is not installed.")` because the
    PyPI distribution is `imbue-mngr`, not `mngr`.
    """
    result = minimal_install_env.run_mngr(["--version"])

    assert result.returncode == 0, (
        f"mngr --version failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    match = re.fullmatch(r"mngr (\d+\.\d+\.\d+\S*)\n", result.stdout)
    assert match is not None, f"Unexpected --version output: {result.stdout!r}"
