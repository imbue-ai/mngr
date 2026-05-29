"""Tests for the agent-type tutorial blocks (command type, codex, custom types)."""

import json
from pathlib import Path

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
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

    # Verify the command actually became a running agent of type `command`.
    list_result = e2e.run("mngr list --format json", comment="verify the command agent is running")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-server"]
    assert len(matching) == 1, f"Expected exactly one 'my-server' agent, got: {agents}"
    assert matching[0]["type"] == "command"
    assert matching[0]["state"] in ("RUNNING", "WAITING")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_create_command_custom_script(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run a custom script as an agent
        mngr create my-task --type command -- my-tool --some-flag
    """)
    # my-tool isn't installed; substitute a uniquely-named sleep so the agent
    # process keeps running while still demonstrating that the custom command
    # (and its arguments after `--`) get forwarded verbatim into the agent.
    custom_command = "sleep 100951"
    expect(
        e2e.run(
            f"mngr create my-task --type command --no-ensure-clean --no-connect -- {custom_command}",
            comment="run a custom script as an agent (substituted with sleep)",
            timeout=45.0,
        )
    ).to_succeed()

    # The whole point of the tutorial block is that the custom command is run
    # as the agent, so verify the forwarded command is recorded in the agent's
    # metadata rather than just trusting the exit code.
    list_result = e2e.run(
        "mngr list --format json",
        comment="verify the custom command was forwarded to the agent",
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [agent for agent in agents if agent["name"] == "my-task"]
    assert len(matching) == 1, f"expected exactly one my-task agent, got {matching}"
    assert matching[0]["command"] == custom_command

    # And confirm the custom command is actually running inside the agent.
    ps_result = e2e.run(
        "mngr exec my-task 'ps aux'",
        comment="verify the custom command is running inside the agent",
    )
    expect(ps_result).to_succeed()
    expect(ps_result.stdout).to_contain(custom_command)


@pytest.mark.release
@pytest.mark.timeout(120)
def test_plugin_list_active_to_see_types(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # agent types are provided by plugins -- see MANAGING PLUGINS above
        # to see which agent types are available:
        mngr plugin list --active
    """)
    result = e2e.run("mngr plugin list --active", comment="see which agent types are available")
    expect(result).to_succeed()
    # The point of the command is to surface the agent types provided by
    # plugins. The built-in `claude` and `command` agent types are always
    # present, so they must appear in the active plugin list.
    expect(result.stdout).to_contain("claude")
    expect(result.stdout).to_contain("command")

    # `--active` shows only enabled plugins. Parse the JSON form and verify
    # that every listed plugin really is enabled (the `enabled` field is
    # emitted as the lowercased string "true"/"false").
    json_result = e2e.run("mngr plugin list --active --format json", comment="active plugins as JSON")
    expect(json_result).to_succeed()
    plugins = json.loads(json_result.stdout)["plugins"]
    assert plugins, "expected at least one active plugin"
    disabled = [p["name"] for p in plugins if p["enabled"] != "true"]
    assert not disabled, f"--active must only list enabled plugins, but these were disabled: {disabled}"


@pytest.mark.release
@pytest.mark.timeout(120)
def test_plugin_list_active_excludes_disabled(e2e: E2eSession) -> None:
    # Shares the MANAGING PLUGINS tutorial block: verifies that `--active`
    # actually filters out a plugin once it has been disabled, while the
    # unfiltered list still reports it (marked as disabled).
    e2e.write_tutorial_block("""
        # agent types are provided by plugins -- see MANAGING PLUGINS above
        # to see which agent types are available:
        mngr plugin list --active
    """)
    # The notifications plugin is purely additive, so disabling it is a safe
    # way to exercise the filter. Use local scope so the change lands in the
    # pytest-opted-in settings.local.toml.
    expect(e2e.run("mngr plugin disable notifications --scope local", comment="disable a plugin")).to_succeed()

    active = e2e.run("mngr plugin list --active --format json", comment="active plugins exclude the disabled one")
    expect(active).to_succeed()
    active_names = {p["name"] for p in json.loads(active.stdout)["plugins"]}
    assert "notifications" not in active_names, f"disabled plugin must not appear in --active list: {active_names}"

    full = e2e.run("mngr plugin list --format json", comment="full list still includes the disabled plugin")
    expect(full).to_succeed()
    full_plugins = {p["name"]: p for p in json.loads(full.stdout)["plugins"]}
    assert "notifications" in full_plugins, "disabled plugin must still appear in the unfiltered list"
    assert full_plugins["notifications"]["enabled"] == "false", "disabled plugin must be reported as not enabled"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_create_codex_positional(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can specify the agent type as the second positional argument to create:
        mngr create my-task codex
    """)
    # Configure codex via the local scope so the setting lands in
    # settings.local.toml, which the e2e fixture opts into pytest with
    # is_allowed_in_pytest = true. The default project scope writes a fresh
    # settings.toml that lacks that opt-in, so `mngr create` would reject it.
    expect(
        e2e.run(
            "mngr config set --scope local agent_types.codex.command 'sleep 100952'",
            comment="configure codex command for test environment",
        )
    ).to_succeed()
    create_result = e2e.run(
        "mngr create my-task codex --no-ensure-clean --no-connect",
        comment="agent type as second positional argument",
    )
    expect(create_result).to_succeed()
    # The agent must actually be created with the codex type, not just exit 0.
    # Render name+type so we verify the positional argument selected the codex
    # type rather than merely that some agent exists.
    list_result = e2e.run(
        "mngr list --format '{name} {type}'",
        comment="verify the agent was created with the codex type",
        timeout=120.0,
    )
    expect(list_result).to_succeed()
    assert "my-task codex" in list_result.stdout, (
        f"expected agent 'my-task' of type 'codex' in list output:\n{list_result.stdout}"
    )


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_codex_explicit_type(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or by specifying it explicitly
        mngr create my-task --type codex
    """)
    # Use --scope user so the codex command lands in the profile's settings.toml
    # (which already sets is_allowed_in_pytest = true). The default project scope
    # would create a fresh settings.toml without that flag, and the subsequent
    # `mngr create` would refuse to load it under pytest.
    expect(
        e2e.run(
            "mngr config set agent_types.codex.command 'sleep 100953' --scope user",
            comment="configure codex command for test environment",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create my-task --type codex --no-ensure-clean --no-connect",
            comment="agent type via --type",
        )
    ).to_succeed()

    # Verify the --type flag actually produced a codex agent (not just exit 0).
    list_result = e2e.run("mngr list --format json", comment="verify the agent is a running codex agent")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [agent for agent in agents if agent["name"] == "my-task"]
    assert len(matching) == 1, f"expected exactly one 'my-task' agent, got: {agents}"
    assert matching[0]["type"] == "codex", f"expected codex agent, got type: {matching[0]['type']}"
    assert matching[0]["state"] in ("RUNNING", "WAITING"), f"unexpected agent state: {matching[0]['state']}"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_create_custom_yolo_agent_type(e2e: E2eSession, project_config_dir: Path) -> None:
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
    # `mngr config edit` spawns $EDITOR on the project settings.toml; force it to
    # /bin/true so the command returns immediately. This creates the file from a
    # template (without `is_allowed_in_pytest = true`).
    expect(
        e2e.run(
            "EDITOR=/bin/true mngr config edit --scope project",
            comment="open project config",
        )
    ).to_succeed()
    # Simulate the editor edit the tutorial describes: define the custom `yolo`
    # type by writing the `[agent_types.yolo]` block into the project config.
    # The tutorial inherits from `claude` with `--dangerously-skip-permissions`;
    # substitute the built-in `command` parent with a pinned `sleep` command so
    # the test doesn't depend on claude being installed. A custom type needs both
    # `parent_type` and (for the command parent) a `command`. The
    # `is_allowed_in_pytest` opt-in is required because every config file loaded
    # during a pytest run must opt in (the conftest only opts in settings.local.toml).
    project_settings = project_config_dir / "settings.toml"
    project_settings.write_text(
        'is_allowed_in_pytest = true\n\n[agent_types.yolo]\nparent_type = "command"\ncommand = "sleep 100954"\n'
    )
    expect(
        e2e.run(
            "mngr create my-task yolo --no-ensure-clean --no-connect",
            comment="create custom yolo agent",
        )
    ).to_succeed()
    # Verify the concrete effect, not just that create exited 0: the agent must
    # actually be tracked as the custom `yolo` type. Scope the listing to the
    # local provider so it doesn't depend on remote (Modal) discovery.
    listing = e2e.run(
        "mngr list --provider local --format json",
        comment="confirm the agent is tracked as the custom yolo type",
    )
    expect(listing).to_succeed()
    agents = json.loads(listing.stdout)["agents"]
    my_task = next((agent for agent in agents if agent["name"] == "my-task"), None)
    assert my_task is not None, f"expected an agent named my-task, got: {[a['name'] for a in agents]}"
    # The agent must be recorded as the custom `yolo` type and inherit the
    # command we configured on it -- this is what proves the custom-type
    # definition actually took effect (rather than create silently falling back).
    assert my_task["type"] == "yolo", f"expected type yolo, got {my_task['type']!r}"
    assert my_task["command"] == "sleep 100954", f"expected the configured command, got {my_task['command']!r}"
