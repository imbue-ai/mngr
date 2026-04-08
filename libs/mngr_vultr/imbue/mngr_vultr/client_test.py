"""Tests for the Vultr API client."""

import json
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.errors import VpsProvisioningError
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vps_docker.primitives import VpsInstanceStatus
from imbue.mngr_vultr.client import VultrVpsClient


@pytest.fixture()
def client() -> VultrVpsClient:
    return VultrVpsClient(api_key=SecretStr("test-api-key"))


def _mock_response(
    status_code: int = 200,
    json_data: dict[str, Any] | None = None,
    text: str = "",
    content_type: str = "application/json",
) -> MagicMock:
    """Create a mock requests.Response."""
    response = MagicMock()
    response.status_code = status_code
    response.ok = 200 <= status_code < 300
    response.text = text or (json.dumps(json_data) if json_data else "")
    response.headers = {"content-type": content_type}
    if json_data is not None:
        response.json.return_value = json_data
    else:
        response.json.side_effect = ValueError("No JSON")
    return response


class TestVultrVpsClientHeaders:
    def test_headers_contain_bearer_token(self, client: VultrVpsClient) -> None:
        headers = client._headers()
        assert headers["Authorization"] == "Bearer test-api-key"
        assert headers["Content-Type"] == "application/json"


class TestVultrVpsClientRequest:
    def test_request_returns_json(self, client: VultrVpsClient) -> None:
        response = _mock_response(json_data={"data": "test"})
        with patch("requests.request", return_value=response):
            result = client._request("GET", "/test")
            assert result == {"data": "test"}

    def test_request_204_returns_none(self, client: VultrVpsClient) -> None:
        response = _mock_response(status_code=204)
        with patch("requests.request", return_value=response):
            result = client._request("DELETE", "/test")
            assert result is None

    def test_request_error_raises_vps_api_error(self, client: VultrVpsClient) -> None:
        response = _mock_response(
            status_code=404,
            json_data={"error": "Not found"},
        )
        with patch("requests.request", return_value=response):
            with pytest.raises(VpsApiError) as exc_info:
                client._request("GET", "/test")
            assert exc_info.value.status_code == 404

    def test_request_network_error_raises_vps_api_error(self, client: VultrVpsClient) -> None:
        import requests as req

        with patch("requests.request", side_effect=req.ConnectionError("Connection failed")):
            with pytest.raises(VpsApiError) as exc_info:
                client._request("GET", "/test")
            assert exc_info.value.status_code == 0


class TestVultrVpsClientInstances:
    def test_create_instance(self, client: VultrVpsClient) -> None:
        response = _mock_response(
            json_data={"instance": {"id": "inst-abc123", "status": "pending"}}
        )
        with patch("requests.request", return_value=response):
            instance_id = client.create_instance(
                label="test",
                region="ewr",
                plan="vc2-1c-1gb",
                os_id=2136,
                user_data="test data",
                ssh_key_ids=["key1"],
                tags=["tag1"],
            )
            assert instance_id == VpsInstanceId("inst-abc123")

    def test_create_instance_no_response_raises(self, client: VultrVpsClient) -> None:
        response = _mock_response(json_data={})
        with patch("requests.request", return_value=response):
            with pytest.raises(VpsProvisioningError):
                client.create_instance(
                    label="test",
                    region="ewr",
                    plan="vc2-1c-1gb",
                    os_id=2136,
                    user_data="test",
                    ssh_key_ids=[],
                    tags=[],
                )

    def test_get_instance_status_active_running(self, client: VultrVpsClient) -> None:
        response = _mock_response(
            json_data={"instance": {"status": "active", "power_status": "running"}}
        )
        with patch("requests.request", return_value=response):
            status = client.get_instance_status(VpsInstanceId("inst-123"))
            assert status == VpsInstanceStatus.ACTIVE

    def test_get_instance_status_active_stopped(self, client: VultrVpsClient) -> None:
        response = _mock_response(
            json_data={"instance": {"status": "active", "power_status": "stopped"}}
        )
        with patch("requests.request", return_value=response):
            status = client.get_instance_status(VpsInstanceId("inst-123"))
            assert status == VpsInstanceStatus.HALTED

    def test_get_instance_status_pending(self, client: VultrVpsClient) -> None:
        response = _mock_response(
            json_data={"instance": {"status": "pending", "power_status": "off"}}
        )
        with patch("requests.request", return_value=response):
            status = client.get_instance_status(VpsInstanceId("inst-123"))
            assert status == VpsInstanceStatus.PENDING

    def test_get_instance_status_unknown_response(self, client: VultrVpsClient) -> None:
        response = _mock_response(json_data={})
        with patch("requests.request", return_value=response):
            status = client.get_instance_status(VpsInstanceId("inst-123"))
            assert status == VpsInstanceStatus.UNKNOWN

    def test_get_instance_ip(self, client: VultrVpsClient) -> None:
        response = _mock_response(
            json_data={"instance": {"main_ip": "1.2.3.4"}}
        )
        with patch("requests.request", return_value=response):
            ip = client.get_instance_ip(VpsInstanceId("inst-123"))
            assert ip == "1.2.3.4"

    def test_get_instance_ip_not_ready(self, client: VultrVpsClient) -> None:
        response = _mock_response(
            json_data={"instance": {"main_ip": "0.0.0.0"}}
        )
        with patch("requests.request", return_value=response):
            with pytest.raises(VpsProvisioningError):
                client.get_instance_ip(VpsInstanceId("inst-123"))

    def test_list_instances(self, client: VultrVpsClient) -> None:
        response = _mock_response(
            json_data={"instances": [{"id": "i1"}, {"id": "i2"}]}
        )
        with patch("requests.request", return_value=response):
            instances = client.list_instances()
            assert len(instances) == 2


class TestVultrVpsClientSshKeys:
    def test_upload_ssh_key(self, client: VultrVpsClient) -> None:
        response = _mock_response(
            json_data={"ssh_key": {"id": "key-123", "name": "test"}}
        )
        with patch("requests.request", return_value=response):
            key_id = client.upload_ssh_key("test", "ssh-ed25519 AAAA test")
            assert key_id == "key-123"

    def test_upload_ssh_key_failure(self, client: VultrVpsClient) -> None:
        response = _mock_response(json_data={})
        with patch("requests.request", return_value=response):
            with pytest.raises(VpsApiError):
                client.upload_ssh_key("test", "ssh-ed25519 AAAA test")

    def test_list_ssh_keys(self, client: VultrVpsClient) -> None:
        response = _mock_response(
            json_data={"ssh_keys": [{"id": "k1", "name": "key1"}, {"id": "k2", "name": "key2"}]}
        )
        with patch("requests.request", return_value=response):
            keys = client.list_ssh_keys()
            assert len(keys) == 2
            assert keys[0].id == "k1"


class TestVultrVpsClientSnapshots:
    def test_create_snapshot(self, client: VultrVpsClient) -> None:
        response = _mock_response(
            json_data={"snapshot": {"id": "snap-123"}}
        )
        with patch("requests.request", return_value=response):
            snap_id = client.create_snapshot(VpsInstanceId("inst-123"), "test snapshot")
            assert str(snap_id) == "snap-123"

    def test_list_snapshots_empty(self, client: VultrVpsClient) -> None:
        response = _mock_response(json_data={"snapshots": []})
        with patch("requests.request", return_value=response):
            snapshots = client.list_snapshots()
            assert snapshots == []
