"""Tests for OVH image / datacenter resolution."""

from typing import Any
from unittest.mock import MagicMock

import ovh
import pytest

from imbue.mngr.errors import MngrError
from imbue.mngr_ovh.catalog import find_required_field
from imbue.mngr_ovh.catalog import list_available_image_names
from imbue.mngr_ovh.catalog import resolve_image_id
from imbue.mngr_ovh.catalog import validate_datacenter
from imbue.mngr_ovh.client import OvhVpsClient


def _client(call_side_effect: Any) -> OvhVpsClient:
    m = MagicMock(spec=ovh.Client)
    m.call = MagicMock(side_effect=call_side_effect)
    return OvhVpsClient(ovh_client=m, subsidiary="US", task_poll_interval=0.0)


def test_resolve_image_id_returns_uuid_for_matching_name() -> None:
    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if path.endswith("/images/available"):
            return ["uuid-a", "uuid-b"]
        if path.endswith("/images/available/uuid-a"):
            return {"id": "uuid-a", "name": "Ubuntu 24.04"}
        if path.endswith("/images/available/uuid-b"):
            return {"id": "uuid-b", "name": "Debian 12 - Docker"}
        raise AssertionError(f"unexpected {path}")

    client = _client(fake)
    assert resolve_image_id(client, "vps-x", "Debian 12 - Docker") == "uuid-b"


def test_resolve_image_id_raises_when_name_not_found() -> None:
    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if path.endswith("/images/available"):
            return ["uuid-a"]
        return {"id": "uuid-a", "name": "Ubuntu 24.04"}

    client = _client(fake)
    with pytest.raises(MngrError, match="No OVH image named 'Debian 12 - Docker'"):
        resolve_image_id(client, "vps-x", "Debian 12 - Docker")


def test_list_available_image_names() -> None:
    def fake(method: str, path: str, body: Any = None, need_auth: bool = True) -> Any:
        if path.endswith("/images/available"):
            return ["1", "2"]
        if path.endswith("/1"):
            return {"id": "1", "name": "Ubuntu 24.04"}
        if path.endswith("/2"):
            return {"id": "2", "name": "Debian 12 - Docker"}
        raise AssertionError(path)

    client = _client(fake)
    assert sorted(list_available_image_names(client, "vps-x")) == ["Debian 12 - Docker", "Ubuntu 24.04"]


def test_validate_datacenter_passes_for_known() -> None:
    assert validate_datacenter(["US-EAST-VA", "US-WEST-OR"], "US-EAST-VA") == "US-EAST-VA"


def test_validate_datacenter_raises_for_unknown() -> None:
    with pytest.raises(MngrError, match="not available"):
        validate_datacenter(["US-EAST-VA"], "EU-WEST-1")


def test_find_required_field_returns_matching_entry() -> None:
    fields = [
        {"label": "vps_datacenter", "allowedValues": ["US-EAST-VA"]},
        {"label": "vps_os", "allowedValues": ["Debian 12"]},
    ]
    assert find_required_field(fields, "vps_os")["allowedValues"] == ["Debian 12"]


def test_find_required_field_raises_for_missing() -> None:
    with pytest.raises(MngrError, match="not present"):
        find_required_field([{"label": "vps_os"}], "vps_region")
