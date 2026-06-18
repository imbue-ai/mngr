from datetime import datetime
from datetime import timezone

from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr_vps.host_store import VpsHostRecord
from imbue.mngr_vps.instance import VpsProvider


def seed_stopped_host_record(provider: VpsProvider, host_id: HostId, *, host_name: str = "myhost") -> None:
    """Cache a STOPPED host record (``vps_ip=None``) so the base on-volume path short-circuits.

    The provider's agent-data hooks call ``super()`` first (the authoritative
    on-volume store) and only then fall back to / additionally write the external
    mirror (bucket / instance tags / metadata). For a stopped host the base raises
    ``HostNotFoundError`` (no reachable ``vps_ip``); seeding such a record makes
    the base short-circuit immediately without any SSH or discovery sweep, so a
    test can exercise the stopped-host mirror fallback without standing up a fake
    VPS.
    """
    certified = CertifiedHostData(
        host_id=str(host_id),
        host_name=host_name,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        stop_reason=HostState.STOPPED.value,
    )
    provider._host_record_cache[host_id] = VpsHostRecord(certified_host_data=certified)
