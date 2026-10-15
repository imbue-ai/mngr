"""Tests for the agent-type tutorial blocks (command type, codex, custom types)."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect

# A codex create installs the codex CLI when the host has none.
_CODEX_CREATE_TIMEOUT = 120.0
# Computing a just-launched codex agent's live state is occasionally slow.
_CODEX_LIST_TIMEOUT = 60.0


@pytest.mark.release
@pytest.mark.tmux
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
    # that the create command exited 0). Scope discovery to the local provider
    # (where the command agent was created): --provider restricts which providers
    # are queried, so the check stays fast and never fails on an unrelated remote
    # provider that is merely enabled-by-default but unreachable in this
    # environment (e.g. aws without credentials, which exits provider-inaccessible).
    list_result = e2e.run("mngr list --provider local --format json", comment="verify the command agent was created")
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
    # exited 0. Scope discovery to the local provider so the check stays fast and
    # never queries remote providers (the agent was created on the local
    # provider); an enabled-but-unreachable cloud backend without credentials
    # (e.g. aws) would otherwise make the listing exit non-zero (even though the
    # local agents are still emitted), failing this to_succeed() check.
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
# Verification scopes `mngr list` to the local provider (`--provider local`), so
# this never queries Modal. The source repo is clean, so the worktree transfer
# finds no untracked or modified files to copy and never invokes rsync; this test
# is therefore not marked @pytest.mark.rsync.
# Flaky: the read-back below occasionally overshoots even _CODEX_LIST_TIMEOUT.
@pytest.mark.flaky
def test_create_codex_positional(e2e: E2eSession) -> None:
    """Tutorial block:
        # you can specify the agent type as the second positional argument to create:
        mngr create my-task codex

    Scope: the agent type can be given as the second positional argument to
    `mngr create`. With `codex` passed positionally, the created agent appears in
    `mngr list --format json` with type == codex (not a default fallback),
    confirming positional-argument type resolution. The real codex conversation is
    covered by the mngr_codex release test.
    """
    # `-y` is load-bearing: this create installs the codex CLI on a host that has
    # none, and a local install only proceeds under auto-approve.
    expect(
        e2e.run(
            "mngr create my-task codex -y --no-ensure-clean --no-connect",
            comment="agent type as second positional argument",
            timeout=_CODEX_CREATE_TIMEOUT,
        )
    ).to_succeed()
    # Verify the positional argument was interpreted as the agent type: the
    # created agent must actually be of type codex, not merely exit 0.
    list_result = e2e.run(
        "mngr list --provider local --format json",
        comment="verify the agent was created with the codex type",
        timeout=_CODEX_LIST_TIMEOUT,
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [agent for agent in agents if agent["name"] == "my-task"]
    assert len(matching) == 1, f"expected exactly one 'my-task' agent, got: {agents}"
    assert matching[0]["type"] == "codex", f"expected agent type 'codex', got: {matching[0]}"


@pytest.mark.release
@pytest.mark.tmux
# Verification scopes `mngr list` to the local provider (`--provider local`), so
# this never queries Modal.
def test_create_codex_explicit_type(e2e: E2eSession) -> None:
    """Tutorial block:
        # or by specifying it explicitly
        mngr create my-task --type codex

    Scope: the agent type can be given explicitly via `--type` (counterpart to
    test_create_codex_positional). With `--type codex`, the created agent appears
    in `mngr list --format json` with type == codex (not a silent default
    fallback), confirming `--type` resolves the agent type. The real codex
    conversation is covered by the mngr_codex release test.
    """
    # `-y` is load-bearing: this create installs the codex CLI on a host that has
    # none, and a local install only proceeds under auto-approve.
    expect(
        e2e.run(
            "mngr create my-task --type codex -y --no-connect",
            comment="agent type via --type",
            timeout=_CODEX_CREATE_TIMEOUT,
        )
    ).to_succeed()

    # Verify the effect of --type codex: the agent exists and was created with the
    # codex type (not silently falling back to a default), confirming the type
    # resolves. Scope discovery to the local provider so the check stays fast and
    # never queries Modal (--provider restricts which providers are queried, unlike
    # the --local result filter which still fans out to remote providers).
    list_result = e2e.run(
        "mngr list --provider local --format json",
        comment="verify the codex agent was created",
        timeout=_CODEX_LIST_TIMEOUT,
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [agent for agent in agents if agent["name"] == "my-task"]
    assert len(matching) == 1, f"expected exactly one 'my-task' agent, got {agents}"
    assert matching[0]["type"] == "codex", f"expected type 'codex', got {matching[0]['type']}"


@pytest.mark.release
@pytest.mark.tmux
# This test stays on the local provider throughout (the create defaults to local
# and the verification below scopes `mngr list` to --provider local), so it is
# intentionally not marked @pytest.mark.modal.
def test_create_custom_yolo_agent_type(e2e: E2eSession) -> None:
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
    expect(
        e2e.run(
            "EDITOR=true mngr config edit --scope project",
            comment="open project config",
        )
    ).to_succeed()
    # Simulate the in-editor edit by appending the yolo type to the project
    # config that `config edit` just created. is_allowed_in_pytest opts this
    # newly created project config into the pytest run (it defaults to False, so
    # without it the config loader would refuse to load it -- both here and on
    # the `mngr create` below); a real user editing the file outside pytest
    # never needs it. The tutorial parents yolo on "claude" with cli_args; we
    # parent on the built-in `command` type with a `sleep` command instead so
    # the test doesn't need claude installed.
    project_cfg = ".$MNGR_ROOT_NAME/settings.toml"
    expect(
        e2e.run(
            f"echo '' >> {project_cfg}"
            f" && echo 'is_allowed_in_pytest = true' >> {project_cfg}"
            f" && echo '[agent_types.yolo]' >> {project_cfg}"
            f" && echo 'parent_type = \"command\"' >> {project_cfg}"
            f" && echo 'command = \"sleep 100954\"' >> {project_cfg}",
            comment="in the editor, define the yolo type (command parent + sleep substituted for the local test)",
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
