"""Tests for CLI help output.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block.
"""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
def test_help_succeeds(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # or see the other commands--list, destroy, message, connect, git, clone, and more!  These other commands are covered in their own sections below.
    mngr --help
    """)
    result = e2e.run(
        "mngr --help",
        comment="or see the other commands--list, destroy, message, connect, git, clone, and more!",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("Usage")
    # Verify the commands mentioned in the tutorial comment are present
    expect(result.stdout).to_contain("create")
    expect(result.stdout).to_contain("list")
    expect(result.stdout).to_contain("destroy")
    expect(result.stdout).to_contain("message")
    expect(result.stdout).to_contain("connect")
    expect(result.stdout).to_contain("git")
    expect(result.stdout).to_contain("clone")


@pytest.mark.release
def test_create_help_succeeds(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # tons more arguments for anything you could want! As always, you can learn more via --help
    mngr create --help
    """)
    result = e2e.run(
        "mngr create --help",
        comment="tons more arguments for anything you could want! As always, you can learn more via --help",
    )
    expect(result).to_succeed()
    # Help text goes to stdout; stderr should stay clean (no warnings/errors).
    expect(result.stderr).to_be_empty()
    # Verify the help text has key structural sections
    expect(result.stdout).to_contain("SYNOPSIS")
    expect(result.stdout).to_contain("DESCRIPTION")
    expect(result.stdout).to_contain("OPTIONS")
    expect(result.stdout).to_contain("EXAMPLES")
    # Verify a few representative flags are documented, including ones the
    # preceding tutorial blocks demonstrate (--no-connect and --message in
    # `mngr create my-task --no-connect --message "Do the thing"`).
    expect(result.stdout).to_contain("--no-connect")
    expect(result.stdout).to_contain("--type")
    expect(result.stdout).to_contain("--message")


@pytest.mark.release
def test_create_help_alias_succeeds(e2e: E2eSession) -> None:
    # The create command is also reachable via its documented `c` alias
    # (the SYNOPSIS renders it as `mngr [create|c]`). Verify the alias works
    # and produces the same help as the canonical command.
    e2e.write_tutorial_block("""
    # tons more arguments for anything you could want! As always, you can learn more via --help
    mngr create --help
    """)
    canonical = e2e.run("mngr create --help", comment="canonical help for the create command")
    expect(canonical).to_succeed()
    result = e2e.run("mngr c --help", comment="the same help, reached via the `c` alias")
    expect(result).to_succeed()
    expect(result.stderr).to_be_empty()
    # The alias must resolve to create, not some other command.
    expect(result.stdout).to_contain("mngr create - Create and run an agent")
    # The alias output should be identical to the canonical command's help.
    expect(result.stdout).to_equal(canonical.stdout)
