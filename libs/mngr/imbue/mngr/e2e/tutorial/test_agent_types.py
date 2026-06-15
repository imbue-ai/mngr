"""Tests for the agent-type tutorial blocks (command type, codex, custom types)."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.tmux
# Creating a command agent and the subsequent `mngr list`/`mngr exec` calls
# enumerate every configured provider; that discovery (plus agent startup) can
# exceed the default 10s per-test timeout when a remote provider (e.g. Docker)
# is unreachable and the client waits on a connection. Allow extra headroom so
# the verification below is robust across environments.
@pytest.mark.timeout(120)
def test_create_command_python_http(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # mngr supports multiple agent types out of the box (claude, codex, etc.)
        # you can also run any shell command as an "agent" using the built-in `command` type:
        mngr create my-server --type command -- python -m http.server 8080
    """)
    # python -m http.server would bind a port; substitute `sleep` so the test
    # doesn't conflict with anything else on 8080. Use a locally-bound name
    # since we assert on the exact command string below.
    expected_command = "sleep 100950"
    expect(
        e2e.run(
            f"mngr create my-server --type command --no-connect -- {expected_command}",
            comment="run any shell command as an agent (substituted with sleep)",
        )
    ).to_succeed()

    # Verify the agent was actually created with the custom command (not just
    # that the create command exited 0).
    list_result = e2e.run("mngr list --format json", comment="verify the command agent was created")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-server"]
    assert len(matching) == 1, f"expected exactly one my-server agent, got {matching}"
    assert matching[0]["type"] == "command"
    assert matching[0]["command"] == expected_command

    # Verify the substituted command is actually running inside the agent.
    ps_result = e2e.run(
        "mngr exec my-server 'ps aux | grep sleep'",
        comment="verify the agent's command is running",
    )
    expect(ps_result).to_succeed()
    expect(ps_result.stdout).to_contain(expected_command)


@pytest.mark.release
@pytest.mark.tmux
# `mngr list` enumerates every configured provider; that discovery can exceed
# the default 10s per-test timeout when a remote provider (e.g. Docker) is
# unreachable and the client waits on a connection. Allow extra headroom so the
# verification below is robust across environments.
@pytest.mark.timeout(120)
def test_create_command_custom_script(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run a custom script as an agent
        mngr create my-task --type command -- my-tool --some-flag
    """)
    # my-tool isn't installed; substitute sleep so the agent process stays
    # alive while still demonstrating that custom commands get forwarded.
    expect(
        e2e.run(
            "mngr create my-task --type command --no-connect -- sleep 100951",
            comment="run a custom script as an agent (substituted with sleep)",
        )
    ).to_succeed()

    # Verify the custom command was actually forwarded to a running command
    # agent (the whole point of the tutorial block), not just that create
    # exited 0.
    list_result = e2e.run("mngr list --format json", comment="verify the custom command was forwarded")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly 1 agent named 'my-task', got {len(matching)}"
    agent = matching[0]
    assert agent["type"] == "command", f"Expected a command-type agent, got: {agent['type']}"
    assert "sleep 100951" in agent["command"], f"Custom command not forwarded; got: {agent['command']}"
    assert agent["state"] in ("RUNNING", "WAITING"), f"Expected a running agent, got state: {agent['state']}"

    # Confirm the forwarded command is not merely recorded in metadata but is
    # actually running as a process inside the agent.
    ps_result = e2e.run(
        "mngr exec my-task 'ps aux | grep sleep'",
        comment="verify the custom command is running inside the agent",
    )
    expect(ps_result).to_succeed()
    expect(ps_result.stdout).to_contain("sleep 100951")


@pytest.mark.release
def test_plugin_list_active_to_see_types(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # agent types are provided by plugins -- see MANAGING PLUGINS above
        # to see which agent types are available:
        mngr plugin list --active
    """)
    result = e2e.run("mngr plugin list --active", comment="see which agent types are available")
    expect(result).to_succeed()
    # The point of running this command is to discover the agent types provided
    # by plugins, so verify the built-in agent types this tutorial discusses
    # (claude, codex, command) actually show up in the listing.
    for agent_type in ("claude", "codex", "command"):
        expect(result.stdout).to_contain(agent_type)

    # Substring matches on the human table are weak: "claude" also occurs inside
    # "claude_usage"/"headless_claude" and "command" inside "headless_command",
    # so the loop above would pass even if the bare agent-type plugins were
    # absent. Re-run with JSON output and assert each agent type is present as
    # its own plugin entry, and that --active reports it enabled.
    json_result = e2e.run(
        "mngr plugin list --active --format json",
        comment="verify exact agent-type plugin entries are enabled",
    )
    expect(json_result).to_succeed()
    plugins_by_name = {p["name"]: p for p in json.loads(json_result.stdout)["plugins"]}
    for agent_type in ("claude", "codex", "command"):
        assert agent_type in plugins_by_name, f"expected a plugin named {agent_type!r}, got {sorted(plugins_by_name)}"
        assert plugins_by_name[agent_type]["enabled"] == "true", (
            f"expected {agent_type} to be enabled under --active, got {plugins_by_name[agent_type]}"
        )


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
# Agent creation (provisioning, rsync, ttyd install attempt) can exceed the
# default 10s per-test timeout, so allow extra headroom. Verification scopes
# `mngr list` to the local provider (`--provider local`), so this never queries
# Modal.
@pytest.mark.timeout(120)
def test_create_codex_positional(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can specify the agent type as the second positional argument to create:
        mngr create my-task codex
    """)
    # codex is a real agent-type plugin now (not a command-driven stub), so it
    # can't be faked with a `command` override. Create it without launching
    # (--no-auto-start) and auto-approve workspace trust (-y), which exercises the
    # positional-argument type resolution without needing a codex binary or auth
    # on this host. The real codex run is covered by the mngr_codex release test.
    expect(
        e2e.run(
            "mngr create my-task codex -y --no-auto-start --no-ensure-clean --no-connect",
            comment="agent type as second positional argument",
        )
    ).to_succeed()
    # Verify the positional argument was interpreted as the agent type: the
    # created agent must actually be of type codex, not merely exit 0.
    list_result = e2e.run(
        "mngr list --provider local --format json", comment="verify the agent was created with the codex type"
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [agent for agent in agents if agent["name"] == "my-task"]
    assert len(matching) == 1, f"expected exactly one 'my-task' agent, got: {agents}"
    assert matching[0]["type"] == "codex", f"expected agent type 'codex', got: {matching[0]}"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
# Agent creation (provisioning, rsync, ttyd install attempt) can exceed the
# default 10s per-test timeout, so allow extra headroom. Verification scopes
# `mngr list` to the local provider (`--provider local`), so this never queries
# Modal.
@pytest.mark.timeout(120)
def test_create_codex_explicit_type(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or by specifying it explicitly
        mngr create my-task --type codex
    """)
    # codex is a real agent-type plugin now (not a command-driven stub), so it
    # can't be faked with a `command` override. Create it without launching
    # (--no-auto-start) and auto-approve workspace trust (-y): this verifies
    # `--type codex` resolves to the codex agent type without needing a codex
    # binary or auth. The real codex run is covered by the mngr_codex release test.
    expect(
        e2e.run(
            "mngr create my-task --type codex -y --no-auto-start --no-ensure-clean --no-connect",
            comment="agent type via --type",
        )
    ).to_succeed()

    # Verify the effect of --type codex: the agent exists and was created with the
    # codex type (not silently falling back to a default), confirming the type
    # resolves. Scope discovery to the local provider so the check stays fast and
    # never queries Modal (--provider restricts which providers are queried, unlike
    # the --local result filter which still fans out to remote providers).
    list_result = e2e.run("mngr list --provider local --format json", comment="verify the codex agent was created")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [agent for agent in agents if agent["name"] == "my-task"]
    assert len(matching) == 1, f"expected exactly one 'my-task' agent, got {agents}"
    assert matching[0]["type"] == "codex", f"expected type 'codex', got {matching[0]['type']}"


@pytest.mark.release
@pytest.mark.tmux
# Agent creation (provisioning, ttyd install attempt, etc.) can exceed the
# default 10s per-test timeout, so allow extra headroom. This test stays on the
# local provider throughout (the create defaults to local and the verification
# below scopes `mngr list` to --provider local), so it is intentionally not
# marked @pytest.mark.modal.
@pytest.mark.timeout(120)
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
    # The tutorial uses parent_type = "claude"; we use the built-in `command`
    # parent instead so the test doesn't need claude installed, and give it a
    # `command` to run (mirroring the tutorial's cli_args override on the parent).
    expect(
        e2e.run(
            "mngr config set agent_types.yolo.parent_type command",
            comment="point the custom yolo type at the built-in command parent",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr config set agent_types.yolo.command 'sleep 100954'",
            comment="configure yolo command for test environment",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create my-task yolo --no-connect",
            comment="create custom yolo agent",
        )
    ).to_succeed()
    # The custom type really resolved and produced a running agent (an unknown
    # type would have failed the create above). Verify the concrete effect, not
    # just that create exited 0: the agent must be recorded with the *custom*
    # `yolo` type (not a fallback), and be running. Scope discovery to the local
    # provider so the check stays fast and never queries remote providers (the
    # agent was created on the local provider).
    list_result = e2e.run("mngr list --provider local --format json", comment="confirm the yolo agent is running")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [agent for agent in agents if agent["name"] == "my-task"]
    assert len(matching) == 1, f"expected exactly one 'my-task' agent, got {agents}"
    assert matching[0]["type"] == "yolo", f"expected custom type 'yolo', got {matching[0]['type']}"
    assert matching[0]["state"] in ("RUNNING", "WAITING"), f"unexpected agent state: {matching[0]['state']}"

    # The yolo type inherits the built-in `command` parent, so creating it should
    # have launched the configured command inside the agent. Confirm that process
    # is actually running (the whole point of a command-parented custom type).
    ps_result = e2e.run(
        "mngr exec my-task 'ps aux | grep sleep'",
        comment="verify the custom type's command is running",
    )
    expect(ps_result).to_succeed()
    expect(ps_result.stdout).to_contain("sleep 100954")
