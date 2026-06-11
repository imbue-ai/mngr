"""Tests for VPS Docker primitives."""

import pytest

from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vps_docker.primitives import VpsInstanceStatus


def test_vps_instance_id_valid() -> None:
    instance_id = VpsInstanceId("abc-123")
    assert str(instance_id) == "abc-123"


def test_vps_instance_id_empty_raises() -> None:
    with pytest.raises(ValueError):
        VpsInstanceId("")


def test_vps_instance_status_values() -> None:
    assert VpsInstanceStatus.PENDING == "PENDING"
    assert VpsInstanceStatus.ACTIVE == "ACTIVE"
    assert VpsInstanceStatus.HALTED == "HALTED"
    assert VpsInstanceStatus.DESTROYING == "DESTROYING"
    assert VpsInstanceStatus.UNKNOWN == "UNKNOWN"


def test_vps_instance_status_from_string() -> None:
    assert VpsInstanceStatus("ACTIVE") == VpsInstanceStatus.ACTIVE
