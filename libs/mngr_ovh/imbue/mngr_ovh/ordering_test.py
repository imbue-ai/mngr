"""Tests for the OVH order/cart flow."""

from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import ovh
import pytest

from imbue.mngr.errors import MngrError
from imbue.mngr_ovh.client import OvhVpsClient
from imbue.mngr_ovh.ordering import order_and_wait_for_vps
from imbue.mngr_ovh.ordering import rebuild_vps_with_public_key
from imbue.mngr_vps_docker.errors import VpsProvisioningError


def _client(call_side_effect: Any) -> OvhVpsClient:
    m = MagicMock(spec=ovh.Client)
    m.call = MagicMock(side_effect=call_side_effect)
    return OvhVpsClient(ovh_client=m, subsidiary="US", task_poll_interval=0.0)


def _success_order_responses() -> list[Any]:
    """Drives ordering through every required step exactly once."""
    return [
        [],
        {"cartId": "cart-1"},
        {"itemId": 99},
        [
            {"label": "vps_datacenter", "allowedValues": ["US-EAST-VA"]},
            {"label": "vps_os", "allowedValues": ["Debian 12 - Docker"]},
            {"label": "vps_install_rtm", "allowedValues": ["if_available", "no"]},
        ],
        None,
        None,
        None,
        None,
        None,
        ["vps-new.vps.ovh.us"],
    ]


def test_order_and_wait_for_vps_success() -> None:
    responses = iter(_success_order_responses())

    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        return next(responses)

    client = _client(fake_call)
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
            [],
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
            [],
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
    responses = iter(
        [
            ["vps-old.vps.ovh.us"],
            {"cartId": "cart-4"},
            {"itemId": 102},
            [
                {"label": "vps_datacenter", "allowedValues": ["US-EAST-VA"]},
                {"label": "vps_os", "allowedValues": ["Debian 12 - Docker"]},
                {"label": "vps_install_rtm", "allowedValues": ["if_available", "no"]},
            ],
            None,
            None,
            None,
            None,
            None,
            ["vps-old.vps.ovh.us"],
            ["vps-old.vps.ovh.us"],
        ]
    )

    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        try:
            return next(responses)
        except StopIteration:
            return ["vps-old.vps.ovh.us"]

    client = _client(fake_call)
    with patch("imbue.mngr_ovh.ordering._OVH_DELIVERY_POLL_INTERVAL_SECONDS", 0.0):
        with pytest.raises(VpsProvisioningError, match="did not deliver"):
            order_and_wait_for_vps(
                client,
                plan_code="vps-2025-model1",
                datacenter="US-EAST-VA",
                image_name="Debian 12 - Docker",
                pricing_mode="default",
                duration="P1M",
                deliver_timeout_seconds=0.05,
            )


def test_rebuild_polls_task_to_completion() -> None:
    responses = iter(
        [
            {"id": 555, "state": "todo", "type": "reinstallVm"},
            {"id": 555, "state": "doing", "type": "reinstallVm"},
            {"id": 555, "state": "done", "type": "reinstallVm"},
        ]
    )

    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if method == "POST" and path.endswith("/rebuild"):
            return {"id": 555, "state": "todo"}
        return next(responses)

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
