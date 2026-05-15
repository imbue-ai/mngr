"""Tests for the OVH VPS client."""

from typing import Any
from unittest.mock import MagicMock

import ovh
import pytest
from ovh.exceptions import APIError

from imbue.mngr.errors import MngrError
from imbue.mngr_ovh.client import OvhVpsClient
from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.errors import VpsProvisioningError
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vps_docker.primitives import VpsInstanceStatus
from imbue.mngr_vps_docker.primitives import VpsSnapshotId


def _client_with_call(call_side_effect: Any) -> OvhVpsClient:
    mock_client = MagicMock(spec=ovh.Client)
    mock_client.call = MagicMock(side_effect=call_side_effect)
    return OvhVpsClient(ovh_client=mock_client, subsidiary="US", task_poll_interval=0.0)


class TestOvhVpsClientErrorMapping:
    def test_api_error_becomes_vps_api_error(self) -> None:
        client = _client_with_call(APIError("nope"))
        with pytest.raises(VpsApiError):
            client.list_instances()


class TestOvhVpsClientLifecycle:
    def test_destroy_instance_calls_terminate(self) -> None:
        captured: list[tuple[str, str]] = []

        def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
            captured.append((method, path))
            return None

        client = _client_with_call(fake_call)
        client.destroy_instance(VpsInstanceId("vps-abc.vps.ovh.us"))
        assert captured == [("POST", "/vps/vps-abc.vps.ovh.us/terminate")]

    def test_get_instance_status_active_when_running(self) -> None:
        def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
            return {"state": "running"}

        client = _client_with_call(fake_call)
        assert client.get_instance_status(VpsInstanceId("vps-x")) == VpsInstanceStatus.ACTIVE

    def test_get_instance_status_halted_when_stopped(self) -> None:
        def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
            return {"state": "stopped"}

        client = _client_with_call(fake_call)
        assert client.get_instance_status(VpsInstanceId("vps-x")) == VpsInstanceStatus.HALTED

    def test_get_instance_status_unknown_on_api_error(self) -> None:
        client = _client_with_call(APIError("boom"))
        assert client.get_instance_status(VpsInstanceId("vps-x")) == VpsInstanceStatus.UNKNOWN

    def test_get_instance_ip_returns_dotted_service_name(self) -> None:
        client = _client_with_call(lambda *a, **k: None)
        assert client.get_instance_ip(VpsInstanceId("vps-abc.vps.ovh.us")) == "vps-abc.vps.ovh.us"

    def test_list_instances_passes_through(self) -> None:
        client = _client_with_call(lambda *a, **k: ["vps-a", "vps-b"])
        assert client.list_instances() == ["vps-a", "vps-b"]

    def test_create_instance_raises_not_implemented(self) -> None:
        client = _client_with_call(lambda *a, **k: None)
        with pytest.raises(NotImplementedError):
            client.create_instance(label="x", region="r", plan="p", os_id=0, user_data="", ssh_key_ids=[], tags=[])


class TestOvhVpsClientTask:
    def test_wait_for_task_returns_payload_on_done(self) -> None:
        responses = iter(
            [
                {"id": 1, "state": "doing", "type": "rebuild"},
                {"id": 1, "state": "done", "type": "rebuild"},
            ]
        )

        def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
            return next(responses)

        client = _client_with_call(fake_call)
        result = client.wait_for_task("vps-x", 1, timeout_seconds=5.0)
        assert result["state"] == "done"

    def test_wait_for_task_raises_on_error_state(self) -> None:
        def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
            return {"id": 2, "state": "error", "type": "rebuild"}

        client = _client_with_call(fake_call)
        with pytest.raises(VpsProvisioningError):
            client.wait_for_task("vps-x", 2, timeout_seconds=5.0)

    def test_wait_for_task_raises_on_timeout(self) -> None:
        def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
            return {"id": 3, "state": "doing", "type": "rebuild"}

        client = _client_with_call(fake_call)
        client.task_poll_interval = 0.0
        with pytest.raises(VpsProvisioningError):
            client.wait_for_task("vps-x", 3, timeout_seconds=0.05)


class TestOvhVpsClientSnapshots:
    def test_create_snapshot_raises_when_one_already_exists(self) -> None:
        def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
            if method == "GET" and path.endswith("/snapshot"):
                return {"id": "existing", "description": "old"}
            raise AssertionError(f"Unexpected call {method} {path}")

        client = _client_with_call(fake_call)
        with pytest.raises(MngrError, match="already has a snapshot"):
            client.create_snapshot(VpsInstanceId("vps-x"), "new")

    def test_delete_snapshot_deletes_owning_vps_slot(self) -> None:
        captured: list[tuple[str, str]] = []

        def fake_call(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
            captured.append((method, path))
            return None

        client = _client_with_call(fake_call)
        client.delete_snapshot(VpsSnapshotId("vps-eec8860b.vps.ovh.us"))
        assert captured == [("DELETE", "/vps/vps-eec8860b.vps.ovh.us/snapshot")]


class TestOvhVpsClientSshKeyShim:
    def test_upload_ssh_key_caches_and_returns_name(self) -> None:
        client = _client_with_call(lambda *a, **k: None)
        assert client.upload_ssh_key("mngr-host-1", "ssh-ed25519 AAA") == "mngr-host-1"
        assert client.get_cached_public_key("mngr-host-1") == "ssh-ed25519 AAA"

    def test_get_cached_public_key_raises_for_unknown_id(self) -> None:
        client = _client_with_call(lambda *a, **k: None)
        with pytest.raises(MngrError):
            client.get_cached_public_key("ghost")

    def test_delete_ssh_key_removes_from_cache(self) -> None:
        client = _client_with_call(lambda *a, **k: None)
        client.upload_ssh_key("k1", "ssh-rsa K")
        client.delete_ssh_key("k1")
        assert client.list_ssh_keys() == []

    def test_list_ssh_keys_reflects_cache(self) -> None:
        client = _client_with_call(lambda *a, **k: None)
        client.upload_ssh_key("k1", "ssh-rsa A")
        client.upload_ssh_key("k2", "ssh-rsa B")
        names = {k.name for k in client.list_ssh_keys()}
        assert names == {"k1", "k2"}
