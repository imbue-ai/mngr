"""Unit tests for the listing-output shaping helpers."""

import pytest

from imbue.mngr.primitives import HostState
from imbue.mngr_imbue_cloud.providers.listing import INNER_UNREADABLE_NOTE
from imbue.mngr_imbue_cloud.providers.listing import derive_host_state_from_raw
from imbue.mngr_imbue_cloud.providers.listing import derive_offline_note_from_raw
from imbue.mngr_imbue_cloud.providers.listing import map_docker_status_to_host_state


@pytest.mark.parametrize(
    "status,exit_code,expected_state",
    [
        # Running container we couldn't read from inside: mngr does not claim to
        # know its state, so UNKNOWN (host is up, but unreadable).
        ("running", 0, HostState.UNKNOWN),
        # exit_code is ignored when running.
        ("running", 137, HostState.UNKNOWN),
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
        # Unrecognized statuses are a mapping gap, not crash evidence, so
        # they surface as UNKNOWN (which consumers never auto-restart off).
        ("nonsense", 0, HostState.UNKNOWN),
        ("", 0, HostState.UNKNOWN),
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


def test_map_docker_status_running_note_explains_unreadable() -> None:
    """The running-but-unreadable case must explain why we landed on UNKNOWN."""
    state, note = map_docker_status_to_host_state("running", 0)
    assert state == HostState.UNKNOWN
    assert note == INNER_UNREADABLE_NOTE


def test_map_docker_status_exited_nonzero_note_includes_exit_code() -> None:
    """A crashed container's note should surface the exit code for debugging."""
    _state, note = map_docker_status_to_host_state("exited", 137)
    assert note is not None
    assert "137" in note


def test_derive_host_state_empty_container_state_is_unknown() -> None:
    """Outer SSH succeeded but produced no container state: a degraded observation,
    not evidence the container is down, so UNKNOWN rather than CRASHED."""
    assert derive_host_state_from_raw({}) == HostState.UNKNOWN
    assert derive_host_state_from_raw({"container_state": ""}) == HostState.UNKNOWN


def test_derive_host_state_running_with_certified_data_is_running() -> None:
    """A running container whose data.json we read is a healthy RUNNING host, no note."""
    raw = {"container_state": "running", "certified_data": {"image": "x"}}
    assert derive_host_state_from_raw(raw) == HostState.RUNNING
    assert derive_offline_note_from_raw(raw) is None


def test_derive_host_state_running_without_certified_data_is_unknown_with_note() -> None:
    """A running container we couldn't read from inside is UNKNOWN, carrying the diagnostic note."""
    raw = {"container_state": "running"}
    assert derive_host_state_from_raw(raw) == HostState.UNKNOWN
    assert derive_offline_note_from_raw(raw) == INNER_UNREADABLE_NOTE
