"""Tests for the plugin management commands from the tutorial.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block.
"""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
def test_plugin_list_shows_installed(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list all available plugins
        mngr plugin list
    """)
    result = e2e.run("mngr plugin list", comment="List all installed plugins")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("claude")


@pytest.mark.release
def test_plugin_list_active(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list only active plugins
        mngr plugin list --active
    """)
    expect(e2e.run("mngr plugin list --active", comment="list only active plugins")).to_succeed()


@pytest.mark.release
def test_plugin_add_by_name(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # add a plugin by name (from the registry)
        mngr plugin add my-plugin
    """)
    # `my-plugin` does not exist in any registry the test env can reach;
    # verify mngr parses the command and exits cleanly with an error.
    result = e2e.run("mngr plugin add my-plugin", comment="add a plugin by name")
    assert result.exit_code != 0


@pytest.mark.release
def test_plugin_add_by_path(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # add a plugin from a local path
        mngr plugin add --path /path/to/my-plugin
    """)
    result = e2e.run("mngr plugin add --path /path/to/my-plugin", comment="add a plugin from a local path")
    assert result.exit_code != 0


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
    assert result.exit_code != 0


@pytest.mark.release
def test_plugin_remove(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # remove a plugin
        mngr plugin remove my-plugin
    """)
    result = e2e.run("mngr plugin remove my-plugin", comment="remove a plugin")
    assert result.exit_code != 0 or "not found" in (result.stdout + result.stderr).lower()


@pytest.mark.release
def test_plugin_enable_project_scope(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # enable a plugin at the project scope
        mngr plugin enable my-plugin --scope project
    """)
    result = e2e.run(
        "mngr plugin enable my-plugin --scope project",
        comment="enable a plugin at the project scope",
    )
    assert result.exit_code != 0 or "not found" in (result.stdout + result.stderr).lower()


@pytest.mark.release
def test_plugin_disable_user_scope(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # disable a plugin at the user scope
        mngr plugin disable my-plugin --scope user
    """)
    result = e2e.run(
        "mngr plugin disable my-plugin --scope user",
        comment="disable a plugin at the user scope",
    )
    assert result.exit_code != 0 or "not found" in (result.stdout + result.stderr).lower()


@pytest.mark.release
def test_plugin_list_fields(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list plugins with specific fields
        mngr plugin list --fields "name,version,active"
    """)
    expect(
        e2e.run(
            'mngr plugin list --fields "name,version,active"',
            comment="list plugins with specific fields",
        )
    ).to_succeed()
