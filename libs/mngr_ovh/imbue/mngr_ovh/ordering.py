import time
from collections.abc import Mapping
from typing import Any

from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mngr.errors import MngrError
from imbue.mngr_ovh.catalog import find_required_field
from imbue.mngr_ovh.catalog import validate_datacenter
from imbue.mngr_ovh.client import OvhVpsClient
from imbue.mngr_vps.errors import VpsApiError
from imbue.mngr_vps.errors import VpsProvisioningError
from imbue.mngr_vps.primitives import VpsInstanceId


class OvhOrderDeliveryTimeoutError(VpsProvisioningError):
    """Raised when an OVH order succeeds at checkout but doesn't deliver a VPS in time.

    Carries ``order_id`` so the caller can write a pending-order marker
    (see ``pending_orders.py``) for the next bake's
    ``_reconcile_pending_orders`` sweep to pick up. Subclasses
    :class:`VpsProvisioningError` so existing ``except VpsProvisioningError``
    blocks (e.g. the cart-cleanup branch in :func:`order_and_wait_for_vps`)
    still catch it; only the call sites that want to react to the order_id
    need to special-case it.
    """

    def __init__(self, *, order_id: int, timeout_seconds: float, last_status: str) -> None:
        self.order_id = order_id
        self.timeout_seconds = timeout_seconds
        self.last_status = last_status
        super().__init__(
            f"OVH order {order_id} did not produce a VPS serviceName within {timeout_seconds}s; "
            f"last status: {last_status}"
        )


# OVH's ``billing.OrderDetail.domain`` is always the literal ``"*"`` for
# VPS orders -- empirically verified on 2026-05-18 against the live OVH-US
# API by walking every detail of a recent order. We never want to treat
# this as a real serviceName, so spawn sites filter on it explicitly.
_BILLING_DETAIL_DOMAIN_PLACEHOLDER: str = "*"

# Value of ``billing.ItemDetail.order.plan.product.name`` for a VPS line
# item, used to disambiguate the VPS detail from the OS / backup /
# installation line items that appear in the same order.
_OVH_VPS_PRODUCT_NAME: str = "virtualPrivateServer"

_OVH_DELIVERY_POLL_INTERVAL_SECONDS: float = 10.0
# Cap on how long the post-delivery `deliverVm` task is allowed to run before
# we give up. Verified live at ~1-2min on `vps-2025-model1`; 10min leaves
# comfortable headroom for slower install paths.
_OVH_POST_DELIVERY_TASK_DRAIN_TIMEOUT_SECONDS: float = 600.0
# Shorter sanity-check drain immediately before /rebuild. The fresh-order
# path has already waited at the end of order_and_wait_for_vps, so this is
# usually a single round-trip that returns immediately; it exists to cover
# the recycle path and to defend against a task slipping in after the
# initial wait.
_OVH_REBUILD_PREFLIGHT_DRAIN_SECONDS: float = 180.0
# How long to keep retrying the /rebuild POST itself when OVH rejects it with
# "...running tasks on the VPS". The task listing that
# wait_for_no_active_tasks polls is eventually consistent: it can report no
# active tasks while OVH still refuses the rebuild because the post-delivery
# deliverVm task is in flight. OVH's own rejection of the action is therefore
# the authoritative signal, so we re-drain and retry the POST until it takes.
# deliverVm runs ~1-2min, so 300s is ample headroom.
_OVH_REBUILD_START_RETRY_TIMEOUT_SECONDS: float = 300.0
# Substring identifying OVH's in-flight-task rejection of a mutating action.
_OVH_RUNNING_TASKS_ERROR_MARKER: str = "running tasks"


def order_and_wait_for_vps(
    client: OvhVpsClient,
    *,
    plan_code: str,
    datacenter: str,
    image_name: str,
    pricing_mode: str,
    duration: str,
    deliver_timeout_seconds: float,
    install_rtm: bool = False,
) -> str:
    """Drive the OVH order/cart flow for a single VPS and return its serviceName.

    Steps:
        1. ``POST /order/cart`` (subsidiary scoped) to get a cart id.
        2. ``POST /order/cart/{id}/vps`` to add a VPS item (plan + pricing).
        3. ``POST /order/cart/{id}/item/{itemId}/configuration`` once per required
           field (datacenter + OS).
        4. ``POST /order/cart/{id}/assign`` to attach the cart to the account.
        5. ``POST /order/cart/{id}/checkout`` to place the order. Capture
           the returned ``order.Order.orderId``; this is the link between
           our checkout call and the new VPS.
        6. Poll the ``/me/order/{orderId}/details`` chain until the VPS
           detail's operation reports its assigned ``resource.name``.
           See :func:`_wait_for_service_name_from_order` for the exact
           polling shape. **Strong correlation: no shared-account race**
           is possible because every poll is scoped to OUR ``orderId``
           and OVH only reports OUR order's resources via OUR orderId.
           The previous implementation diff'd ``GET /vps`` before vs
           after checkout, which silently picked the wrong serviceName
           when a concurrent order finished delivering during our wait
           (F3 in OVH_AUDIT.md). Verified against the live OVH API:
           the ``billing.OrderDetail.domain`` field is the literal
           ``"*"`` for VPS orders (useless for correlation); the
           ``service.Operation.resource.name`` chain is the only
           OVH-side path that actually yields the assigned serviceName.
        7. Wait for the post-delivery ``deliverVm`` task to drain. The
           serviceName becomes visible in ``GET /vps`` before this task
           finishes; any mutating call (e.g. ``/rebuild``) issued in the
           interim fails with "Action not available while there are
           running tasks on the VPS".
        8. Post-hoc verify: ``GET /vps/{serviceName}`` and assert
           ``model.name == plan_code`` and ``zone`` contains
           ``datacenter`` (case-insensitive, matching the recycle path's
           filter). Defends against OVH ever delivering a VPS of the
           wrong shape and against any future bug in this function's
           cart construction.

    Returns the new VPS's serviceName. Raises ``VpsProvisioningError`` on
    timeout or any step failure.
    """
    with log_span("OVH order cart flow for plan={} datacenter={}", plan_code, datacenter):
        cart = client.call_api("POST", "/order/cart", ovhSubsidiary=client.subsidiary)
        cart_id = str((cart or {}).get("cartId", ""))
        if not cart_id:
            raise VpsProvisioningError(f"OVH /order/cart returned no cartId: {cart!r}")
        logger.debug("OVH cart created: {}", cart_id)

        try:
            item = client.call_api(
                "POST",
                f"/order/cart/{cart_id}/vps",
                planCode=plan_code,
                pricingMode=pricing_mode,
                duration=duration,
                quantity=1,
            )
            item_id = int((item or {}).get("itemId", 0))
            if not item_id:
                raise VpsProvisioningError(f"OVH cart {cart_id} returned no itemId: {item!r}")

            required = client.call_api("GET", f"/order/cart/{cart_id}/item/{item_id}/requiredConfiguration")
            if not isinstance(required, list):
                raise VpsProvisioningError(f"Unexpected requiredConfiguration shape: {required!r}")

            dc_field = find_required_field(required, "vps_datacenter")
            allowed_dcs = list(dc_field.get("allowedValues") or [])
            validate_datacenter(allowed_dcs, datacenter)

            os_field = find_required_field(required, "vps_os")
            allowed_os = list(os_field.get("allowedValues") or [])
            if image_name not in allowed_os:
                raise MngrError(
                    f"OVH OS {image_name!r} not available for plan {plan_code}; valid options: {sorted(allowed_os)}"
                )

            _set_configuration(client, cart_id, item_id, "vps_datacenter", datacenter)
            _set_configuration(client, cart_id, item_id, "vps_os", image_name)
            _set_configuration(client, cart_id, item_id, "vps_install_rtm", "if_available" if install_rtm else "no")

            client.call_api("POST", f"/order/cart/{cart_id}/assign")
            order_response = client.call_api(
                "POST", f"/order/cart/{cart_id}/checkout", autoPayWithPreferredPaymentMethod=True
            )
            order_id = _extract_order_id(order_response, cart_id=cart_id)

            logger.info("OVH order placed (cart={}, order_id={}); waiting for VPS delivery", cart_id, order_id)
            # Poll the /me/order/{orderId}/details chain until OVH
            # assigns this order's VPS a serviceName. The ``domain``
            # field on the ``billing.OrderDetail`` is always the literal
            # ``"*"`` for VPS orders (verified live); the assigned
            # serviceName only appears via the operation's
            # ``resource.name`` chain.
            service_name = _wait_for_service_name_from_order(
                client,
                order_id=order_id,
                requested_plan_code=plan_code,
                timeout_seconds=deliver_timeout_seconds,
            )
            logger.info("OVH order {} produced serviceName {!r}", order_id, service_name)

            client.wait_for_no_active_tasks(
                service_name,
                timeout_seconds=_OVH_POST_DELIVERY_TASK_DRAIN_TIMEOUT_SECONDS,
            )
            _verify_vps_matches_order(
                client,
                service_name=service_name,
                requested_plan=plan_code,
                requested_datacenter=datacenter,
            )
            return service_name
        except MngrError:
            _safe_delete_cart(client, cart_id)
            raise


def try_poll_order_for_delivered_vps(
    client: OvhVpsClient,
    *,
    order_id: int,
    plan_code: str,
) -> str | None:
    """One-shot poll of an OVH order's details/operations chain. Returns the serviceName or None.

    Wraps :func:`_try_fetch_order_service_name` with a public name + a
    fixed "single sweep" semantic so callers (notably the
    pending-orders reconcile sweep) don't accidentally drag in the
    blocking poll loop ``_wait_for_service_name_from_order`` uses.
    A bake under reconcile can have multiple pending orders to check;
    waiting on each one would balloon the bake's startup time.

    Returns ``None`` when:
      - OVH hasn't yet allocated this order's VPS detail (delivery still pending).
      - The fetch hit any transient API error (logged at DEBUG).
    Either way the caller should leave the pending-order marker in
    place so the next reconcile re-checks.
    """
    service_name, _status = _try_fetch_order_service_name(client, order_id=order_id, requested_plan_code=plan_code)
    return service_name


def _set_configuration(
    client: OvhVpsClient,
    cart_id: str,
    item_id: int,
    label: str,
    value: str,
) -> None:
    client.call_api(
        "POST",
        f"/order/cart/{cart_id}/item/{item_id}/configuration",
        label=label,
        value=value,
    )


def _safe_delete_cart(client: OvhVpsClient, cart_id: str) -> None:
    try:
        client.call_api("DELETE", f"/order/cart/{cart_id}")
    except MngrError as e:
        logger.debug("Failed to clean up OVH cart {}: {}", cart_id, e)


def _extract_order_id(order_response: Any, *, cart_id: str) -> int:
    """Pull the ``orderId`` out of a ``POST /order/cart/{id}/checkout`` response.

    The OVH ``order.Order`` schema declares ``orderId: long`` (nullable
    per spec but always populated in practice for a successful checkout).
    Raises ``VpsProvisioningError`` if missing -- we cannot correlate
    the resulting VPS to OUR order without it.
    """
    if not isinstance(order_response, dict):
        raise VpsProvisioningError(f"OVH /order/cart/{cart_id}/checkout returned a non-dict body: {order_response!r}")
    raw_order_id = order_response.get("orderId")
    if raw_order_id is None:
        raise VpsProvisioningError(
            f"OVH /order/cart/{cart_id}/checkout returned no orderId; "
            "cannot correlate the resulting VPS to our specific order. "
            f"Body: {order_response!r}"
        )
    try:
        return int(raw_order_id)
    except (TypeError, ValueError) as exc:
        raise VpsProvisioningError(
            f"OVH /order/cart/{cart_id}/checkout returned non-integer orderId {raw_order_id!r}: {exc}"
        ) from exc


def _wait_for_service_name_from_order(
    client: OvhVpsClient,
    *,
    order_id: int,
    requested_plan_code: str,
    timeout_seconds: float,
) -> str:
    """Poll the OVH order/details/operations chain until our VPS's serviceName appears.

    OVH's order processing is asynchronous. The checkout response
    returns immediately but the VPS's serviceName ("vps-XXX.vps.ovh.us")
    is only assigned during the delivery phase (typically 30-90s on
    ``vps-2025-model1``, can take longer on bigger plans / busier
    regions). To find OUR order's serviceName specifically, we walk:

        GET /me/order/{orderId}/details
            -> list of detailIds
        For each detailId:
            GET /me/order/{orderId}/details/{detailId}/extension
                -> billing.ItemDetail; check
                   ``order.plan.code == requested_plan_code`` AND
                   ``order.plan.product.name == "virtualPrivateServer"``
                   to identify the VPS detail (vs the OS / backup /
                   installation line items that show up alongside it).
            GET /me/order/{orderId}/details/{detailId}/operations
                -> list of operationIds (empty until OVH has assigned
                   the resource, which happens during delivery).
            For each operationId:
                GET /me/order/{orderId}/details/{detailId}/operations/{operationId}
                    -> service.Operation; ``resource.name`` is the
                       assigned serviceName.

    **Strong correlation:** every poll is scoped to OUR ``orderId``, so
    a concurrent ``order_and_wait_for_vps`` against the same OVH
    account sees only ITS order's resources. No race possible.

    Verified against the live OVH-US API on 2026-05-18: the
    ``billing.OrderDetail.domain`` field is always the literal ``"*"``
    for VPS orders (useless for correlation); the operation's
    ``resource.name`` IS the serviceName once delivery completes.

    Polling is on a single sleep at the end of each iteration so the
    per-iteration latency is uniform.

    Raises :class:`OvhOrderDeliveryTimeoutError` on timeout (a
    :class:`VpsProvisioningError` subclass that carries the order_id so the
    caller can attempt a post-hoc adoption of any slowly-delivered VPS).
    """
    deadline = time.monotonic() + timeout_seconds
    last_log_message: str = "no successful poll yet"
    while time.monotonic() < deadline:
        service_name, last_log_message = _try_fetch_order_service_name(
            client, order_id=order_id, requested_plan_code=requested_plan_code
        )
        if service_name:
            return service_name
        time.sleep(_OVH_DELIVERY_POLL_INTERVAL_SECONDS)
    raise OvhOrderDeliveryTimeoutError(
        order_id=order_id,
        timeout_seconds=timeout_seconds,
        last_status=last_log_message,
    )


def _try_fetch_order_service_name(
    client: OvhVpsClient, *, order_id: int, requested_plan_code: str
) -> tuple[str | None, str]:
    """One sweep through the order/details/extension/operations chain.

    Returns ``(service_name, status_message)``. ``service_name`` is the
    assigned VPS serviceName when found, ``None`` otherwise. The status
    message is for the timeout error path.

    Skips details whose extension's ``order.plan.code`` doesn't match
    the requested plan -- a VPS order also produces non-VPS line items
    ("VPS-1" installation, "Linux" OS, optional "Option Automated
    Backup Standard - VPS-1") that have their own resources we don't
    want to confuse with the actual VPS serviceName.
    """
    try:
        raw_detail_ids = client.call_api("GET", f"/me/order/{order_id}/details")
    except VpsApiError as e:
        logger.debug("OVH order-detail listing not ready yet for order {}: {}", order_id, e)
        return None, f"GET /me/order/{order_id}/details failed: {e}"
    if not isinstance(raw_detail_ids, list) or not raw_detail_ids:
        return None, f"GET /me/order/{order_id}/details returned empty list (order not yet materialised)"
    matched_detail_count = 0
    for detail_id in raw_detail_ids:
        if not _detail_extension_matches_plan(
            client, order_id=order_id, detail_id=detail_id, requested_plan_code=requested_plan_code
        ):
            continue
        matched_detail_count += 1
        service_name = _fetch_first_operation_resource_name(client, order_id=order_id, detail_id=detail_id)
        if service_name:
            return service_name, "ok"
    if matched_detail_count == 0:
        return (
            None,
            f"GET /me/order/{order_id}/details/* returned {len(raw_detail_ids)} detail(s) "
            f"but none had extension.order.plan.code == {requested_plan_code!r} (order not yet decomposed)",
        )
    return (
        None,
        f"matched {matched_detail_count} detail(s) for plan {requested_plan_code!r} but none had a populated "
        "operation.resource.name yet (delivery in progress)",
    )


def _detail_extension_matches_plan(
    client: OvhVpsClient, *, order_id: int, detail_id: int, requested_plan_code: str
) -> bool:
    """Return True iff this detail's extension says it's a VPS line item for our plan.

    Matches on both ``order.plan.code == requested_plan_code`` AND
    ``order.plan.product.name == "virtualPrivateServer"`` to defend
    against the (unlikely) case that OVH reuses the same plan code
    across products.
    """
    try:
        extension = client.call_api("GET", f"/me/order/{order_id}/details/{detail_id}/extension")
    except VpsApiError as e:
        logger.debug(
            "OVH GET /me/order/{}/details/{}/extension failed: {}; treating as no match",
            order_id,
            detail_id,
            e,
        )
        return False
    if not isinstance(extension, dict):
        return False
    raw_order = extension.get("order")
    if not isinstance(raw_order, dict):
        return False
    plan = raw_order.get("plan") or {}
    if plan.get("code") != requested_plan_code:
        return False
    product = plan.get("product") or {}
    if isinstance(product, dict):
        product_name = product.get("name")
        if isinstance(product_name, str) and product_name and product_name != _OVH_VPS_PRODUCT_NAME:
            return False
    return True


def _fetch_first_operation_resource_name(client: OvhVpsClient, *, order_id: int, detail_id: int) -> str | None:
    """Return the first non-empty ``operation.resource.name`` for this detail.

    The recurring billing line of a successful VPS order ends up with
    exactly one operation whose ``resource.name`` is the assigned
    serviceName. We iterate defensively in case OVH ever returns
    multiple operations per detail (e.g. installation + activation
    split across operations).
    """
    try:
        op_ids = client.call_api("GET", f"/me/order/{order_id}/details/{detail_id}/operations")
    except VpsApiError as e:
        logger.debug(
            "OVH GET /me/order/{}/details/{}/operations failed: {}; not ready",
            order_id,
            detail_id,
            e,
        )
        return None
    if not isinstance(op_ids, list) or not op_ids:
        return None
    for op_id in op_ids:
        try:
            op = client.call_api("GET", f"/me/order/{order_id}/details/{detail_id}/operations/{op_id}")
        except VpsApiError as e:
            logger.debug(
                "OVH GET /me/order/{}/details/{}/operations/{} failed: {}; trying next",
                order_id,
                detail_id,
                op_id,
                e,
            )
            continue
        if not isinstance(op, dict):
            continue
        resource = op.get("resource") or {}
        if not isinstance(resource, dict):
            continue
        name = resource.get("name")
        if isinstance(name, str) and name and name != _BILLING_DETAIL_DOMAIN_PLACEHOLDER:
            return name
    return None


def _verify_vps_matches_order(
    client: OvhVpsClient,
    *,
    service_name: str,
    requested_plan: str,
    requested_datacenter: str,
) -> None:
    """Belt-and-suspenders: confirm OVH gave us the plan + region we ordered.

    Mirrors the recycle path's eligibility filter
    (``recycle.py:_evaluate_candidate``) so the comparison semantics are
    consistent: ``model.name == requested_plan`` and ``requested_datacenter``
    is a case-insensitive substring of ``zone`` (OVH zone strings look
    like ``Region OpenStack: os-us-east-va-vps-1`` for the
    ``US-EAST-VA`` datacenter).

    On mismatch raises :class:`VpsProvisioningError` so the
    :func:`_provision_vps` ``finally`` cleanup cancels future renewal on
    the wrong VPS (limiting the damage to the already-paid month).
    """
    try:
        info = client.get_instance(VpsInstanceId(service_name))
    except VpsApiError as exc:
        raise VpsProvisioningError(
            f"OVH post-order verify: cannot GET /vps/{service_name} to confirm the delivered plan + region: {exc}"
        ) from exc
    actual_plan = str((info.get("model") or {}).get("name", ""))
    if actual_plan != requested_plan:
        raise VpsProvisioningError(
            f"OVH delivered VPS {service_name} with plan {actual_plan!r}, but we ordered {requested_plan!r}. "
            "Aborting before TOFU + rebuild operate on the wrong machine."
        )
    actual_zone = str(info.get("zone", ""))
    if requested_datacenter.lower() not in actual_zone.lower():
        raise VpsProvisioningError(
            f"OVH delivered VPS {service_name} in zone {actual_zone!r}, "
            f"but we ordered datacenter {requested_datacenter!r}. "
            "Aborting before TOFU + rebuild operate on the wrong machine."
        )


def rebuild_vps_with_public_key(
    client: OvhVpsClient,
    service_name: str,
    image_id: str,
    public_ssh_key: str,
    task_timeout_seconds: float,
) -> None:
    """Trigger ``POST /vps/{s}/rebuild`` with our SSH pubkey, then wait for it to finish.

    Pre-installs ``public_ssh_key`` (registered for the OVH image's
    default user; ``debian`` on the Debian 12 - Docker image) via the
    OVH-side rebuild flow, sets ``doNotSendPassword=true`` so OVH does
    not generate or email a root password, and waits for the rebuild
    task to reach a terminal state.

    OVH rejects ``/rebuild`` with "Action not available while there are
    running tasks on the VPS" if any task is in flight. We drain active
    tasks first, but the task listing the drain polls is eventually
    consistent and can report an empty list while OVH still refuses the
    action, so the POST is retried (re-draining each round) until OVH
    accepts it -- OVH's rejection of the action is the authoritative
    signal that a task is still in flight. Protects both the fresh-order
    and recycle paths.
    """
    body: Mapping[str, Any] = {
        "imageId": image_id,
        "publicSshKey": public_ssh_key,
        "doNotSendPassword": True,
        "installRTM": False,
    }
    with log_span("OVH rebuild on {} (image_id={})", service_name, image_id):
        task = _post_rebuild_retrying_in_flight_task(client, service_name, body)
        task_id = int(task.get("id", 0))
        if not task_id:
            raise VpsProvisioningError(f"OVH /vps/{service_name}/rebuild returned no task id: {task!r}")
        client.wait_for_task(service_name, task_id, timeout_seconds=task_timeout_seconds)


def _post_rebuild_retrying_in_flight_task(
    client: OvhVpsClient,
    service_name: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    """``POST /vps/{s}/rebuild``, retrying while OVH reports an in-flight task.

    Each attempt first drains the (eventually-consistent) task listing,
    then issues the rebuild. On OVH's in-flight-task rejection it sleeps,
    re-drains, and retries until ``_OVH_REBUILD_START_RETRY_TIMEOUT_SECONDS``
    elapses; any other API error propagates immediately. Returns the
    rebuild task payload once OVH accepts the call.
    """
    deadline = time.monotonic() + _OVH_REBUILD_START_RETRY_TIMEOUT_SECONDS
    attempt = 0
    last_error: VpsApiError | None = None
    while time.monotonic() < deadline:
        attempt += 1
        client.wait_for_no_active_tasks(service_name, timeout_seconds=_OVH_REBUILD_PREFLIGHT_DRAIN_SECONDS)
        try:
            return dict(client.call_api("POST", f"/vps/{service_name}/rebuild", **body) or {})
        except VpsApiError as e:
            if _OVH_RUNNING_TASKS_ERROR_MARKER not in str(e).lower():
                raise
            last_error = e
            logger.warning(
                "OVH /rebuild on {} rejected by an in-flight task (attempt {}); re-draining and retrying: {}",
                service_name,
                attempt,
                e,
            )
            time.sleep(client.task_poll_interval)
    raise VpsProvisioningError(
        f"OVH /vps/{service_name}/rebuild still rejected with an in-flight-task error after "
        f"{_OVH_REBUILD_START_RETRY_TIMEOUT_SECONDS}s ({attempt} attempt(s)); last error: {last_error}"
    )
