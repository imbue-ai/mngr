"""Tests for VPS primitives."""

from pathlib import Path

import pytest

from imbue.mngr.primitives import HostId
from imbue.mngr.providers.ssh_utils import load_or_create_host_keypair
from imbue.mngr_vps.primitives import VPS_HOST_KEY_NAME
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vps.primitives import VpsInstanceStatus
from imbue.mngr_vps.primitives import load_or_create_per_host_host_keypair
from imbue.mngr_vps.primitives import per_host_key_dir
from imbue.mngr_vps.primitives import read_host_public_key_with_legacy_fallback


def test_vps_instance_id_empty_raises() -> None:
    with pytest.raises(ValueError):
        VpsInstanceId("")


def test_per_host_host_keys_are_unique_per_host(tmp_path: Path) -> None:
    """Two different hosts must get different host keys, so one host's key can never
    be reused to impersonate another."""
    host_a = HostId.generate()
    host_b = HostId.generate()

    _path_a, pub_a = load_or_create_per_host_host_keypair(tmp_path, host_a, VPS_HOST_KEY_NAME)
    _path_b, pub_b = load_or_create_per_host_host_keypair(tmp_path, host_b, VPS_HOST_KEY_NAME)

    assert pub_a != pub_b
    # Each host's key lives under its own subdir.
    assert per_host_key_dir(tmp_path, host_a) != per_host_key_dir(tmp_path, host_b)


def test_per_host_host_key_is_stable_for_a_given_host(tmp_path: Path) -> None:
    """Re-reading the same host's key returns the same key (load-or-create is idempotent)."""
    host_id = HostId.generate()
    _path1, pub1 = load_or_create_per_host_host_keypair(tmp_path, host_id, VPS_HOST_KEY_NAME)
    _path2, pub2 = load_or_create_per_host_host_keypair(tmp_path, host_id, VPS_HOST_KEY_NAME)
    assert pub1 == pub2


def test_per_host_key_never_falls_back_to_legacy_on_create(tmp_path: Path) -> None:
    """A fresh host must NOT inherit a pre-existing provider-global (legacy) key."""
    # Simulate a legacy shared key left over from before per-host keys existed.
    _legacy_path, legacy_pub = load_or_create_host_keypair(tmp_path, VPS_HOST_KEY_NAME)
    host_id = HostId.generate()

    _path, per_host_pub = load_or_create_per_host_host_keypair(tmp_path, host_id, VPS_HOST_KEY_NAME)

    assert per_host_pub != legacy_pub


def test_read_with_legacy_fallback_prefers_per_host_then_legacy_then_none(tmp_path: Path) -> None:
    """The resume read returns the per-host key if present, else the legacy shared key, else None."""
    host_id = HostId.generate()
    # Neither exists yet.
    assert read_host_public_key_with_legacy_fallback(tmp_path, host_id, VPS_HOST_KEY_NAME) is None

    # Only a legacy shared key exists (a host created before per-host keys).
    _legacy_path, legacy_pub = load_or_create_host_keypair(tmp_path, VPS_HOST_KEY_NAME)
    assert read_host_public_key_with_legacy_fallback(tmp_path, host_id, VPS_HOST_KEY_NAME) == legacy_pub

    # Once the per-host key exists, it takes precedence over the legacy key.
    _path, per_host_pub = load_or_create_per_host_host_keypair(tmp_path, host_id, VPS_HOST_KEY_NAME)
    assert read_host_public_key_with_legacy_fallback(tmp_path, host_id, VPS_HOST_KEY_NAME) == per_host_pub


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
