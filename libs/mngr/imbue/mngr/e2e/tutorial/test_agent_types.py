"""Tests for the agent-type tutorial blocks (command type, codex, custom types)."""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_command_python_http(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # mngr supports multiple agent types out of the box (claude, codex, etc.)
        # you can also run any shell command as an "agent" using the built-in `command` type:
        mngr create my-server --type command -- python -m http.server 8080
    """)
    # python -m http.server would bind a port; substitute `sleep` so the test
    # doesn't conflict with anything else on 8080.
    expect(
        e2e.run(
            "mngr create my-server --type command --no-ensure-clean --no-connect -- sleep 100950",
            comment="run any shell command as an agent (substituted with sleep)",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_command_custom_script(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run a custom script as an agent
        mngr create my-task --type command -- my-tool --some-flag
    """)
    # my-tool isn't installed; substitute echo so the agent process exits
    # cleanly while still demonstrating that custom commands get forwarded.
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean --no-connect -- sleep 100951",
            comment="run a custom script as an agent (substituted with sleep)",
        )
    ).to_succeed()


@pytest.mark.release
def test_plugin_list_active_to_see_types(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # agent types are provided by plugins -- see MANAGING PLUGINS above
        # to see which agent types are available:
        mngr plugin list --active
    """)
    expect(e2e.run("mngr plugin list --active", comment="see which agent types are available")).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_codex_positional(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can specify the agent type as the second positional argument to create:
        mngr create my-task codex
    """)
    expect(
        e2e.run(
            "mngr config set agent_types.codex.command 'sleep 100952'",
            comment="configure codex command for test environment",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create my-task codex --no-ensure-clean --no-connect",
            comment="agent type as second positional argument",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_codex_explicit_type(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or by specifying it explicitly
        mngr create my-task --type codex
    """)
    expect(
        e2e.run(
            "mngr config set agent_types.codex.command 'sleep 100953'",
            comment="configure codex command for test environment",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create my-task --type codex --no-ensure-clean --no-connect",
            comment="agent type via --type",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_custom_yolo_agent_type(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can also create your own custom agent types by defining them in a config:
        # here's how to set one up using the config command:
        mngr config edit --scope project
        # in the editor, add something like:
        #   [agent_types.yolo]
        #   parent_type = "claude"
        #   cli_args = "--dangerously-skip-permissions"
        # then you can create agents of that type:
        mngr create my-task yolo
        # you'll have to look at the agent config class for each agent type to know what config options are supported
    """)
    expect(
        e2e.run(
            "EDITOR=/bin/true mngr config edit --scope project",
            comment="open project config",
        )
    ).to_succeed()
    # Define the yolo agent type via config set so the create command resolves.
    # Use the built-in `command` parent so the test doesn't need claude installed.
    expect(
        e2e.run(
            "mngr config set agent_types.yolo.command 'sleep 100954'",
            comment="configure yolo command for test environment",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create my-task yolo --no-ensure-clean --no-connect",
            comment="create custom yolo agent",
        )
    ).to_succeed()
