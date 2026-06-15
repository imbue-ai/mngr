"""Tests for the OVH VPS client."""

from typing import Any

import pytest
from ovh.exceptions import APIError
from ovh.exceptions import BadParametersError
from ovh.exceptions import HTTPError
from ovh.exceptions import ResourceNotFoundError

from imbue.mngr.errors import MngrError
from imbue.mngr_ovh.client import RecycleHandle
from imbue.mngr_ovh.mock_ovh_client_test import make_fake_ovh_vps_client
from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.errors import VpsProvisioningError
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vps_docker.primitives import VpsInstanceStatus
from imbue.mngr_vps_docker.primitives import VpsSnapshotId


def test_api_error_becomes_vps_api_error() -> None:
    client = make_fake_ovh_vps_client(APIError("nope"))
    with pytest.raises(VpsApiError):
        client.list_instances()


def test_destroy_instance_flips_delete_at_expiration() -> None:
    """`destroy_instance` must set `renew.deleteAtExpiration=true` directly.

    The legacy implementation called ``POST /terminate`` which only
    emails a confirmation token; without acting on the email the VPS
    would auto-renew indefinitely. The corrected implementation goes
    straight to the ``PUT /serviceInfos`` flow so the VPS actually
    stops billing at end of month.
    """
    captured: list[tuple[str, str, Any]] = []

    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        captured.append((method, path, body))
        if method == "GET" and path.endswith("/serviceInfos"):
            return {
                "renew": {"deleteAtExpiration": False, "automatic": True, "period": 1},
                "expiration": "2026-06-15",
                "contactAdmin": "infra@imbue.com",
                "renewalType": "automaticV2012",
            }
        return None

    client = make_fake_ovh_vps_client(fake_call)
    client.destroy_instance(VpsInstanceId("vps-abc.vps.ovh.us"))
    methods_paths = [(m, p) for m, p, _ in captured]
    assert methods_paths == [
        ("GET", "/vps/vps-abc.vps.ovh.us/serviceInfos"),
        ("PUT", "/vps/vps-abc.vps.ovh.us/serviceInfos"),
    ]
    # Crucially: no /terminate -- that endpoint is documented as
    # email-confirmed termination and is not what we want.
    for _method, p, _body in captured:
        assert "/terminate" not in p
    put_body = captured[-1][2]
    assert put_body["renew"]["deleteAtExpiration"] is True


def test_destroy_instance_short_circuits_when_mid_recycle() -> None:
    """A pending recycle handle skips cancellation and releases the IAM lock instead.

    The base ``VpsDockerProvider.create_host`` cleanup path calls
    ``vps_client.destroy_instance`` on failure. For a mid-recycle VPS
    (un-cancel not yet applied because we defer it to finalize), the
    VPS is already cancelled, so re-cancelling would be wasted work.
    Instead the client releases the IAM ``mngr-recycling-by`` lock
    tag so a subsequent ``mngr create`` can re-try the recycle.
    """
    captured: list[tuple[str, str]] = []

    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        captured.append((method, path))
        if method == "GET" and path.endswith("/serviceInfos"):
            return {
                "renew": {"deleteAtExpiration": False, "automatic": True, "period": 1},
                "expiration": "2026-06-15",
                "contactAdmin": "infra@imbue.com",
                "renewalType": "automaticV2012",
            }
        return None

    client = make_fake_ovh_vps_client(fake_call)
    handle = RecycleHandle(
        urn="urn:v1:us:resource:vps:vps-abc.vps.ovh.us",
        service_name="vps-abc.vps.ovh.us",
        lock_value="lock-uuid",
    )
    client.register_recycle_handle(handle)
    client.destroy_instance(VpsInstanceId("vps-abc.vps.ovh.us"))
    # Crucially: no /serviceInfos mutation (would be a wasted call
    # against an already-cancelled VPS); only the IAM lock DELETE.
    assert captured == [
        ("DELETE", "/v2/iam/resource/urn:v1:us:resource:vps:vps-abc.vps.ovh.us/tag/mngr-recycling-by"),
    ]
    # Handle is consumed exactly once: a second destroy on the same
    # service_name should fall through to the normal cancellation
    # path (GET + PUT serviceInfos).
    captured.clear()
    client.destroy_instance(VpsInstanceId("vps-abc.vps.ovh.us"))
    assert captured == [
        ("GET", "/vps/vps-abc.vps.ovh.us/serviceInfos"),
        ("PUT", "/vps/vps-abc.vps.ovh.us/serviceInfos"),
    ]


def test_destroy_instance_short_circuit_swallows_404_on_lock_release() -> None:
    """A 404 on the lock-release DELETE is treated as "lock already gone" and not raised.

    Common when the lock has already been finalize_recycle'd or
    abort_recycle'd before destroy_instance runs.
    """

    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        raise ResourceNotFoundError("tag gone")

    client = make_fake_ovh_vps_client(fake_call)
    client.register_recycle_handle(
        RecycleHandle(
            urn="urn:v1:us:resource:vps:vps-x",
            service_name="vps-x",
            lock_value="lock-uuid",
        )
    )
    client.destroy_instance(VpsInstanceId("vps-x"))


def test_get_instance_status_active_when_running() -> None:
    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        return {"state": "running"}

    client = make_fake_ovh_vps_client(fake_call)
    assert client.get_instance_status(VpsInstanceId("vps-x")) == VpsInstanceStatus.ACTIVE


def test_get_instance_status_halted_when_stopped() -> None:
    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        return {"state": "stopped"}

    client = make_fake_ovh_vps_client(fake_call)
    assert client.get_instance_status(VpsInstanceId("vps-x")) == VpsInstanceStatus.HALTED


def test_get_instance_status_unknown_on_api_error() -> None:
    client = make_fake_ovh_vps_client(APIError("boom"))
    assert client.get_instance_status(VpsInstanceId("vps-x")) == VpsInstanceStatus.UNKNOWN


def test_get_instance_ip_returns_dotted_service_name() -> None:
    client = make_fake_ovh_vps_client(lambda *a, **k: None)
    assert client.get_instance_ip(VpsInstanceId("vps-abc.vps.ovh.us")) == "vps-abc.vps.ovh.us"


def test_list_instances_passes_through() -> None:
    client = make_fake_ovh_vps_client(lambda *a, **k: ["vps-a", "vps-b"])
    assert client.list_instances() == ["vps-a", "vps-b"]


def test_create_instance_raises_not_implemented() -> None:
    client = make_fake_ovh_vps_client(lambda *a, **k: None)
    with pytest.raises(NotImplementedError):
        client.create_instance(label="x", region="r", plan="p", user_data="", ssh_key_ids=[], tags={})


def test_wait_for_task_returns_payload_on_done() -> None:
    responses = iter(
        [
            {"id": 1, "state": "doing", "type": "rebuild"},
            {"id": 1, "state": "done", "type": "rebuild"},
        ]
    )

    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        return next(responses)

    client = make_fake_ovh_vps_client(fake_call)
    result = client.wait_for_task("vps-x", 1, timeout_seconds=5.0)
    assert result["state"] == "done"


def test_wait_for_task_raises_on_error_state() -> None:
    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        return {"id": 2, "state": "error", "type": "rebuild"}

    client = make_fake_ovh_vps_client(fake_call)
    with pytest.raises(VpsProvisioningError):
        client.wait_for_task("vps-x", 2, timeout_seconds=5.0)


def test_wait_for_task_raises_on_timeout() -> None:
    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        return {"id": 3, "state": "doing", "type": "rebuild"}

    client = make_fake_ovh_vps_client(fake_call)
    client.task_poll_interval = 0.0
    with pytest.raises(VpsProvisioningError):
        client.wait_for_task("vps-x", 3, timeout_seconds=0.05)


def test_wait_for_no_active_tasks_returns_immediately_when_idle() -> None:
    """Each poll calls both ?state=todo and ?state=doing; both empty -> return."""
    calls: list[str] = []

    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        calls.append(path)
        return []

    client = make_fake_ovh_vps_client(fake_call)
    client.wait_for_no_active_tasks("vps-x", timeout_seconds=5.0)
    assert calls == ["/vps/vps-x/tasks?state=todo", "/vps/vps-x/tasks?state=doing"]


def test_wait_for_no_active_tasks_blocks_then_returns_when_tasks_drain() -> None:
    """Reproduces the Bug 1 sequence: deliverVm in `doing`, then done.

    Each poll round queries ?state=todo and ?state=doing in order.
    First round returns ([], [42]) -> still active. Second returns
    ([], []) -> drained, return.
    """
    todo_iter = iter([[], []])
    doing_iter = iter([[42], []])

    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if "?state=todo" in path:
            return next(todo_iter)
        if "?state=doing" in path:
            return next(doing_iter)
        raise AssertionError(f"Unexpected path {path}")

    client = make_fake_ovh_vps_client(fake_call)
    client.task_poll_interval = 0.0
    client.wait_for_no_active_tasks("vps-x", timeout_seconds=5.0)


def test_wait_for_no_active_tasks_raises_on_timeout() -> None:
    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        return [99] if "?state=doing" in path else []

    client = make_fake_ovh_vps_client(fake_call)
    client.task_poll_interval = 0.0
    with pytest.raises(VpsProvisioningError, match="still has active tasks"):
        client.wait_for_no_active_tasks("vps-x", timeout_seconds=0.05)


def test_wait_for_no_active_tasks_distinguishes_api_outage_from_lingering_tasks() -> None:
    """If every poll errors, the timeout message must surface the API error.

    Previously the function reported "still has active tasks []" whenever
    every poll raised, which is self-contradicting and gave the operator
    no clue that the failure mode was the tasks endpoint itself.
    """

    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        raise VpsApiError(503, "OVH tasks API is unavailable")

    client = make_fake_ovh_vps_client(fake_call)
    client.task_poll_interval = 0.0
    with pytest.raises(VpsProvisioningError, match="tasks listing never succeeded"):
        client.wait_for_no_active_tasks("vps-x", timeout_seconds=0.05)


def test_create_snapshot_raises_when_one_already_exists() -> None:
    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if method == "GET" and path.endswith("/snapshot"):
            return {"id": "existing", "description": "old"}
        raise AssertionError(f"Unexpected call {method} {path}")

    client = make_fake_ovh_vps_client(fake_call)
    with pytest.raises(MngrError, match="already has a snapshot"):
        client.create_snapshot(VpsInstanceId("vps-x"), "new")


def test_delete_snapshot_deletes_owning_vps_slot() -> None:
    captured: list[tuple[str, str]] = []

    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        captured.append((method, path))
        return None

    client = make_fake_ovh_vps_client(fake_call)
    client.delete_snapshot(VpsSnapshotId("vps-eec8860b.vps.ovh.us"))
    assert captured == [("DELETE", "/vps/vps-eec8860b.vps.ovh.us/snapshot")]


def test_create_snapshot_returns_service_name_for_delete_round_trip() -> None:
    """create_snapshot must return the owning serviceName, not the snapshot's internal id.

    delete_snapshot's URL pattern is /vps/{serviceName}/snapshot, so the id
    round-tripped through VpsSnapshotId must always be the serviceName --
    even when the OVH API populates a distinct 'id' field on the snapshot.
    """
    service_name = "vps-abc.vps.ovh.us"
    call_log: list[tuple[str, str]] = []

    def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        call_log.append((method, path))
        if method == "GET" and path == f"/vps/{service_name}/snapshot":
            # First call (existence check): no snapshot yet. Subsequent calls
            # (post-task verification): a snapshot whose internal id differs
            # from the serviceName, which is what would break delete if we
            # returned snap['id'] instead of the serviceName.
            get_calls = [c for c in call_log if c == ("GET", f"/vps/{service_name}/snapshot")]
            if len(get_calls) == 1:
                return None
            return {"id": "internal-snapshot-uuid", "description": "x"}
        if method == "POST" and path.endswith("/createSnapshot"):
            return {"id": 42}
        if method == "GET" and "/tasks/" in path:
            return {"id": 42, "state": "done", "type": "snapshotCreate"}
        raise AssertionError(f"Unexpected call: {method} {path}")

    client = make_fake_ovh_vps_client(fake_call)
    client.task_poll_interval = 0.0
    snapshot_id = client.create_snapshot(VpsInstanceId(service_name), "desc")
    assert str(snapshot_id) == service_name


def test_get_service_info_returns_payload() -> None:
    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        assert method == "GET" and path == "/vps/vps-x/serviceInfos"
        return {"renew": {"deleteAtExpiration": True}, "expiration": "2026-06-15"}

    client = make_fake_ovh_vps_client(fake)
    info = client.get_service_info("vps-x")
    assert info["renew"]["deleteAtExpiration"] is True


def test_set_renew_at_expiration_false_restores_auto_renewal_fields() -> None:
    """Un-cancelling must restore the fields OVH auto-flips on cancel.

    Verified live: setting ``renew.deleteAtExpiration=true`` causes
    OVH to also flip ``renew.automatic`` to ``false`` and
    ``renewalType`` to ``"manual"``. Un-cancelling without explicitly
    restoring those would leave the VPS in a state where it does not
    auto-renew at the next anniversary even though our flag flip
    succeeded -- silently breaking the recycle path.
    """
    seen: dict[str, Any] = {}

    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if method == "GET" and path.endswith("/serviceInfos"):
            # Mirror the post-cancellation state OVH leaves us in.
            return {
                "renew": {"deleteAtExpiration": True, "automatic": False, "period": 1},
                "expiration": "2026-06-15",
                "contactAdmin": "infra@imbue.com",
                "renewalType": "manual",
            }
        if method == "PUT" and path.endswith("/serviceInfos"):
            seen["body"] = body
            return None
        raise AssertionError(f"Unexpected {method} {path}")

    client = make_fake_ovh_vps_client(fake)
    client.set_renew_at_expiration("vps-x", delete_at_expiration=False)
    body = seen["body"]
    assert body["renew"]["deleteAtExpiration"] is False
    # Un-cancel must restore auto-renewal so the VPS actually renews
    # at the next anniversary; OVH does not do this automatically.
    assert body["renew"]["automatic"] is True
    assert body["renewalType"] == "automaticV2012"
    # Unrelated fields preserved (read-modify-write contract).
    assert body["contactAdmin"] == "infra@imbue.com"


def test_set_renew_at_expiration_true_does_not_force_auto_renewal_fields() -> None:
    """Cancelling does not touch ``automatic`` / ``renewalType``.

    OVH flips them itself as a server-side side effect; clobbering
    them client-side would be redundant and could mask a future
    OVH-side behavior change. The fix-up only runs on un-cancel.
    """
    seen: dict[str, Any] = {}

    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if method == "GET" and path.endswith("/serviceInfos"):
            return {
                "renew": {"deleteAtExpiration": False, "automatic": True, "period": 1},
                "expiration": "2026-06-15",
                "contactAdmin": "infra@imbue.com",
                "renewalType": "automaticV2012",
            }
        if method == "PUT" and path.endswith("/serviceInfos"):
            seen["body"] = body
            return None
        raise AssertionError(f"Unexpected {method} {path}")

    client = make_fake_ovh_vps_client(fake)
    client.set_renew_at_expiration("vps-x", delete_at_expiration=True)
    body = seen["body"]
    assert body["renew"]["deleteAtExpiration"] is True
    # automatic / renewalType left as-read; OVH flips them server-side.
    assert body["renew"]["automatic"] is True
    assert body["renewalType"] == "automaticV2012"


def test_f39_set_renew_at_expiration_retries_on_subscription_not_active_yet() -> None:
    """F39: PUT serviceInfos right after a fresh order 400s with 'subscription not active yet'.

    Verified live on 2026-05-18 during the F3 end-to-end probe:
    ``set_renew_at_expiration(name, True)`` called immediately
    after ``order_and_wait_for_vps`` returned failed with this
    exact 400 message; a 30-second retry succeeded.

    The fix retries the PUT (and only the PUT) when OVH responds
    with this specific message. This test pins that behavior: the
    first two PUT attempts return the subscription-not-active 400,
    the third succeeds. The retry interval is set to 0.0 via
    ``set_renew_retry_poll_interval_seconds`` on the test client so
    the test runs in well under a second.
    """
    put_attempts = {"n": 0}

    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if method == "GET" and path.endswith("/serviceInfos"):
            return {
                "renew": {"deleteAtExpiration": False, "automatic": True, "period": 1},
                "expiration": "2026-06-15",
                "renewalType": "automaticV2012",
            }
        if method == "PUT" and path.endswith("/serviceInfos"):
            put_attempts["n"] += 1
            if put_attempts["n"] <= 2:
                raise BadParametersError("Unable to synchronize l1::Service, subscription is not active yet")
            return None
        raise AssertionError(f"Unexpected {method} {path}")

    client = make_fake_ovh_vps_client(fake)
    # No exception expected -- retry recovers.
    client.set_renew_at_expiration("vps-x", delete_at_expiration=True)
    assert put_attempts["n"] == 3, f"expected 3 PUT attempts, got {put_attempts['n']}"


def test_set_renew_at_expiration_retries_on_transient_transport_error() -> None:
    """A dropped connection during the cancel PUT is retried, not surfaced.

    Reproduces the failure-cleanup cancel that lost a freshly-ordered
    VPS to a transient ``ConnectionError`` (the rebuild-race cleanup
    path): ``_call`` tags transport failures with ``status_code == 0``,
    and the PUT retry must treat those as transient rather than letting
    a single dropped connection leak a month of billing.
    """
    put_attempts = {"n": 0}

    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if method == "GET" and path.endswith("/serviceInfos"):
            return {
                "renew": {"deleteAtExpiration": False, "automatic": True},
                "renewalType": "automaticV2012",
            }
        if method == "PUT" and path.endswith("/serviceInfos"):
            put_attempts["n"] += 1
            if put_attempts["n"] <= 2:
                raise HTTPError("Connection aborted: Remote end closed connection without response")
            return None
        raise AssertionError(f"Unexpected {method} {path}")

    client = make_fake_ovh_vps_client(fake)
    # No exception expected -- the transient transport error is retried.
    client.set_renew_at_expiration("vps-x", delete_at_expiration=True)
    assert put_attempts["n"] == 3, f"expected 3 PUT attempts, got {put_attempts['n']}"


def test_f39_set_renew_at_expiration_does_not_retry_on_other_400() -> None:
    """A different 400 propagates immediately -- only the subscription-not-active retry is special.

    Guards against the retry loop swallowing unrelated client
    errors (a bad request body, a stale serviceName, etc.).
    """
    put_attempts = {"n": 0}

    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if method == "GET" and path.endswith("/serviceInfos"):
            return {
                "renew": {"deleteAtExpiration": False, "automatic": True},
                "renewalType": "automaticV2012",
            }
        if method == "PUT" and path.endswith("/serviceInfos"):
            put_attempts["n"] += 1
            raise BadParametersError("Invalid renewalType value: 'banana'")
        raise AssertionError(f"Unexpected {method} {path}")

    client = make_fake_ovh_vps_client(fake)
    with pytest.raises(VpsApiError, match="Invalid renewalType"):
        client.set_renew_at_expiration("vps-x", delete_at_expiration=True)
    # Exactly one PUT attempt -- no retry on the unrelated error.
    assert put_attempts["n"] == 1, f"expected 1 PUT attempt (no retry), got {put_attempts['n']}"


def test_f39_set_renew_at_expiration_raises_after_retry_budget_exhausted() -> None:
    """If OVH keeps returning subscription-not-active past the budget, we surface a clear error.

    Better than blocking forever in a ``finally`` cleanup. The
    operator sees the message and can clean up manually. The tiny
    retry budget on the test client (50ms) makes this test exit
    quickly.
    """

    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if method == "GET" and path.endswith("/serviceInfos"):
            return {
                "renew": {"deleteAtExpiration": False, "automatic": True},
                "renewalType": "automaticV2012",
            }
        if method == "PUT" and path.endswith("/serviceInfos"):
            raise BadParametersError("Unable to synchronize l1::Service, subscription is not active yet")
        raise AssertionError(f"Unexpected {method} {path}")

    client = make_fake_ovh_vps_client(fake, set_renew_retry_timeout_seconds=0.05)
    with pytest.raises(VpsApiError, match="subscription is not active yet"):
        client.set_renew_at_expiration("vps-x", delete_at_expiration=True)


def test_upload_ssh_key_caches_and_returns_name() -> None:
    client = make_fake_ovh_vps_client(lambda *a, **k: None)
    assert client.upload_ssh_key("mngr-host-1", "ssh-ed25519 AAA") == "mngr-host-1"
    assert client.get_cached_public_key("mngr-host-1") == "ssh-ed25519 AAA"


def test_get_cached_public_key_raises_for_unknown_id() -> None:
    client = make_fake_ovh_vps_client(lambda *a, **k: None)
    with pytest.raises(MngrError):
        client.get_cached_public_key("ghost")


def test_delete_ssh_key_removes_from_cache() -> None:
    client = make_fake_ovh_vps_client(lambda *a, **k: None)
    client.upload_ssh_key("k1", "ssh-rsa K")
    client.delete_ssh_key("k1")
    assert client.list_ssh_keys() == []


def test_list_ssh_keys_reflects_cache() -> None:
    client = make_fake_ovh_vps_client(lambda *a, **k: None)
    client.upload_ssh_key("k1", "ssh-rsa A")
    client.upload_ssh_key("k2", "ssh-rsa B")
    names = {k.name for k in client.list_ssh_keys()}
    assert names == {"k1", "k2"}
