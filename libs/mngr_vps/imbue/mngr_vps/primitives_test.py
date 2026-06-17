"""Tests for VPS Docker primitives."""

import pytest

from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vps.primitives import VpsInstanceStatus


def test_vps_instance_id_empty_raises() -> None:
    with pytest.raises(ValueError):
        VpsInstanceId("")


def test_vps_instance_status_values() -> None:
    # Pins the serialized (wire) value of each status. These strings cross the
    # provider-API / persistence boundary, so an UpperCaseStrEnum/auto() change
    # that altered them would be a silent compatibility break -- guard it here.
    assert VpsInstanceStatus.PENDING == "PENDING"
    assert VpsInstanceStatus.ACTIVE == "ACTIVE"
    assert VpsInstanceStatus.HALTED == "HALTED"
    assert VpsInstanceStatus.DESTROYING == "DESTROYING"
    assert VpsInstanceStatus.UNKNOWN == "UNKNOWN"


def test_vps_instance_status_from_string() -> None:
    assert VpsInstanceStatus("ACTIVE") == VpsInstanceStatus.ACTIVE
