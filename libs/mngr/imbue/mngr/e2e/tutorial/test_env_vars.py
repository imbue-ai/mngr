"""Tests for the create-time environment-variable tutorial blocks."""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_with_env_vars(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # set environment variables for the agent at creation time
        mngr create my-task --env DEBUG=true --env LOG_LEVEL=verbose
    """)
    expect(
        e2e.run(
            "mngr create my-task --env DEBUG=true --env LOG_LEVEL=verbose --type command --no-ensure-clean --no-connect -- sleep 100960",
            comment="set environment variables at creation time",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
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


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
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


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.timeout(180)
def test_create_with_pass_host_env(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # set host-level environment variables (for all agents on the host, not just that particular agent process)
        mngr create my-task --provider modal --pass-host-env MODAL_TOKEN_ID --pass-host-env MODAL_TOKEN_SECRET
    """)
    expect(
        e2e.run(
            "mngr create my-task --provider modal --pass-host-env MODAL_TOKEN_ID --pass-host-env MODAL_TOKEN_SECRET --no-connect --no-ensure-clean",
            comment="set host-level environment variables",
            timeout=150.0,
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_control_mngr_via_env(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # control mngr itself via environment variables. All config options can be set this way, use double-underscore ("__")
        # in order to index into the nested config structure. For example, to set the provider to "modal" for a create command:
        export MNGR__COMMANDS__CREATE__PROVIDER=modal
        mngr create my-task
    """)
    # We can't actually export modal here without paying the modal startup
    # cost; override the env var to "local" instead so the create stays cheap.
    expect(
        e2e.run(
            "export MNGR__COMMANDS__CREATE__PROVIDER=local && mngr create my-task --type command --no-ensure-clean --no-connect -- sleep 100963",
            comment="control mngr via env var (local override for the test)",
        )
    ).to_succeed()
