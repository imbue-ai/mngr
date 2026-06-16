"""Tests for the IAM v2 tag wrappers."""

from typing import Any

import pytest

from imbue.mngr.errors import MngrError
from imbue.mngr_ovh.iam_tags import MNGR_HOST_ID_TAG_KEY
from imbue.mngr_ovh.iam_tags import MNGR_PROVIDER_TAG_KEY
from imbue.mngr_ovh.iam_tags import attach_tag
from imbue.mngr_ovh.iam_tags import attach_tags
from imbue.mngr_ovh.iam_tags import delete_tag
from imbue.mngr_ovh.iam_tags import list_vps_resources
from imbue.mngr_ovh.iam_tags import list_vps_resources_for_provider
from imbue.mngr_ovh.iam_tags import parse_extra_tags_env
from imbue.mngr_ovh.iam_tags import vps_urn_for
from imbue.mngr_ovh.mock_ovh_client_test import make_fake_ovh_vps_client


def test_vps_urn_for_us_account() -> None:
    assert vps_urn_for("vps-eec8860b.vps.ovh.us") == "urn:v1:us:resource:vps:vps-eec8860b.vps.ovh.us"


def test_vps_urn_for_eu_account() -> None:
    assert vps_urn_for("vps-foo.vps.ovh.fr", region_code="eu") == "urn:v1:eu:resource:vps:vps-foo.vps.ovh.fr"


def test_attach_tag_issues_post() -> None:
    captured: list[tuple[str, str, dict[str, Any]]] = []

    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        captured.append((method, path, body or {}))
        return None

    client = make_fake_ovh_vps_client(fake)
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
    captured: list[tuple[str, str, Any]] = []

    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        captured.append((method, path, body))
        return None

    client = make_fake_ovh_vps_client(fake)
    attach_tags(client, "urn:v1:us:resource:vps:vps-x", {"a": "1", "b": "2"})
    # Each pair must POST to the resource's tag endpoint with its own
    # ``{key, value}`` body -- not just "two POSTs happened". A bug that
    # swapped key/value, posted to the wrong urn, or dropped a pair would
    # still produce two POSTs but fail this comparison.
    by_key = sorted(captured, key=lambda c: c[2]["key"])
    assert by_key == [
        ("POST", "/v2/iam/resource/urn:v1:us:resource:vps:vps-x/tag", {"key": "a", "value": "1"}),
        ("POST", "/v2/iam/resource/urn:v1:us:resource:vps:vps-x/tag", {"key": "b", "value": "2"}),
    ]


def test_delete_tag_issues_delete() -> None:
    captured: list[tuple[str, str]] = []

    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        captured.append((method, path))
        return None

    client = make_fake_ovh_vps_client(fake)
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

    client = make_fake_ovh_vps_client(fake)
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

    client = make_fake_ovh_vps_client(fake)
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

    client = make_fake_ovh_vps_client(fake)
    assert [r.name for r in list_vps_resources(client)] == ["b"]


def test_parse_extra_tags_env_empty_returns_empty_dict() -> None:
    assert parse_extra_tags_env("") == {}
    assert parse_extra_tags_env("   ") == {}
    assert parse_extra_tags_env(",,,") == {}


def test_parse_extra_tags_env_single_entry() -> None:
    assert parse_extra_tags_env("minds_env=alice") == {"minds_env": "alice"}


def test_parse_extra_tags_env_multiple_entries() -> None:
    assert parse_extra_tags_env("minds_env=alice,pool-owner=bob") == {
        "minds_env": "alice",
        "pool-owner": "bob",
    }


def test_parse_extra_tags_env_strips_whitespace() -> None:
    assert parse_extra_tags_env(" minds_env = alice , pool-owner = bob ") == {
        "minds_env": "alice",
        "pool-owner": "bob",
    }


def test_parse_extra_tags_env_rejects_entry_without_equals() -> None:
    with pytest.raises(MngrError, match="missing '='"):
        parse_extra_tags_env("minds_env=alice,nosgn")


def test_parse_extra_tags_env_rejects_uppercase_key() -> None:
    with pytest.raises(MngrError, match="MindsEnv"):
        parse_extra_tags_env("MindsEnv=alice")


def test_parse_extra_tags_env_rejects_key_starting_with_digit() -> None:
    with pytest.raises(MngrError, match="1bad"):
        parse_extra_tags_env("1bad=value")


def test_parse_extra_tags_env_rejects_key_with_illegal_symbol() -> None:
    with pytest.raises(MngrError, match=r"bad\.key"):
        parse_extra_tags_env("bad.key=value")


def test_parse_extra_tags_env_accepts_empty_value() -> None:
    # Empty value is fine; OVH IAM tag values are unconstrained (the
    # ``minds_env=`` shape isn't actually useful, but we don't want to
    # reject it here either).
    assert parse_extra_tags_env("minds_env=") == {"minds_env": ""}


def test_parse_extra_tags_env_rejects_reserved_provider_key() -> None:
    """The ``mngr-provider`` tag backs discovery -- overwriting it via env breaks list/find."""
    with pytest.raises(MngrError, match="reserved by mngr internals"):
        parse_extra_tags_env("mngr-provider=hijack")


def test_parse_extra_tags_env_rejects_reserved_host_id_key() -> None:
    """``mngr-host-id`` ties the OVH VPS back to the mngr host record."""
    with pytest.raises(MngrError, match="reserved by mngr internals"):
        parse_extra_tags_env("mngr-host-id=spoofed")


def test_parse_extra_tags_env_rejects_reserved_recycle_lock_key() -> None:
    """``mngr-recycling-by`` is the cooperative recycle lock; do not let callers spoof it."""
    with pytest.raises(MngrError, match="reserved by mngr internals"):
        parse_extra_tags_env("mngr-recycling-by=other-process")
