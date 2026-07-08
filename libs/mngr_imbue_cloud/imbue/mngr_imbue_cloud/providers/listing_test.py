"""Unit tests for the listing-output shaping helpers."""

import pytest

from imbue.mngr.primitives import HostState
from imbue.mngr_imbue_cloud.providers.listing import map_docker_status_to_host_state


@pytest.mark.parametrize(
    "status,exit_code,expected_state",
    [
        # Running container with unreachable inner SSH should report as
        # UNAUTHENTICATED (host is up; we just can't get inside).
        ("running", 0, HostState.UNAUTHENTICATED),
        # exit_code is ignored when running.
        ("running", 137, HostState.UNAUTHENTICATED),
        # Cleanly-exited containers map to STOPPED.
        ("exited", 0, HostState.STOPPED),
        # Non-zero exit means the container crashed.
        ("exited", 1, HostState.CRASHED),
        ("exited", 137, HostState.CRASHED),
        # Paused containers preserve their PAUSED state.
        ("paused", 0, HostState.PAUSED),
        # In-progress lifecycle states render as STARTING so the user knows
        # to wait, not assume the host is broken.
        ("created", 0, HostState.STARTING),
        ("restarting", 0, HostState.STARTING),
        # Terminal-but-broken docker states surface as CRASHED.
        ("dead", 0, HostState.CRASHED),
        ("removing", 0, HostState.CRASHED),
        # Unknown statuses default to CRASHED so we never silently misreport.
        ("nonsense", 0, HostState.CRASHED),
        ("", 0, HostState.CRASHED),
    ],
)
def test_map_docker_status_to_host_state(status: str, exit_code: int, expected_state: HostState) -> None:
    state, note = map_docker_status_to_host_state(status, exit_code)
    assert state == expected_state
    # Every mapping returns a non-empty diagnostic note that gets folded
    # into HostDetails.failure_reason; assert it's at least populated so
    # the user sees *something* in the listing.
    assert note is not None
    assert note != ""


def test_map_docker_status_running_note_mentions_inner_ssh() -> None:
    """The running-but-unreachable case must explain why we landed there."""
    _state, note = map_docker_status_to_host_state("running", 0)
    assert note is not None
    assert "inner SSH" in note


def test_map_docker_status_exited_nonzero_note_includes_exit_code() -> None:
    """A crashed container's note should surface the exit code for debugging."""
    _state, note = map_docker_status_to_host_state("exited", 137)
    assert note is not None
    assert "137" in note
