from collections.abc import Mapping
from typing import Any

from imbue.mngr.primitives import HostState


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
        # Outer SSH succeeded but produced no state -- treat as crashed
        # (no info to be more specific).
        return HostState.CRASHED
    exit_code = raw.get("container_exit_code") or 0
    has_certified_data = bool(raw.get("certified_data"))
    if container_state == "running" and has_certified_data:
        return HostState.RUNNING
    if container_state == "running":
        # Container is up but docker exec didn't give us data -- we know
        # the host exists but can't read its state from inside.
        return HostState.UNAUTHENTICATED
    state, _note = map_docker_status_to_host_state(container_state, exit_code)
    return state


def derive_offline_note_from_raw(raw: Mapping[str, Any]) -> str | None:
    """Produce a short ``failure_reason`` note for non-running containers.

    Returns None for running containers (no note needed) and for the
    DESTROYED / missing case (the state itself is the message). For
    stopped/paused/etc., returns the human-readable note that
    ``map_docker_status_to_host_state`` produced.
    """
    container_state = raw.get("container_state")
    if not container_state or container_state == "running":
        return None
    if raw.get("container_missing"):
        return None
    exit_code = raw.get("container_exit_code") or 0
    _state, note = map_docker_status_to_host_state(container_state, exit_code)
    return note


def map_docker_status_to_host_state(status: str, exit_code: int) -> tuple[HostState, str | None]:
    """Translate docker's container ``State.Status`` into a ``HostState``.

    Returns ``(state, note)`` where ``note`` is a short human-readable
    diagnostic appended to ``HostDetails.failure_reason``. If the docker
    container is ``running`` but inner SSH was unreachable we treat that
    as an authentication problem -- the host is up; we just can't get
    inside it.
    """
    if status == "running":
        return HostState.UNAUTHENTICATED, "container is running on outer host but inner SSH was unreachable"
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
    return HostState.CRASHED, f"unrecognized docker status {status!r}"
