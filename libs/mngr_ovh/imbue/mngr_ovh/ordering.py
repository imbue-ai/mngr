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
        5. ``POST /order/cart/{id}/checkout`` to place the order.
        6. Poll ``GET /vps`` until the new serviceName appears (the snapshot taken
           before checkout is the diff baseline).
        7. Wait for the post-delivery ``deliverVm`` task to drain. The
           serviceName becomes visible in ``GET /vps`` before this task
           finishes; any mutating call (e.g. ``/rebuild``) issued in the
           interim fails with "Action not available while there are
           running tasks on the VPS".

    Returns the new VPS's serviceName. Raises ``VpsProvisioningError`` on
    timeout or any step failure.
    """
    with log_span("OVH order cart flow for plan={} datacenter={}", plan_code, datacenter):
        existing_before = set(client.list_instances())

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
            client.call_api("POST", f"/order/cart/{cart_id}/checkout", autoPayWithPreferredPaymentMethod=True)

            logger.info("OVH order placed (cart={}); waiting for VPS delivery", cart_id)
            service_name = _wait_for_new_service_name(client, existing_before, deliver_timeout_seconds)
            client.wait_for_no_active_tasks(
                service_name,
                timeout_seconds=_OVH_POST_DELIVERY_TASK_DRAIN_TIMEOUT_SECONDS,
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


def _wait_for_new_service_name(
    client: OvhVpsClient,
    existing_before: set[str],
    timeout_seconds: float,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        current = set(client.list_instances())
        new_names = current - existing_before
        if new_names:
            chosen = sorted(new_names)[0]
            logger.info("OVH delivered new VPS: {}", chosen)
            return chosen
        time.sleep(_OVH_DELIVERY_POLL_INTERVAL_SECONDS)
    raise VpsProvisioningError(
        f"OVH order did not deliver a new VPS within {timeout_seconds}s "
        f"(known VPSes at start: {sorted(existing_before)})"
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
