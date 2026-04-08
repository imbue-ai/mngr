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
    # or see the other commands--list, destroy, message, connect, push, pull, clone, and more!  These other commands are covered in their own sections below.
    mngr --help
    """)
    result = e2e.run(
        "mngr --help",
        comment="or see the other commands--list, destroy, message, connect, push, pull, clone, and more!",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("Usage")
    # Verify the commands mentioned in the tutorial comment are present
    expect(result.stdout).to_contain("create")
    expect(result.stdout).to_contain("list")
    expect(result.stdout).to_contain("destroy")
    expect(result.stdout).to_contain("message")
    expect(result.stdout).to_contain("connect")
    expect(result.stdout).to_contain("push")
    expect(result.stdout).to_contain("pull")
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
    # Verify the help text has key structural sections
    expect(result.stdout).to_contain("SYNOPSIS")
    expect(result.stdout).to_contain("DESCRIPTION")
    expect(result.stdout).to_contain("OPTIONS")
    expect(result.stdout).to_contain("EXAMPLES")
    # Verify a few representative flags are documented
    expect(result.stdout).to_contain("--no-connect")
    expect(result.stdout).to_contain("--command")
