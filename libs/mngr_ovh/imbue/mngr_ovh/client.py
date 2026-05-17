import time
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Final

import ovh
from loguru import logger
from ovh.exceptions import APIError
from ovh.exceptions import BadParametersError
from ovh.exceptions import Forbidden
from ovh.exceptions import HTTPError
from ovh.exceptions import InvalidConfiguration
from ovh.exceptions import InvalidCredential
from ovh.exceptions import NotCredential
from ovh.exceptions import NotGrantedCall
from ovh.exceptions import ResourceConflictError
from ovh.exceptions import ResourceNotFoundError
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.errors import MngrError
from imbue.mngr_ovh._tag_keys import MNGR_RECYCLING_LOCK_TAG_KEY
from imbue.mngr_ovh.config import OvhProviderConfig
from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.errors import VpsProvisioningError
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vps_docker.primitives import VpsInstanceStatus
from imbue.mngr_vps_docker.primitives import VpsSnapshotId
from imbue.mngr_vps_docker.vps_client import VpsClientInterface
from imbue.mngr_vps_docker.vps_client import VpsSnapshotInfo
from imbue.mngr_vps_docker.vps_client import VpsSshKeyInfo

_DEFAULT_VPS_TASK_POLL_INTERVAL: Final[float] = 5.0

_VPS_STATE_MAP: Final[dict[str, VpsInstanceStatus]] = {
    "running": VpsInstanceStatus.ACTIVE,
    "rescued": VpsInstanceStatus.ACTIVE,
    "stopped": VpsInstanceStatus.HALTED,
    "starting": VpsInstanceStatus.PENDING,
    "stopping": VpsInstanceStatus.PENDING,
    "installing": VpsInstanceStatus.PENDING,
    "maintenance": VpsInstanceStatus.PENDING,
    "rebooting": VpsInstanceStatus.PENDING,
    "rescuing": VpsInstanceStatus.PENDING,
    "unrescuing": VpsInstanceStatus.PENDING,
    "ko": VpsInstanceStatus.UNKNOWN,
}

_TASK_TERMINAL_STATES: Final[frozenset[str]] = frozenset({"done", "error", "cancelled", "blocked"})
_TASK_FAILURE_STATES: Final[frozenset[str]] = frozenset({"error", "cancelled", "blocked"})


class RecycleHandle(FrozenModel):
    """In-flight recycle of a cancelled OVH VPS.

    Returned by ``recycle.try_recycle_cancelled_vps`` once a candidate
    has been locked and re-tagged with the new host id, but **before**
    the VPS has been un-cancelled. Defined in this module (rather than
    in ``recycle.py``) so that ``OvhVpsClient.destroy_instance`` can
    consult the pending-handles dict without an import cycle.

    The caller drives the rest of provisioning and then calls either:
    - ``recycle.finalize_recycle(client, handle)`` -- flips
      ``deleteAtExpiration=False`` and releases the lock,
    - ``recycle.abort_recycle(client, handle)`` -- releases the lock
      only; the VPS stays cancelled and auto-decommissions at end of
      month so no orphan billing.
    """

    urn: str
    service_name: str
    lock_value: str


class OvhVpsClient(VpsClientInterface):
    """OVH classic-VPS API client built on the official ``python-ovh`` SDK.

    Wraps a small subset of the OVH API surface that the VPS Docker provider
    actually needs:
    - ``/vps`` and ``/vps/{s}/...`` for lifecycle, IP lookup, task polling,
      snapshots, and termination
    - ``/order/...`` for the multi-step VPS purchase flow (driven by
      ``OvhProvider`` via the helpers in ``ordering.py`` -- this client
      exposes ``ovh_call`` as the low-level escape hatch they share)

    Implementations of ``create_instance`` and ``wait_for_instance_active``
    intentionally raise ``NotImplementedError``: provisioning an OVH VPS is
    a multi-step order+rebuild+TOFU dance that doesn't fit the single-POST
    shape of ``VpsClientInterface.create_instance``. ``OvhProvider``
    overrides ``_provision_vps`` and drives that flow directly.

    ``upload_ssh_key`` / ``delete_ssh_key`` are in-memory shims: OVH classic
    VPS does not have an SSH-key store on the provider side -- public keys
    are passed inline to ``POST /vps/{s}/rebuild`` via the ``publicSshKey``
    field. The shim keeps the ``VpsClientInterface`` contract intact and
    lets ``OvhProvider`` resolve a returned key-id back to its pubkey via
    ``get_cached_public_key``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ovh_client: ovh.Client = Field(description="Authenticated python-ovh client")
    subsidiary: str = Field(default="US", description="OVHcloud subsidiary code (US, CA, GB, FR, ...)")
    task_poll_interval: float = Field(
        default=_DEFAULT_VPS_TASK_POLL_INTERVAL,
        description="Seconds between polls when waiting for a VPS task to complete",
    )
    is_unconfigured: bool = Field(
        default=False,
        description=(
            "True iff this client was constructed with placeholder credentials because no "
            "OVH credentials were configured. Used by OvhProvider to short-circuit "
            "discovery silently in environments (e.g. CI for unrelated tests) that "
            "merely enumerate registered backends."
        ),
    )

    _ssh_key_cache: dict[str, str] = PrivateAttr(default_factory=dict)
    _pending_recycle_handles: dict[str, RecycleHandle] = PrivateAttr(default_factory=dict)

    def register_recycle_handle(self, handle: RecycleHandle) -> None:
        """Record an in-flight ``RecycleHandle``.

        Used by ``recycle.try_recycle_cancelled_vps`` so that if the
        base ``VpsDockerProvider.create_host`` cleanup calls
        ``destroy_instance`` on a VPS that's mid-recycle,
        ``destroy_instance`` releases the recycle lock instead of
        terminating an already-cancelled VPS.
        """
        self._pending_recycle_handles[handle.service_name] = handle

    def discard_recycle_handle(self, service_name: str) -> None:
        """Drop the tracked ``RecycleHandle`` for ``service_name`` if any.

        Called by ``recycle.finalize_recycle`` and ``abort_recycle`` once
        they've taken over responsibility for releasing the lock.
        """
        self._pending_recycle_handles.pop(service_name, None)

    def get_recycle_handle(self, service_name: str) -> RecycleHandle | None:
        """Return the in-flight ``RecycleHandle`` for ``service_name`` if any."""
        return self._pending_recycle_handles.get(service_name)

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        """Invoke the OVH SDK and translate its exceptions to ``VpsApiError``."""
        try:
            return self.ovh_client.call(method, path, kwargs or None, True)
        except HTTPError as e:
            raise VpsApiError(0, f"OVH API {method} {path} transport failed: {e}") from e
        except APIError as e:
            status = _ovh_api_error_status_code(e)
            raise VpsApiError(status, f"OVH API {method} {path} returned error: {e}") from e

    def call_api(self, method: str, path: str, **kwargs: Any) -> Any:
        """Public escape hatch for helpers in the same package.

        Used by ``ordering.py`` / ``iam_tags.py`` to issue arbitrary OVH
        calls (e.g. ``/order/cart``, ``/v2/iam/resource/{urn}/tag``) through
        the same authenticated client, with uniform error mapping.
        """
        return self._call(method, path, **kwargs)

    def get_cached_public_key(self, key_id: str) -> str:
        """Return the public-key string that ``upload_ssh_key`` previously cached.

        Raises ``MngrError`` if the id is unknown -- the caller should
        always pass back exactly the id they got from ``upload_ssh_key``
        earlier in the same provider-instance process.
        """
        if key_id not in self._ssh_key_cache:
            raise MngrError(
                f"No cached OVH SSH public key for id {key_id!r}; "
                "OVH VPS keys live in-memory only and do not persist across processes."
            )
        return self._ssh_key_cache[key_id]

    # =========================================================================
    # Instance operations
    # =========================================================================

    def create_instance(
        self,
        label: str,
        region: str,
        plan: str,
        user_data: str,
        ssh_key_ids: Sequence[str],
        tags: Sequence[str],
    ) -> VpsInstanceId:
        raise NotImplementedError(
            "OVH VPS provisioning is multi-step (order + rebuild + TOFU); "
            "OvhProvider overrides _provision_vps to drive that flow."
        )

    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        """Request termination of an OVH VPS (or abort an in-flight recycle).

        OVH's termination is asynchronous and billing-anniversary aware:
        ``POST /vps/{s}/terminate`` registers the request and OVH emails a
        confirmation token. ``POST /vps/{s}/confirmTermination`` with that
        token finalizes termination. In practice for mngr's use case, the
        VPS is logically destroyed at this point even though the service
        may linger until the end of the billing period.

        If this VPS is currently mid-recycle (a ``RecycleHandle`` was
        registered via ``register_recycle_handle`` but neither
        ``finalize_recycle`` nor ``abort_recycle`` has run yet), this
        call short-circuits to release the recycle lock only. The VPS
        is already cancelled, so calling ``/terminate`` again would be
        a no-op on OVH's side; releasing the lock lets a subsequent
        ``mngr create`` re-attempt the recycle.
        """
        service_name = str(instance_id)
        handle = self._pending_recycle_handles.pop(service_name, None)
        if handle is not None:
            logger.info("OVH VPS {} is mid-recycle; releasing recycle lock instead of re-terminating", service_name)
            try:
                self._call("DELETE", f"/v2/iam/resource/{handle.urn}/tag/{MNGR_RECYCLING_LOCK_TAG_KEY}")
            except VpsApiError as e:
                if e.status_code != 404:
                    logger.warning("OVH recycle lock release failed for {}: {}", service_name, e)
            return
        try:
            self._call("POST", f"/vps/{instance_id}/terminate")
            logger.info("Requested termination of OVH VPS {} (billing remainder is forfeit)", instance_id)
        except VpsApiError as e:
            logger.warning("OVH VPS {} termination request failed: {}", instance_id, e)
            raise

    def get_instance_status(self, instance_id: VpsInstanceId) -> VpsInstanceStatus:
        try:
            info = self._call("GET", f"/vps/{instance_id}")
        except VpsApiError:
            return VpsInstanceStatus.UNKNOWN
        state = (info or {}).get("state", "")
        return _VPS_STATE_MAP.get(str(state), VpsInstanceStatus.UNKNOWN)

    def get_instance_ip(self, instance_id: VpsInstanceId) -> str:
        """Return an SSH-reachable hostname for the VPS.

        OVH ``serviceName`` is itself a DNS name like
        ``vps-eec8860b.vps.ovh.us`` that resolves to the VPS's public IPv4.
        That's sufficient for paramiko/pyinfra SSH targets. We fall through
        to ``/vps/{s}/ips`` only if the DNS-name shape isn't present (which
        would indicate a non-standard OVH product).
        """
        instance_str = str(instance_id)
        if "." in instance_str:
            return instance_str
        ips = self._call("GET", f"/vps/{instance_id}/ips")
        if not ips:
            raise VpsProvisioningError(f"OVH VPS {instance_id} has no IPs assigned yet")
        return str(ips[0])

    def wait_for_instance_active(
        self,
        instance_id: VpsInstanceId,
        timeout_seconds: float = 300.0,
    ) -> str:
        raise NotImplementedError(
            "OVH VPS provisioning is driven by OvhProvider._provision_vps, which "
            "uses wait_for_vps_delivery / wait_for_task helpers directly."
        )

    def list_instances(self) -> list[str]:
        """List ``serviceName`` for every VPS visible to this account."""
        result = self._call("GET", "/vps")
        if not isinstance(result, list):
            return []
        return [str(s) for s in result]

    def get_instance(self, instance_id: VpsInstanceId) -> dict[str, Any]:
        """Return the raw ``GET /vps/{s}`` payload."""
        return dict(self._call("GET", f"/vps/{instance_id}") or {})

    def get_service_info(self, service_name: str) -> dict[str, Any]:
        """Return the raw ``GET /vps/{s}/serviceInfos`` payload.

        Used by the recycle path to read the ``renew.deleteAtExpiration``
        flag and the ``expiration`` date, and as the basis for a
        read-modify-write to set or clear that flag.
        """
        return dict(self._call("GET", f"/vps/{service_name}/serviceInfos") or {})

    def set_renew_at_expiration(self, service_name: str, delete_at_expiration: bool) -> None:
        """Toggle whether the VPS is scheduled for deletion at the next billing boundary.

        Performs a read-modify-write on the full ``services.Service`` body to
        avoid clobbering unrelated fields (contact info, renewal type, etc.):
        ``GET /vps/{s}/serviceInfos`` → mutate ``renew.deleteAtExpiration`` →
        ``PUT /vps/{s}/serviceInfos``. Setting ``False`` is the way to undo a
        prior cancellation request (no email token required, verified live).
        Setting ``True`` is equivalent to the user clicking "confirm
        termination" in the email (also skips the email round-trip).
        """
        info = self.get_service_info(service_name)
        renew = dict(info.get("renew") or {})
        renew["deleteAtExpiration"] = delete_at_expiration
        info["renew"] = renew
        self._call("PUT", f"/vps/{service_name}/serviceInfos", **info)

    # =========================================================================
    # Task polling
    # =========================================================================

    def wait_for_task(
        self,
        service_name: str,
        task_id: int,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """Poll a VPS task until it reaches a terminal state.

        Raises ``VpsProvisioningError`` on terminal failure
        (``error``/``cancelled``/``blocked``) or timeout. Returns the final
        task payload on success.
        """
        deadline = time.monotonic() + timeout_seconds
        last_payload: dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                payload = self._call("GET", f"/vps/{service_name}/tasks/{task_id}")
            except VpsApiError as e:
                logger.warning("Failed to read OVH task {}/{}: {}", service_name, task_id, e)
                time.sleep(self.task_poll_interval)
                continue
            last_payload = dict(payload or {})
            state = str(last_payload.get("state", ""))
            if state in _TASK_TERMINAL_STATES:
                if state in _TASK_FAILURE_STATES:
                    raise VpsProvisioningError(
                        f"OVH task {task_id} ({last_payload.get('type', '?')}) on {service_name} "
                        f"ended in state {state!r}: {last_payload!r}"
                    )
                return last_payload
            time.sleep(self.task_poll_interval)
        raise VpsProvisioningError(
            f"OVH task {task_id} ({last_payload.get('type', '?')}) on {service_name} "
            f"did not finish within {timeout_seconds}s (last state: {last_payload.get('state', '?')})"
        )

    # =========================================================================
    # Snapshot operations (VPS-level)
    # =========================================================================

    def create_snapshot(self, instance_id: VpsInstanceId, description: str) -> VpsSnapshotId:
        """Create a VPS-level snapshot. OVH supports at most one snapshot per VPS.

        Returns a ``VpsSnapshotId`` equal to the owning ``serviceName``. OVH's
        single-snapshot-per-VPS model means the snapshot is identified solely
        by which VPS owns it (``DELETE /vps/{s}/snapshot`` operates on the
        only slot), so encoding the serviceName here keeps ``create_snapshot``
        / ``list_snapshots`` / ``delete_snapshot`` round-trippable.
        """
        existing = self._safe_get_snapshot(instance_id)
        if existing is not None:
            raise MngrError(
                f"OVH VPS {instance_id} already has a snapshot ({existing.get('id', '?')}); "
                "delete it first -- OVH supports at most one snapshot per VPS."
            )
        result = self._call("POST", f"/vps/{instance_id}/createSnapshot", description=description)
        task_id = int((result or {}).get("id", 0))
        if not task_id:
            raise VpsApiError(0, f"OVH createSnapshot on {instance_id} returned no task id")
        self.wait_for_task(str(instance_id), task_id, timeout_seconds=900.0)
        snap = self._safe_get_snapshot(instance_id)
        if snap is None:
            raise MngrError(f"OVH createSnapshot completed but no snapshot returned for {instance_id}")
        return VpsSnapshotId(str(instance_id))

    def delete_snapshot(self, snapshot_id: VpsSnapshotId) -> None:
        """Delete the snapshot whose id (== owning ``serviceName`` for OVH) is given.

        OVH's ``DELETE /vps/{s}/snapshot`` deletes the VPS's single snapshot
        slot; the snapshot is identified solely by which VPS owns it. We
        encode the owning serviceName into the ``VpsSnapshotId`` returned
        from ``create_snapshot``.
        """
        self._call("DELETE", f"/vps/{snapshot_id}/snapshot")
        logger.info("Deleted OVH snapshot for VPS {}", snapshot_id)

    def list_snapshots(self) -> list[VpsSnapshotInfo]:
        """Return all snapshots across every VPS this account owns.

        OVH has no global snapshot index, so this iterates ``/vps`` and
        queries each VPS's single snapshot slot.
        """
        snapshots: list[VpsSnapshotInfo] = []
        for service_name in self.list_instances():
            snap = self._safe_get_snapshot(VpsInstanceId(service_name))
            if snap is None:
                continue
            snapshots.append(_snapshot_info_from_payload(service_name, snap))
        return snapshots

    def _safe_get_snapshot(self, instance_id: VpsInstanceId) -> dict[str, Any] | None:
        try:
            payload = self._call("GET", f"/vps/{instance_id}/snapshot")
        except VpsApiError as e:
            if e.status_code == 404:
                return None
            raise
        if not payload:
            return None
        return dict(payload)

    # =========================================================================
    # SSH key shim
    # =========================================================================

    def upload_ssh_key(self, name: str, public_key: str) -> str:
        """In-memory cache: OVH classic VPS has no SSH key store.

        The returned id is the (caller-supplied) name; the public key is
        cached so ``OvhProvider._provision_vps`` can later resolve the id
        back into the actual key string for ``POST /vps/{s}/rebuild``.
        """
        self._ssh_key_cache[name] = public_key
        return name

    def delete_ssh_key(self, key_id: str) -> None:
        self._ssh_key_cache.pop(key_id, None)

    def list_ssh_keys(self) -> list[VpsSshKeyInfo]:
        return [VpsSshKeyInfo(id=key, name=key) for key in self._ssh_key_cache]


def _ovh_api_error_status_code(error: APIError) -> int:
    """Map a python-ovh ``APIError`` subclass to its HTTP status code.

    The SDK doesn't expose an ``http_status`` attribute; instead each
    well-known status maps to a specific exception subclass. We return ``0``
    for anything we don't recognise so callers can fall through to the
    string form of the error for diagnostics.
    """
    if isinstance(error, ResourceNotFoundError):
        return 404
    if isinstance(error, BadParametersError):
        return 400
    if isinstance(error, (Forbidden, NotGrantedCall)):
        return 403
    if isinstance(error, (InvalidCredential, NotCredential)):
        return 401
    if isinstance(error, ResourceConflictError):
        return 409
    return 0


def _snapshot_info_from_payload(service_name: str, payload: dict[str, Any]) -> VpsSnapshotInfo:
    created_raw = payload.get("creationDate", "")
    try:
        created_at = datetime.fromisoformat(str(created_raw))
    except ValueError:
        created_at = datetime.now(timezone.utc)
    return VpsSnapshotInfo(
        id=VpsSnapshotId(service_name),
        description=str(payload.get("description", "")),
        created_at=created_at,
    )


def build_ovh_client(config: OvhProviderConfig) -> "OvhVpsClient":
    """Construct an ``OvhVpsClient`` from config / env / ``~/.ovh.conf``.

    If no credentials are configured anywhere, ``python-ovh`` raises
    ``InvalidConfiguration`` at construction time. We catch that and
    substitute placeholder credentials so the client is still
    constructible -- any actual API call will then fail with a clear
    auth error rather than the provider blowing up at registration
    time. This mirrors how ``mngr_vultr`` accepts an empty API key when
    none is configured, and lets unrelated tests that merely enumerate
    registered backends run without OVH credentials. The returned
    client has ``is_unconfigured=True`` so ``OvhProvider`` can
    short-circuit discovery without emitting log noise.
    """
    kwargs = config.resolve_python_ovh_kwargs()
    try:
        raw_client = ovh.Client(**kwargs)
        is_unconfigured = False
    except InvalidConfiguration:
        logger.debug(
            "OVH credentials not configured; constructing a placeholder client. "
            "OVH provider API calls will fail until credentials are provided."
        )
        raw_client = ovh.Client(
            endpoint=kwargs.get("endpoint", "ovh-us"),
            application_key="mngr-ovh-unconfigured",
            application_secret="mngr-ovh-unconfigured",
            consumer_key="mngr-ovh-unconfigured",
        )
        is_unconfigured = True
    return OvhVpsClient(
        ovh_client=raw_client,
        subsidiary=config.ovh_subsidiary,
        is_unconfigured=is_unconfigured,
    )
