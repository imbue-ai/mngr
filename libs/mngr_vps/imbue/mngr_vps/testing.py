from abc import abstractmethod
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Final

from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.providers.provider_release_testing import ProviderReleaseProfile
from imbue.mngr_vps.host_store import VpsHostRecord
from imbue.mngr_vps.instance import VpsProvider
from imbue.mngr_vps.primitives import IsolationMode
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vps.primitives import VpsInstanceStatus
from imbue.mngr_vps.vps_client import VpsClientInterface

# Trip 2's idle-watcher timeout for the cloud trio. 45s mirrors the existing per-provider idle
# tests: the in-host watcher polls every ~15s, so a 45s idle window self-stops reliably while
# keeping the boot-to-stop wall clock short. With no SSH connection (``--no-connect``) the host
# is idle from the start, so the stop fires shortly after this window elapses.
_CLOUD_IDLE_TIMEOUT_SECONDS: Final[int] = 45


def find_handle_by_launched_label(instances: Sequence[Mapping[str, Any]], launched_label: str) -> str | None:
    """Return the id of the single instance carrying ``<launched_label>=true``, or None.

    ``list_instances`` returns dicts whose ``tags`` is a list of ``"key=value"`` strings
    (built from EC2 tags / GCE labels / Azure tags). The release tests tag exactly the one
    instance they launched with the pytest-launched marker, so an ambiguous count (0 or >1)
    means leftover state and is reported as "not found".
    """
    matches = [instance["id"] for instance in instances if f"{launched_label}=true" in instance.get("tags", ())]
    if len(matches) == 1:
        return str(matches[0])
    return None


class VpsCloudReleaseProfile(ProviderReleaseProfile):
    """Shared plumbing for the VPS-family cloud providers (AWS / GCP / Azure) in the release trip.

    These providers all stop/start a real VM and probe it through a ``VpsClientInterface``
    (``get_instance_status`` / ``destroy_instance``), so the cost-stop, sketchy-kill, and
    backend-clean probes are identical -- only the credential gate, settings.toml, and
    pytest-launched label differ. Subclasses supply those plus ``find_launched_host_handle``
    (which needs the concrete client's ``list_instances``, not on the shared interface).
    """

    supports_shutdown_hosts = True
    # The container shape snapshots via `docker commit`, which lives on the VPS's own disk and
    # dies with `destroy_host`; so a VPS-family snapshot is not portable.
    snapshot_survives_destroy = False
    # The cloud trio's idle watcher self-stops into a resumable state (AWS stop / GCP TERMINATED /
    # Azure deallocated), so Trip 2 resumes via `mngr start` and checks the marker survived.
    resumes_after_auto_shutdown = True

    # Trip 4 (error classification). All three clouds raise the contract
    # ``ProviderUnavailableError`` ("is not available") when credentials are unresolvable, and all
    # route ``--vps-*`` build args through the shared migration check. Curated help text and the
    # exact credential-unresolvable env differ per provider, so subclasses override those.
    raises_contract_unavailable_error = True
    supports_vps_migration_arg_check = True
    unavailable_error_substring = "is not available"

    def __init__(self, client: VpsClientInterface, isolation: IsolationMode) -> None:
        self._client = client
        self._isolation = isolation
        # The container shape snapshots via `docker commit`; the bare shape has no snapshots.
        self.supports_snapshots = isolation is IsolationMode.CONTAINER
        # NONE isolation runs the agent on the VM's OS (no container), so Trip 1 runs its bare-shape
        # assertion -- the coverage the retired per-provider bare lifecycle tests used to own.
        self.is_bare_host = isolation is IsolationMode.NONE

    def auto_shutdown_create_args(self) -> Sequence[str]:
        # Drive the idle watcher: with no SSH connection the in-host watcher sees no activity and
        # powers the VM off into its resumable stopped state. (The base ``write_auto_shutdown_settings``
        # is the default; AWS overrides it for the ``terminate_on_shutdown = false`` resumable variant.)
        return ("--idle-timeout", str(_CLOUD_IDLE_TIMEOUT_SECONDS))

    @abstractmethod
    def find_launched_host_handle(self, host_name: str) -> str | None:
        """Return the cloud id of the host this test launched (via its pytest-launched label)."""

    def is_host_compute_running(self, handle: str) -> bool:
        return self._client.get_instance_status(VpsInstanceId(handle)) == VpsInstanceStatus.ACTIVE

    def is_host_compute_stopped(self, handle: str) -> bool:
        return self._client.get_instance_status(VpsInstanceId(handle)) == VpsInstanceStatus.HALTED

    def force_strand_host(self, handle: str) -> None:
        # Terminate the VM directly through the cloud API, bypassing `mngr destroy`. Idempotent
        # without swallowing errors: if the instance is already gone or terminating (the finally
        # backstop can re-run this after gc), there is nothing left to strand, so check the state
        # first and let any genuine destroy failure surface.
        if self._client.get_instance_status(VpsInstanceId(handle)) in (
            VpsInstanceStatus.DESTROYING,
            VpsInstanceStatus.UNKNOWN,
        ):
            return
        self._client.destroy_instance(VpsInstanceId(handle))

    def is_backend_clean(self, handle: str) -> bool:
        # A force-terminated instance reports DESTROYING (terminated, still listed briefly) or
        # UNKNOWN (dropped from the API) -- either way no running/stopped compute leaks.
        return self._client.get_instance_status(VpsInstanceId(handle)) in (
            VpsInstanceStatus.DESTROYING,
            VpsInstanceStatus.UNKNOWN,
        )


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
