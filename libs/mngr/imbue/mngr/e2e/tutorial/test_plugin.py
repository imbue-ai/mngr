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

# Each command spawns the mngr CLI as a subprocess; its import/startup alone
# takes several seconds, which exceeds the repo-wide default 10s timeout for an
# e2e subprocess test. These commands are pure CLI (no agent creation), so a
# modest override is sufficient, matching the per-test overrides used by the
# other tutorial test files.
pytestmark = pytest.mark.timeout(60)


@pytest.mark.release
def test_plugin_list_shows_installed(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list all available plugins
        mngr plugin list
    """)
    result = e2e.run("mngr plugin list", comment="List all installed plugins")
    expect(result).to_succeed()
    # The output is a table; confirm the column headers are present so we know
    # the listing is structured rather than an incidental "claude" substring.
    expect(result.stdout).to_contain("NAME")
    expect(result.stdout).to_contain("VERSION")
    expect(result.stdout).to_contain("ENABLED")
    # Verify several stable built-in plugins are listed as named rows (anchored
    # to the NAME column at the start of a line) rather than merely appearing in
    # some description text. These ship with mngr and should always be installed.
    for plugin_name in ("claude", "modal", "local"):
        expect(result.stdout).to_match(rf"(?m)^\s*{plugin_name}\s")


@pytest.mark.release
@pytest.mark.timeout(60)
def test_plugin_list_active(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list only active plugins
        mngr plugin list --active
    """)
    # The tutorial command itself.
    expect(e2e.run("mngr plugin list --active", comment="list only active plugins")).to_succeed()

    # Verify the contract of --active: every listed plugin is enabled. Parse the
    # JSON form of the same command so the check does not depend on table layout.
    # (`enabled` is rendered as the lowercase string "true"/"false".)
    active_result = e2e.run(
        "mngr plugin list --active --format json",
        comment="list only active plugins as JSON for verification",
    )
    expect(active_result).to_succeed()
    active_plugins = json.loads(active_result.stdout)["plugins"]
    assert active_plugins, "expected at least one active plugin"
    assert all(p["enabled"] == "true" for p in active_plugins), active_plugins

    # The active set must be a subset of the full plugin list, and must include a
    # core plugin like `claude` that is always present and enabled by default.
    all_result = e2e.run("mngr plugin list --format json", comment="list all plugins for comparison")
    expect(all_result).to_succeed()
    all_names = {p["name"] for p in json.loads(all_result.stdout)["plugins"]}
    active_names = {p["name"] for p in active_plugins}
    assert active_names <= all_names, (active_names, all_names)
    assert "claude" in active_names


@pytest.mark.release
@pytest.mark.timeout(60)
def test_plugin_list_active_excludes_disabled(e2e: E2eSession) -> None:
    """A disabled plugin disappears from `--active` but remains in the full list.

    This exercises the actual filtering behavior of `--active`, which the happy
    path above cannot: by default every plugin is enabled, so `--active` returns
    the full set and the filter is never observed doing anything.
    """
    e2e.write_tutorial_block("""
        # list only active plugins
        mngr plugin list --active
    """)
    # Pick a plugin that is currently enabled to disable. Avoid `claude` since it
    # is the default agent type; any other enabled plugin demonstrates the filter.
    all_result = e2e.run("mngr plugin list --format json", comment="list all plugins")
    expect(all_result).to_succeed()
    enabled_names = sorted(
        p["name"] for p in json.loads(all_result.stdout)["plugins"] if p["enabled"] == "true" and p["name"] != "claude"
    )
    assert enabled_names, "expected at least one non-claude enabled plugin to disable"
    target = enabled_names[0]

    # Disable it in the isolated test profile (user scope).
    expect(
        e2e.run(f"mngr plugin disable {target} --scope user", comment=f"disable the {target} plugin")
    ).to_succeed()

    # The full list still shows the plugin, now marked disabled.
    after_all = e2e.run("mngr plugin list --format json", comment="list all plugins after disabling")
    expect(after_all).to_succeed()
    after_all_plugins = {p["name"]: p["enabled"] for p in json.loads(after_all.stdout)["plugins"]}
    assert target in after_all_plugins
    assert after_all_plugins[target] == "false", after_all_plugins[target]

    # The active list no longer includes the disabled plugin.
    after_active = e2e.run("mngr plugin list --active --format json", comment="list only active plugins after disabling")
    expect(after_active).to_succeed()
    after_active_names = {p["name"] for p in json.loads(after_active.stdout)["plugins"]}
    assert target not in after_active_names, after_active_names


@pytest.mark.release
def test_plugin_add_by_name(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # add a plugin by name (from the registry)
        mngr plugin add my-plugin
    """)
    # In the e2e environment mngr runs via `uv run`, not `uv tool install`, so
    # `plugin add` is guarded by `require_uv_tool_receipt` and aborts before any
    # registry lookup. Verify it fails with that specific, actionable guard
    # message rather than just any non-zero exit (which could mask a parse error).
    result = e2e.run("mngr plugin add my-plugin", comment="add a plugin by name")
    expect(result).to_fail()
    expect(result.stdout + result.stderr).to_contain("not installed via 'uv tool install'")


@pytest.mark.release
def test_plugin_add_by_path(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # add a plugin from a local path
        mngr plugin add --path /path/to/my-plugin
    """)
    result = e2e.run("mngr plugin add --path /path/to/my-plugin", comment="add a plugin from a local path")
    # `/path/to/my-plugin` does not exist, so the command must fail.
    expect(result).to_fail()
    # Crucially, the failure must NOT be a usage error: `--path` has to be a
    # recognized option that is consumed as a plugin source. If `--path` were
    # unknown, click would print "No such option"; if it were not treated as a
    # source, source-parsing would report "Provide at least one of ...". Ruling
    # both out confirms the command exercises the path-install flow this
    # tutorial line demonstrates, rather than passing on any arbitrary error.
    combined = (result.stdout + result.stderr).lower()
    assert "no such option" not in combined, combined
    assert "provide at least one of" not in combined, combined


# mngr CLI startup plus setup_command_context takes ~8-9s, which sits right at the
# default 10s global pytest timeout. Override so a cold or loaded run has headroom.
@pytest.mark.timeout(60)
@pytest.mark.release
def test_plugin_add_by_git(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # add a plugin from a git repository
        mngr plugin add --git https://github.com/user/mngr-plugin.git
    """)
    result = e2e.run(
        "mngr plugin add --git https://github.com/user/mngr-plugin.git",
        comment="add a plugin from a git repository",
    )
    # The git URL points at a repo that does not exist, so the install must fail.
    assert result.exit_code != 0
    # Verify the failure is a clean, reported error rather than a crash/traceback,
    # and that no plugin named after the URL was left registered.
    combined_output = result.stdout + result.stderr
    assert "Traceback" not in combined_output
    assert "mngr-plugin" not in _list_plugins(e2e)


def _list_plugins(e2e: E2eSession) -> str:
    """Return the stdout of `mngr plugin list`, used to assert plugin registration."""
    listing = e2e.run("mngr plugin list", comment="verify the failed plugin was not registered")
    expect(listing).to_succeed()
    return listing.stdout


@pytest.mark.release
@pytest.mark.timeout(60)
def test_plugin_remove(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # remove a plugin
        mngr plugin remove my-plugin
    """)
    result = e2e.run("mngr plugin remove my-plugin", comment="remove a plugin")
    # Removing a plugin that cannot be removed (here, mngr is not installed via
    # `uv tool install` in the test env) must fail cleanly with a user-facing
    # error rather than crash with a raw Python traceback.
    expect(result).to_fail()
    expect(result.stdout + result.stderr).not_to_contain("Traceback")


@pytest.mark.release
@pytest.mark.timeout(60)
def test_plugin_enable_project_scope(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # enable a plugin at the project scope
        mngr plugin enable my-plugin --scope project
    """)
    # `plugin enable` is a soft operation: it records the enabled state in the
    # project config even for a plugin that is not yet installed, so a user can
    # pre-configure a plugin before installing it. It therefore succeeds (with a
    # warning) rather than failing for an unknown plugin name.
    result = e2e.run(
        "mngr plugin enable my-plugin --scope project",
        comment="enable a plugin at the project scope",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("project")
    expect(result.stderr).to_contain("not currently registered")
    # Verify the actual effect: the command writes the enabled state into the
    # project-scope config file whose path it reports. Read that file directly
    # (rather than via a second `mngr` invocation, which would be rejected by
    # the pytest config opt-in guard since this freshly written project config
    # has no `is_allowed_in_pytest` flag).
    path_match = re.search(r"in project \((.+settings\.toml)\)", result.stdout)
    assert path_match is not None, f"could not find written config path in: {result.stdout!r}"
    config = tomllib.loads(Path(path_match.group(1)).read_text())
    assert config["plugins"]["my-plugin"]["enabled"] is True, config


@pytest.mark.release
@pytest.mark.timeout(120)
def test_plugin_disable_user_scope(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # disable a plugin at the user scope
        mngr plugin disable my-plugin --scope user
    """)
    # `my-plugin` is not registered, but disabling is a soft pre-configuration:
    # the command succeeds, warns that the plugin is unknown, and persists the
    # disabled state to the user-scope config so it takes effect once installed.
    result = e2e.run(
        "mngr plugin disable my-plugin --scope user",
        comment="disable a plugin at the user scope",
    )
    expect(result).to_succeed()
    expect(result.stderr).to_contain("not currently registered")
    expect(result.stdout).to_contain("user")
    # The disabled state must actually be persisted at the user scope.
    persisted = e2e.run(
        "mngr config get plugins.my-plugin.enabled --scope user --format json",
        comment="verify the disabled state was persisted at the user scope",
    )
    expect(persisted).to_succeed()
    assert json.loads(persisted.stdout)["value"] is False
    # `--scope user` must not leak the setting into the project scope.
    project_scoped = e2e.run(
        "mngr config get plugins.my-plugin.enabled --scope project --format json",
        comment="confirm the setting was not written at the project scope",
    )
    expect(project_scoped).to_fail()


@pytest.mark.release
def test_plugin_list_fields(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list plugins with specific fields
        mngr plugin list --fields "name,version,enabled"
    """)
    result = e2e.run(
        'mngr plugin list --fields "name,version,enabled"',
        comment="list plugins with specific fields",
    )
    expect(result).to_succeed()
    # Only the requested columns should appear, in the requested order.
    expect(result.stdout).to_contain("NAME")
    expect(result.stdout).to_contain("VERSION")
    expect(result.stdout).to_contain("ENABLED")
    # A default field that was NOT requested must be omitted entirely.
    expect(result.stdout).not_to_contain("DESCRIPTION")
    # The `claude` plugin is always present and is enabled, so the `enabled`
    # field must resolve to a real boolean value (not the `-` placeholder that
    # an unrecognized field name would produce).
    expect(result.stdout).to_match(r"claude\s+\S+\s+true")
