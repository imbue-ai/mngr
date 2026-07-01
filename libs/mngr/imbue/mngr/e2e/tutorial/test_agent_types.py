"""Tests for the agent-type tutorial blocks (command type, codex, custom types)."""

import json
from pathlib import Path

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.tmux
# Creating a command agent plus the subsequent `mngr exec` (which enumerates
# every configured provider to locate the agent) and agent startup can exceed
# the default 10s per-test timeout when a remote provider (e.g. Docker) is
# unreachable and the client waits on a connection. Allow extra headroom so the
# verification below is robust across environments.
@pytest.mark.timeout(120)
def test_create_command_python_http(e2e: E2eSession) -> None:
    """Tutorial block:
        # mngr supports multiple agent types out of the box (claude, codex, etc.)
        # you can also run any shell command as an "agent" using the built-in `command` type:
        mngr create my-server --type command -- python -m http.server 8080

    Scope: `mngr create --type command -- <cmd>` runs an arbitrary shell command
    as an agent. The created agent appears in `mngr list --format json` with
    type == command and its command set to exactly the forwarded `<cmd>`, and
    that command is actually running as a process inside the agent (verified via
    `mngr exec`). The real `python -m http.server` command is substituted with
    `sleep` so the test does not bind a port.
    """
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
    # that the create command exited 0). The command agent lives on the local
    # provider, so scope discovery to it (--provider restricts which providers
    # are queried): a full fan-out would exit non-zero if an unrelated enabled
    # provider (e.g. aws without credentials, or an unreachable Docker daemon) is
    # inaccessible, which is orthogonal to what this test verifies.
    list_result = e2e.run(
        "mngr list --provider local --format json", comment="verify the command agent was created"
    )
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
# Agent creation (provisioning, ttyd install attempt, etc.) can exceed the
# default 10s per-test timeout, so allow extra headroom. The command agent is
# created on the local provider, and verification scopes `mngr list` to
# `--provider local`, so this never queries (nor blocks/fails on) unreachable
# remote providers such as Docker/AWS.
@pytest.mark.timeout(120)
def test_create_command_custom_script(e2e: E2eSession) -> None:
    """Tutorial block:
        # run a custom script as an agent
        mngr create my-task --type command -- my-tool --some-flag

    Scope: a custom command passed after `--` is forwarded into a running
    `command`-type agent. The created agent appears in `mngr list --format json`
    with type == command, its command containing the forwarded command, and a
    running state (RUNNING/WAITING); the forwarded command is also live as a
    process inside the agent (verified via `mngr exec`). The unavailable
    `my-tool` is substituted with `sleep` so the agent process stays alive.
    """
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
    list_result = e2e.run(
        "mngr list --provider local --format json", comment="verify the custom command was forwarded"
    )
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
# `mngr plugin list` loads every installed plugin entry point (agent types,
# provider backends, command plugins, etc.); that discovery alone can approach
# or exceed the default 10s per-test timeout on a cold interpreter. Allow extra
# headroom so the verification below is robust across environments.
@pytest.mark.timeout(120)
def test_plugin_list_active_to_see_types(e2e: E2eSession) -> None:
    """Tutorial block:
        # agent types are provided by plugins -- see MANAGING PLUGINS above
        # to see which agent types are available:
        mngr plugin list --active

    Scope: `mngr plugin list --active` succeeds and lists the available
    agent-type plugins. The built-in agent types (claude, codex, command) each
    appear as their own plugin entry in the `--format json` output and are
    reported enabled under `--active` -- the JSON check guards against weak
    substring matches on the human table (e.g. "claude" inside "claude_usage").
    """
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


@pytest.mark.release
@pytest.mark.tmux
# Agent creation (provisioning, ttyd install attempt) can exceed the default 10s
# per-test timeout, so allow extra headroom. Verification scopes `mngr list` to
# the local provider (`--provider local`), so this never queries Modal.
@pytest.mark.timeout(120)
def test_create_codex_positional(e2e: E2eSession) -> None:
    """Tutorial block:
        # you can specify the agent type as the second positional argument to create:
        mngr create my-task codex

    Scope: the agent type can be given as the second positional argument to
    `mngr create`. With `codex` passed positionally, the created agent appears in
    `mngr list --format json` with type == codex (not a default fallback),
    confirming positional-argument type resolution. Flags (-y, --no-auto-start)
    let the type resolve without a codex binary or auth on this host.
    """
    # codex is a real agent-type plugin now (not a command-driven stub), so it
    # can't be faked with a `command` override. Create it without launching
    # (--no-auto-start) and auto-approve workspace trust (-y), which exercises the
    # positional-argument type resolution without needing a codex binary or auth
    # on this host. Provisioning would otherwise npm-install the codex CLI, so
    # disable that with `-S agent_types.codex.check_installation=false`. The real
    # codex run is covered by the mngr_codex release test.
    expect(
        e2e.run(
            "mngr create my-task codex -y --no-auto-start --no-ensure-clean --no-connect "
            "-S agent_types.codex.check_installation=false",
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


@pytest.mark.release
@pytest.mark.tmux
# Agent creation (provisioning, ttyd install attempt) can exceed the default 10s
# per-test timeout, so allow extra headroom. Verification scopes `mngr list` to
# the local provider (`--provider local`), so this never queries Modal. (No
# @pytest.mark.rsync: a local codex create runs the agent in the work dir
# directly, so it never copies a workspace via rsync.)
@pytest.mark.timeout(120)
def test_create_codex_explicit_type(e2e: E2eSession) -> None:
    """Tutorial block:
        # or by specifying it explicitly
        mngr create my-task --type codex

    Scope: the agent type can be given explicitly via `--type` (counterpart to
    test_create_codex_positional). With `--type codex`, the created agent appears
    in `mngr list --format json` with type == codex (not a silent default
    fallback), confirming `--type` resolves the agent type. Flags (-y,
    --no-auto-start) let it resolve without a codex binary or auth on this host.
    """
    # codex is a real agent-type plugin now (not a command-driven stub), so it
    # can't be faked with a `command` override. Create it without launching
    # (--no-auto-start) and auto-approve workspace trust (-y): this verifies
    # `--type codex` resolves to the codex agent type without needing a codex
    # binary or auth. The real codex run is covered by the mngr_codex release test.
    #
    # Disable the codex install check so provisioning never shells out to
    # `npm i -g @openai/codex` (which -y would otherwise auto-approve). This is
    # what lets the test resolve the type "without a codex binary" on a host that
    # has neither codex nor npm. Written to the local scope, whose
    # settings.local.toml already carries is_allowed_in_pytest = true so the
    # config is loaded by the subsequent create (the default project scope would
    # create a settings.toml that the pytest config guard rejects).
    expect(
        e2e.run(
            "mngr config set --scope local agent_types.codex.check_installation false",
            comment="skip the codex install check so create needs no codex binary",
        )
    ).to_succeed()
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
def test_create_custom_yolo_agent_type(e2e: E2eSession, project_config_dir: Path) -> None:
    """Tutorial block:
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

    Scope: a custom agent type defined in project config (an `[agent_types.<name>]`
    section with a `parent_type`) can then be created via `mngr create <task>
    <type>`. After defining a `yolo` type and creating an agent with it, the
    agent appears in `mngr list --format json` with type == yolo (the custom
    type, not a fallback) and a running state, and the parent's command runs as a
    live process inside it (verified via `mngr exec`). To avoid needing claude
    installed, the test parents `yolo` on the built-in `command` type with a
    `sleep` command instead of the tutorial's claude/cli_args.
    """
    # The tutorial opens the project config in $EDITOR and adds an
    # `[agent_types.yolo]` section by hand. Drive `config edit --scope project`
    # with a fake editor that writes that section directly into the project
    # config file it is handed, mirroring the tutorial's manual edit. The
    # tutorial parents yolo on "claude" with cli_args; to avoid needing claude
    # installed we parent on the built-in `command` type with a `sleep` command
    # instead. The section also carries `is_allowed_in_pytest = true` so the
    # freshly-created project config file opts into the pytest run (required for
    # the subsequent `mngr create` to load it), matching how the e2e fixture opts
    # in every other config file. The editor script lives in the gitignored
    # project config dir so it does not dirty the working tree ahead of create.
    fake_editor = project_config_dir / "add_yolo_type.sh"
    fake_editor.write_text(
        "#!/bin/sh\n"
        'cat >> "$1" <<\'EOF\'\n'
        "\n"
        "is_allowed_in_pytest = true\n"
        "\n"
        "[agent_types.yolo]\n"
        'parent_type = "command"\n'
        'command = "sleep 100954"\n'
        "EOF\n"
    )
    fake_editor.chmod(0o755)
    expect(
        e2e.run(
            f"EDITOR={fake_editor} mngr config edit --scope project",
            comment="define the custom yolo agent type in the project config",
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
