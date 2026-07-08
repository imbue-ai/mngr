"""Tests for the OVH order/cart flow."""

import threading
from typing import Any
from typing import Callable
from unittest.mock import MagicMock
from unittest.mock import patch

import ovh
import pytest
from ovh.exceptions import APIError

from imbue.mngr.errors import MngrError
from imbue.mngr_ovh.client import OvhVpsClient
from imbue.mngr_ovh.ordering import OvhOrderDeliveryTimeoutError
from imbue.mngr_ovh.ordering import order_and_wait_for_vps
from imbue.mngr_ovh.ordering import rebuild_vps_with_public_key
from imbue.mngr_ovh.ordering import try_poll_order_for_delivered_vps
from imbue.mngr_vps.errors import VpsApiError
from imbue.mngr_vps.errors import VpsProvisioningError


def _client(call_side_effect: Any) -> OvhVpsClient:
    m = MagicMock(spec=ovh.Client)
    m.call = MagicMock(side_effect=call_side_effect)
    return OvhVpsClient(ovh_client=m, subsidiary="US", task_poll_interval=0.0)


def _vps_info_for(
    plan: str = "vps-2025-model1", zone: str = "Region OpenStack: os-us-east-va-vps-1"
) -> dict[str, Any]:
    """Sample ``GET /vps/{name}`` response used by post-hoc verify in success paths."""
    return {"model": {"name": plan}, "zone": zone, "state": "installing"}


def _fake_order_router(
    *,
    cart_id: str = "cart-1",
    item_id: int = 99,
    order_id: int = 42,
    vps_detail_id: int = 7001,
    linux_detail_id: int = 7002,
    vps_operation_id: int = 9001,
    requested_plan: str = "vps-2025-model1",
    service_name: str = "vps-new.vps.ovh.us",
    allowed_datacenters: tuple[str, ...] = ("US-EAST-VA",),
    allowed_os: tuple[str, ...] = ("Debian 12 - Docker",),
    vps_info: dict[str, Any] | None = None,
    detail_listing_first_calls_404: int = 0,
    resource_populated_after_n_polls: int = 0,
) -> Callable[[str, str, Any, bool], Any]:
    """Build a fake ``client.call`` that drives ``order_and_wait_for_vps`` through one happy run.

    The fake models the live OVH API shape verified on 2026-05-18:
    ``billing.OrderDetail.domain`` is always ``"*"`` and the real
    serviceName only appears via
    ``GET /me/order/{id}/details/{detailId}/operations/{opId}.resource.name``.

    Knobs:
    - ``detail_listing_first_calls_404``: simulate OVH not having
      materialised the order yet -- /me/order/{id}/details returns []
      this many times before the real list appears.
    - ``resource_populated_after_n_polls``: number of operation GETs
      that return an unpopulated resource name (None) before OVH
      writes the real serviceName.
    """
    if vps_info is None:
        vps_info = _vps_info_for(plan=requested_plan)
    detail_list_call_count = {"n": 0}
    operation_get_call_count = {"n": 0}

    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        # Cart construction.
        if method == "POST" and path == "/order/cart":
            return {"cartId": cart_id}
        if method == "POST" and path == f"/order/cart/{cart_id}/vps":
            return {"itemId": item_id}
        if method == "GET" and path == f"/order/cart/{cart_id}/item/{item_id}/requiredConfiguration":
            return [
                {"label": "vps_datacenter", "allowedValues": list(allowed_datacenters)},
                {"label": "vps_os", "allowedValues": list(allowed_os)},
                {"label": "vps_install_rtm", "allowedValues": ["if_available", "no"]},
            ]
        if method == "POST" and path == f"/order/cart/{cart_id}/item/{item_id}/configuration":
            return None
        if method == "POST" and path == f"/order/cart/{cart_id}/assign":
            return None
        if method == "POST" and path == f"/order/cart/{cart_id}/checkout":
            # billing.OrderDetail.domain is the literal "*" -- our code
            # MUST ignore it and follow the operations chain instead.
            return {
                "orderId": order_id,
                "prices": {},
                "url": "https://x",
                "details": [{"cartItemID": item_id, "domain": "*"}],
            }
        # /me/order/{orderId}/details -> list of detailIds. Returns the
        # VPS detail and the OS-sublineitem detail to mirror the live
        # OVH shape (the user's first probe saw 6 details for a single
        # VPS order; here 2 is enough to exercise the per-plan filter).
        if method == "GET" and path == f"/me/order/{order_id}/details":
            detail_list_call_count["n"] += 1
            if detail_list_call_count["n"] <= detail_listing_first_calls_404:
                return []
            return [vps_detail_id, linux_detail_id]
        # Per-detail extension -- our code matches on
        # extension.order.plan.code so this is the disambiguation point
        # between the VPS detail and the OS sublineitem.
        if method == "GET" and path == f"/me/order/{order_id}/details/{vps_detail_id}/extension":
            return {
                "order": {
                    "action": "installation",
                    "type": "plan",
                    "plan": {
                        "code": requested_plan,
                        "duration": "P1M",
                        "pricingMode": "default",
                        "quantity": 1,
                        "product": {"name": "virtualPrivateServer"},
                    },
                    "configurations": [
                        {"label": "vps_datacenter", "value": list(allowed_datacenters)[0]},
                        {"label": "vps_os", "value": list(allowed_os)[0]},
                    ],
                },
            }
        if method == "GET" and path == f"/me/order/{order_id}/details/{linux_detail_id}/extension":
            return {
                "order": {
                    "action": "installation",
                    "type": "plan",
                    "plan": {
                        "code": "option-linux",
                        "duration": "P1M",
                        "product": {"name": "virtualPrivateServer"},
                    },
                    "configurations": [],
                },
            }
        # Per-detail operations.
        if method == "GET" and path == f"/me/order/{order_id}/details/{vps_detail_id}/operations":
            return [vps_operation_id]
        if method == "GET" and path == f"/me/order/{order_id}/details/{linux_detail_id}/operations":
            # OS sub-resource op; has its own resource.name that we
            # must NOT pick up. The plan-code filter on the extension
            # call above already excludes this whole detail, so this
            # branch should never fire in a passing test.
            return [9999]
        # The VPS operation, possibly still pre-delivery.
        if method == "GET" and path == f"/me/order/{order_id}/details/{vps_detail_id}/operations/{vps_operation_id}":
            operation_get_call_count["n"] += 1
            if operation_get_call_count["n"] <= resource_populated_after_n_polls:
                return {"id": vps_operation_id, "status": "doing", "type": "installation", "resource": {}}
            return {
                "id": vps_operation_id,
                "status": "done",
                "type": "installation",
                "resource": {"name": service_name, "displayName": service_name, "state": "ok"},
            }
        # The OS sub-resource operation. Returns its OWN resource.name,
        # but our filter on plan.code rejects this detail entirely so
        # this branch should only fire if the filter is broken.
        if method == "GET" and path == f"/me/order/{order_id}/details/{linux_detail_id}/operations/9999":
            return {
                "id": 9999,
                "status": "done",
                "type": "installation",
                "resource": {"name": f"{service_name}-linux", "displayName": "OS", "state": "ok"},
            }
        # Post-delivery task drain.
        if method == "GET" and "/tasks?state=" in path and service_name in path:
            return []
        # Post-hoc verify.
        if method == "GET" and path == f"/vps/{service_name}":
            return vps_info
        # Failure-path cleanup -- the post-hoc verify raises into the
        # ``except`` branch which calls ``_safe_delete_cart``.
        if method == "DELETE" and path == f"/order/cart/{cart_id}":
            return None
        raise AssertionError(f"unexpected fake OVH call: {method} {path}")

    return fake


def test_order_never_configures_a_backup_option() -> None:
    """Regression: the order/cart flow must never enable an OVH backup option.

    OVH automated backups freeze the guest filesystem and cause serious
    runtime problems, so we disable them at provision time by purging qemu.
    This test locks in the other half of that guarantee: we must not
    *order* backups in the first place. It records every
    ``/order/cart/.../configuration`` call and asserts the configured
    labels are exactly datacenter / OS / RTM (with RTM set to ``no``) and
    that none is a backup option.
    """
    configured_labels_and_values: list[tuple[str, str]] = []
    happy_path = _fake_order_router(resource_populated_after_n_polls=0)

    def recording_fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if method == "POST" and path.endswith("/configuration") and isinstance(body, dict):
            configured_labels_and_values.append((body["label"], body["value"]))
        return happy_path(method, path, body, need_auth)

    client = _client(recording_fake)
    with patch("imbue.mngr_ovh.ordering._OVH_DELIVERY_POLL_INTERVAL_SECONDS", 0.0):
        order_and_wait_for_vps(
            client,
            plan_code="vps-2025-model1",
            datacenter="US-EAST-VA",
            image_name="Debian 12 - Docker",
            pricing_mode="default",
            duration="P1M",
            deliver_timeout_seconds=10.0,
        )

    configured_labels = {label for label, _value in configured_labels_and_values}
    assert configured_labels == {"vps_datacenter", "vps_os", "vps_install_rtm"}
    assert not any("backup" in label.lower() for label, _value in configured_labels_and_values)
    # RTM (real-time monitoring) defaults off; assert it explicitly so a
    # future default flip is caught alongside the backup guarantee.
    assert ("vps_install_rtm", "no") in configured_labels_and_values


def test_order_and_wait_for_vps_success_polled_path() -> None:
    """Happy path: serviceName arrives via the operations chain after a few polls."""
    client = _client(_fake_order_router(resource_populated_after_n_polls=2))
    with patch("imbue.mngr_ovh.ordering._OVH_DELIVERY_POLL_INTERVAL_SECONDS", 0.0):
        result = order_and_wait_for_vps(
            client,
            plan_code="vps-2025-model1",
            datacenter="US-EAST-VA",
            image_name="Debian 12 - Docker",
            pricing_mode="default",
            duration="P1M",
            deliver_timeout_seconds=10.0,
        )
    assert result == "vps-new.vps.ovh.us"


def test_order_and_wait_for_vps_polls_when_order_detail_listing_initially_empty() -> None:
    """OVH may not materialise the order's details immediately. We retry on empty list."""
    client = _client(_fake_order_router(detail_listing_first_calls_404=3))
    with patch("imbue.mngr_ovh.ordering._OVH_DELIVERY_POLL_INTERVAL_SECONDS", 0.0):
        result = order_and_wait_for_vps(
            client,
            plan_code="vps-2025-model1",
            datacenter="US-EAST-VA",
            image_name="Debian 12 - Docker",
            pricing_mode="default",
            duration="P1M",
            deliver_timeout_seconds=10.0,
        )
    assert result == "vps-new.vps.ovh.us"


def test_order_and_wait_for_vps_filters_out_os_subresource_detail() -> None:
    """The OS sub-resource has its OWN operation+resource; the plan-code filter must skip it.

    The fake's linux_detail_id has plan.code = ``"option-linux"`` and a
    resource.name of ``"<vps>-linux"`` that is NOT a real VPS service.
    The fake will raise AssertionError if we ever query its operation
    branch (vps_detail_id matches plan + has the real serviceName, so
    we should return after finding it without touching the OS detail's
    operation). This pins the plan-code filter.
    """
    client = _client(_fake_order_router())
    with patch("imbue.mngr_ovh.ordering._OVH_DELIVERY_POLL_INTERVAL_SECONDS", 0.0):
        result = order_and_wait_for_vps(
            client,
            plan_code="vps-2025-model1",
            datacenter="US-EAST-VA",
            image_name="Debian 12 - Docker",
            pricing_mode="default",
            duration="P1M",
            deliver_timeout_seconds=10.0,
        )
    assert result == "vps-new.vps.ovh.us"
    # Sanity: the result is NOT the OS sub-resource ``"<vps>-linux"`` name
    # the fake exposes on the linux_detail's operation. If the filter were
    # broken and we iterated by detail id, we'd be at risk of returning
    # whichever resource.name came first.
    assert not result.endswith("-linux")


def test_order_rejects_unavailable_datacenter() -> None:
    responses = iter(
        [
            {"cartId": "cart-2"},
            {"itemId": 100},
            [
                {"label": "vps_datacenter", "allowedValues": ["US-EAST-VA"]},
                {"label": "vps_os", "allowedValues": ["Debian 12 - Docker"]},
                {"label": "vps_install_rtm", "allowedValues": ["if_available", "no"]},
            ],
            None,
        ]
    )

    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        return next(responses)

    client = _client(fake_call)
    with pytest.raises(MngrError, match="not available"):
        order_and_wait_for_vps(
            client,
            plan_code="vps-2025-model1",
            datacenter="EU-WEST-1",
            image_name="Debian 12 - Docker",
            pricing_mode="default",
            duration="P1M",
            deliver_timeout_seconds=10.0,
        )


def test_order_rejects_unavailable_os() -> None:
    responses = iter(
        [
            {"cartId": "cart-3"},
            {"itemId": 101},
            [
                {"label": "vps_datacenter", "allowedValues": ["US-EAST-VA"]},
                {"label": "vps_os", "allowedValues": ["Ubuntu 24.04"]},
                {"label": "vps_install_rtm", "allowedValues": ["if_available", "no"]},
            ],
            None,
        ]
    )

    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        return next(responses)

    client = _client(fake_call)
    with pytest.raises(MngrError, match="not available"):
        order_and_wait_for_vps(
            client,
            plan_code="vps-2025-model1",
            datacenter="US-EAST-VA",
            image_name="Debian 12 - Docker",
            pricing_mode="default",
            duration="P1M",
            deliver_timeout_seconds=10.0,
        )


def test_order_raises_when_delivery_times_out() -> None:
    """OVH never assigns a resource.name -- polling exhausts the budget."""
    operation_fetches = {"n": 0}

    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if method == "POST" and path == "/order/cart":
            return {"cartId": "cart-4"}
        if method == "POST" and path == "/order/cart/cart-4/vps":
            return {"itemId": 102}
        if method == "GET" and path == "/order/cart/cart-4/item/102/requiredConfiguration":
            return [
                {"label": "vps_datacenter", "allowedValues": ["US-EAST-VA"]},
                {"label": "vps_os", "allowedValues": ["Debian 12 - Docker"]},
                {"label": "vps_install_rtm", "allowedValues": ["if_available", "no"]},
            ]
        if method == "POST" and path.startswith("/order/cart/cart-4/item/102/configuration"):
            return None
        if method == "POST" and path == "/order/cart/cart-4/assign":
            return None
        if method == "POST" and path == "/order/cart/cart-4/checkout":
            return {"orderId": 4242, "details": [{"cartItemID": 102, "domain": "*"}]}
        if method == "GET" and path == "/me/order/4242/details":
            return [101]
        if method == "GET" and path == "/me/order/4242/details/101/extension":
            return {
                "order": {
                    "plan": {
                        "code": "vps-2025-model1",
                        "duration": "P1M",
                        "product": {"name": "virtualPrivateServer"},
                    },
                },
            }
        if method == "GET" and path == "/me/order/4242/details/101/operations":
            return [201]
        if method == "GET" and path == "/me/order/4242/details/101/operations/201":
            operation_fetches["n"] += 1
            # Resource never populated -- OVH delivery stuck.
            return {"id": 201, "status": "doing", "resource": {}}
        if method == "DELETE" and path == "/order/cart/cart-4":
            return None
        raise AssertionError(f"unexpected call: {method} {path}")

    client = _client(fake)
    with patch("imbue.mngr_ovh.ordering._OVH_DELIVERY_POLL_INTERVAL_SECONDS", 0.0):
        with pytest.raises(OvhOrderDeliveryTimeoutError) as exc_info:
            order_and_wait_for_vps(
                client,
                plan_code="vps-2025-model1",
                datacenter="US-EAST-VA",
                image_name="Debian 12 - Docker",
                pricing_mode="default",
                duration="P1M",
                deliver_timeout_seconds=0.05,
            )
    # The exception subclasses VpsProvisioningError so existing handlers still catch it,
    # AND carries order_id so the cleanup path can attempt post-hoc adoption.
    assert isinstance(exc_info.value, VpsProvisioningError)
    assert exc_info.value.order_id == 4242
    assert "did not produce a VPS serviceName" in str(exc_info.value)
    assert operation_fetches["n"] >= 1


def test_try_poll_returns_none_when_order_not_delivered_yet() -> None:
    """One-shot poll returns None when no operation has a populated resource.name yet."""

    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if method == "GET" and path == "/me/order/9999/details":
            return [555]
        if method == "GET" and path == "/me/order/9999/details/555/extension":
            return {
                "order": {
                    "plan": {
                        "code": "vps-2025-model1",
                        "duration": "P1M",
                        "product": {"name": "virtualPrivateServer"},
                    },
                },
            }
        if method == "GET" and path == "/me/order/9999/details/555/operations":
            return [777]
        if method == "GET" and path == "/me/order/9999/details/555/operations/777":
            return {"id": 777, "status": "doing", "resource": {}}
        raise AssertionError(f"unexpected call: {method} {path}")

    client = _client(fake)
    result = try_poll_order_for_delivered_vps(client, order_id=9999, plan_code="vps-2025-model1")
    assert result is None


def test_try_poll_returns_service_name_when_order_has_delivered() -> None:
    """One-shot poll returns the assigned serviceName as soon as the operation publishes it."""

    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if method == "GET" and path == "/me/order/8888/details":
            return [444]
        if method == "GET" and path == "/me/order/8888/details/444/extension":
            return {
                "order": {
                    "plan": {
                        "code": "vps-2025-model1",
                        "duration": "P1M",
                        "product": {"name": "virtualPrivateServer"},
                    },
                },
            }
        if method == "GET" and path == "/me/order/8888/details/444/operations":
            return [666]
        if method == "GET" and path == "/me/order/8888/details/444/operations/666":
            return {"id": 666, "status": "done", "resource": {"name": "vps-late42.vps.ovh.us"}}
        raise AssertionError(f"unexpected call: {method} {path}")

    client = _client(fake)
    result = try_poll_order_for_delivered_vps(client, order_id=8888, plan_code="vps-2025-model1")
    assert result == "vps-late42.vps.ovh.us"


def test_order_raises_when_checkout_returns_no_order_id() -> None:
    """Without an orderId we cannot correlate the VPS; refuse loudly."""

    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if method == "POST" and path == "/order/cart":
            return {"cartId": "cart-5"}
        if method == "POST" and path == "/order/cart/cart-5/vps":
            return {"itemId": 103}
        if method == "GET" and path == "/order/cart/cart-5/item/103/requiredConfiguration":
            return [
                {"label": "vps_datacenter", "allowedValues": ["US-EAST-VA"]},
                {"label": "vps_os", "allowedValues": ["Debian 12 - Docker"]},
                {"label": "vps_install_rtm", "allowedValues": ["if_available", "no"]},
            ]
        if method == "POST" and path.startswith("/order/cart/cart-5/item/103/configuration"):
            return None
        if method == "POST" and path == "/order/cart/cart-5/assign":
            return None
        if method == "POST" and path == "/order/cart/cart-5/checkout":
            # Intentionally missing orderId -- the test pins
            # ``order_and_wait_for_vps`` refusing to proceed without one.
            return {"prices": {}}
        if method == "DELETE" and path == "/order/cart/cart-5":
            return None
        raise AssertionError(f"unexpected call: {method} {path}")

    client = _client(fake)
    with pytest.raises(VpsProvisioningError, match="returned no orderId"):
        order_and_wait_for_vps(
            client,
            plan_code="vps-2025-model1",
            datacenter="US-EAST-VA",
            image_name="Debian 12 - Docker",
            pricing_mode="default",
            duration="P1M",
            deliver_timeout_seconds=10.0,
        )


def test_order_post_hoc_verify_catches_wrong_plan() -> None:
    """The post-hoc verify aborts if OVH gave us the wrong plan.

    The fake's operation chain returns the requested serviceName, but
    the post-hoc GET /vps/{name} returns a DIFFERENT plan than what
    was requested -- the verify must catch this.
    """
    client = _client(
        _fake_order_router(
            service_name="vps-wrong-plan.vps.ovh.us",
            vps_info={"model": {"name": "vps-2024-larger"}, "zone": "Region OpenStack: os-us-east-va-vps-1"},
        )
    )
    with patch("imbue.mngr_ovh.ordering._OVH_DELIVERY_POLL_INTERVAL_SECONDS", 0.0):
        with pytest.raises(VpsProvisioningError, match="plan 'vps-2024-larger'"):
            order_and_wait_for_vps(
                client,
                plan_code="vps-2025-model1",
                datacenter="US-EAST-VA",
                image_name="Debian 12 - Docker",
                pricing_mode="default",
                duration="P1M",
                deliver_timeout_seconds=10.0,
            )


def test_order_post_hoc_verify_catches_wrong_region() -> None:
    """The post-hoc verify aborts if OVH gave us the wrong datacenter."""
    client = _client(
        _fake_order_router(
            service_name="vps-wrong-zone.vps.ovh.us",
            vps_info={"model": {"name": "vps-2025-model1"}, "zone": "Region OpenStack: os-us-west-or-vps-1"},
        )
    )
    with patch("imbue.mngr_ovh.ordering._OVH_DELIVERY_POLL_INTERVAL_SECONDS", 0.0):
        with pytest.raises(VpsProvisioningError, match="zone 'Region OpenStack: os-us-west-or-vps-1'"):
            order_and_wait_for_vps(
                client,
                plan_code="vps-2025-model1",
                datacenter="US-EAST-VA",
                image_name="Debian 12 - Docker",
                pricing_mode="default",
                duration="P1M",
                deliver_timeout_seconds=10.0,
            )


def test_f3_parallel_orders_each_get_their_own_service_name() -> None:
    """Regression for F3: two concurrent orders return their OWN serviceNames.

    Models the parallel-pool-bake scenario the user explicitly cares
    about. Both threads' checkout calls interleave; both threads see
    both new serviceNames once delivery completes. The legacy
    diff-against-/vps approach (still in git history) would have
    picked ``sorted(new_names)[0]`` -- both threads end up with the
    same serviceName, race lost silently. The orderId-correlated path
    returns each thread's OWN serviceName.
    """
    delivered_service_names: set[str] = {"vps-aaa.vps.ovh.us", "vps-bbb.vps.ovh.us"}
    fake_lock = threading.Lock()
    carts_handed_out = {"n": 0}
    cart_to_thread: dict[str, int] = {}

    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        with fake_lock:
            if method == "POST" and path == "/order/cart":
                carts_handed_out["n"] += 1
                cart_id = f"cart-{carts_handed_out['n']}"
                cart_to_thread[cart_id] = carts_handed_out["n"]
                return {"cartId": cart_id}
            if method == "POST" and path.endswith("/vps") and path.startswith("/order/cart/"):
                cart_id = path.split("/")[3]
                thread_n = cart_to_thread[cart_id]
                return {"itemId": 10 + thread_n - 1}
            if method == "GET" and "/requiredConfiguration" in path:
                return [
                    {"label": "vps_datacenter", "allowedValues": ["US-EAST-VA"]},
                    {"label": "vps_os", "allowedValues": ["Debian 12 - Docker"]},
                    {"label": "vps_install_rtm", "allowedValues": ["if_available", "no"]},
                ]
            if method == "POST" and "/configuration" in path:
                return None
            if method == "POST" and path.endswith("/assign"):
                return None
            if method == "POST" and path.endswith("/checkout"):
                cart_id = path.split("/")[3]
                thread_n = cart_to_thread[cart_id]
                order_id = 99 + thread_n
                # billing.OrderDetail.domain is the literal "*" in the
                # real OVH API; our code must look up the serviceName
                # via the operations chain, NOT this field.
                return {
                    "orderId": order_id,
                    "details": [{"cartItemID": 10 + thread_n - 1, "domain": "*"}],
                }
            # /me/order/{orderId}/details -> [detailId]
            if method == "GET" and path == "/me/order/100/details":
                return [200]
            if method == "GET" and path == "/me/order/101/details":
                return [201]
            # /extension -> billing.ItemDetail. Each order's detail has
            # the matching plan code.
            if method == "GET" and path == "/me/order/100/details/200/extension":
                return {
                    "order": {
                        "plan": {
                            "code": "vps-2025-model1",
                            "duration": "P1M",
                            "product": {"name": "virtualPrivateServer"},
                        },
                    },
                }
            if method == "GET" and path == "/me/order/101/details/201/extension":
                return {
                    "order": {
                        "plan": {
                            "code": "vps-2025-model1",
                            "duration": "P1M",
                            "product": {"name": "virtualPrivateServer"},
                        },
                    },
                }
            # /operations -> [operationId]
            if method == "GET" and path == "/me/order/100/details/200/operations":
                return [3001]
            if method == "GET" and path == "/me/order/101/details/201/operations":
                return [3002]
            # /operations/{opId} -> service.Operation with the assigned
            # resource.name. THIS is the strong-correlation point:
            # thread1's orderId never sees thread2's resource.name.
            if method == "GET" and path == "/me/order/100/details/200/operations/3001":
                return {
                    "id": 3001,
                    "status": "done",
                    "resource": {"name": "vps-aaa.vps.ovh.us", "state": "ok"},
                }
            if method == "GET" and path == "/me/order/101/details/201/operations/3002":
                return {
                    "id": 3002,
                    "status": "done",
                    "resource": {"name": "vps-bbb.vps.ovh.us", "state": "ok"},
                }
            if method == "GET" and "/tasks?state=" in path:
                return []
            if method == "GET" and path == "/vps/vps-aaa.vps.ovh.us":
                return {"model": {"name": "vps-2025-model1"}, "zone": "Region OpenStack: os-us-east-va-vps-1"}
            if method == "GET" and path == "/vps/vps-bbb.vps.ovh.us":
                return {"model": {"name": "vps-2025-model1"}, "zone": "Region OpenStack: os-us-east-va-vps-1"}
            raise AssertionError(f"unexpected call: {method} {path}")

    client = _client(fake)
    # The thread body catches exactly the exception types
    # ``order_and_wait_for_vps`` can raise, plus ``AssertionError`` from
    # the fake router's unrecognised-call branch. The narrow tuple form
    # is required by the project ratchets that forbid broad / base
    # catches.
    results: dict[str, str] = {}
    errors: list[MngrError | VpsApiError | VpsProvisioningError | AssertionError] = []

    def worker(label: str) -> None:
        try:
            with patch("imbue.mngr_ovh.ordering._OVH_DELIVERY_POLL_INTERVAL_SECONDS", 0.0):
                got = order_and_wait_for_vps(
                    client,
                    plan_code="vps-2025-model1",
                    datacenter="US-EAST-VA",
                    image_name="Debian 12 - Docker",
                    pricing_mode="default",
                    duration="P1M",
                    deliver_timeout_seconds=10.0,
                )
            with fake_lock:
                results[label] = got
        except (MngrError, VpsApiError, VpsProvisioningError, AssertionError) as exc:
            with fake_lock:
                errors.append(exc)

    t1 = threading.Thread(target=worker, args=("thread1",))
    t2 = threading.Thread(target=worker, args=("thread2",))
    t1.start()
    t2.start()
    t1.join(timeout=30.0)
    t2.join(timeout=30.0)

    assert not errors, f"parallel orders raised: {errors!r}"
    assert set(results.values()) == delivered_service_names, (
        f"parallel orders returned overlapping serviceNames (race not fixed): {results}"
    )


def test_rebuild_polls_task_to_completion() -> None:
    task_polls = iter(
        [
            {"id": 555, "state": "todo", "type": "reinstallVm"},
            {"id": 555, "state": "doing", "type": "reinstallVm"},
            {"id": 555, "state": "done", "type": "reinstallVm"},
        ]
    )

    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        # The pre-rebuild drain probes both ?state=todo and ?state=doing;
        # both return [] here so the drain returns immediately.
        if method == "GET" and "/tasks?state=" in path:
            return []
        if method == "POST" and path.endswith("/rebuild"):
            return {"id": 555, "state": "todo"}
        return next(task_polls)

    client = _client(fake_call)
    rebuild_vps_with_public_key(
        client,
        service_name="vps-x.vps.ovh.us",
        image_id="uuid-img",
        public_ssh_key="ssh-ed25519 AAAA test",
        task_timeout_seconds=10.0,
    )


def test_rebuild_raises_when_task_errors() -> None:
    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if method == "GET" and "/tasks?state=" in path:
            return []
        if method == "POST" and path.endswith("/rebuild"):
            return {"id": 556, "state": "todo"}
        return {"id": 556, "state": "error", "type": "reinstallVm"}

    client = _client(fake_call)
    with pytest.raises(VpsProvisioningError):
        rebuild_vps_with_public_key(
            client,
            service_name="vps-x.vps.ovh.us",
            image_id="uuid-img",
            public_ssh_key="ssh-ed25519 AAAA test",
            task_timeout_seconds=10.0,
        )


def test_rebuild_waits_when_tasks_still_active() -> None:
    """The pre-rebuild drain must block until both active-state lists empty.

    Reproduces the original Bug 1 condition: a deliverVm task is still
    in `doing` immediately after the VPS appears in /vps. The fixed
    `rebuild_vps_with_public_key` must wait that task out before POSTing
    /rebuild (which would otherwise return HTTP 400 with "Action not
    available while there are running tasks on the VPS").
    """
    todo_responses = iter([[], [], []])
    doing_responses = iter([[42], [42], []])
    rebuild_was_called: list[bool] = []

    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if method == "GET" and "/tasks?state=todo" in path:
            return next(todo_responses)
        if method == "GET" and "/tasks?state=doing" in path:
            return next(doing_responses)
        if method == "POST" and path.endswith("/rebuild"):
            rebuild_was_called.append(True)
            return {"id": 999, "state": "todo"}
        if method == "GET" and "/tasks/999" in path:
            return {"id": 999, "state": "done", "type": "reinstallVm"}
        raise AssertionError(f"Unexpected call: {method} {path}")

    client = _client(fake_call)
    rebuild_vps_with_public_key(
        client,
        service_name="vps-x.vps.ovh.us",
        image_id="uuid-img",
        public_ssh_key="ssh-ed25519 AAAA test",
        task_timeout_seconds=10.0,
    )
    assert rebuild_was_called == [True]


def test_rebuild_retries_when_ovh_rejects_with_running_tasks_despite_empty_drain() -> None:
    """OVH's task listing is eventually consistent, so the drain can report no
    active tasks while ``/rebuild`` is still rejected with "...running tasks on
    the VPS". The rebuild must re-drain and retry that POST until OVH accepts
    it instead of failing the whole bake (the 3A fresh-order failure).
    """
    rebuild_attempts: list[int] = []

    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        # Drain always reports empty -- the listing lags reality.
        if method == "GET" and "/tasks?state=" in path:
            return []
        if method == "POST" and path.endswith("/rebuild"):
            rebuild_attempts.append(1)
            # OVH rejects while a task is in flight; accept on the third try.
            if len(rebuild_attempts) < 3:
                raise APIError("Action not available while there are running tasks on the VPS")
            return {"id": 321, "state": "todo"}
        if method == "GET" and "/tasks/321" in path:
            return {"id": 321, "state": "done", "type": "reinstallVm"}
        raise AssertionError(f"Unexpected call: {method} {path}")

    client = _client(fake_call)
    rebuild_vps_with_public_key(
        client,
        service_name="vps-x.vps.ovh.us",
        image_id="uuid-img",
        public_ssh_key="ssh-ed25519 AAAA test",
        task_timeout_seconds=10.0,
    )
    assert len(rebuild_attempts) == 3


def test_rebuild_does_not_retry_non_running_task_api_errors() -> None:
    """A rebuild rejection unrelated to in-flight tasks surfaces immediately, not retried."""
    rebuild_attempts: list[int] = []

    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if method == "GET" and "/tasks?state=" in path:
            return []
        if method == "POST" and path.endswith("/rebuild"):
            rebuild_attempts.append(1)
            raise APIError("Bad Request: imageId not found")
        raise AssertionError(f"Unexpected call: {method} {path}")

    client = _client(fake_call)
    with pytest.raises(VpsApiError):
        rebuild_vps_with_public_key(
            client,
            service_name="vps-x.vps.ovh.us",
            image_id="uuid-img",
            public_ssh_key="ssh-ed25519 AAAA test",
            task_timeout_seconds=10.0,
        )
    assert len(rebuild_attempts) == 1
