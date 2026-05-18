import time
from collections.abc import Mapping
from typing import Any

from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mngr.errors import MngrError
from imbue.mngr_ovh.catalog import find_required_field
from imbue.mngr_ovh.catalog import validate_datacenter
from imbue.mngr_ovh.client import OvhVpsClient
from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.errors import VpsProvisioningError
from imbue.mngr_vps_docker.primitives import VpsInstanceId

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
        6. Poll ``GET /me/order/{orderId}/details`` for the order's
           detail-id list, then ``GET /me/order/{orderId}/details/{detailId}``
           until OVH populates the ``domain`` field with the assigned
           serviceName. **Strong correlation: no shared-account race**
           is possible because each in-flight order has its own orderId
           and OVH only reports OUR order's domain via OUR orderId. The
           previous implementation diff'd ``GET /vps`` before vs after
           checkout, which silently picked the wrong serviceName when
           a concurrent order finished delivering during our wait
           (F3 in OVH_AUDIT.md).
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
            # First check the inline ``details`` on the checkout response.
            # OVH MAY populate the domain inline at checkout (the schema
            # declares ``order.OrderDetail.domain`` as non-nullable), but
            # for VPS orders the serviceName is assigned during delivery
            # so this is usually empty -- in which case we fall through
            # to the polled /me/order path.
            service_name = _extract_inline_domain(order_response, cart_item_id=item_id)
            if not service_name:
                service_name = _wait_for_service_name_from_order(
                    client, order_id=order_id, timeout_seconds=deliver_timeout_seconds
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
        except (MngrError, VpsApiError, VpsProvisioningError):
            _safe_delete_cart(client, cart_id)
            raise


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
    except (VpsApiError, MngrError) as e:
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


def _extract_inline_domain(order_response: Any, *, cart_item_id: int) -> str | None:
    """Find the new VPS's serviceName inline on the checkout response if present.

    Looks at ``order.Order.details[]`` for the entry whose ``cartItemID``
    matches the cart item we built. Returns the ``domain`` (== serviceName)
    if it's already populated at checkout time. Returns ``None`` if the
    field is empty -- which is the common case for VPS orders since the
    serviceName is assigned during delivery, after checkout returns.
    """
    if not isinstance(order_response, dict):
        return None
    raw_details = order_response.get("details")
    if not isinstance(raw_details, list):
        return None
    for detail in raw_details:
        if not isinstance(detail, dict):
            continue
        if detail.get("cartItemID") != cart_item_id:
            continue
        domain = detail.get("domain")
        if isinstance(domain, str) and domain:
            return domain
    return None


def _wait_for_service_name_from_order(
    client: OvhVpsClient,
    *,
    order_id: int,
    timeout_seconds: float,
) -> str:
    """Poll ``GET /me/order/{orderId}/details/{detailId}`` until ``domain`` populates.

    OVH's order processing is asynchronous: the checkout response returns
    immediately but the VPS's serviceName is only assigned during the
    delivery phase (typically 30-90s on ``vps-2025-model1``, can take
    longer on bigger plans or busier regions). This helper polls until
    OVH writes the serviceName into the order detail's ``domain`` field.

    **Strong correlation:** every poll is scoped to OUR ``orderId``, so
    a concurrent ``order_and_wait_for_vps`` against the same OVH account
    sees only ITS order's details. The previous diff-against-``/vps``
    approach could silently pick up the other order's serviceName when
    two deliveries finished within the same poll interval (F3 in
    OVH_AUDIT.md). With this helper, that race is eliminated.

    Two retryable failure modes during the early window:

    * ``GET /me/order/{orderId}/details`` returns 404 / empty list
      because OVH hasn't yet materialised the order's details server-side.
      Keep polling.
    * ``GET /me/order/{orderId}/details/{detailId}`` returns a detail
      whose ``domain`` is the empty string because the delivery hasn't
      run yet. Keep polling.

    Raises :class:`VpsProvisioningError` on timeout.
    """
    deadline = time.monotonic() + timeout_seconds
    last_log_message: str = "no successful poll yet"
    # Single sleep at the end of each iteration (vs. one per failure mode)
    # so the per-iteration latency is uniform and the ratchet on
    # ``time.sleep`` counts only the one truly-needed call.
    while time.monotonic() < deadline:
        domain, last_log_message = _try_fetch_order_service_name(client, order_id)
        if domain:
            return domain
        time.sleep(_OVH_DELIVERY_POLL_INTERVAL_SECONDS)
    raise VpsProvisioningError(
        f"OVH order {order_id} did not produce a VPS serviceName within {timeout_seconds}s; "
        f"last status: {last_log_message}"
    )


def _try_fetch_order_service_name(client: OvhVpsClient, order_id: int) -> tuple[str | None, str]:
    """Single-shot attempt to read the populated ``domain`` from an OVH order's details.

    Returns ``(domain, status_message)`` -- ``domain`` is the serviceName
    when populated, ``None`` otherwise. The status message describes the
    current state for the timeout error path.

    Iterates every detail (in practice there's exactly one because the
    cart had exactly one item, but iterates defensively in case OVH ever
    bundles ancillary items with a VPS order). Returns the first
    non-empty domain.
    """
    try:
        raw_detail_ids = client.call_api("GET", f"/me/order/{order_id}/details")
    except VpsApiError as e:
        logger.debug("OVH order-detail listing not ready yet for order {}: {}", order_id, e)
        return None, f"GET /me/order/{order_id}/details failed: {e}"
    if not isinstance(raw_detail_ids, list) or not raw_detail_ids:
        return None, f"GET /me/order/{order_id}/details returned empty list (order not yet materialised)"
    for detail_id in raw_detail_ids:
        try:
            detail = client.call_api("GET", f"/me/order/{order_id}/details/{detail_id}")
        except VpsApiError as e:
            logger.debug(
                "OVH GET /me/order/{}/details/{} failed: {}; trying remaining details", order_id, detail_id, e
            )
            continue
        if not isinstance(detail, dict):
            continue
        domain = detail.get("domain")
        if isinstance(domain, str) and domain:
            return domain, "ok"
    return (
        None,
        f"GET /me/order/{order_id}/details/* returned no populated domain yet (saw {len(raw_detail_ids)} detail(s))",
    )


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

    OVH rejects ``/rebuild`` with HTTP 400 if any task is in flight on
    the VPS, so we first drain any active tasks. In the fresh-order path
    ``order_and_wait_for_vps`` already waited; this call is the canonical
    chokepoint that also protects the recycle path.
    """
    client.wait_for_no_active_tasks(service_name, timeout_seconds=_OVH_REBUILD_PREFLIGHT_DRAIN_SECONDS)
    body: Mapping[str, Any] = {
        "imageId": image_id,
        "publicSshKey": public_ssh_key,
        "doNotSendPassword": True,
        "installRTM": False,
    }
    with log_span("OVH rebuild on {} (image_id={})", service_name, image_id):
        task = client.call_api("POST", f"/vps/{service_name}/rebuild", **body)
        task_id = int((task or {}).get("id", 0))
        if not task_id:
            raise VpsProvisioningError(f"OVH /vps/{service_name}/rebuild returned no task id: {task!r}")
        client.wait_for_task(service_name, task_id, timeout_seconds=task_timeout_seconds)
