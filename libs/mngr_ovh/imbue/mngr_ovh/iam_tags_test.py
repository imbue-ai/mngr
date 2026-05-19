"""Tests for the IAM v2 tag wrappers."""

from typing import Any
from unittest.mock import MagicMock

import ovh

from imbue.mngr_ovh.client import OvhVpsClient
from imbue.mngr_ovh.iam_tags import MNGR_HOST_ID_TAG_KEY
from imbue.mngr_ovh.iam_tags import MNGR_PROVIDER_TAG_KEY
from imbue.mngr_ovh.iam_tags import attach_tag
from imbue.mngr_ovh.iam_tags import attach_tags
from imbue.mngr_ovh.iam_tags import delete_tag
from imbue.mngr_ovh.iam_tags import list_vps_resources
from imbue.mngr_ovh.iam_tags import list_vps_resources_for_provider
from imbue.mngr_ovh.iam_tags import vps_urn_for


def _client(call_side_effect: Any) -> OvhVpsClient:
    m = MagicMock(spec=ovh.Client)
    m.call = MagicMock(side_effect=call_side_effect)
    return OvhVpsClient(ovh_client=m, subsidiary="US", task_poll_interval=0.0)


def test_vps_urn_for_us_account() -> None:
    assert vps_urn_for("vps-eec8860b.vps.ovh.us") == "urn:v1:us:resource:vps:vps-eec8860b.vps.ovh.us"


def test_vps_urn_for_eu_account() -> None:
    assert vps_urn_for("vps-foo.vps.ovh.fr", region_code="eu") == "urn:v1:eu:resource:vps:vps-foo.vps.ovh.fr"


def test_attach_tag_issues_post() -> None:
    captured: list[tuple[str, str, dict[str, Any]]] = []

    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        captured.append((method, path, body or {}))
        return None

    client = _client(fake)
    attach_tag(client, "urn:v1:us:resource:vps:vps-x", MNGR_HOST_ID_TAG_KEY, "abc")
    assert captured == [
        (
            "POST",
            "/v2/iam/resource/urn:v1:us:resource:vps:vps-x/tag",
            {
                "key": MNGR_HOST_ID_TAG_KEY,
                "value": "abc",
            },
        )
    ]


def test_attach_tags_issues_one_post_per_pair() -> None:
    captured: list[str] = []

    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        captured.append(method)
        return None

    client = _client(fake)
    attach_tags(client, "urn:v1:us:resource:vps:vps-x", {"a": "1", "b": "2"})
    assert captured == ["POST", "POST"]


def test_delete_tag_issues_delete() -> None:
    captured: list[tuple[str, str]] = []

    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        captured.append((method, path))
        return None

    client = _client(fake)
    delete_tag(client, "urn:v1:us:resource:vps:vps-x", MNGR_HOST_ID_TAG_KEY)
    assert captured == [("DELETE", f"/v2/iam/resource/urn:v1:us:resource:vps:vps-x/tag/{MNGR_HOST_ID_TAG_KEY}")]


def test_list_vps_resources_parses_payload() -> None:
    payload = [
        {
            "urn": "urn:v1:us:resource:vps:vps-a.vps.ovh.us",
            "name": "vps-a.vps.ovh.us",
            "displayName": "vps-a.vps.ovh.us",
            "type": "vps",
            "tags": {"mngr-provider": "alice-ovh", "mngr-host-id": "host-1"},
        },
    ]

    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        assert path == "/v2/iam/resource?resourceType=vps"
        return payload

    client = _client(fake)
    resources = list_vps_resources(client)
    assert len(resources) == 1
    assert resources[0].name == "vps-a.vps.ovh.us"
    assert resources[0].tags[MNGR_PROVIDER_TAG_KEY] == "alice-ovh"
    assert resources[0].tags[MNGR_HOST_ID_TAG_KEY] == "host-1"


def test_list_vps_resources_for_provider_filters_by_provider_tag() -> None:
    payload = [
        {
            "urn": "urn:v1:us:resource:vps:a",
            "name": "a",
            "type": "vps",
            "tags": {"mngr-provider": "alice-ovh", "mngr-host-id": "h1"},
        },
        {
            "urn": "urn:v1:us:resource:vps:b",
            "name": "b",
            "type": "vps",
            "tags": {"mngr-provider": "bob-ovh", "mngr-host-id": "h2"},
        },
        {
            "urn": "urn:v1:us:resource:vps:c",
            "name": "c",
            "type": "vps",
            "tags": {},
        },
    ]

    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        return payload

    client = _client(fake)
    matching = list_vps_resources_for_provider(client, provider_name="alice-ovh")
    assert [r.name for r in matching] == ["a"]


def test_list_vps_resources_skips_malformed_entries() -> None:
    payload = [
        {"name": "no-urn", "type": "vps"},
        {
            "urn": "urn:v1:us:resource:vps:b",
            "name": "b",
            "type": "vps",
            "tags": {},
        },
    ]

    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        return payload

    client = _client(fake)
    assert [r.name for r in list_vps_resources(client)] == ["b"]
