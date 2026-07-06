"""Tests for CLI help output.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block.
"""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
def test_help_succeeds(e2e: E2eSession) -> None:
    """Tutorial block:
        # or see the other commands--list, destroy, message, connect, git, clone, and more!  These other commands are covered in their own sections below.
        mngr --help

    Scope: `mngr --help` exits 0 and its output is a usage page that lists every
    subcommand the tutorial comment advertises (create, list, destroy, message,
    connect, git, clone).
    """
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
def test_help_unknown_command_fails(e2e: E2eSession) -> None:
    """Tutorial block:
        # or see the other commands--list, destroy, message, connect, git, clone, and more!  These other commands are covered in their own sections below.
        mngr --help

    Scope: the unhappy path of the same block, which steers users to `mngr --help`
    to discover commands. Invoking a command that does not exist fails, and the
    error names the offending command and points the user back to `--help` rather
    than crashing with a Traceback.
    """
    result = e2e.run(
        "mngr definitely-not-a-real-command",
        comment="an unknown command fails and points the user back to --help",
    )
    expect(result).to_fail()
    combined = result.stdout + result.stderr
    # The error names the offending command and suggests how to get help.
    expect(combined).to_contain("No such command")
    expect(combined).to_contain("definitely-not-a-real-command")
    expect(combined).to_contain("--help")
    # A usage error must not surface as an uncaught exception.
    expect(combined).not_to_contain("Traceback")


@pytest.mark.release
def test_create_help_succeeds(e2e: E2eSession) -> None:
    """Tutorial block:
        # tons more arguments for anything you could want! As always, you can learn more via --help
        mngr create --help

    Scope: `mngr create --help` exits 0 with no stderr (help is informational
    output, not a warning), and renders the create command's man-page help with
    its structural sections (SYNOPSIS, DESCRIPTION, OPTIONS, EXAMPLES) plus the
    advertised flags (a representative spread: --no-connect, --type).
    """
    result = e2e.run(
        "mngr create --help",
        comment="tons more arguments for anything you could want! As always, you can learn more via --help",
    )
    expect(result).to_succeed()
    # Help is informational output: it must go to stdout and not emit any
    # warnings or deprecation notices on stderr.
    expect(result.stderr).to_be_empty()
    # Verify the help text has key structural sections
    expect(result.stdout).to_contain("SYNOPSIS")
    expect(result.stdout).to_contain("DESCRIPTION")
    expect(result.stdout).to_contain("OPTIONS")
    expect(result.stdout).to_contain("EXAMPLES")
    # Verify a few representative flags are documented
    expect(result.stdout).to_contain("--no-connect")
    expect(result.stdout).to_contain("--type")


@pytest.mark.release
def test_create_help_short_form_and_alias_succeed(e2e: E2eSession) -> None:
    """Tutorial block:
        # tons more arguments for anything you could want! As always, you can learn more via --help
        mngr create --help

    Scope: the abbreviated forms advertised in the help's own SYNOPSIS line
    ("mngr [create|c] ... -h"). The `-h` short flag and the `c` alias each
    succeed with no stderr and produce the same `create` help (its NAME summary
    and SYNOPSIS) as the canonical `mngr create --help`.
    """
    short_form = e2e.run("mngr create -h", comment="the -h short flag is equivalent to --help")
    expect(short_form).to_succeed()
    expect(short_form.stderr).to_be_empty()
    expect(short_form.stdout).to_contain("mngr create - Create and run an agent")
    expect(short_form.stdout).to_contain("SYNOPSIS")

    alias = e2e.run("mngr c --help", comment="the c alias is equivalent to create")
    expect(alias).to_succeed()
    expect(alias.stderr).to_be_empty()
    expect(alias.stdout).to_contain("mngr create - Create and run an agent")
