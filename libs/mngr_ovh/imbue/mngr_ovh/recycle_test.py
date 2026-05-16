"""Tests for OVH cancelled-VPS recycling."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from unittest.mock import MagicMock

import ovh
import pytest

from imbue.mngr.primitives import HostId
from imbue.mngr_ovh.client import OvhVpsClient
from imbue.mngr_ovh.iam_tags import MNGR_HOST_ID_TAG_KEY
from imbue.mngr_ovh.iam_tags import MNGR_PROVIDER_TAG_KEY
from imbue.mngr_ovh.iam_tags import MNGR_RECYCLING_LOCK_TAG_KEY
from imbue.mngr_ovh.recycle import try_recycle_cancelled_vps


def _client(call_side_effect: Any, *, is_unconfigured: bool = False) -> OvhVpsClient:
    m = MagicMock(spec=ovh.Client)
    m.call = MagicMock(side_effect=call_side_effect)
    return OvhVpsClient(ovh_client=m, subsidiary="US", task_poll_interval=0.0, is_unconfigured=is_unconfigured)


def _iam_payload(
    name: str,
    *,
    provider: str = "alice-ovh",
    host_id: str | None = "old-host-id",
    lock_holder: str | None = None,
) -> dict[str, Any]:
    tags: dict[str, str] = {MNGR_PROVIDER_TAG_KEY: provider}
    if host_id is not None:
        tags[MNGR_HOST_ID_TAG_KEY] = host_id
    if lock_holder is not None:
        tags[MNGR_RECYCLING_LOCK_TAG_KEY] = lock_holder
    return {
        "urn": f"urn:v1:us:resource:vps:{name}",
        "name": name,
        "displayName": name,
        "type": "vps",
        "tags": tags,
    }


def _service_info(
    *,
    delete_at_expiration: bool = True,
    status: str = "ok",
    expiration_days_from_now: int = 25,
    engaged_up_to: str | None = None,
) -> dict[str, Any]:
    expiration = (datetime.now(timezone.utc) + timedelta(days=expiration_days_from_now)).strftime("%Y-%m-%d")
    return {
        "renew": {"deleteAtExpiration": delete_at_expiration, "automatic": True, "period": 1},
        "status": status,
        "expiration": expiration,
        "engagedUpTo": engaged_up_to,
        "contactAdmin": "infra@imbue.com",
        "contactBilling": "infra@imbue.com",
        "contactTech": "infra@imbue.com",
        "renewalType": "automaticV2012",
        "domain": "vps-x.vps.ovh.us",
        "serviceId": 12345,
        "creation": "2026-05-15",
        "possibleRenewPeriod": [],
        "canDeleteAtExpiration": False,
    }


def _vps_details(
    *,
    state: str = "running",
    plan_code: str = "vps-2025-model1",
    zone: str = "Region OpenStack: os-us-east-va-vps-1",
) -> dict[str, Any]:
    return {
        "state": state,
        "model": {"name": plan_code, "offer": "VPS-1", "vcore": 1, "memory": 2048, "disk": 40},
        "zone": zone,
        "name": "vps-x.vps.ovh.us",
        "displayName": "vps-x.vps.ovh.us",
    }


class _FakeOvh:
    """Driver for ``ovh.Client.call`` that scripts the recycle conversation.

    Encapsulates the request log so individual tests can assert that the
    right POST/PUT/DELETE calls happened in the right order.
    """

    def __init__(self) -> None:
        self.iam_payload: list[dict[str, Any]] = []
        self.service_info_by_name: dict[str, dict[str, Any]] = {}
        self.vps_details_by_name: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        # If set, the next GET serviceInfos / IAM call raises this; useful
        # to simulate transient API failures or race-detection paths.
        self.uncancel_propagation_steps: int = 0

    def __call__(self, method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        self.calls.append((method, path, body))
        if method == "GET" and path == "/v2/iam/resource?resourceType=vps":
            return list(self.iam_payload)
        if method == "GET" and path.endswith("/serviceInfos"):
            name = path.split("/")[2]
            info = dict(self.service_info_by_name[name])
            return info
        if method == "GET" and path.startswith("/vps/") and path.count("/") == 2:
            name = path.split("/")[2]
            return dict(self.vps_details_by_name[name])
        if method == "POST" and "/tag" in path:
            urn = path.split("/v2/iam/resource/")[1].rsplit("/tag", 1)[0]
            assert body is not None
            for entry in self.iam_payload:
                if entry["urn"] == urn:
                    entry["tags"][body["key"]] = body["value"]
                    break
            return None
        if method == "DELETE" and "/tag/" in path:
            urn, key = path.split("/v2/iam/resource/")[1].split("/tag/", 1)
            for entry in self.iam_payload:
                if entry["urn"] == urn:
                    entry["tags"].pop(key, None)
                    break
            return None
        if method == "PUT" and path.endswith("/serviceInfos"):
            name = path.split("/")[2]
            assert body is not None
            self.service_info_by_name[name] = dict(body)
            # Optionally delay propagation visibility for `uncancel_propagation_steps` reads
            return None
        raise AssertionError(f"Unscripted call: {method} {path}")


def _make_fake_client_with_one_candidate(
    *,
    plan: str = "vps-2025-model1",
    zone: str = "Region OpenStack: os-us-east-va-vps-1",
    delete_at_expiration: bool = True,
    state: str = "running",
    days_to_expiration: int = 25,
) -> tuple[OvhVpsClient, _FakeOvh]:
    fake = _FakeOvh()
    fake.iam_payload = [_iam_payload("vps-x.vps.ovh.us")]
    fake.service_info_by_name["vps-x.vps.ovh.us"] = _service_info(
        delete_at_expiration=delete_at_expiration,
        expiration_days_from_now=days_to_expiration,
    )
    fake.vps_details_by_name["vps-x.vps.ovh.us"] = _vps_details(state=state, plan_code=plan, zone=zone)
    return _client(fake), fake


def test_returns_none_when_unconfigured() -> None:
    fake = _FakeOvh()
    fake.iam_payload = []
    client = _client(fake, is_unconfigured=True)
    assert (
        try_recycle_cancelled_vps(
            client=client,
            provider_name="alice-ovh",
            new_host_id=HostId.generate(),
            requested_plan="vps-2025-model1",
            requested_region="US-EAST-VA",
            safety_margin_hours=24,
            max_candidates=10,
        )
        is None
    )
    assert fake.calls == []


def test_returns_none_when_no_candidates() -> None:
    fake = _FakeOvh()
    fake.iam_payload = []
    client = _client(fake)
    assert (
        try_recycle_cancelled_vps(
            client=client,
            provider_name="alice-ovh",
            new_host_id=HostId.generate(),
            requested_plan="vps-2025-model1",
            requested_region="US-EAST-VA",
            safety_margin_hours=24,
            max_candidates=10,
        )
        is None
    )


def test_recycles_eligible_candidate() -> None:
    client, fake = _make_fake_client_with_one_candidate()
    new_host_id = HostId.generate()
    result = try_recycle_cancelled_vps(
        client=client,
        provider_name="alice-ovh",
        new_host_id=new_host_id,
        requested_plan="vps-2025-model1",
        requested_region="US-EAST-VA",
        safety_margin_hours=24,
        max_candidates=10,
    )
    assert result == "vps-x.vps.ovh.us"
    # Final state should reflect the un-cancel and tag swap.
    info = fake.service_info_by_name["vps-x.vps.ovh.us"]
    assert info["renew"]["deleteAtExpiration"] is False
    tags = fake.iam_payload[0]["tags"]
    assert tags[MNGR_HOST_ID_TAG_KEY] == str(new_host_id)
    assert MNGR_RECYCLING_LOCK_TAG_KEY not in tags


def test_skips_non_cancelled_candidate() -> None:
    client, fake = _make_fake_client_with_one_candidate(delete_at_expiration=False)
    result = try_recycle_cancelled_vps(
        client=client,
        provider_name="alice-ovh",
        new_host_id=HostId.generate(),
        requested_plan="vps-2025-model1",
        requested_region="US-EAST-VA",
        safety_margin_hours=24,
        max_candidates=10,
    )
    assert result is None
    # No PUT to serviceInfos should have happened.
    assert not any(method == "PUT" for method, _, _ in fake.calls)


def test_skips_candidate_inside_safety_margin() -> None:
    client, fake = _make_fake_client_with_one_candidate(days_to_expiration=0)
    result = try_recycle_cancelled_vps(
        client=client,
        provider_name="alice-ovh",
        new_host_id=HostId.generate(),
        requested_plan="vps-2025-model1",
        requested_region="US-EAST-VA",
        safety_margin_hours=24,
        max_candidates=10,
    )
    assert result is None


def test_skips_candidate_with_plan_mismatch() -> None:
    client, fake = _make_fake_client_with_one_candidate(plan="vps-2025-model4")
    result = try_recycle_cancelled_vps(
        client=client,
        provider_name="alice-ovh",
        new_host_id=HostId.generate(),
        requested_plan="vps-2025-model1",
        requested_region="US-EAST-VA",
        safety_margin_hours=24,
        max_candidates=10,
    )
    assert result is None


def test_skips_candidate_with_region_mismatch() -> None:
    client, fake = _make_fake_client_with_one_candidate(zone="Region OpenStack: os-eu-west-vps-1")
    result = try_recycle_cancelled_vps(
        client=client,
        provider_name="alice-ovh",
        new_host_id=HostId.generate(),
        requested_plan="vps-2025-model1",
        requested_region="US-EAST-VA",
        safety_margin_hours=24,
        max_candidates=10,
    )
    assert result is None


def test_skips_candidate_in_installing_state() -> None:
    client, fake = _make_fake_client_with_one_candidate(state="installing")
    result = try_recycle_cancelled_vps(
        client=client,
        provider_name="alice-ovh",
        new_host_id=HostId.generate(),
        requested_plan="vps-2025-model1",
        requested_region="US-EAST-VA",
        safety_margin_hours=24,
        max_candidates=10,
    )
    assert result is None


def test_skips_candidate_already_locked_by_someone_else() -> None:
    fake = _FakeOvh()
    fake.iam_payload = [_iam_payload("vps-x.vps.ovh.us", lock_holder="other-process-uuid")]
    fake.service_info_by_name["vps-x.vps.ovh.us"] = _service_info()
    fake.vps_details_by_name["vps-x.vps.ovh.us"] = _vps_details()
    client = _client(fake)
    result = try_recycle_cancelled_vps(
        client=client,
        provider_name="alice-ovh",
        new_host_id=HostId.generate(),
        requested_plan="vps-2025-model1",
        requested_region="US-EAST-VA",
        safety_margin_hours=24,
        max_candidates=10,
    )
    assert result is None


def test_picks_candidate_with_latest_expiration() -> None:
    fake = _FakeOvh()
    fake.iam_payload = [
        _iam_payload("vps-near.vps.ovh.us"),
        _iam_payload("vps-far.vps.ovh.us"),
    ]
    fake.service_info_by_name["vps-near.vps.ovh.us"] = _service_info(expiration_days_from_now=2)
    fake.service_info_by_name["vps-far.vps.ovh.us"] = _service_info(expiration_days_from_now=27)
    fake.vps_details_by_name["vps-near.vps.ovh.us"] = _vps_details()
    fake.vps_details_by_name["vps-far.vps.ovh.us"] = _vps_details()
    client = _client(fake)
    result = try_recycle_cancelled_vps(
        client=client,
        provider_name="alice-ovh",
        new_host_id=HostId.generate(),
        requested_plan="vps-2025-model1",
        requested_region="US-EAST-VA",
        safety_margin_hours=24,
        max_candidates=10,
    )
    assert result == "vps-far.vps.ovh.us"


def test_caps_candidates_considered() -> None:
    fake = _FakeOvh()
    fake.iam_payload = [_iam_payload(f"vps-{i}.vps.ovh.us") for i in range(20)]
    for entry in fake.iam_payload:
        fake.service_info_by_name[entry["name"]] = _service_info()
        fake.vps_details_by_name[entry["name"]] = _vps_details()
    client = _client(fake)
    result = try_recycle_cancelled_vps(
        client=client,
        provider_name="alice-ovh",
        new_host_id=HostId.generate(),
        requested_plan="vps-2025-model1",
        requested_region="US-EAST-VA",
        safety_margin_hours=24,
        max_candidates=3,
    )
    # Recycle should still succeed, but only the first 3 VPSes are evaluated.
    assert result is not None
    unique_eval_targets = {
        path for method, path, _ in fake.calls if method == "GET" and path.endswith("/serviceInfos")
    }
    assert len(unique_eval_targets) <= 3


def test_skips_candidate_with_active_engagement() -> None:
    fake = _FakeOvh()
    fake.iam_payload = [_iam_payload("vps-x.vps.ovh.us")]
    fake.service_info_by_name["vps-x.vps.ovh.us"] = _service_info(engaged_up_to="2027-01-01")
    fake.vps_details_by_name["vps-x.vps.ovh.us"] = _vps_details()
    client = _client(fake)
    result = try_recycle_cancelled_vps(
        client=client,
        provider_name="alice-ovh",
        new_host_id=HostId.generate(),
        requested_plan="vps-2025-model1",
        requested_region="US-EAST-VA",
        safety_margin_hours=24,
        max_candidates=10,
    )
    assert result is None


def test_uncancel_uses_read_modify_write() -> None:
    """The PUT must preserve every non-`renew` field, not just clobber to a partial body."""
    client, fake = _make_fake_client_with_one_candidate()
    new_host_id = HostId.generate()
    try_recycle_cancelled_vps(
        client=client,
        provider_name="alice-ovh",
        new_host_id=new_host_id,
        requested_plan="vps-2025-model1",
        requested_region="US-EAST-VA",
        safety_margin_hours=24,
        max_candidates=10,
    )
    # The PUT body should still carry every contact / renewalType field.
    put_call = next(c for c in fake.calls if c[0] == "PUT" and c[1].endswith("/serviceInfos"))
    body = put_call[2]
    assert body is not None
    for key in ("contactAdmin", "contactBilling", "contactTech", "renewalType", "expiration"):
        assert key in body, f"PUT serviceInfos body is missing {key} -- the read-modify-write is broken"


@pytest.mark.parametrize(
    "safety_hours, days, should_recycle",
    [
        (24, 25, True),
        (24, 0, False),
        (0, 2, True),
        (48 * 30, 25, False),
    ],
)
def test_safety_margin_thresholds(safety_hours: int, days: int, should_recycle: bool) -> None:
    client, _ = _make_fake_client_with_one_candidate(days_to_expiration=days)
    result = try_recycle_cancelled_vps(
        client=client,
        provider_name="alice-ovh",
        new_host_id=HostId.generate(),
        requested_plan="vps-2025-model1",
        requested_region="US-EAST-VA",
        safety_margin_hours=safety_hours,
        max_candidates=10,
    )
    if should_recycle:
        assert result is not None
    else:
        assert result is None
