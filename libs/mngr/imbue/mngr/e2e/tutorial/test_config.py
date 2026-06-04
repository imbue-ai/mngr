"""Tests for the CONFIGURATION tutorial section.

Each test corresponds 1:1 to a tutorial script block.
"""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
def test_config_list(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list all configuration values
        mngr config list
    """)
    expect(e2e.run("mngr config list", comment="list all configuration values")).to_succeed()


@pytest.mark.release
def test_config_list_scope(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list configuration at a specific scope (user, project, or local)
        mngr config list --scope user
        mngr config list --scope project
        mngr config list --scope local
    """)
    expect(e2e.run("mngr config list --scope user", comment="list user scope")).to_succeed()
    expect(e2e.run("mngr config list --scope project", comment="list project scope")).to_succeed()
    expect(e2e.run("mngr config list --scope local", comment="list local scope")).to_succeed()


@pytest.mark.release
def test_config_get(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # get a specific config value
        mngr config get commands.create.provider
    """)
    expect(e2e.run("mngr config get commands.create.provider", comment="get a specific config value")).to_succeed()


@pytest.mark.release
def test_config_set(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # set a config value (at the default scope)
        mngr config set commands.create.provider modal
    """)
    expect(e2e.run("mngr config set commands.create.provider modal", comment="set a config value")).to_succeed()


@pytest.mark.release
def test_config_set_scope(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # set a config value at a specific scope
        mngr config set headless true --scope user
    """)
    expect(e2e.run("mngr config set headless true --scope user", comment="set at a specific scope")).to_succeed()


@pytest.mark.release
def test_config_unset(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # unset a config value
        mngr config unset commands.create.provider
    """)
    expect(e2e.run("mngr config unset commands.create.provider", comment="unset a config value")).to_succeed()


@pytest.mark.release
def test_config_edit(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # open the config file in your editor
        mngr config edit
    """)
    # `mngr config edit` spawns $EDITOR; force it to /bin/true so the command
    # returns immediately with success.
    expect(e2e.run("EDITOR=/bin/true mngr config edit", comment="open the config file in your editor")).to_succeed()


@pytest.mark.release
def test_config_edit_scope(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # open a specific scope's config file
        mngr config edit --scope project
    """)
    expect(
        e2e.run(
            "EDITOR=/bin/true mngr config edit --scope project",
            comment="open a specific scope's config file",
        )
    ).to_succeed()


@pytest.mark.release
def test_config_path(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show the path to the config file
        mngr config path
    """)
    expect(e2e.run("mngr config path", comment="show the path to the config file")).to_succeed()


@pytest.mark.release
def test_config_path_scope(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show the path to a specific scope's config file
        mngr config path --scope user
    """)
    expect(
        e2e.run("mngr config path --scope user", comment="show the path to a specific scope's config file")
    ).to_succeed()
