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
    # Verify the top-level commands mentioned in the tutorial comment are present.
    # Note: "push" and "pull" are not top-level commands -- they are accomplished
    # via the "git" command ("mngr git push"/"mngr git pull") or "rsync", so we
    # assert on "git" rather than literal "push"/"pull".
    expect(result.stdout).to_contain("create")
    expect(result.stdout).to_contain("list")
    expect(result.stdout).to_contain("destroy")
    expect(result.stdout).to_contain("message")
    expect(result.stdout).to_contain("connect")
    expect(result.stdout).to_contain("clone")
    expect(result.stdout).to_contain("git")


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
    # Help is purely informational: it must go to stdout, leaving stderr clean.
    expect(result.stderr).to_be_empty()
    # Verify the help text has key structural sections
    expect(result.stdout).to_contain("NAME")
    expect(result.stdout).to_contain("mngr create - Create and run an agent")
    expect(result.stdout).to_contain("SYNOPSIS")
    expect(result.stdout).to_contain("DESCRIPTION")
    expect(result.stdout).to_contain("OPTIONS")
    expect(result.stdout).to_contain("EXAMPLES")
    # Verify representative flags that the surrounding tutorial actually
    # demonstrates are documented, backing up the "tons more arguments" claim.
    expect(result.stdout).to_contain("--no-connect")
    expect(result.stdout).to_contain("--type")
    expect(result.stdout).to_contain("--provider")
    expect(result.stdout).to_contain("--branch")
    expect(result.stdout).to_contain("--message")


@pytest.mark.release
def test_create_help_alias_and_short_flag_succeeds(e2e: E2eSession) -> None:
    # The help text documents both the `c` alias (SYNOPSIS: `mngr [create|c]`)
    # and the `-h` short flag (OPTIONS: `-h, --help`). Exercise that documented
    # behavior: `mngr c -h` must surface the same `mngr create` help.
    e2e.write_tutorial_block("""
    # tons more arguments for anything you could want! As always, you can learn more via --help
    mngr create --help
    """)
    result = e2e.run("mngr c -h", comment="learn more via --help, using the `c` alias and `-h` short flag")
    expect(result).to_succeed()
    expect(result.stderr).to_be_empty()
    # Resolves to the create command's help, not some other command's.
    expect(result.stdout).to_contain("mngr create - Create and run an agent")
    expect(result.stdout).to_contain("SYNOPSIS")
    expect(result.stdout).to_contain("OPTIONS")
    expect(result.stdout).to_contain("--no-connect")
