"""Tests for the plugin management commands from the tutorial.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block.
"""

import json
import re
import tomllib
from pathlib import Path

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


def _list_plugins(e2e: E2eSession, command: str) -> list[dict[str, str]]:
    """Run a `mngr plugin list ... --format json` command and return its rows.

    Each row is a dict of stringified field values (e.g. ``enabled`` is the
    string ``"true"`` / ``"false"``), matching the CLI's JSON serialization.
    """
    result = e2e.run(command)
    expect(result).to_succeed()
    return json.loads(result.stdout)["plugins"]


@pytest.mark.release
def test_plugin_list_shows_installed(e2e: E2eSession) -> None:
    """Tutorial block:
        # list all available plugins
        mngr plugin list

    Scope: `mngr plugin list` exits 0 and renders the default-field table whose
    headers (NAME, VERSION, DESCRIPTION, ENABLED) are produced by the CLI itself,
    listing the core plugins that always ship in the `imbue` package (claude, modal).
    """
    result = e2e.run("mngr plugin list", comment="List all installed plugins")
    expect(result).to_succeed()
    # The listing renders a table whose columns come from the default fields
    # (name, version, description, enabled) -- these headers are produced by the
    # CLI itself, independent of which plugins happen to be installed.
    for header in ("NAME", "VERSION", "DESCRIPTION", "ENABLED"):
        expect(result.stdout).to_contain(header)
    # Core plugins that always ship inside the `imbue` package must be listed.
    expect(result.stdout).to_contain("claude")
    expect(result.stdout).to_contain("modal")


@pytest.mark.release
@pytest.mark.timeout(60)
def test_plugin_list_active(e2e: E2eSession) -> None:
    """Tutorial block:
        # list only active plugins
        mngr plugin list --active

    Scope: `--active` lists ONLY enabled plugins -- disabling a real plugin drops
    it from the `--active` listing while the unfiltered list still reports it as
    enabled=false. The bare command succeeds and `--active` never includes a
    disabled plugin.
    """
    # The bare command from the tutorial block must succeed.
    expect(e2e.run("mngr plugin list --active", comment="list only active plugins")).to_succeed()

    # Verify the documented behavior: --active lists ONLY enabled plugins.
    # Parse the JSON form so assertions key off structured fields rather than
    # scraping the human-formatted table.
    active = _list_plugins(e2e, "mngr plugin list --active --format json")
    assert active, "expected --active to list at least one plugin"
    disabled_in_active = [p["name"] for p in active if p["enabled"] != "true"]
    assert not disabled_in_active, f"--active listed disabled plugins: {disabled_in_active}"

    # Disabling a real plugin must drop it from --active, while the unfiltered
    # list still reports it (as disabled). This is precisely what --active does.
    expect(e2e.run("mngr plugin disable tutor --scope user", comment="disable a plugin")).to_succeed()

    active_names = {p["name"] for p in _list_plugins(e2e, "mngr plugin list --active --format json")}
    assert "tutor" not in active_names, "disabled plugin should not appear under --active"

    all_by_name = {p["name"]: p for p in _list_plugins(e2e, "mngr plugin list --format json")}
    assert "tutor" in all_by_name, "unfiltered list should still show the disabled plugin"
    assert all_by_name["tutor"]["enabled"] == "false", "disabled plugin should report enabled=false"


@pytest.mark.release
# Every mngr invocation pays a ~10s cold-start cost (importing and registering
# all plugins), which alone exceeds the 10s default per-test timeout, so this
# e2e test needs a higher budget like the other subprocess-driven e2e tests.
@pytest.mark.timeout(60)
def test_plugin_add_by_name(e2e: E2eSession) -> None:
    """Tutorial block:
        # add a plugin by name (from the registry)
        mngr plugin add my-plugin

    Scope: `mngr plugin add` mutates the uv tool installation, but the test env
    runs mngr from a project venv (not a `uv tool install`), so the command exits
    non-zero with a controlled "Aborted" on its output and no Python traceback,
    rather than crashing.
    """
    # `mngr plugin add` mutates the uv tool installation, but the test env runs
    # mngr from a project venv (not a `uv tool install`), so the command exits
    # cleanly with a controlled AbortError -- a non-zero exit and an "Aborted"
    # message on stderr, with no Python traceback -- rather than crashing.
    result = e2e.run("mngr plugin add my-plugin", comment="add a plugin by name")
    assert result.exit_code != 0, result.stderr
    combined_output = result.stdout + result.stderr
    assert "Aborted" in combined_output, combined_output
    assert "Traceback" not in combined_output, combined_output


@pytest.mark.release
def test_plugin_add_by_path(e2e: E2eSession) -> None:
    """Tutorial block:
        # add a plugin from a local path
        mngr plugin add --path /path/to/my-plugin

    Scope: `--path` is a recognized option (no "No such option"), so the command
    fails with a clean abort (exit code 1, "Aborted", no traceback) -- not a click
    usage error (exit code 2) -- because the given path does not exist.
    """
    result = e2e.run("mngr plugin add --path /path/to/my-plugin", comment="add a plugin from a local path")
    # `--path` is a recognized option, so the command must fail with a clean
    # abort (exit code 1) -- not a click usage error (exit code 2, which would
    # indicate the flag was unparsed) and not an uncaught crash. The path does
    # not exist, so the install cannot succeed.
    expect(result).to_have_exit_code(1)
    combined_output = result.stdout + result.stderr
    expect(combined_output).to_contain("Aborted")
    expect(combined_output).not_to_contain("Traceback")
    expect(combined_output).not_to_contain("No such option")


@pytest.mark.release
def test_plugin_add_by_git(e2e: E2eSession) -> None:
    """Tutorial block:
        # add a plugin from a git repository
        mngr plugin add --git https://github.com/user/mngr-plugin.git

    Scope: `--git` is accepted as a source specifier (no "No such option"), and
    the non-existent repo drives the command to a clean, intentional AbortError
    path -- exit code 1 with an "Aborted" message -- not a click usage error or an
    uncaught traceback.
    """
    # The git URL points at a non-existent repo, so the command always fails --
    # either at install time ("Failed to install plugin packages: ...") or, in an
    # environment where mngr is not managed by `uv tool`, at the receipt check.
    # Both paths raise AbortError, which click renders as exit code 1 with an
    # "Aborted:" message. Asserting on this (rather than just `exit_code != 0`)
    # confirms `--git` is accepted as a source specifier and that the command
    # reaches a clean, intentional error path -- not a click usage error (exit
    # code 2, which `!= 0` would also accept) or an uncaught traceback.
    result = e2e.run(
        "mngr plugin add --git https://github.com/user/mngr-plugin.git",
        comment="add a plugin from a git repository",
    )
    expect(result).to_have_exit_code(1)
    combined_output = result.stdout + result.stderr
    expect(combined_output).to_contain("Aborted")
    # `--git` must be recognized: an unknown option would make click emit "No such option".
    expect(combined_output).not_to_contain("No such option")


@pytest.mark.release
def test_plugin_remove(e2e: E2eSession) -> None:
    """Tutorial block:
        # remove a plugin
        mngr plugin remove my-plugin

    Scope: `my-plugin` is not an installed plugin, so removal fails *cleanly* --
    a user-facing "Aborted" error, never a bare Python traceback -- regardless of
    the precise reason (mngr not installed via uv tool, or not a declared plugin).
    """
    # `my-plugin` is not an installed plugin, so removal must fail. Whatever the
    # precise reason (mngr not installed via uv tool, or the package not being a
    # declared plugin), the command must fail *cleanly* -- a user-facing
    # "Aborted" error, never a bare Python traceback.
    result = e2e.run("mngr plugin remove my-plugin", comment="remove a plugin")
    expect(result).to_fail()
    combined_output = result.stdout + result.stderr
    expect(combined_output).to_contain("Aborted")
    expect(combined_output).not_to_contain("Traceback")


@pytest.mark.release
def test_plugin_remove_rejects_invalid_name(e2e: E2eSession) -> None:
    """Tutorial block:
        # remove a plugin
        mngr plugin remove my-plugin

    Scope: the unhappy path of the same `mngr plugin remove` block. An unparseable
    package name is rejected up front with a clear "Invalid package name"
    argument-validation error, before any installation state is consulted (so this
    holds regardless of how mngr itself was installed).
    """
    result = e2e.run(
        "mngr plugin remove 'not a valid name'",
        comment="remove a plugin with an invalid package name",
    )
    expect(result).to_fail()
    expect(result.stdout + result.stderr).to_contain("Invalid package name")


@pytest.mark.release
def test_plugin_enable_project_scope(e2e: E2eSession) -> None:
    """Tutorial block:
        # enable a plugin at the project scope
        mngr plugin enable my-plugin --scope project

    Scope: enabling is a soft pre-configuration that succeeds even for a plugin
    that is not installed yet -- it prints "Enabled plugin 'my-plugin' in project",
    warns the plugin is "not currently registered", and persists enabled=true in
    the project settings.toml whose path it prints.
    """
    result = e2e.run(
        "mngr plugin enable my-plugin --scope project",
        comment="enable a plugin at the project scope",
    )
    # Enabling is a soft pre-configuration: it succeeds even for a plugin that
    # is not installed yet, recording the setting and warning that it will only
    # take effect once the plugin is installed.
    expect(result).to_succeed()
    expect(result.stdout).to_contain("Enabled plugin 'my-plugin' in project")
    expect(result.stderr).to_contain("not currently registered")

    # Verify the concrete effect: the project settings.toml referenced in the
    # output now marks the plugin enabled. The path is printed in parentheses.
    path_match = re.search(r"\((?P<path>/\S+settings\.toml)\)", result.stdout)
    assert path_match is not None, f"could not find settings path in output: {result.stdout!r}"
    settings_path = Path(path_match.group("path"))
    settings_text = settings_path.read_text()
    parsed = tomllib.loads(settings_text)
    assert parsed["plugins"]["my-plugin"]["enabled"] is True, settings_text


@pytest.mark.release
@pytest.mark.timeout(60)
def test_plugin_disable_user_scope(e2e: E2eSession) -> None:
    """Tutorial block:
        # disable a plugin at the user scope
        mngr plugin disable my-plugin --scope user

    Scope: disabling is a soft operation that persists the setting even for a
    plugin not yet installed -- it prints "Disabled plugin 'my-plugin' in user",
    warns the plugin is "not currently registered", and writes the user-scope
    config so `mngr config get` reads the value back as false.
    """
    result = e2e.run(
        "mngr plugin disable my-plugin --scope user",
        comment="disable a plugin at the user scope",
    )
    # Disabling is a soft operation: it persists the setting even for a plugin
    # that is not yet installed, so the user can pre-configure it. The command
    # therefore succeeds and warns that the plugin is not currently registered.
    expect(result).to_succeed()
    expect(result.stdout).to_contain("Disabled plugin 'my-plugin' in user")
    expect(result.stderr).to_contain("not currently registered")
    # Verify the actual effect: the disabled state is persisted in the
    # user-scope config and reads back as false.
    read_back = e2e.run(
        "mngr config get plugins.my-plugin.enabled --scope user",
        comment="read back the persisted user-scope setting",
    )
    expect(read_back).to_succeed()
    expect(read_back.stdout.strip().lower()).to_contain("false")


@pytest.mark.release
def test_plugin_list_fields(e2e: E2eSession) -> None:
    """Tutorial block:
        # list plugins with specific fields
        mngr plugin list --fields "name,version,enabled"

    Scope: `--fields "name,version,enabled"` shows only the requested columns in
    the requested order (NAME VERSION ENABLED), omits unrequested fields
    (DESCRIPTION), and renders real values -- the built-in `claude` plugin is
    present and the `enabled` column shows a true boolean, not the `-` placeholder.
    """
    result = e2e.run(
        'mngr plugin list --fields "name,version,enabled"',
        comment="list plugins with specific fields",
    )
    expect(result).to_succeed()
    # Only the requested columns are shown, in the requested order.
    expect(result.stdout).to_match(r"NAME\s+VERSION\s+ENABLED")
    # Unrequested fields are omitted.
    expect(result.stdout).not_to_contain("DESCRIPTION")
    # The built-in `claude` plugin is present and the `enabled` column renders a
    # real boolean (not the `-` placeholder produced by an unknown field name).
    expect(result.stdout).to_contain("claude")
    expect(result.stdout).to_match(r"\btrue\b")
