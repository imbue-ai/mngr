"""Tests for the Vultr API client.

These tests inject a recording transport into ``VultrVpsClient`` instead of
mocking ``requests``. Each test asserts on the *request the client built*
(method, URL, body) as well as the parsed result, so a regression in URL
construction, base64 encoding, or body shape is actually caught -- not just
regressions in response parsing.
"""

import base64
import json
from typing import Any

import pytest
import requests
from pydantic import SecretStr

from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.errors import VpsProvisioningError
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vps_docker.primitives import VpsInstanceStatus
from imbue.mngr_vultr.client import VultrVpsClient


class _RecordingTransport:
    """A real callable standing in for ``requests.request``.

    Records every outgoing request and returns pre-seeded responses in order
    (or raises a seeded exception). This is a concrete fake, not a
    ``unittest.mock`` object, so it stays connected to the real
    ``requests.Response`` interface.
    """

    def __init__(self, responses: list[requests.Response | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        json: dict[str, Any] | None,
        timeout: float,
    ) -> requests.Response:
        self.calls.append({"method": method, "url": url, "headers": headers, "json": json, "timeout": timeout})
        if not self._responses:
            raise AssertionError(f"No seeded response for {method} {url}")
        next_response = self._responses.pop(0)
        if isinstance(next_response, Exception):
            raise next_response
        return next_response


def _response(
    status_code: int = 200,
    json_body: dict[str, Any] | None = None,
    text: str | None = None,
    content_type: str = "application/json",
) -> requests.Response:
    """Build a real ``requests.Response`` with the given body and status."""
    response = requests.Response()
    response.status_code = status_code
    if text is not None:
        body = text
    elif json_body is not None:
        body = json.dumps(json_body)
    else:
        body = ""
    response._content = body.encode()
    response.headers["content-type"] = content_type
    return response


def _client_with(responses: list[requests.Response | Exception]) -> tuple[VultrVpsClient, _RecordingTransport]:
    transport = _RecordingTransport(responses)
    client = VultrVpsClient(api_key=SecretStr("test-api-key"), os_id=2136, request_func=transport)
    return client, transport


# ---------------------------------------------------------------------------
# _request / transport behavior
# ---------------------------------------------------------------------------


def test_request_returns_parsed_json_body() -> None:
    client, _ = _client_with([_response(json_body={"data": "test"})])
    assert client._request("GET", "/test") == {"data": "test"}


def test_request_targets_full_url_and_sends_bearer_auth_header() -> None:
    client, transport = _client_with([_response(json_body={})])
    client._request("GET", "/test")
    call = transport.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "https://api.vultr.com/v2/test"
    assert call["headers"]["Authorization"] == "Bearer test-api-key"
    assert call["headers"]["Content-Type"] == "application/json"


def test_request_204_returns_none_without_parsing_body() -> None:
    client, _ = _client_with([_response(status_code=204)])
    assert client._request("DELETE", "/test") is None


def test_request_http_error_raises_vps_api_error_with_status_and_api_message() -> None:
    client, _ = _client_with([_response(status_code=404, json_body={"error": "Not found"})])
    with pytest.raises(VpsApiError) as exc_info:
        client._request("GET", "/test")
    assert exc_info.value.status_code == 404
    assert "Not found" in str(exc_info.value)


def test_request_non_json_error_body_falls_back_to_response_text() -> None:
    # Exercises the previously-untested branch where the error body is not JSON:
    # the client must surface the raw response text rather than crash decoding it.
    client, _ = _client_with([_response(status_code=500, text="upstream exploded", content_type="text/plain")])
    with pytest.raises(VpsApiError) as exc_info:
        client._request("GET", "/test")
    assert exc_info.value.status_code == 500
    assert "upstream exploded" in str(exc_info.value)


def test_request_network_error_raises_vps_api_error_with_status_zero() -> None:
    client, _ = _client_with([requests.ConnectionError("Connection failed")])
    with pytest.raises(VpsApiError) as exc_info:
        client._request("GET", "/test")
    assert exc_info.value.status_code == 0


# ---------------------------------------------------------------------------
# Instance operations
# ---------------------------------------------------------------------------


def test_create_instance_base64_encodes_user_data_and_posts_full_body() -> None:
    client, transport = _client_with([_response(json_body={"instance": {"id": "inst-abc123", "status": "pending"}})])
    instance_id = client.create_instance(
        label="agent-label",
        region="ewr",
        plan="vc2-1c-1gb",
        user_data="cloud-config-payload",
        ssh_key_ids=["key1", "key2"],
        tags={"mngr-provider": "vultr"},
    )
    assert instance_id == VpsInstanceId("inst-abc123")

    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.vultr.com/v2/instances"
    body = call["json"]
    assert body is not None
    # The most bug-prone part: user_data must be base64-encoded, not sent raw.
    assert base64.b64decode(body["user_data"]).decode() == "cloud-config-payload"
    assert body["region"] == "ewr"
    assert body["plan"] == "vc2-1c-1gb"
    assert body["os_id"] == 2136
    assert body["label"] == "agent-label"
    assert body["hostname"] == "agent-label"
    assert body["sshkey_id"] == ["key1", "key2"]
    assert body["tags"] == ["mngr-provider=vultr"]
    assert body["backups"] == "disabled"


def test_create_instance_missing_instance_in_response_raises() -> None:
    client, _ = _client_with([_response(json_body={})])
    with pytest.raises(VpsProvisioningError):
        client.create_instance(
            label="test",
            region="ewr",
            plan="vc2-1c-1gb",
            user_data="test",
            ssh_key_ids=[],
            tags={},
        )


@pytest.mark.parametrize(
    ("status_str", "power_status", "expected"),
    [
        ("active", "running", VpsInstanceStatus.ACTIVE),
        ("active", "stopped", VpsInstanceStatus.HALTED),
        ("pending", "off", VpsInstanceStatus.PENDING),
        ("halted", "stopped", VpsInstanceStatus.HALTED),
        ("suspended", "stopped", VpsInstanceStatus.HALTED),
    ],
)
def test_get_instance_status_maps_vultr_status_to_enum(
    status_str: str, power_status: str, expected: VpsInstanceStatus
) -> None:
    client, _ = _client_with([_response(json_body={"instance": {"status": status_str, "power_status": power_status}})])
    assert client.get_instance_status(VpsInstanceId("inst-123")) == expected


def test_get_instance_status_unknown_when_instance_absent_from_response() -> None:
    client, _ = _client_with([_response(json_body={})])
    assert client.get_instance_status(VpsInstanceId("inst-123")) == VpsInstanceStatus.UNKNOWN


def test_get_instance_ip_returns_main_ip_from_correct_endpoint() -> None:
    client, transport = _client_with([_response(json_body={"instance": {"main_ip": "1.2.3.4"}})])
    assert client.get_instance_ip(VpsInstanceId("inst-123")) == "1.2.3.4"
    assert transport.calls[0]["url"] == "https://api.vultr.com/v2/instances/inst-123"


def test_get_instance_ip_raises_when_ip_is_placeholder() -> None:
    client, _ = _client_with([_response(json_body={"instance": {"main_ip": "0.0.0.0"}})])
    with pytest.raises(VpsProvisioningError):
        client.get_instance_ip(VpsInstanceId("inst-123"))


def test_list_instances_returns_raw_instance_dicts() -> None:
    client, transport = _client_with([_response(json_body={"instances": [{"id": "i1"}, {"id": "i2"}]})])
    instances = client.list_instances()
    assert [i["id"] for i in instances] == ["i1", "i2"]
    assert transport.calls[0]["url"] == "https://api.vultr.com/v2/instances"


def test_list_instances_with_tag_appends_tag_query_param() -> None:
    client, transport = _client_with([_response(json_body={"instances": []})])
    client.list_instances(tag="mngr-provider=vultr")
    assert transport.calls[0]["url"] == "https://api.vultr.com/v2/instances?tag=mngr-provider=vultr"


# ---------------------------------------------------------------------------
# SSH key operations
# ---------------------------------------------------------------------------


def test_upload_ssh_key_posts_name_and_key_and_returns_id() -> None:
    client, transport = _client_with([_response(json_body={"ssh_key": {"id": "key-123", "name": "test"}})])
    key_id = client.upload_ssh_key("test", "ssh-ed25519 AAAA test")
    assert key_id == "key-123"
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.vultr.com/v2/ssh-keys"
    assert call["json"] == {"name": "test", "ssh_key": "ssh-ed25519 AAAA test"}


def test_upload_ssh_key_missing_key_in_response_raises() -> None:
    client, _ = _client_with([_response(json_body={})])
    with pytest.raises(VpsApiError):
        client.upload_ssh_key("test", "ssh-ed25519 AAAA test")


def test_list_ssh_keys_parses_id_and_name() -> None:
    client, _ = _client_with(
        [_response(json_body={"ssh_keys": [{"id": "k1", "name": "key1"}, {"id": "k2", "name": "key2"}]})]
    )
    keys = client.list_ssh_keys()
    assert [(k.id, k.name) for k in keys] == [("k1", "key1"), ("k2", "key2")]


# ---------------------------------------------------------------------------
# Snapshot operations
# ---------------------------------------------------------------------------


def test_create_snapshot_posts_instance_id_and_returns_snapshot_id() -> None:
    client, transport = _client_with([_response(json_body={"snapshot": {"id": "snap-123"}})])
    snapshot_id = client.create_snapshot(VpsInstanceId("inst-123"), "test snapshot")
    assert str(snapshot_id) == "snap-123"
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.vultr.com/v2/snapshots"
    assert call["json"] == {"instance_id": "inst-123", "description": "test snapshot"}


def test_list_snapshots_empty_returns_empty_list() -> None:
    client, _ = _client_with([_response(json_body={"snapshots": []})])
    assert client.list_snapshots() == []


def test_list_snapshots_parses_id_description_and_iso_date() -> None:
    client, _ = _client_with(
        [
            _response(
                json_body={
                    "snapshots": [
                        {"id": "snap-1", "description": "nightly", "date_created": "2026-06-01T12:30:00+00:00"}
                    ]
                }
            )
        ]
    )
    snapshots = client.list_snapshots()
    assert len(snapshots) == 1
    assert str(snapshots[0].id) == "snap-1"
    assert snapshots[0].description == "nightly"
    assert snapshots[0].created_at.year == 2026
    assert snapshots[0].created_at.month == 6


def test_list_snapshots_falls_back_to_now_for_malformed_date() -> None:
    # A malformed date_created must not crash list_snapshots; it falls back to
    # a timezone-aware "now" so the snapshot is still surfaced.
    client, _ = _client_with([_response(json_body={"snapshots": [{"id": "snap-1", "date_created": "not-a-date"}]})])
    snapshots = client.list_snapshots()
    assert len(snapshots) == 1
    assert snapshots[0].created_at.tzinfo is not None
