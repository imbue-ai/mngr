import time
from collections.abc import Mapping
from collections.abc import Sequence
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
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr_ovh._tag_keys import MNGR_RECYCLING_LOCK_TAG_KEY
from imbue.mngr_ovh.config import OvhProviderConfig
from imbue.mngr_vps.errors import VpsApiError
from imbue.mngr_vps.errors import VpsProvisioningError
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vps.primitives import VpsInstanceStatus
from imbue.mngr_vps.vps_client import VpsClientInterface

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
# Only `todo` and `doing` are valid for OVH's `?state=` task filter; other
# values (e.g. `init`) return HTTP 400 BadParametersError.
_TASK_ACTIVE_STATE_FILTERS: Final[tuple[str, ...]] = ("todo", "doing")

# OVH's billing subsystem returns this HTTP 400 message for the first
# few minutes after a fresh VPS order, even though the VPS itself is
# already running. Verified live on 2026-05-18: ``PUT /vps/{s}/serviceInfos``
# right after ``order_and_wait_for_vps`` returned failed with this
# exact message; a 30-second retry succeeded. The string is an
# OVH-side standard for "subscription state not yet propagated to the
# billing layer." See F39 in OVH_AUDIT.md.
_SUBSCRIPTION_NOT_ACTIVE_MARKER: Final[str] = "subscription is not active yet"

# Total retry budget for ``set_renew_at_expiration`` when OVH responds
# with ``_SUBSCRIPTION_NOT_ACTIVE_MARKER``. Live observation showed
# the activation propagates in ~30 seconds for a fresh ``vps-2025-model1``
# order; 5 minutes leaves comfortable headroom for slower billing-side
# propagation. Cap matters because the cleanup path in
# ``OvhProvider._terminate_orphaned_fresh_order`` invokes this method
# from a ``finally`` branch we don't want to block forever.
_SET_RENEW_RETRY_TIMEOUT_SECONDS: Final[float] = 300.0
_SET_RENEW_RETRY_POLL_INTERVAL_SECONDS: Final[float] = 15.0


class _PutServiceInfosAttempt(FrozenModel):
    """Single-shot callable for ``OvhVpsClient._put_service_infos_with_retry``.

    ``poll_for_value`` expects a no-arg callable returning ``None``
    when it should retry, non-``None`` when it has a real value. We
    return :class:`True` on a successful PUT and ``None`` for retryable
    transient failures: OVH's ``"subscription is not active yet"`` 400
    (the billing layer lagging a fresh order) and transport-level errors
    (which :meth:`OvhVpsClient._call` tags with ``status_code == 0`` --
    e.g. a dropped connection during the failure-cleanup cancel that
    would otherwise leak a freshly-ordered month of billing). Anything
    else propagates.

    Lifted to module scope (vs. an inline ``def`` inside the method)
    so the project ratchet against inline functions is satisfied and
    so the call shape is explicitly testable. See F39 in OVH_AUDIT.md.
    """

    model_config = {"arbitrary_types_allowed": True, "frozen": True}

    client: "OvhVpsClient"
    path: str
    info: dict[str, Any]

    def __call__(self) -> bool | None:
        try:
            self.client._call("PUT", self.path, **self.info)
            return True
        except VpsApiError as e:
            if e.status_code == 400 and _SUBSCRIPTION_NOT_ACTIVE_MARKER in str(e):
                logger.info(
                    "OVH PUT {} returned 'subscription is not active yet'; "
                    "the billing layer hasn't propagated this VPS's subscription state yet. Will retry.",
                    self.path,
                )
                return None
            # Transient transport failure (dropped connection, timeout):
            # ``_call`` tags these with status_code 0. Retrying matters most
            # for the failure-cleanup cancel, where a single dropped
            # connection would otherwise leak a freshly-ordered month of
            # billing.
            if e.status_code == 0:
                logger.warning(
                    "OVH PUT {} failed with a transient transport error; will retry: {}",
                    self.path,
                    e,
                )
                return None
            raise


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
    set_renew_retry_timeout_seconds: float = Field(
        default=_SET_RENEW_RETRY_TIMEOUT_SECONDS,
        description=(
            "Total retry budget for ``set_renew_at_expiration`` when OVH responds with "
            '``"subscription is not active yet"``. See :data:`_SET_RENEW_RETRY_TIMEOUT_SECONDS`.'
        ),
    )
    set_renew_retry_poll_interval_seconds: float = Field(
        default=_SET_RENEW_RETRY_POLL_INTERVAL_SECONDS,
        description="Sleep between retries inside ``set_renew_at_expiration``'s subscription-not-active loop.",
    )
    is_unconfigured: bool = Field(
        default=False,
        description=(
            "True iff this client was constructed with placeholder credentials because no "
            "OVH credentials were configured. OvhProviderBackend.build_provider_instance "
            "detects this and raises ProviderNotAuthorizedError instead of constructing a "
            "provider whose API calls would all fail; the placeholder keeps build_ovh_client "
            "total so environments (e.g. CI for unrelated tests) that merely enumerate "
            "registered backends still work."
        ),
    )

    _ssh_key_cache: dict[str, str] = PrivateAttr(default_factory=dict)
    _pending_recycle_handles: dict[str, RecycleHandle] = PrivateAttr(default_factory=dict)

    def register_recycle_handle(self, handle: RecycleHandle) -> None:
        """Record an in-flight ``RecycleHandle``.

        Used by ``recycle.try_recycle_cancelled_vps`` so that if the
        base ``VpsProvider.create_host`` cleanup calls
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
        tags: Mapping[str, str],
    ) -> VpsInstanceId:
        raise NotImplementedError(
            "OVH VPS provisioning is multi-step (order + rebuild + TOFU); "
            "OvhProvider overrides _provision_vps to drive that flow."
        )

    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        """Cancel an OVH VPS so it stops auto-renewing past its next expiration.

        OVH offers two cancellation paths:

        - ``POST /vps/{s}/terminate`` queues termination behind an
          email confirmation step (``POST /confirmTermination`` with
          the emailed token). Without that confirmation, the VPS
          continues to auto-renew indefinitely. This is the right call
          for an interactive human flow but useless for an unattended
          CLI.
        - ``PUT /vps/{s}/serviceInfos`` with ``renew.deleteAtExpiration=True``
          flips the auto-renewal flag directly, no email needed. OVH
          then decommissions the VPS at the next ``expiration`` date.
          Verified live against the OVH-US API.

        We use the serviceInfos path so ``mngr destroy`` actually stops
        the meter. The remainder of the already-paid period is still
        forfeit (OVH does not prorate classic VPS cancellations); for
        monthly subscriptions that is the rest of the current month,
        for ``UPFRONT6`` / ``UPFRONT12`` it can be up to 6 / 12 months
        of prepaid balance respectively. The VPS will not auto-renew
        past the next OVH-side expiration date.

        If this VPS is currently mid-recycle (a ``RecycleHandle`` was
        registered via ``register_recycle_handle`` but neither
        ``finalize_recycle`` nor ``abort_recycle`` has run yet), this
        call short-circuits to release the recycle lock only. The VPS
        is already cancelled in OVH's eyes; flipping the flag again is
        a no-op and releasing the lock lets a subsequent ``mngr create``
        re-attempt the recycle.
        """
        service_name = str(instance_id)
        handle = self._pending_recycle_handles.pop(service_name, None)
        if handle is not None:
            logger.info("OVH VPS {} is mid-recycle; releasing recycle lock instead of re-cancelling", service_name)
            try:
                self._call("DELETE", f"/v2/iam/resource/{handle.urn}/tag/{MNGR_RECYCLING_LOCK_TAG_KEY}")
            except VpsApiError as e:
                if e.status_code != 404:
                    logger.warning("OVH recycle lock release failed for {}: {}", service_name, e)
            return
        try:
            self.set_renew_at_expiration(service_name, True)
            logger.info(
                "Cancelled OVH VPS {} (deleteAtExpiration=true; "
                "decommissions at next OVH expiration date, any already-paid balance is forfeit)",
                instance_id,
            )
        except VpsApiError as e:
            logger.warning("OVH VPS {} cancellation request failed: {}", instance_id, e)
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
        ``PUT /vps/{s}/serviceInfos``. No email token required for either
        direction (verified live on US-EAST-VA).

        Setting ``True`` only flips ``deleteAtExpiration``; OVH auto-flips
        ``renew.automatic`` to ``False`` and ``renewalType`` to ``"manual"``
        as a server-side side effect of the cancellation. Setting ``False``
        un-cancels, but OVH does **not** auto-restore ``automatic`` /
        ``renewalType``, so this function explicitly restores both on the
        un-cancel path; otherwise a recycled VPS would silently fail to
        renew at the next anniversary even though our flag flip succeeded.

        Retries on the OVH ``"subscription is not active yet"`` 400 error
        (see :data:`_SUBSCRIPTION_NOT_ACTIVE_MARKER` for full context) and
        on transient transport failures (a dropped connection / timeout,
        tagged ``status_code == 0`` by :meth:`_call`). The billing
        subsystem takes a few minutes to propagate a fresh order's
        subscription state, during which any ``PUT serviceInfos`` call
        fails with that specific message; without the retry, the
        ``OvhProvider._terminate_orphaned_fresh_order`` cleanup loses
        the race and silently leaks a freshly-ordered month of billing
        (F39 in OVH_AUDIT.md). Verified live: a single 30-second-later
        retry succeeded.
        """
        info = self.get_service_info(service_name)
        renew = dict(info.get("renew") or {})
        renew["deleteAtExpiration"] = delete_at_expiration
        if not delete_at_expiration:
            renew["automatic"] = True
            info["renewalType"] = "automaticV2012"
        info["renew"] = renew
        self._put_service_infos_with_retry(service_name, info)

    def _put_service_infos_with_retry(self, service_name: str, info: dict[str, Any]) -> None:
        """``PUT /vps/{s}/serviceInfos`` with retry on transient failures.

        Wraps :meth:`_call` so OTHER 400s / 404s / 5xxs propagate
        immediately. Two transient conditions trigger retry: the OVH-side
        ``"subscription is not active yet"`` message (a documented
        transient state the billing system reports for the first few
        minutes after a fresh order; F39 in OVH_AUDIT.md) and transport
        failures (``status_code == 0``: dropped connection / timeout),
        which would otherwise fail a failure-cleanup cancel and leak a
        freshly-ordered month of billing.
        """
        path = f"/vps/{service_name}/serviceInfos"
        attempt = _PutServiceInfosAttempt(client=self, path=path, info=info)
        result, polls, elapsed = poll_for_value(
            attempt,
            timeout=self.set_renew_retry_timeout_seconds,
            poll_interval=self.set_renew_retry_poll_interval_seconds,
        )
        if result is None:
            raise VpsApiError(
                400,
                f"OVH {path} kept failing with a retryable transient error ('subscription is not active yet' "
                f"or a transport failure) after {polls} attempt(s) over {elapsed:.0f}s. "
                "Manual cleanup may be needed.",
            )
        if polls > 1:
            logger.info(
                "OVH PUT {} succeeded after {} attempt(s) in {:.1f}s (billing-layer propagation race)",
                path,
                polls,
                elapsed,
            )

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

    def wait_for_no_active_tasks(
        self,
        service_name: str,
        timeout_seconds: float,
    ) -> None:
        """Block until ``/vps/{s}`` has no tasks in ``todo`` or ``doing`` state.

        OVH's ``order/cart`` flow returns once the new ``serviceName`` is
        visible in ``GET /vps``, but a background ``deliverVm`` task is
        typically still running for ~1-2 minutes after that point.
        Subsequent mutating calls (``/rebuild`` in particular) fail with
        ``Action not available while there are running tasks on the VPS``
        until that task drains. This helper polls until both active-state
        filters return empty.

        Raises ``VpsProvisioningError`` on timeout.
        """
        deadline = time.monotonic() + timeout_seconds
        last_active: list[int] = []
        last_api_error: VpsApiError | None = None
        had_successful_poll = False
        while time.monotonic() < deadline:
            try:
                last_active = self._list_active_task_ids(service_name)
                had_successful_poll = True
                if not last_active:
                    return
            except VpsApiError as e:
                last_api_error = e
                logger.warning("Failed to list active OVH tasks for {}: {}", service_name, e)
            time.sleep(self.task_poll_interval)
        if had_successful_poll:
            raise VpsProvisioningError(
                f"OVH VPS {service_name} still has active tasks {last_active!r} after {timeout_seconds}s; "
                "subsequent /rebuild would race the in-flight task"
            )
        raise VpsProvisioningError(
            f"OVH VPS {service_name} tasks listing never succeeded within {timeout_seconds}s; "
            f"cannot confirm whether tasks are active. Last API error: {last_api_error!r}"
        )

    def _list_active_task_ids(self, service_name: str) -> list[int]:
        ids: list[int] = []
        for state in _TASK_ACTIVE_STATE_FILTERS:
            payload = self._call("GET", f"/vps/{service_name}/tasks?state={state}")
            if isinstance(payload, list):
                ids.extend(int(t) for t in payload)
        return ids

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


def build_ovh_client(config: OvhProviderConfig) -> "OvhVpsClient":
    """Construct an ``OvhVpsClient`` from config / env / ``~/.ovh.conf``.

    If no credentials are configured anywhere, ``python-ovh`` raises
    ``InvalidConfiguration`` at construction time. We catch that and
    substitute placeholder credentials so the client is still
    constructible -- this lets ``build_ovh_client`` itself stay total
    (e.g. so unrelated tests that merely enumerate registered backends
    run without OVH credentials). The returned client has
    ``is_unconfigured=True``, which ``OvhProviderBackend.build_provider_instance``
    detects and turns into a clear ``ProviderNotAuthorizedError`` rather than
    constructing a provider whose every API call is doomed to fail.
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
