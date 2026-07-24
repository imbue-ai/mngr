from collections.abc import Mapping
from typing import Any

from imbue.mngr.primitives import HostState

# Diagnostic carried on the UNKNOWN state of a container the outer host reports
# running but whose inner data we could not read (docker exec produced no
# data.json). The state is UNKNOWN -- mngr does not claim to know what is wrong --
# but the note preserves the observation that the container itself is up.
INNER_UNREADABLE_NOTE = "container is running on outer host but its inner data was unreadable"


def derive_host_state_from_raw(raw: Mapping[str, Any]) -> HostState:
    """Map the outer-listing raw output to a HostState.

    The outer listing script tags the output with ``CONTAINER_STATE``,
    ``CONTAINER_EXIT_CODE``, and ``CONTAINER_MISSING`` so we don't have
    to re-run docker inspect.
    """
    if raw.get("container_missing"):
        return HostState.DESTROYED
    container_state = raw.get("container_state")
    if not container_state:
        # Outer SSH succeeded but produced no state -- a degraded
        # observation, not evidence that the container is down, so
        # UNKNOWN rather than CRASHED (consumers auto-restart off
        # CRASHED and must not do so off non-evidence).
        return HostState.UNKNOWN
    exit_code = raw.get("container_exit_code") or 0
    has_certified_data = bool(raw.get("certified_data"))
    if container_state == "running" and has_certified_data:
        return HostState.RUNNING
    if container_state == "running":
        # Container is up but docker exec gave us no data -- the host exists
        # but we cannot read its state from inside, so mngr does not claim to
        # know what is wrong: UNKNOWN, not a positive up/down verdict. The
        # ``INNER_UNREADABLE_NOTE`` diagnostic rides along on failure_reason.
        return HostState.UNKNOWN
    state, _note = map_docker_status_to_host_state(container_state, exit_code)
    return state


def derive_offline_note_from_raw(raw: Mapping[str, Any]) -> str | None:
    """Produce a short ``failure_reason`` note for a host we could not read cleanly.

    Returns None for the DESTROYED / missing case (the state itself is the
    message) and for a healthy running container (readable data, no note
    needed). A running container whose inner data we could not read carries
    the ``INNER_UNREADABLE_NOTE`` diagnostic behind its UNKNOWN state. For
    stopped/paused/etc., returns the human-readable note that
    ``map_docker_status_to_host_state`` produced.
    """
    if raw.get("container_missing"):
        return None
    container_state = raw.get("container_state")
    if not container_state:
        return None
    if container_state == "running":
        if bool(raw.get("certified_data")):
            return None
        return INNER_UNREADABLE_NOTE
    exit_code = raw.get("container_exit_code") or 0
    _state, note = map_docker_status_to_host_state(container_state, exit_code)
    return note


def map_docker_status_to_host_state(status: str, exit_code: int) -> tuple[HostState, str | None]:
    """Translate a non-running docker container ``State.Status`` into a ``HostState``.

    Returns ``(state, note)`` where ``note`` is a short human-readable
    diagnostic appended to ``HostDetails.failure_reason``. The ``running``
    status is decided by the callers from ``certified_data`` before reaching
    here (readable -> RUNNING, unreadable -> UNKNOWN); it is mapped here only
    for self-consistency, to the same UNKNOWN + ``INNER_UNREADABLE_NOTE``.
    """
    if status == "running":
        return HostState.UNKNOWN, INNER_UNREADABLE_NOTE
    if status == "exited":
        if exit_code == 0:
            return HostState.STOPPED, "container exited cleanly"
        return HostState.CRASHED, f"container exited with code {exit_code}"
    if status == "paused":
        return HostState.PAUSED, "container is paused"
    if status in ("created", "restarting"):
        return HostState.STARTING, f"container in {status} state"
    if status in ("dead", "removing"):
        return HostState.CRASHED, f"container in {status} state"
    # An unrecognized status is a gap in our mapping, not evidence the
    # container is down: UNKNOWN, so consumers don't auto-restart off it.
    return HostState.UNKNOWN, f"could not determine state: unrecognized docker status {status!r}"
