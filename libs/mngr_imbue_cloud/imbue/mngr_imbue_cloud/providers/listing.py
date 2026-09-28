from collections.abc import Mapping
from typing import Any

from imbue.imbue_common.pure import pure
from imbue.mngr.interfaces.data_types import CpuResources
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr.primitives import HostState
from imbue.mngr_imbue_cloud.wire_types import WorkspaceInfo

# Diagnostic carried on the UNKNOWN state of a container the outer host reports
# running but whose inner data we could not read (docker exec produced no
# data.json). The state is UNKNOWN -- mngr does not claim to know what is wrong --
# but the note preserves the observation that the container itself is up.
INNER_UNREADABLE_NOTE = "container is running on outer host but its inner data was unreadable"

# Diagnostic carried on the FAILED state of a leased VM that has no container.
# The lease (the billable resource) is still held, so the host is not
# destroyed: this is what a destroy leaves behind when its data wipe ran
# but the lease release failed, and nothing can be started on it.
CONTAINER_MISSING_NOTE = "leased VM has no container; the lease is still held"


def derive_host_state_from_raw(raw: Mapping[str, Any]) -> HostState:
    """Map the outer-listing raw output to a HostState.

    The outer listing script tags the output with ``CONTAINER_STATE``,
    ``CONTAINER_EXIT_CODE``, and ``CONTAINER_MISSING`` so we don't have
    to re-run docker inspect.
    """
    if raw.get("container_missing"):
        # The lease outlives the container, so the host is terminal but not
        # gone: FAILED keeps it listed (and destroyable) rather than reading
        # as a destroyed host that every consumer then treats as absent.
        # CRASHED would be wrong too: consumers auto-start CRASHED hosts, and
        # there is no container here to start.
        return HostState.FAILED
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

    Returns None for a healthy running container (readable data, no note
    needed). A leased VM with no container carries ``CONTAINER_MISSING_NOTE``
    behind its FAILED state. A running container whose inner data we could not read carries
    the ``INNER_UNREADABLE_NOTE`` diagnostic behind its UNKNOWN state. For
    stopped/paused/etc., returns the human-readable note that
    ``map_docker_status_to_host_state`` produced.
    """
    if raw.get("container_missing"):
        return CONTAINER_MISSING_NOTE
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


@pure
def host_resources_for_machine(lease_attributes: Mapping[str, Any], machine: WorkspaceInfo | None) -> HostResources:
    """The machine's recorded allocation: memory and disk from the machine row, vCPUs from the lease attributes.

    ``memory_units`` (1 unit = 1GiB of machine RAM) and ``disk_gb`` are the
    connector's current-size columns, restamped on every start, so they follow
    a resize. The bake-time lease attributes are never updated after a resize:
    they are only the memory fallback against a connector too old to serve the
    sizing columns, and the sole source of the vCPU count, which the client
    cannot derive (it is the machine's proportional share of its box's threads).
    """
    cpus_attr = lease_attributes.get("cpus")
    cpu_count = int(cpus_attr) if isinstance(cpus_attr, (int, float)) else 1
    memory_attr = lease_attributes.get("memory_gb")
    fallback_memory_gb = float(memory_attr) if isinstance(memory_attr, (int, float)) else 1.0
    memory_units = machine.memory_units if machine is not None else None
    memory_gb = float(memory_units) if memory_units is not None else fallback_memory_gb
    disk_gb = float(machine.disk_gb) if machine is not None and machine.disk_gb is not None else None
    return HostResources(cpu=CpuResources(count=cpu_count), memory_gb=memory_gb, disk_gb=disk_gb, gpu=None)
