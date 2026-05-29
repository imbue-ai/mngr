"""Tests for the create-time environment-variable tutorial blocks."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_create_with_env_vars(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # set environment variables for the agent at creation time
        mngr create my-task --env DEBUG=true --env LOG_LEVEL=verbose
    """)
    expect(
        e2e.run(
            "mngr create my-task --env DEBUG=true --env LOG_LEVEL=verbose --type command --no-ensure-clean --no-connect -- sleep 100960",
            comment="set environment variables at creation time",
            timeout=110.0,
        )
    ).to_succeed()
    # Verify the agent actually received the environment variables by reading
    # them back from inside the agent's own environment (mngr exec sources the
    # agent env file before running the command).
    debug_result = e2e.run(
        'mngr exec my-task "printenv DEBUG"',
        comment="confirm DEBUG was set on the agent",
        timeout=45.0,
    )
    expect(debug_result).to_succeed()
    expect(debug_result.stdout).to_contain("true")
    log_level_result = e2e.run(
        'mngr exec my-task "printenv LOG_LEVEL"',
        comment="confirm LOG_LEVEL was set on the agent",
        timeout=45.0,
    )
    expect(log_level_result).to_succeed()
    expect(log_level_result.stdout).to_contain("verbose")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_create_with_env_file(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # load environment variables from a file (recommended for sensitive values, eg, secrets/api keys/tokens/etc)
        mngr create my-task --env-file .env.agent
    """)
    # Write a small .env.agent file in the test cwd so --env-file resolves.
    expect(
        e2e.run(
            "echo 'FOO=bar' > .env.agent",
            comment="write a minimal .env.agent",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create my-task --env-file .env.agent --type command --no-ensure-clean --no-connect -- sleep 100961",
            comment="load environment variables from a file",
        )
    ).to_succeed()

    # Verify the variable from the file was actually loaded into the agent's
    # on-disk environment, not just that the create command succeeded.
    env_file_result = e2e.run(
        "cat $MNGR_HOST_DIR/agents/*/env",
        comment="Verify FOO from .env.agent was loaded into agent environment",
    )
    expect(env_file_result).to_succeed()
    expect(env_file_result.stdout).to_contain("FOO=bar")


@pytest.mark.release
def test_create_with_missing_env_file(e2e: E2eSession) -> None:
    # Unhappy path for the same tutorial block: pointing --env-file at a file
    # that does not exist must fail fast (at argument validation) with a clear
    # error, rather than silently creating an agent with no extra env vars.
    e2e.write_tutorial_block("""
        # load environment variables from a file (recommended for sensitive values, eg, secrets/api keys/tokens/etc)
        mngr create my-task --env-file .env.agent
    """)
    result = e2e.run(
        "mngr create my-task --env-file does-not-exist.env --type command --no-ensure-clean --no-connect -- sleep 100964",
        comment="--env-file pointing at a missing file should fail",
    )
    # click.Path(exists=True) rejects the missing file with a usage error (exit 2).
    expect(result).to_have_exit_code(2)
    expect(result.stderr).to_contain("does-not-exist.env")
    expect(result.stderr).to_contain("does not exist")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_with_pass_env(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # forward an environment variable from your current shell
        export ANTHROPIC_API_KEY=sk-ant-...
        mngr create my-task --pass-env ANTHROPIC_API_KEY
    """)
    expect(
        e2e.run(
            "export ANTHROPIC_API_KEY=sk-ant-test && mngr create my-task --pass-env ANTHROPIC_API_KEY --type command --no-ensure-clean --no-connect -- sleep 100962",
            comment="forward an environment variable from your current shell",
        )
    ).to_succeed()
    # Verify the variable was actually forwarded into the agent's environment
    # (rather than merely accepting the flag): read it back from inside the agent.
    exec_result = e2e.run(
        "mngr exec my-task 'printenv ANTHROPIC_API_KEY'",
        comment="confirm the forwarded variable is visible inside the agent",
    )
    expect(exec_result).to_succeed()
    # `mngr exec` prints the command's stdout followed by a status footer line,
    # so look for the printenv output on its own line rather than matching exactly.
    printed_values = [line.strip() for line in exec_result.stdout.splitlines()]
    assert "sk-ant-test" in printed_values, (
        f"expected forwarded ANTHROPIC_API_KEY inside the agent, got stdout={exec_result.stdout!r}"
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_create_with_pass_host_env(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # set host-level environment variables (for all agents on the host, not just that particular agent process)
        mngr create my-task --provider modal --pass-host-env MODAL_TOKEN_ID --pass-host-env MODAL_TOKEN_SECRET
    """)
    # The isolated test profile has no default agent type, so pin --type
    # command (matching the other env-var tests) and give it a trivial command
    # to run. --pass-host-env still applies regardless of agent type.
    expect(
        e2e.run(
            "mngr create my-task --provider modal --pass-host-env MODAL_TOKEN_ID --pass-host-env MODAL_TOKEN_SECRET --type command --no-connect --no-ensure-clean -- sleep 100964",
            comment="set host-level environment variables",
            timeout=150.0,
        )
    ).to_succeed()
    # Verify the host-level env var actually reached the remote host. mngr exec
    # sources the host env file before running the command, so a forwarded
    # MODAL_TOKEN_ID must be present: printenv exits non-zero when the variable
    # is unset, and a non-empty value confirms the host's value was forwarded
    # (not just declared as an empty placeholder).
    exec_result = e2e.run(
        "mngr exec my-task 'printenv MODAL_TOKEN_ID'",
        comment="confirm the host-level env var reached the remote host",
        timeout=60.0,
    )
    expect(exec_result).to_succeed()
    expect(exec_result.stdout).to_match(r"\S")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_control_mngr_via_env(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # control mngr itself via environment variables. All config options can be set this way, use double-underscore ("__")
        # in order to index into the nested config structure. For example, to set the provider to "modal" for a create command:
        export MNGR__COMMANDS__CREATE__PROVIDER=modal
        mngr create my-task
    """)
    # We can't actually export modal here without paying the modal startup
    # cost; override the env var to "local" instead so the create stays cheap.
    # The e2e harness pre-sets commands.create.connect_command in the local
    # settings layer, so the higher-precedence MNGR__COMMANDS__CREATE__PROVIDER
    # env var would narrow (replace) that defaults map. Opt into the
    # assign-by-default behavior the same way the error message instructs real
    # users to; --no-connect means dropping the connect_command default is moot.
    expect(
        e2e.run(
            "export MNGR__ALLOW_SETTINGS_KEY_ASSIGNMENT_NARROWING=true && export MNGR__COMMANDS__CREATE__PROVIDER=local && mngr create my-task --type command --no-ensure-clean --no-connect -- sleep 100963",
            comment="control mngr via env var (local override for the test)",
            timeout=90.0,
        )
    ).to_succeed()
    # Verify the env var actually controlled the provider: the agent must have
    # landed on the "local" backend, not merely have been created.
    list_result = e2e.run(
        "mngr list --format json",
        comment="confirm the agent was created on the provider set by the env var",
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [agent for agent in agents if agent["name"] == "my-task"]
    assert len(matching) == 1, f"expected exactly one 'my-task' agent, got {agents}"
    assert matching[0]["host"]["provider_name"] == "local", (
        f"expected agent on the 'local' provider, got {matching[0]['host']['provider_name']}"
    )
