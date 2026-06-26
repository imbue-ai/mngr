"""Tests for the create-time environment-variable tutorial blocks."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_create_with_env_vars(e2e: E2eSession) -> None:
    """Tutorial block:
        # set environment variables for the agent at creation time
        mngr create my-task --env DEBUG=true --env LOG_LEVEL=verbose

    Scope: each `--env KEY=VALUE` sets that variable in the agent's environment.
    The create succeeds and `printenv DEBUG LOG_LEVEL` exec'd inside the agent
    returns the exact values passed (true, verbose) -- proving both --env flags
    actually landed, not merely that create exited 0.
    """
    expect(
        e2e.run(
            "mngr create my-task --env DEBUG=true --env LOG_LEVEL=verbose --type command --no-ensure-clean --no-connect -- sleep 100960",
            comment="set environment variables at creation time",
        )
    ).to_succeed()
    # Verify the variables actually landed in the agent's environment, not just
    # that the create succeeded: exec printenv inside the agent and check the values.
    env_result = e2e.run(
        "mngr exec my-task 'printenv DEBUG LOG_LEVEL'",
        comment="confirm the agent received the env vars",
    )
    expect(env_result).to_succeed()
    expect(env_result.stdout).to_contain("true")
    expect(env_result.stdout).to_contain("verbose")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_create_with_env_file(e2e: E2eSession) -> None:
    """Tutorial block:
        # load environment variables from a file (recommended for sensitive values, eg, secrets/api keys/tokens/etc)
        mngr create my-task --env-file .env.agent

    Scope: `--env-file` reads variables from the named file and loads them into
    the agent's environment. After creating with a .env.agent containing
    FOO=bar, the agent's on-disk env file contains FOO=bar -- proving the file
    was actually parsed and honored, not silently ignored.
    """
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

    # Verify the file's variable was actually loaded into the agent's
    # environment on disk -- a clean exit alone does not prove --env-file
    # was honored.
    env_file_result = e2e.run(
        "cat $MNGR_HOST_DIR/agents/*/env",
        comment="verify the env-file variable was loaded into the agent environment",
    )
    expect(env_file_result).to_succeed()
    expect(env_file_result.stdout).to_contain("FOO=bar")


@pytest.mark.release
def test_create_with_missing_env_file_is_rejected(e2e: E2eSession) -> None:
    """Tutorial block:
        # load environment variables from a file (recommended for sensitive values, eg, secrets/api keys/tokens/etc)
        mngr create my-task --env-file .env.agent

    Scope: the unhappy path of the same `--env-file` block. Pointing
    ``--env-file`` at a file that does not exist is rejected up front (the option
    is declared with ``click.Path(exists=True)``): create fails, stderr names
    both --env-file and the missing .env.agent path, and no agent is created --
    proving the flag genuinely resolves the path rather than ignoring it.
    """
    # Deliberately do NOT create .env.agent, so the path cannot resolve.
    result = e2e.run(
        "mngr create my-task --env-file .env.agent --type command --no-ensure-clean --no-connect -- sleep 100965",
        comment="reject --env-file pointing at a nonexistent file",
    )
    expect(result).to_fail()
    # The error must name the offending option and the missing path, not be a
    # generic create failure.
    expect(result.stderr).to_contain("--env-file")
    expect(result.stderr).to_contain(".env.agent")
    # The agent must not have been created when the env file is missing.
    list_result = e2e.run("mngr list --provider local --format json", comment="confirm no agent was created")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    assert not any(a["name"] == "my-task" for a in agents), f"Expected no 'my-task' agent, got: {agents}"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_create_with_pass_env(e2e: E2eSession) -> None:
    """Tutorial block:
        # forward an environment variable from your current shell
        export ANTHROPIC_API_KEY=sk-ant-...
        mngr create my-task --pass-env ANTHROPIC_API_KEY

    Scope: `--pass-env NAME` forwards the named variable's value from the
    current shell into the agent's environment (without restating the value on
    the command line). After exporting ANTHROPIC_API_KEY in the shell, create
    succeeds and `printenv ANTHROPIC_API_KEY` inside the agent returns that
    exact value -- proving the value was forwarded, not merely accepted.
    """
    expect(
        e2e.run(
            "export ANTHROPIC_API_KEY=sk-ant-test && mngr create my-task --pass-env ANTHROPIC_API_KEY --type command --no-ensure-clean --no-connect -- sleep 100962",
            comment="forward an environment variable from your current shell",
        )
    ).to_succeed()

    # Verify the variable was actually forwarded into the agent's environment,
    # not merely accepted by the create command. `mngr exec` sources the agent's
    # env file, so printenv inside the agent reflects the value passed via
    # --pass-env from the parent shell.
    env_result = e2e.run(
        "mngr exec my-task 'printenv ANTHROPIC_API_KEY'",
        comment="confirm the forwarded variable is present in the agent",
        timeout=120.0,
    )
    expect(env_result).to_succeed()
    expect(env_result.stdout).to_contain("sk-ant-test")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_create_with_pass_env_skips_unset_var(e2e: E2eSession) -> None:
    """Tutorial block:
        # forward an environment variable from your current shell
        export ANTHROPIC_API_KEY=sk-ant-...
        mngr create my-task --pass-env ANTHROPIC_API_KEY

    Scope: the unhappy path of the same `--pass-env` block. Because --pass-env
    forwards a variable *from the current shell*, naming one that is not set in
    the shell must not break create -- it is simply skipped (see
    ``resolve_env_vars``). Create succeeds and the variable is absent from the
    agent (printenv resolves to ABSENT), proving unset names are skipped rather
    than forwarded as an empty value.
    """
    # Explicitly unset the variable in the shell so the outcome does not depend
    # on the test runner's ambient environment, then forward it anyway.
    expect(
        e2e.run(
            "unset MNGR_E2E_DEFINITELY_UNSET"
            " && mngr create my-task --pass-env MNGR_E2E_DEFINITELY_UNSET --type command --no-ensure-clean --no-connect -- sleep 100965",
            comment="forward a variable that is not set in the current shell",
        )
    ).to_succeed()

    # The agent must not have the variable: `printenv` exits non-zero for an
    # unset name, so the branch resolves to ABSENT. This proves --pass-env
    # silently skips unset variables rather than forwarding an empty value.
    env_result = e2e.run(
        "mngr exec my-task 'if printenv MNGR_E2E_DEFINITELY_UNSET; then echo PRESENT; else echo ABSENT; fi'",
        comment="confirm the unset variable was not forwarded into the agent",
        timeout=120.0,
    )
    expect(env_result).to_succeed()
    expect(env_result.stdout).to_contain("ABSENT")
    expect(env_result.stdout).not_to_contain("PRESENT")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_create_with_pass_host_env(e2e: E2eSession) -> None:
    """Tutorial block:
        # set host-level environment variables (for all agents on the host, not just that particular agent process)
        mngr create my-task --provider modal --pass-host-env MODAL_TOKEN_ID --pass-host-env MODAL_TOKEN_SECRET

    Scope: `--pass-host-env NAME` forwards the named variable from the current
    shell to the *host* environment (shared by every agent on the host), not
    just the single agent process. After creating a modal agent with two
    --pass-host-env flags, a command exec'd on the host sees both
    MODAL_TOKEN_ID and MODAL_TOKEN_SECRET set -- proving they propagated to the
    host (the secret values are checked for non-emptiness, never printed).
    """
    expect(
        e2e.run(
            "mngr create my-task --provider modal --pass-host-env MODAL_TOKEN_ID --pass-host-env MODAL_TOKEN_SECRET --type command --no-ensure-clean --no-connect -- sleep 100964",
            comment="set host-level environment variables",
            timeout=240.0,
        )
    ).to_succeed()
    # Verify the host-level env vars actually propagated to the host: a command
    # exec'd on the agent's host should see them (they are sourced for every
    # agent on the host, which is the whole point of --pass-host-env). We assert
    # the var is present without ever printing its (secret) value.
    host_env_check = e2e.run(
        'mngr exec my-task \'test -n "$MODAL_TOKEN_ID" && test -n "$MODAL_TOKEN_SECRET" && echo HOST_ENV_PRESENT\'',
        comment="confirm host-level env vars are visible to agents on the host",
        timeout=120.0,
    )
    expect(host_env_check).to_succeed()
    expect(host_env_check.stdout).to_contain("HOST_ENV_PRESENT")


# NOTE: no @pytest.mark.modal here. Unlike the sibling tests (which use the
# default provider and therefore query Modal during host discovery), this test
# pins the create provider to "local" via MNGR__COMMANDS__CREATE__PROVIDER, so
# Modal is never invoked and the resource guard would flag the mark as
# superfluous.
@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
# The create plus the follow-up `mngr list` exceed the default 10s per-test
# timeout, so override it (mirrors test_create_with_pass_host_env).
@pytest.mark.timeout(120)
def test_control_mngr_via_env(e2e: E2eSession) -> None:
    """Tutorial block:
        # control mngr itself via environment variables. All config options can be set this way, use double-underscore ("__")
        # in order to index into the nested config structure. For example, to set the provider to "modal" for a create command:
        export MNGR__COMMANDS__CREATE__PROVIDER=modal
        mngr create my-task

    Scope: any config option can be set via an MNGR__* env var, using "__" to
    index into the nested config (here MNGR__COMMANDS__CREATE__PROVIDER selects
    the create provider). Exporting it to "local" and running a bare create
    lands the agent on the local provider (verified via `mngr list --format
    json`: host.provider_name == local, agent alive) -- proving the env var
    flowed into provider selection, not merely that create exited 0.
    """
    # We can't actually export modal here without paying the modal startup
    # cost; override the env var to "local" instead so the create stays cheap.
    #
    # A fresh user (the tutorial's scenario) has no `commands.create` config, so
    # the env var lands in an empty `defaults` map and applies cleanly. The e2e
    # fixture, however, writes `connect_command` under `[commands.create]` in
    # local settings, so the env-var layer's `commands.create.defaults` would
    # narrow over it. Opt into the assign-by-default behavior to work around the
    # fixture-injected setting (this is also a documented MNGR__* config key).
    expect(
        e2e.run(
            "export MNGR__ALLOW_SETTINGS_KEY_ASSIGNMENT_NARROWING=true MNGR__COMMANDS__CREATE__PROVIDER=local"
            " && mngr create my-task --type command --no-ensure-clean --no-connect -- sleep 100963",
            comment="control mngr via env var (local override for the test)",
        )
    ).to_succeed()

    # Verify the env var actually controlled the provider: the agent must exist
    # and be running on the "local" provider (the value we set via
    # MNGR__COMMANDS__CREATE__PROVIDER), not just that the command exited 0.
    # Scope the listing to the local provider so we neither query Modal (slow,
    # and would otherwise require @pytest.mark.modal) nor see agents from other
    # providers -- the agent appearing here is itself proof it landed on local.
    list_result = e2e.run(
        "mngr list --provider local --format json", comment="verify the agent landed on the local provider"
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one 'my-task' agent, got: {agents}"
    assert matching[0]["host"]["provider_name"] == "local", (
        f"Expected the agent on the 'local' provider (set via env var), got: {matching[0]['host']['provider_name']}"
    )
    # The agent should be alive (a command agent running `sleep` sits in WAITING;
    # an interactive one would be RUNNING) -- not STOPPED/DONE/UNKNOWN.
    assert matching[0]["state"] in ("RUNNING", "WAITING"), (
        f"Expected the agent to be alive (RUNNING/WAITING), got: {matching[0]['state']}"
    )


@pytest.mark.release
def test_control_mngr_via_env_rejects_invalid_value(e2e: E2eSession) -> None:
    """Tutorial block:
        # control mngr itself via environment variables. All config options can be set this way, use double-underscore ("__")
        # in order to index into the nested config structure. For example, to set the provider to "modal" for a create command:
        export MNGR__COMMANDS__CREATE__PROVIDER=modal
        mngr create my-task

    Scope: the unhappy path of the same MNGR__* block. An invalid value set via
    MNGR__COMMANDS__CREATE__PROVIDER is rejected exactly like an invalid
    ``--provider`` flag: create fails with an "Unknown provider backend" error
    naming the bad value (not the narrowing guard), proving the env var is
    genuinely wired into provider selection.
    """
    # MNGR__ALLOW_SETTINGS_KEY_ASSIGNMENT_NARROWING=true gets us past the
    # fixture-injected `commands.create` narrowing (see the happy-path test) so
    # the failure we observe is the provider rejection, not a config-load error.
    result = e2e.run(
        "export MNGR__ALLOW_SETTINGS_KEY_ASSIGNMENT_NARROWING=true MNGR__COMMANDS__CREATE__PROVIDER=nonexistent"
        " && mngr create my-task --type command --no-ensure-clean --no-connect -- sleep 100964",
        comment="control mngr via env var with an invalid provider value",
    )
    expect(result).to_fail()
    # The failure must come from provider resolution (naming the bad value we
    # supplied via the env var), not from the narrowing guard we opted out of.
    expect(result.stderr).not_to_contain("Settings narrowing detected")
    # Assert on the provider-backend rejection specifically (and that it names
    # the bad value), so this proves the env var flowed into provider selection
    # rather than merely that some error mentioning "nonexistent" occurred.
    expect(result.stderr).to_contain("Unknown provider backend")
    expect(result.stderr).to_contain("nonexistent")
