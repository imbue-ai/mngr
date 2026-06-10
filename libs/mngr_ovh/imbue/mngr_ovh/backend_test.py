"""Tests for OVH provider backend registration + the _provision_vps ordering contracts."""

from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_ovh.backend import OVH_BACKEND_NAME
from imbue.mngr_ovh.backend import OvhProvider
from imbue.mngr_ovh.backend import OvhProviderBackend
from imbue.mngr_ovh.backend import register_provider_backend
from imbue.mngr_ovh.config import OvhProviderConfig
from imbue.mngr_ovh.iam_tags import MNGR_PROVIDER_TAG_KEY
from imbue.mngr_ovh.mock_ovh_client_test import make_fake_ovh_vps_client
from imbue.mngr_ovh.ordering import OvhOrderDeliveryTimeoutError
from imbue.mngr_ovh.pending_orders import read_pending_order_markers
from imbue.mngr_ovh.pending_orders import write_pending_order_marker


def test_backend_name() -> None:
    assert OvhProviderBackend.get_name() == ProviderBackendName("ovh")


def test_backend_name_constant() -> None:
    assert OVH_BACKEND_NAME == ProviderBackendName("ovh")


def test_backend_description() -> None:
    desc = OvhProviderBackend.get_description()
    assert "OVH" in desc
    assert "Docker" in desc


def test_backend_config_class() -> None:
    assert OvhProviderBackend.get_config_class() is OvhProviderConfig


def test_backend_build_args_help() -> None:
    help_text = OvhProviderBackend.get_build_args_help()
    assert "--vps-datacenter" in help_text
    assert "--vps-plan" in help_text
    assert "--vps-os" in help_text


def test_backend_start_args_help() -> None:
    assert "docker run" in OvhProviderBackend.get_start_args_help()


def test_register_provider_backend_returns_tuple() -> None:
    result = register_provider_backend()
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert result[0] is OvhProviderBackend
    assert result[1] is OvhProviderConfig


# -- _provision_vps ordering contracts (behavioral) ---------------------------
#
# These replace the original source-text invariant tests (which sliced the
# `_provision_vps` source and asserted str.find() ordering of call names --
# fragile to refactors/comments and blind to real bugs). Each drives the
# real provider method against a fake OVH transport and asserts the observable
# effect (no API call before validation, an adopted orphan becoming a recycle
# candidate, a marker landing on disk after a delivery timeout). All offline:
# the fake `ovh.Client` makes no network calls. See `conftest.ovh_provider_factory`.


def _provision_kwargs(*, vps_ssh_key_id: str) -> dict[str, Any]:
    """Standard ``_provision_vps`` arguments for a US-EAST-VA / Debian-Docker VPS.

    ``vps_host_key_path`` / ``vps_host_public_key`` are accepted by the
    method but immediately discarded (OVH can't inject host keys), so any
    placeholder is fine.
    """
    return {
        "name": HostName("ovh-test-host"),
        "region": "US-EAST-VA",
        "plan": "vps-2025-model1",
        "os_id": "Debian 12 - Docker",
        "vps_host_key_path": Path("/unused/host_key"),
        "vps_host_public_key": "",
        "vps_ssh_key_id": vps_ssh_key_id,
    }


def test_provision_vps_validates_extra_tags_before_any_ovh_api_call(
    ovh_provider_factory: Callable[..., OvhProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1: a malformed ``MNGR_VPS_EXTRA_TAGS`` must abort before ordering/recycling.

    Before the F1 fix the parse ran AFTER ``order_and_wait_for_vps``, so a
    typo (uppercase key, reserved key, missing ``=``) raised only after a
    VPS had already been ordered and billed for a month. This drives the
    real ``_provision_vps`` with a recording transport: the malformed env
    must raise ``MngrError`` with zero OVH API calls having fired (no
    reconcile, no recycle, and -- crucially -- no order).
    """
    api_calls: list[tuple[str, str]] = []

    def recording_transport(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        api_calls.append((method, path))
        return None

    client = make_fake_ovh_vps_client(recording_transport)
    client.upload_ssh_key("vps-key", "ssh-ed25519 AAAAtest")
    provider = ovh_provider_factory(client)
    # Uppercase key is rejected by parse_extra_tags_env (OVH IAM key regex).
    # Match on the offending key so we know the raise is the tag-parse
    # rejection and not some incidental earlier failure.
    monkeypatch.setenv("MNGR_VPS_EXTRA_TAGS", "BadKey=oops")

    with pytest.raises(MngrError, match="BadKey"):
        provider._provision_vps(host_id=HostId.generate(), **_provision_kwargs(vps_ssh_key_id="vps-key"))

    assert api_calls == [], (
        "MNGR_VPS_EXTRA_TAGS must be validated before any state-changing OVH call; "
        f"these fired before the malformed value was rejected: {api_calls}"
    )


def test_provision_vps_writes_pending_marker_when_order_delivery_times_out(
    ovh_provider_factory: Callable[..., OvhProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On an order-delivery timeout, ``_provision_vps`` deposits a pending-order marker and re-raises.

    The reconcile sweep is useless if the failure path doesn't leave a
    marker for it to find. Drives the real order/cart flow to checkout, then
    -- with ``vps_boot_timeout=0.0`` so the delivery-poll loop is skipped --
    the real ``order_and_wait_for_vps`` raises ``OvhOrderDeliveryTimeoutError``
    carrying the order id. The test asserts a marker with that id (plus the
    requested plan/region) actually lands under the provider's state dir.
    """
    monkeypatch.delenv("MNGR_VPS_EXTRA_TAGS", raising=False)
    order_id = 778899
    client = make_fake_ovh_vps_client(_cart_checkout_transport(order_id))
    client.upload_ssh_key("vps-key", "ssh-ed25519 AAAAtest")
    # Disable recycling so the run goes straight to the fresh-order path.
    provider = ovh_provider_factory(client, enable_recycle_cancelled=False, vps_boot_timeout=0.0)

    with pytest.raises(OvhOrderDeliveryTimeoutError):
        provider._provision_vps(host_id=HostId.generate(), **_provision_kwargs(vps_ssh_key_id="vps-key"))

    markers = read_pending_order_markers(provider._provider_state_dir())
    assert [m.order_id for m in markers] == [order_id]
    assert markers[0].plan_code == "vps-2025-model1"
    assert markers[0].region == "US-EAST-VA"


def test_reconcile_adopts_delivered_orphan_so_recycle_claims_it_same_bake(
    ovh_provider_factory: Callable[..., OvhProvider],
) -> None:
    """Reconcile-before-recycle: a slowly-delivered orphan is adopted, then claimed in the SAME bake.

    ``_provision_vps`` runs ``_reconcile_pending_orders`` BEFORE
    ``_maybe_claim_recycled_vps`` so a VPS whose order completed between
    bakes is tagged + cancelled in time for THIS bake's recycle check to
    claim it (instead of ordering a fresh one). This drives the two real
    methods in that order against a fake whose orphan starts untagged and
    not-cancelled:

    - Before reconcile, the orphan carries no ``mngr-provider`` tag, so the
      recycle check finds no candidate (returns ``None``).
    - ``_reconcile_pending_orders`` polls the (delivered) order, tags the
      orphan with this provider, and flips ``deleteAtExpiration=true``.
    - The recycle check then claims that exact orphan -- proving the
      adoption made it eligible within the same bake.
    """
    order_id = 445566
    service_name = "vps-orphan.vps.ovh.us"
    fake = _DeliveredOrphanOvh(order_id=order_id, plan_code="vps-2025-model1", service_name=service_name)
    client = make_fake_ovh_vps_client(fake)
    provider = ovh_provider_factory(client, provider_name="alice-ovh")
    # A prior bake's delivery timeout would have written this marker.
    write_pending_order_marker(
        provider._provider_state_dir(), order_id=order_id, plan_code="vps-2025-model1", region="US-EAST-VA"
    )

    claim_kwargs = {
        "new_host_id": HostId.generate(),
        "requested_plan": "vps-2025-model1",
        "requested_region": "US-EAST-VA",
        "extra_tags": {},
    }

    # Untagged orphan is invisible to the recycle filter -> no candidate.
    assert provider._maybe_claim_recycled_vps(**claim_kwargs) is None

    provider._reconcile_pending_orders()
    # Adoption tagged the orphan for this provider and cancelled it.
    assert fake.tags[MNGR_PROVIDER_TAG_KEY] == "alice-ovh"
    assert fake.service_info["renew"]["deleteAtExpiration"] is True
    # The marker was consumed once the orphan was adopted.
    assert read_pending_order_markers(provider._provider_state_dir()) == []

    # Same bake: the recycle check now claims the just-adopted orphan.
    handle = provider._maybe_claim_recycled_vps(**claim_kwargs)
    assert handle is not None
    assert handle.service_name == service_name


def _cart_checkout_transport(order_id: int) -> Callable[..., Any]:
    """Fake transport that completes the OVH cart/checkout flow and returns ``order_id``.

    Models only the pre-delivery half of ``order_and_wait_for_vps`` (cart ->
    item -> required config -> configure -> assign -> checkout). With
    ``vps_boot_timeout=0.0`` the delivery-poll loop never runs, so the order
    times out immediately after checkout -- exactly the slow-delivery
    failure ``_provision_vps`` must convert into a pending-order marker.
    """
    cart_id = "cart-timeout"
    item_id = 99

    def transport(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if method == "POST" and path == "/order/cart":
            return {"cartId": cart_id}
        if method == "POST" and path == f"/order/cart/{cart_id}/vps":
            return {"itemId": item_id}
        if method == "GET" and path == f"/order/cart/{cart_id}/item/{item_id}/requiredConfiguration":
            return [
                {"label": "vps_datacenter", "allowedValues": ["US-EAST-VA"]},
                {"label": "vps_os", "allowedValues": ["Debian 12 - Docker"]},
                {"label": "vps_install_rtm", "allowedValues": ["if_available", "no"]},
            ]
        if method == "POST" and path == f"/order/cart/{cart_id}/item/{item_id}/configuration":
            return None
        if method == "POST" and path == f"/order/cart/{cart_id}/assign":
            return None
        if method == "POST" and path == f"/order/cart/{cart_id}/checkout":
            return {"orderId": order_id, "prices": {}, "url": "https://order", "details": []}
        # On the timeout path, order_and_wait_for_vps best-effort deletes the cart.
        if method == "DELETE" and path == f"/order/cart/{cart_id}":
            return None
        raise AssertionError(f"Unexpected pre-delivery OVH call: {method} {path}")

    return transport


class _DeliveredOrphanOvh:
    """Fake OVH transport for one timed-out order whose VPS has since delivered.

    Answers exactly the calls ``_reconcile_pending_orders`` +
    ``_maybe_claim_recycled_vps`` make for a single orphan: the
    order details/operations poll (yields ``service_name``), the IAM
    resource list, per-VPS ``serviceInfos`` / details, and tag +
    ``serviceInfos`` mutations. The orphan starts untagged (absent from
    the provider's IAM filter) and not-cancelled; adoption tags it and
    flips ``deleteAtExpiration`` so the recycle path treats it as a
    candidate. Tag mutations are matched by endpoint shape (not exact
    urn) since there is only one VPS, so the IAM region code is irrelevant.
    """

    def __init__(self, *, order_id: int, plan_code: str, service_name: str) -> None:
        self.order_id = order_id
        self.plan_code = plan_code
        self.service_name = service_name
        self.tags: dict[str, str] = {}
        far_future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
        self.service_info: dict[str, Any] = {
            "renew": {"deleteAtExpiration": False, "automatic": True, "period": 1},
            "status": "ok",
            "expiration": far_future,
            "engagedUpTo": None,
            "contactAdmin": "infra@imbue.com",
            "contactBilling": "infra@imbue.com",
            "contactTech": "infra@imbue.com",
            "renewalType": "automaticV2012",
            "domain": service_name,
            "serviceId": 4242,
            "creation": "2026-05-15",
            "possibleRenewPeriod": [],
            "canDeleteAtExpiration": False,
        }
        self.vps_details: dict[str, Any] = {
            "state": "running",
            "model": {"name": plan_code, "offer": "VPS-1", "vcore": 1, "memory": 2048, "disk": 40},
            "zone": "Region OpenStack: os-us-east-va-vps-1",
            "name": service_name,
            "displayName": service_name,
        }
        self._detail_id = 5001
        self._op_id = 6001

    def __call__(self, method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        # Order details/operations poll (reconcile).
        if method == "GET" and path == f"/me/order/{self.order_id}/details":
            return [self._detail_id]
        if method == "GET" and path == f"/me/order/{self.order_id}/details/{self._detail_id}/extension":
            return {"order": {"plan": {"code": self.plan_code, "product": {"name": "virtualPrivateServer"}}}}
        if method == "GET" and path == f"/me/order/{self.order_id}/details/{self._detail_id}/operations":
            return [self._op_id]
        if method == "GET" and path == f"/me/order/{self.order_id}/details/{self._detail_id}/operations/{self._op_id}":
            return {"id": self._op_id, "status": "done", "resource": {"name": self.service_name, "state": "ok"}}
        # IAM resource listing (recycle candidate discovery + provider filter).
        if method == "GET" and path == "/v2/iam/resource?resourceType=vps":
            return [
                {
                    "urn": f"urn:v1:us:resource:vps:{self.service_name}",
                    "name": self.service_name,
                    "displayName": self.service_name,
                    "type": "vps",
                    "tags": dict(self.tags),
                }
            ]
        # Per-VPS serviceInfos (read-modify-write for cancel/un-cancel).
        if method == "GET" and path == f"/vps/{self.service_name}/serviceInfos":
            return dict(self.service_info)
        if method == "PUT" and path == f"/vps/{self.service_name}/serviceInfos":
            assert body is not None
            self.service_info = dict(body)
            return None
        # Per-VPS details (plan/zone/state eligibility checks).
        if method == "GET" and path == f"/vps/{self.service_name}":
            return dict(self.vps_details)
        # Tag mutations -- single VPS, so match by endpoint shape, not urn.
        if method == "POST" and "/v2/iam/resource/" in path and path.endswith("/tag"):
            assert body is not None
            self.tags[body["key"]] = body["value"]
            return None
        if method == "DELETE" and "/v2/iam/resource/" in path and "/tag/" in path:
            self.tags.pop(path.rsplit("/tag/", 1)[1], None)
            return None
        raise AssertionError(f"Unscripted OVH call: {method} {path}")
