"""Helpers shared by the imbue_cloud provider unit tests."""

from pathlib import Path

from imbue.mngr.primitives import HostId
from imbue.mngr.providers.host_key_store import HostKeyOrigin
from imbue.mngr.providers.host_key_store import load_host_key_record
from imbue.mngr_imbue_cloud.primitives import LeaseDbId
from imbue.mngr_imbue_cloud.wire_types import WorkspaceInfo
from imbue.mngr_imbue_cloud.wire_types import WorkspaceStatus
from imbue.mngr_imbue_cloud.wire_types import WorkspaceStopKind


def load_pins_by_endpoint(known_hosts_path: Path, host_id: HostId) -> dict[tuple[str, int], tuple[str, HostKeyOrigin]]:
    """The host's pins as ``{(address, port): (public_key, origin)}``; the host must have a record."""
    record = load_host_key_record(known_hosts_path, host_id)
    assert record is not None
    return {(pin.address, pin.port): (pin.public_key, pin.origin) for pin in record.pins}


def make_workspace_info(
    status: WorkspaceStatus | str,
    with_placement: bool = True,
    transition_error: str | None = None,
    memory_units: int | None = None,
    disk_gb: int | None = None,
    stop_kind: str | None = None,
) -> WorkspaceInfo:
    """A ``GET /workspaces`` row with plausible bake-time attributes and (optionally) placement and sizing columns.

    ``status`` and ``stop_kind`` may be raw wire strings so tests can hand the
    WireEnums a value this client does not recognize.
    """
    return WorkspaceInfo(
        host_db_id=LeaseDbId("00000000-0000-0000-0000-0000000000aa"),
        status=WorkspaceStatus(status),
        stop_kind=WorkspaceStopKind(stop_kind) if stop_kind is not None else None,
        vps_address="10.0.0.9" if with_placement else None,
        ssh_port=22000 if with_placement else None,
        ssh_user="root",
        container_ssh_port=22001 if with_placement else None,
        agent_id="agent-abc",
        host_id="host-" + "a" * 32,
        host_name="my-workspace",
        attributes={"cpus": 2},
        leased_at="2026-01-01T00:00:00+00:00",
        transition_error=transition_error,
        outer_host_public_key="ssh-ed25519 AAAA outer",
        container_host_public_key="ssh-ed25519 AAAA container",
        memory_units=memory_units,
        disk_gb=disk_gb,
    )
