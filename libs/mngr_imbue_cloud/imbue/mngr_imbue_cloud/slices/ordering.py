"""Drive the OVH eco order/cart + dedicated-server delivery + OS reinstall for bare-metal boxes.

The eco line (RISE/SYS/KS) is ordered through a different cart product than VPSes
(``/order/cart/{id}/eco`` with mandatory bandwidth/memory/storage/vrack options and
``dedicated_os=none_64.en`` -- the real OS is installed after delivery via
``/dedicated/server/{s}/reinstall``). The pure helpers here are unit-tested; the
client-driven steps are exercised live against a real order.
"""

from collections import defaultdict
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Final

from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr_imbue_cloud.errors import BareMetalConfigError
from imbue.mngr_imbue_cloud.errors import BareMetalProvisioningError
from imbue.mngr_imbue_cloud.slices.pricing import compute_storage_usable_gb
from imbue.mngr_imbue_cloud.slices.pricing import describe_storage_raid_level
from imbue.mngr_imbue_cloud.slices.pricing import parse_memory_gb
from imbue.mngr_ovh.client import OvhVpsClient
from imbue.mngr_vps_docker.errors import VpsApiError

# Month-to-month eco order: no commitment, monthly renewal, one server.
ECO_PRICING_MODE: Final[str] = "default"
ECO_DURATION: Final[str] = "P1M"
# OVH eco orders carry no real OS (``dedicated_os`` only offers ``none_64.en``); we install ours post-delivery.
ECO_ORDER_OS: Final[str] = "none_64.en"
# The ``region`` required-config value for the US subsidiary (OVH groups vin + hil under this).
ECO_ORDER_REGION: Final[str] = "united_states"
# Default OS template reinstalled onto a delivered box (matches the box already in the fleet).
DEFAULT_REINSTALL_OS_TEMPLATE: Final[str] = "debian12_64"

# Mandatory eco option families that have a single offer we auto-pick (the user only chooses memory + storage).
_AUTO_PICKED_OPTION_FAMILIES: Final[tuple[str, ...]] = ("bandwidth", "vrack")

_DELIVERY_POLL_INTERVAL_SECONDS: Final[float] = 60.0
_DELIVERY_TIMEOUT_SECONDS: Final[float] = 4 * 60 * 60.0
_REINSTALL_POLL_INTERVAL_SECONDS: Final[float] = 30.0
_REINSTALL_TIMEOUT_SECONDS: Final[float] = 60 * 60.0
_TERMINAL_TASK_STATUSES: Final[frozenset[str]] = frozenset({"done", "ovhError", "customerError", "cancelled"})


@pure
def select_eco_option_codes(
    eco_options: Sequence[Mapping[str, Any]],
    memory_gb: int,
    storage_short: str,
) -> list[str]:
    """Choose the eco cart option planCodes: the requested memory + storage, plus single-offer mandatory families.

    ``eco_options`` is the ``GET /order/cart/{id}/eco/options`` payload (each item has ``family`` and
    ``planCode``). Memory is matched by parsed GB; storage by the availability short code (prefix). The
    other mandatory families (bandwidth, vrack) must each have exactly one offer, which is auto-picked.
    Raises ``BareMetalConfigError`` if a required choice can't be resolved.
    """
    codes_by_family: dict[str, list[str]] = defaultdict(list)
    for option in eco_options:
        codes_by_family[str(option["family"])].append(str(option["planCode"]))

    memory_code = next(
        (code for code in codes_by_family.get("memory", []) if parse_memory_gb(code) == memory_gb), None
    )
    if memory_code is None:
        raise BareMetalConfigError(
            f"no {memory_gb}GB memory option for this plan; offered: {sorted(codes_by_family.get('memory', []))}"
        )
    storage_code = next(
        (
            code
            for code in codes_by_family.get("storage", [])
            if code == storage_short or code.startswith(storage_short + "-")
        ),
        None,
    )
    if storage_code is None:
        raise BareMetalConfigError(
            f"storage {storage_short!r} not offered for this plan; offered: {sorted(codes_by_family.get('storage', []))}"
        )

    chosen = [memory_code, storage_code]
    for family in _AUTO_PICKED_OPTION_FAMILIES:
        family_codes = codes_by_family.get(family, [])
        if len(family_codes) != 1:
            raise BareMetalConfigError(
                f"expected exactly one {family} option to auto-pick, got {sorted(family_codes)}"
            )
        chosen.append(family_codes[0])
    return chosen


@pure
def derive_server_specs(
    catalog: Mapping[str, Any],
    plan_code: str,
    storage_short: str,
) -> tuple[int, int, int, str]:
    """Derive (cpu_cores, cpu_threads, usable_disk_gb, raid_level) for an ordered box from the catalog.

    We know exactly what we ordered, so the row's hardware specs come from the catalog product blob
    (CPU) and the chosen storage code (usable disk + RAID), with no need to probe the live server.
    """
    products_by_name = {str(product["name"]): product for product in catalog.get("products", [])}
    plan = next((entry for entry in catalog.get("plans", []) if str(entry["planCode"]) == plan_code), None)
    if plan is None:
        raise BareMetalConfigError(f"plan {plan_code!r} not found in OVH catalog")
    cpu = (
        (((products_by_name.get(str(plan.get("product"))) or {}).get("blobs") or {}).get("technical") or {}).get(
            "server"
        )
        or {}
    ).get("cpu") or {}
    cpu_cores = cpu.get("cores")
    cpu_threads = cpu.get("threads")
    if not isinstance(cpu_cores, int) or not isinstance(cpu_threads, int):
        raise BareMetalConfigError(f"plan {plan_code!r} has no CPU core/thread specs in the catalog")
    return cpu_cores, cpu_threads, compute_storage_usable_gb(storage_short), describe_storage_raid_level(storage_short)


@pure
def extract_order_id(checkout_response: Mapping[str, Any]) -> int:
    """Pull the integer ``orderId`` out of a checkout (or checkout-preview) response."""
    raw_order_id = checkout_response.get("orderId")
    if raw_order_id is None:
        raise BareMetalProvisioningError(f"OVH checkout returned no orderId: {checkout_response!r}")
    try:
        return int(raw_order_id)
    except (TypeError, ValueError) as exc:
        raise BareMetalProvisioningError(f"OVH checkout returned non-integer orderId {raw_order_id!r}") from exc


@pure
def summarize_checkout_prices(preview: Mapping[str, Any]) -> str:
    """Render the GET-checkout price preview as a short human summary."""
    lines = []
    for price in preview.get("prices", []):
        label = price.get("label", "?")
        text = (price.get("price") or {}).get("text", "?")
        lines.append(f"  {label}: {text}")
    return "\n".join(lines) if lines else "  (no price lines returned)"


@pure
def _looks_like_service_name(candidate: Any) -> bool:
    """Whether an order-detail domain / operation resource name is a real dedicated serviceName (not a wildcard)."""
    return isinstance(candidate, str) and bool(candidate) and candidate != "*" and "." in candidate


def build_and_assign_eco_cart(
    client: OvhVpsClient,
    *,
    plan_code: str,
    datacenter: str,
    memory_gb: int,
    storage_short: str,
) -> tuple[str, dict[str, Any], list[str]]:
    """Build a single-server eco cart, assign it, and return (cart_id, checkout_preview, option_codes).

    Assigning attaches the cart to the account but does NOT place the order (only ``POST checkout`` does),
    so the returned preview can be shown for confirmation. The caller must then either ``checkout_eco_cart``
    or ``delete_cart_quietly``.
    """
    with log_span("Building OVH eco cart for plan={} datacenter={}", plan_code, datacenter):
        cart_id = str(client.call_api("POST", "/order/cart", ovhSubsidiary=client.subsidiary).get("cartId", ""))
        if not cart_id:
            raise BareMetalProvisioningError("OVH /order/cart returned no cartId")

        item = client.call_api(
            "POST",
            f"/order/cart/{cart_id}/eco",
            planCode=plan_code,
            pricingMode=ECO_PRICING_MODE,
            duration=ECO_DURATION,
            quantity=1,
        )
        item_id = int((item or {}).get("itemId", 0))
        if not item_id:
            raise BareMetalProvisioningError(f"OVH eco cart {cart_id} returned no itemId: {item!r}")

        for label, value in (
            ("dedicated_datacenter", datacenter),
            ("dedicated_os", ECO_ORDER_OS),
            ("region", ECO_ORDER_REGION),
        ):
            client.call_api("POST", f"/order/cart/{cart_id}/item/{item_id}/configuration", label=label, value=value)

        eco_options = client.call_api("GET", f"/order/cart/{cart_id}/eco/options", planCode=plan_code)
        option_codes = select_eco_option_codes(eco_options, memory_gb, storage_short)
        for option_code in option_codes:
            client.call_api(
                "POST",
                f"/order/cart/{cart_id}/eco/options",
                planCode=option_code,
                quantity=1,
                itemId=item_id,
                duration=ECO_DURATION,
                pricingMode=ECO_PRICING_MODE,
            )

        client.call_api("POST", f"/order/cart/{cart_id}/assign")
        preview = client.call_api("GET", f"/order/cart/{cart_id}/checkout")
        return cart_id, preview, option_codes


def delete_cart_quietly(client: OvhVpsClient, cart_id: str) -> None:
    """Best-effort delete of an assigned cart (used to abort an unconfirmed order)."""
    try:
        client.call_api("DELETE", f"/order/cart/{cart_id}")
    except VpsApiError as exc:
        logger.warning("Failed to delete OVH cart {} (it will expire on its own): {}", cart_id, str(exc)[:160])


def checkout_eco_cart(client: OvhVpsClient, cart_id: str) -> int:
    """Place the order for an assigned cart and return its orderId. THIS CHARGES the account."""
    with log_span("Placing OVH eco order for cart={}", cart_id):
        response = client.call_api(
            "POST",
            f"/order/cart/{cart_id}/checkout",
            autoPayWithPreferredPaymentMethod=True,
            waiveRetractationPeriod=True,
        )
        return extract_order_id(response)


def _poll_order_for_service_name(client: OvhVpsClient, order_id: int) -> str | None:
    """One poll of an order's details/operations chain for the assigned dedicated serviceName."""
    detail_ids = client.call_api("GET", f"/me/order/{order_id}/details")
    for detail_id in detail_ids or []:
        detail = client.call_api("GET", f"/me/order/{order_id}/details/{detail_id}")
        if _looks_like_service_name(detail.get("domain")):
            return str(detail["domain"])
        operation_ids = client.call_api("GET", f"/me/order/{order_id}/details/{detail_id}/operations")
        for operation_id in operation_ids or []:
            operation = client.call_api("GET", f"/me/order/{order_id}/details/{detail_id}/operations/{operation_id}")
            resource_name = (operation.get("resource") or {}).get("name")
            if _looks_like_service_name(resource_name):
                return str(resource_name)
    return None


def wait_for_order_service_name(
    client: OvhVpsClient,
    *,
    order_id: int,
    timeout_seconds: float = _DELIVERY_TIMEOUT_SECONDS,
) -> str:
    """Poll an order until OVH assigns its dedicated server a serviceName. Raises on timeout."""
    with log_span("Waiting for OVH order {} to assign a serviceName", order_id):
        service_name, _polls, _elapsed = poll_for_value(
            lambda: _poll_order_for_service_name(client, order_id),
            timeout=timeout_seconds,
            poll_interval=_DELIVERY_POLL_INTERVAL_SECONDS,
        )
    if service_name is None:
        raise BareMetalProvisioningError(
            f"OVH order {order_id} did not assign a serviceName within {timeout_seconds:.0f}s"
        )
    return service_name


def get_dedicated_server_address(client: OvhVpsClient, service_name: str) -> str | None:
    """Return the dedicated server's public IP once OVH has assigned one, else None."""
    info = client.call_api("GET", f"/dedicated/server/{service_name}")
    address = info.get("ip")
    return str(address) if address else None


def wait_for_dedicated_server_address(
    client: OvhVpsClient,
    *,
    service_name: str,
    timeout_seconds: float = _DELIVERY_TIMEOUT_SECONDS,
) -> str:
    """Poll until the delivered server has a reachable public IP. Raises on timeout."""
    with log_span("Waiting for dedicated server {} to report an IP", service_name):
        address, _polls, _elapsed = poll_for_value(
            lambda: get_dedicated_server_address(client, service_name),
            timeout=timeout_seconds,
            poll_interval=_DELIVERY_POLL_INTERVAL_SECONDS,
        )
    if address is None:
        raise BareMetalProvisioningError(
            f"dedicated server {service_name} had no IP within {timeout_seconds:.0f}s of delivery"
        )
    return address


def start_os_reinstall(
    client: OvhVpsClient,
    *,
    service_name: str,
    ssh_public_key: str,
    os_template: str = DEFAULT_REINSTALL_OS_TEMPLATE,
) -> int:
    """Reinstall the box's OS with our SSH key (default RAID1 partitioning) and return the install task id."""
    with log_span("Reinstalling {} with OS {}", service_name, os_template):
        task = client.call_api(
            "POST",
            f"/dedicated/server/{service_name}/reinstall",
            operatingSystem=os_template,
            customizations={"sshKey": ssh_public_key},
        )
    task_id = task.get("taskId") if isinstance(task, dict) else None
    if task_id is None and isinstance(task, dict):
        task_id = task.get("id")
    if task_id is None:
        raise BareMetalProvisioningError(f"OVH reinstall of {service_name} returned no task id: {task!r}")
    return int(task_id)


def _poll_reinstall_task_status(client: OvhVpsClient, service_name: str, task_id: int) -> str | None:
    """Return the task's status once it reaches a terminal state, else None (still running)."""
    task = client.call_api("GET", f"/dedicated/server/{service_name}/task/{task_id}")
    status = str(task.get("status", ""))
    return status if status in _TERMINAL_TASK_STATUSES else None


def wait_for_os_reinstall(
    client: OvhVpsClient,
    *,
    service_name: str,
    task_id: int,
    timeout_seconds: float = _REINSTALL_TIMEOUT_SECONDS,
) -> None:
    """Wait for the reinstall task to finish; raise unless it ends in ``done``."""
    with log_span("Waiting for {} reinstall task {} to finish", service_name, task_id):
        status, _polls, _elapsed = poll_for_value(
            lambda: _poll_reinstall_task_status(client, service_name, task_id),
            timeout=timeout_seconds,
            poll_interval=_REINSTALL_POLL_INTERVAL_SECONDS,
        )
    if status is None:
        raise BareMetalProvisioningError(
            f"reinstall task {task_id} on {service_name} did not finish within {timeout_seconds:.0f}s"
        )
    if status != "done":
        raise BareMetalProvisioningError(f"reinstall task {task_id} on {service_name} ended in status {status!r}")
