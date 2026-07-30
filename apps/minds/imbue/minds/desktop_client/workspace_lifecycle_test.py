"""Tests for the shared workspace host lifecycle helpers."""

from imbue.minds.desktop_client.workspace_lifecycle import _lead_with_error_lines


def test_lead_with_error_lines_puts_the_verdict_ahead_of_the_warnings() -> None:
    """The ERROR line names the host asked about; the warnings name other ones.

    Leading with all of stderr made a start failure read as an unreachable box --
    the warnings are about orphaned key dirs for long-destroyed workspaces, while
    the real reason (mngr looked for the agent under the wrong host_dir) was the
    last line.
    """
    stderr = (
        "WARNING: imbue_cloud[acct] outer SSH unreachable for host host-a15c1302: "
        "Host not found: host-a15c1302\n"
        "ERROR: Agent agent-b790 not found on host host-67ad\n"
    )

    reordered = _lead_with_error_lines(stderr)

    assert reordered.splitlines()[0] == "ERROR: Agent agent-b790 not found on host host-67ad"


def test_lead_with_error_lines_keeps_every_warning() -> None:
    """Reordering, not filtering: the warnings are real diagnostics.

    The caller is an agent on another host with no access to this one's logs, so
    dropping them costs it the only copy it will ever see.
    """
    stderr = (
        "WARNING: imbue_cloud[acct] outer SSH unreachable for host host-a15c1302: "
        "Host not found: host-a15c1302\n"
        "WARNING: imbue_cloud[acct] outer SSH unreachable for host host-0b17800a: "
        "Host not found: host-0b17800a\n"
        "ERROR: Agent agent-b790 not found on host host-67ad\n"
    )

    reordered = _lead_with_error_lines(stderr)

    assert "Host not found: host-a15c1302" in reordered
    assert "Host not found: host-0b17800a" in reordered
    # Nothing is invented or lost -- the same lines, reordered.
    assert sorted(reordered.splitlines()) == sorted(line.rstrip() for line in stderr.splitlines() if line.strip())


def test_lead_with_error_lines_keeps_every_error_line() -> None:
    """A run that failed several ways must report all of them, in order."""
    stderr = "ERROR: first thing broke\nWARNING: noise\nERROR: second thing broke\n"

    assert _lead_with_error_lines(stderr).splitlines() == [
        "ERROR: first thing broke",
        "ERROR: second thing broke",
        "WARNING: noise",
    ]


def test_lead_with_error_lines_passes_through_output_with_no_verdict() -> None:
    """Not every failure announces itself with an ERROR: prefix."""
    assert _lead_with_error_lines("  Traceback (most recent call last)  ") == "Traceback (most recent call last)"
