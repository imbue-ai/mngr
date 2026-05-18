"""Tests for the OVH order/cart flow."""

import threading
from typing import Any
from typing import Callable
from unittest.mock import MagicMock
from unittest.mock import patch

import ovh
import pytest

from imbue.mngr.errors import MngrError
from imbue.mngr_ovh.client import OvhVpsClient
from imbue.mngr_ovh.ordering import order_and_wait_for_vps
from imbue.mngr_ovh.ordering import rebuild_vps_with_public_key
from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.errors import VpsProvisioningError


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
    detail_id: int = 7,
    service_name: str = "vps-new.vps.ovh.us",
    inline_domain: str | None = None,
    allowed_datacenters: tuple[str, ...] = ("US-EAST-VA",),
    allowed_os: tuple[str, ...] = ("Debian 12 - Docker",),
    vps_info: dict[str, Any] | None = None,
    detail_listing_first_calls_404: int = 0,
    domain_populated_after_n_polls: int = 0,
) -> Callable[[str, str, Any, bool], Any]:
    """Build a fake ``client.call`` that drives ``order_and_wait_for_vps`` through one happy run.

    Knobs:
    - ``inline_domain``: if set, the checkout response carries the
      serviceName inline in ``details[].domain`` so the polled
      /me/order path is skipped.
    - ``detail_listing_first_calls_404``: simulate OVH not having
      materialised the order yet -- /me/order/{id}/details returns []
      this many times before the real list appears.
    - ``domain_populated_after_n_polls``: number of detail GETs that
      return an empty ``domain`` before OVH writes the real serviceName.
    """
    # If the inline_domain is set (i.e. checkout returns the serviceName
    # immediately) the rest of the flow operates on THAT name, not the
    # ``service_name`` default. Resolve to a single ``effective_name``
    # the rest of the router uses for /vps/{name} dispatch.
    effective_name = inline_domain if inline_domain is not None else service_name
    if vps_info is None:
        vps_info = _vps_info_for()
    detail_list_call_count = {"n": 0}
    detail_get_call_count = {"n": 0}

    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        # Cart construction
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
            response: dict[str, Any] = {"orderId": order_id, "prices": {}, "url": "https://x"}
            if inline_domain is not None:
                response["details"] = [{"cartItemID": item_id, "domain": inline_domain}]
            else:
                response["details"] = [{"cartItemID": item_id, "domain": ""}]
            return response
        # Order-detail polling
        if method == "GET" and path == f"/me/order/{order_id}/details":
            detail_list_call_count["n"] += 1
            if detail_list_call_count["n"] <= detail_listing_first_calls_404:
                return []
            return [detail_id]
        if method == "GET" and path == f"/me/order/{order_id}/details/{detail_id}":
            detail_get_call_count["n"] += 1
            if detail_get_call_count["n"] <= domain_populated_after_n_polls:
                return {"orderDetailId": detail_id, "domain": "", "description": "VPS"}
            return {"orderDetailId": detail_id, "domain": service_name, "description": "VPS"}
        # Post-delivery task drain
        if method == "GET" and "/tasks?state=" in path and effective_name in path:
            return []
        # Post-hoc verify
        if method == "GET" and path == f"/vps/{effective_name}":
            return vps_info
        # Failure-path cleanup -- the post-hoc verify raises into the
        # ``except`` branch which calls ``_safe_delete_cart``.
        if method == "DELETE" and path == f"/order/cart/{cart_id}":
            return None
        # Anything else is an unexpected call -- fail loudly.
        raise AssertionError(f"unexpected fake OVH call: {method} {path}")

    return fake


def test_order_and_wait_for_vps_success_polled_path() -> None:
    """Happy path: checkout returns empty inline domain; serviceName comes from /me/order polling."""
    client = _client(_fake_order_router(domain_populated_after_n_polls=2))
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


def test_order_and_wait_for_vps_uses_inline_domain_when_populated() -> None:
    """If checkout already populates ``details[].domain``, skip the /me/order poll."""
    client = _client(_fake_order_router(inline_domain="vps-new.vps.ovh.us"))
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
    """OVH never populates ``details[].domain`` -- polling exhausts the budget."""
    detail_fetches = {"n": 0}

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
            return {"orderId": 4242, "details": [{"cartItemID": 102, "domain": ""}]}
        if method == "GET" and path == "/me/order/4242/details":
            return [101]
        if method == "GET" and path == "/me/order/4242/details/101":
            detail_fetches["n"] += 1
            return {"orderDetailId": 101, "domain": ""}
        if method == "DELETE" and path == "/order/cart/cart-4":
            return None
        raise AssertionError(f"unexpected call: {method} {path}")

    client = _client(fake)
    with patch("imbue.mngr_ovh.ordering._OVH_DELIVERY_POLL_INTERVAL_SECONDS", 0.0):
        with pytest.raises(VpsProvisioningError, match="did not produce a VPS serviceName"):
            order_and_wait_for_vps(
                client,
                plan_code="vps-2025-model1",
                datacenter="US-EAST-VA",
                image_name="Debian 12 - Docker",
                pricing_mode="default",
                duration="P1M",
                deliver_timeout_seconds=0.05,
            )
    assert detail_fetches["n"] >= 1


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
    """The post-hoc verify aborts if OVH gave us the wrong plan."""
    client = _client(
        _fake_order_router(
            inline_domain="vps-wrong-plan.vps.ovh.us",
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
            inline_domain="vps-wrong-zone.vps.ovh.us",
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
                return {
                    "orderId": order_id,
                    "details": [{"cartItemID": 10 + thread_n - 1, "domain": ""}],
                }
            if method == "GET" and path == "/me/order/100/details":
                return [200]
            if method == "GET" and path == "/me/order/101/details":
                return [201]
            if method == "GET" and path == "/me/order/100/details/200":
                return {"orderDetailId": 200, "domain": "vps-aaa.vps.ovh.us"}
            if method == "GET" and path == "/me/order/101/details/201":
                return {"orderDetailId": 201, "domain": "vps-bbb.vps.ovh.us"}
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
