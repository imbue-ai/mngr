import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from inline_snapshot import snapshot

from imbue.mngr.primitives import HostId
from imbue.mngr.providers.host_key_store import HostKeyOrigin
from imbue.mngr.providers.host_key_store import load_current_host_key_pins
from imbue.mngr.providers.host_key_store import load_host_key_record
from imbue.mngr.providers.host_key_store import move_host_endpoint_pins
from imbue.mngr.providers.host_key_store import pin_known_hosts_text
from imbue.mngr.providers.host_key_store import render_pins_as_known_hosts_text
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr.providers.ssh_utils import save_ssh_keypair
from imbue.mngr.utils.testing import allow_warnings
from imbue.mngr_imbue_cloud.errors import AdoptionError
from imbue.mngr_imbue_cloud.errors import HostKeyDriftError
from imbue.mngr_imbue_cloud.interfaces import SliceReconcilerState
from imbue.mngr_imbue_cloud.providers.adoption import ADOPTION_SCHEMA_VERSION
from imbue.mngr_imbue_cloud.providers.adoption import AdoptionEndpointKind
from imbue.mngr_imbue_cloud.providers.adoption import BoundEndpoints
from imbue.mngr_imbue_cloud.providers.adoption import SliceAdoptionTarget
from imbue.mngr_imbue_cloud.providers.adoption import bound_endpoints_path
from imbue.mngr_imbue_cloud.providers.adoption import build_reconciler_install_command
from imbue.mngr_imbue_cloud.providers.adoption import ensure_adopted
from imbue.mngr_imbue_cloud.providers.adoption import expected_reconciler_content_hash
from imbue.mngr_imbue_cloud.providers.adoption import invalidate_adoption_verification
from imbue.mngr_imbue_cloud.providers.adoption import is_slice_lease
from imbue.mngr_imbue_cloud.providers.adoption import load_adoption_marker
from imbue.mngr_imbue_cloud.providers.adoption import load_bound_endpoints
from imbue.mngr_imbue_cloud.providers.adoption import load_pending_host_key_rotation
from imbue.mngr_imbue_cloud.providers.adoption import parse_reconciler_state_output
from imbue.mngr_imbue_cloud.providers.adoption import rebind_host_key_pins_to_endpoints
from imbue.mngr_imbue_cloud.providers.adoption import record_bound_endpoints
from imbue.mngr_imbue_cloud.providers.adoption import remove_key_from_desired_authorized_keys
from imbue.mngr_imbue_cloud.providers.adoption import render_desired_authorized_keys
from imbue.mngr_imbue_cloud.providers.adoption import render_reconciler_unit
from imbue.mngr_imbue_cloud.providers.adoption import rotate_client_key
from imbue.mngr_imbue_cloud.providers.adoption import rotate_endpoint_host_key
from imbue.mngr_imbue_cloud.providers.mock_slice_vm_access_test import MockSliceVmAccess
from imbue.mngr_imbue_cloud.providers.testing import load_pins_by_endpoint

_ADDRESS = "203.0.113.7"
_VM_PORT = 22010
_CONTAINER_PORT = 22011
_BAKE_KEY = "ssh-ed25519 AAAABAKE bake-key"
_POOL_KEY = "ssh-ed25519 AAAAPOOL pool-management"
_CLIENT_KEY = "ssh-ed25519 AAAACLIENT per-host-client"
_OLD_VM_HOST_KEY = "ssh-ed25519 AAAAVMOLD baked-vm-host-key"
_OLD_CONTAINER_HOST_KEY = "ssh-ed25519 AAAACONTOLD baked-container-host-key"


def _make_target(tmp_path: Path, host_id: HostId) -> SliceAdoptionTarget:
    host_state_dir = tmp_path / "host_state"
    host_state_dir.mkdir(parents=True, exist_ok=True)
    return SliceAdoptionTarget(
        host_id=host_id,
        address=_ADDRESS,
        vm_port=_VM_PORT,
        container_port=_CONTAINER_PORT,
        host_state_dir=host_state_dir,
        known_hosts_path=host_state_dir / "known_hosts",
        client_public_key=_CLIENT_KEY,
    )


def _make_unadopted_access() -> MockSliceVmAccess:
    return MockSliceVmAccess(
        vm_port=_VM_PORT,
        container_port=_CONTAINER_PORT,
        served_key_by_port={_VM_PORT: _OLD_VM_HOST_KEY, _CONTAINER_PORT: _OLD_CONTAINER_HOST_KEY},
        vm_authorized_keys=f"{_BAKE_KEY}\n{_POOL_KEY}\n{_CLIENT_KEY}\n",
        container_authorized_keys=f"{_CLIENT_KEY}\n",
    )


def _pin_bootstrap_keys(target: SliceAdoptionTarget) -> None:
    add_host_to_known_hosts(
        target.known_hosts_path, target.address, _VM_PORT, _OLD_VM_HOST_KEY, host_id=target.host_id
    )
    add_host_to_known_hosts(
        target.known_hosts_path, target.address, _CONTAINER_PORT, _OLD_CONTAINER_HOST_KEY, host_id=target.host_id
    )


def _adopt_fresh_slice(tmp_path: Path, host_id: HostId) -> tuple[SliceAdoptionTarget, MockSliceVmAccess]:
    """Adopt a bake-fresh slice from this device: bootstrap pins present, no marker yet."""
    target = _make_target(tmp_path, host_id)
    _pin_bootstrap_keys(target)
    access = _make_unadopted_access()
    ensure_adopted(access, target, is_full_verification=False)
    return target, access


def _endpoint_pin_map(target: SliceAdoptionTarget) -> dict[int, tuple[str, HostKeyOrigin]]:
    """The host's pins by port (the slice's two endpoints share one address)."""
    pins = load_pins_by_endpoint(target.known_hosts_path, target.host_id)
    return {port: pin for (_address, port), pin in pins.items()}


def _rebind(target: SliceAdoptionTarget, address: str, vm_port: int, container_port: int) -> None:
    rebind_host_key_pins_to_endpoints(
        target.host_state_dir, target.known_hosts_path, target.host_id, address, vm_port, container_port
    )


def _make_second_device(tmp_path: Path, first_target: SliceAdoptionTarget) -> SliceAdoptionTarget:
    """A second device's view of the host: its own state dir, no marker, and the first
    device's pins delivered as user-origin material the way the synced workspace record does."""
    second_target = _make_target(tmp_path / "second", first_target.host_id)
    synced_text = render_pins_as_known_hosts_text(load_current_host_key_pins(first_target.known_hosts_path))
    pin_known_hosts_text(
        second_target.known_hosts_path, synced_text, host_id=first_target.host_id, origin=HostKeyOrigin.USER
    )
    return second_target


def test_render_desired_authorized_keys_preserves_pool_key_and_appends_client_key() -> None:
    rendered = render_desired_authorized_keys(f"{_BAKE_KEY}\n{_POOL_KEY}\n", (_CLIENT_KEY,))
    assert rendered == snapshot(
        """\
ssh-ed25519 AAAABAKE bake-key
ssh-ed25519 AAAAPOOL pool-management
ssh-ed25519 AAAACLIENT per-host-client
"""
    )


def test_render_desired_authorized_keys_dedupes_and_tolerates_missing_file() -> None:
    rendered = render_desired_authorized_keys(None, (_CLIENT_KEY, _CLIENT_KEY))
    assert rendered == f"{_CLIENT_KEY}\n"
    rerendered = render_desired_authorized_keys(rendered, (_CLIENT_KEY,))
    assert rerendered == rendered


def test_remove_key_from_desired_authorized_keys_drops_only_the_retired_line() -> None:
    desired = f"{_POOL_KEY}\n{_CLIENT_KEY}\n"
    assert remove_key_from_desired_authorized_keys(desired, _CLIENT_KEY) == f"{_POOL_KEY}\n"


def test_parse_reconciler_state_output_round_trips_desired_content() -> None:
    desired = f"{_POOL_KEY}\n{_CLIENT_KEY}\n"
    encoded = base64.b64encode(desired.encode()).decode()
    content_hash = "a" * 64
    stdout = (
        f"MNGR_RECONCILER_ENABLED=enabled\nMNGR_DESIRED_B64={encoded}\n"
        f"MNGR_LIVE_MATCHES=1\nMNGR_RECONCILER_SHA256={content_hash}\n"
    )
    state = parse_reconciler_state_output(stdout)
    assert state == SliceReconcilerState(
        is_unit_enabled=True,
        desired_authorized_keys=desired,
        is_live_matching_desired=True,
        installed_content_hash=content_hash,
    )


def test_parse_reconciler_state_output_reads_absent_desired_file() -> None:
    stdout = "MNGR_RECONCILER_ENABLED=unknown\nMNGR_DESIRED_B64=ABSENT\nMNGR_LIVE_MATCHES=0\nMNGR_RECONCILER_SHA256=ABSENT\n"
    state = parse_reconciler_state_output(stdout)
    assert state == SliceReconcilerState(
        is_unit_enabled=False,
        desired_authorized_keys=None,
        is_live_matching_desired=False,
        installed_content_hash=None,
    )


def test_reconciler_unit_is_activated_by_cloud_init_target_to_avoid_the_ordering_cycle() -> None:
    """WantedBy=multi-user.target + After=cloud-final.service is an ordering cycle
    (cloud-final is itself After=multi-user.target), which systemd breaks by deleting
    the reconciler's start job -- so the unit must hang off cloud-init.target instead."""
    unit = render_reconciler_unit()
    assert "WantedBy=cloud-init.target" in unit
    assert "WantedBy=multi-user.target" not in unit
    assert "After=cloud-final.service" in unit


def test_reconciler_install_command_reenables_so_stale_enablement_symlinks_are_dropped() -> None:
    command = build_reconciler_install_command(f"{_CLIENT_KEY}\n")
    assert "systemctl reenable mngr-key-reconciler.service" in command
    assert "systemctl enable mngr-key-reconciler.service" not in command


def test_schema_bump_sweeps_stale_reconciler_content_onto_adopted_hosts(tmp_path: Path) -> None:
    """A host adopted and verified by an older client version (stamped at the
    previous schema version) carries reconciler content that hashes differently
    -- e.g. the multi-user.target ordering-cycle unit. The schema-version bump
    alone must sweep it through one full verification, whose content-hash check
    replaces the stale unit/script while the host is still reachable (after a
    reboot with the broken unit it no longer would be)."""
    host_id = HostId()
    target = _make_target(tmp_path, host_id)
    _pin_bootstrap_keys(target)
    access = _make_unadopted_access()
    ensure_adopted(access, target, is_full_verification=False)
    assert access.installed_content_hash == expected_reconciler_content_hash()

    # Model an older client version's install: unit enabled, keys fine, but the
    # unit/script content hashes differently and the marker is stamped at the
    # previous schema version (NOT invalidated -- the bump is the trigger).
    access.installed_content_hash = "0" * 64
    marker_path = target.host_state_dir / "adoption.json"
    marker_path.write_text(
        json.dumps({"adopted_at": "2026-07-01T00:00:00+00:00", "verified_schema_version": ADOPTION_SCHEMA_VERSION - 1})
    )

    ensure_adopted(access, target, is_full_verification=True)

    # Only install_reconciler updates the hash, so this proves the reinstall ran.
    assert access.installed_content_hash == expected_reconciler_content_hash()
    marker = load_adoption_marker(target.host_state_dir)
    assert marker is not None
    assert marker.verified_schema_version == ADOPTION_SCHEMA_VERSION


def test_is_slice_lease_distinguishes_forwarded_ports_from_the_publish_port() -> None:
    assert is_slice_lease(container_ssh_port=22011, configured_container_publish_port=2222)
    assert not is_slice_lease(container_ssh_port=2222, configured_container_publish_port=2222)


def test_full_adoption_installs_reconciler_rotates_both_endpoints_and_writes_the_marker(tmp_path: Path) -> None:
    host_id = HostId()
    target = _make_target(tmp_path, host_id)
    _pin_bootstrap_keys(target)
    access = _make_unadopted_access()

    ensure_adopted(access, target, is_full_verification=False)

    # The reconciler owns the VM root's authorized_keys, preserving the pool +
    # bake keys (pre-strip) and carrying the client key.
    assert access.is_unit_enabled
    assert access.desired_authorized_keys is not None
    assert set(access.desired_authorized_keys.splitlines()) == {_BAKE_KEY, _POOL_KEY, _CLIENT_KEY}
    assert access.vm_authorized_keys == access.desired_authorized_keys
    # Both endpoints serve fresh user-origin keys that are pinned in the store;
    # the bake-time bootstrap pins are gone, and the endpoints the pins were
    # written at are remembered for the next relocation.
    pin_by_port = _endpoint_pin_map(target)
    assert set(pin_by_port) == {_VM_PORT, _CONTAINER_PORT}
    for port in (_VM_PORT, _CONTAINER_PORT):
        pinned_key, origin = pin_by_port[port]
        assert origin is HostKeyOrigin.USER
        assert access.served_key_by_port[port] == pinned_key
        assert pinned_key not in (_OLD_VM_HOST_KEY, _OLD_CONTAINER_HOST_KEY)
    assert load_bound_endpoints(target.host_state_dir) == BoundEndpoints(
        vps_address=target.address, ssh_port=_VM_PORT, container_ssh_port=_CONTAINER_PORT
    )
    assert load_adoption_marker(target.host_state_dir) is not None
    # No rotation is left pending.
    for kind in AdoptionEndpointKind:
        assert load_pending_host_key_rotation(target.host_state_dir, kind) is None


def test_ensure_adopted_is_a_pure_local_noop_when_marked_and_not_verifying(tmp_path: Path) -> None:
    host_id = HostId()
    target = _make_target(tmp_path, host_id)
    _pin_bootstrap_keys(target)
    access = _make_unadopted_access()
    ensure_adopted(access, target, is_full_verification=False)
    call_count_after_adoption = access.call_count

    ensure_adopted(access, target, is_full_verification=False)

    assert access.call_count == call_count_after_adoption


def test_successful_verification_is_durable_and_skips_ssh_work(tmp_path: Path) -> None:
    """Once a host has verified clean at the current schema version, later full
    verifications are pure-local no-ops -- across processes, since the stamp
    lives in the marker file, not memory."""
    host_id = HostId()
    target = _make_target(tmp_path, host_id)
    _pin_bootstrap_keys(target)
    access = _make_unadopted_access()
    ensure_adopted(access, target, is_full_verification=False)
    invalidate_adoption_verification(target.host_state_dir)
    ensure_adopted(access, target, is_full_verification=True)
    call_count_after_verification = access.call_count
    marker = load_adoption_marker(target.host_state_dir)
    assert marker is not None
    assert marker.verified_schema_version == ADOPTION_SCHEMA_VERSION

    ensure_adopted(access, target, is_full_verification=True)

    assert access.call_count == call_count_after_verification


def test_invalidating_the_stamp_forces_exactly_one_reverification(tmp_path: Path) -> None:
    host_id = HostId()
    target = _make_target(tmp_path, host_id)
    _pin_bootstrap_keys(target)
    access = _make_unadopted_access()
    ensure_adopted(access, target, is_full_verification=False)
    call_count_after_adoption = access.call_count

    invalidate_adoption_verification(target.host_state_dir)
    marker_after_invalidation = load_adoption_marker(target.host_state_dir)
    assert marker_after_invalidation is not None
    assert marker_after_invalidation.verified_schema_version == 0

    ensure_adopted(access, target, is_full_verification=True)
    call_count_after_reverification = access.call_count
    assert call_count_after_reverification > call_count_after_adoption

    ensure_adopted(access, target, is_full_verification=True)
    assert access.call_count == call_count_after_reverification


def test_marker_from_before_the_stamp_field_gets_one_full_verification(tmp_path: Path) -> None:
    """Markers written before verified_schema_version existed parse as version 0,
    so such a host is swept through exactly one full pass and then stamped."""
    host_id = HostId()
    target = _make_target(tmp_path, host_id)
    _pin_bootstrap_keys(target)
    access = _make_unadopted_access()
    ensure_adopted(access, target, is_full_verification=False)
    marker_path = target.host_state_dir / "adoption.json"
    marker_path.write_text(json.dumps({"adopted_at": "2026-07-01T00:00:00+00:00"}))
    call_count_before = access.call_count

    ensure_adopted(access, target, is_full_verification=True)

    assert access.call_count > call_count_before
    marker = load_adoption_marker(target.host_state_dir)
    assert marker is not None
    assert marker.verified_schema_version == ADOPTION_SCHEMA_VERSION


def test_full_verification_heals_a_disabled_reconciler_and_missing_client_key(tmp_path: Path) -> None:
    host_id = HostId()
    target = _make_target(tmp_path, host_id)
    _pin_bootstrap_keys(target)
    access = _make_unadopted_access()
    ensure_adopted(access, target, is_full_verification=False)

    # Model a cloud-init replay having clobbered the live file and the unit
    # having been disabled out-of-band -- observed through a restart, which
    # invalidates the durable verified stamp (a stamped host is never scanned).
    access.is_unit_enabled = False
    access.vm_authorized_keys = f"{_BAKE_KEY}\n{_POOL_KEY}\n"
    invalidate_adoption_verification(target.host_state_dir)

    ensure_adopted(access, target, is_full_verification=True)

    assert access.is_unit_enabled
    assert access.desired_authorized_keys is not None
    assert _CLIENT_KEY in access.desired_authorized_keys.splitlines()
    assert access.vm_authorized_keys == access.desired_authorized_keys


def test_full_verification_refuses_a_foreign_rekey(tmp_path: Path) -> None:
    """A served key that matches neither the user pin nor a pending rotation is somebody
    else's re-key (e.g. an operator): the device must refuse it, leaving pins untouched."""
    host_id = HostId()
    target = _make_target(tmp_path, host_id)
    _pin_bootstrap_keys(target)
    access = _make_unadopted_access()
    ensure_adopted(access, target, is_full_verification=False)
    pins_before = _endpoint_pin_map(target)

    access.served_key_by_port[_VM_PORT] = "ssh-ed25519 AAAAEVIL operator-rekey"
    invalidate_adoption_verification(target.host_state_dir)

    with pytest.raises(HostKeyDriftError):
        ensure_adopted(access, target, is_full_verification=True)
    assert _endpoint_pin_map(target) == pins_before


def test_explicit_rotation_recovers_a_drifted_container_endpoint(tmp_path: Path) -> None:
    """The recovery HostKeyDriftError points at is an explicit rotation (`hosts rotate`):
    it must converge a foreign-rekeyed container endpoint onto fresh user-origin material
    -- installed through the still-pinned VM door -- without ever pinning the foreign key."""
    host_id = HostId()
    target = _make_target(tmp_path, host_id)
    _pin_bootstrap_keys(target)
    access = _make_unadopted_access()
    ensure_adopted(access, target, is_full_verification=False)
    foreign_key = "ssh-ed25519 AAAAEVIL operator-rekey"
    access.served_key_by_port[_CONTAINER_PORT] = foreign_key
    invalidate_adoption_verification(target.host_state_dir)
    with pytest.raises(HostKeyDriftError):
        ensure_adopted(access, target, is_full_verification=True)

    new_public_key = rotate_endpoint_host_key(access, target, AdoptionEndpointKind.CONTAINER)

    assert new_public_key != foreign_key
    assert _endpoint_pin_map(target)[_CONTAINER_PORT] == (new_public_key, HostKeyOrigin.USER)
    assert access.served_key_by_port[_CONTAINER_PORT] == new_public_key
    # The host verifies clean again -- the drift episode is fully closed.
    ensure_adopted(access, target, is_full_verification=True)


def test_rotation_crash_at_each_step_leaves_a_convergeable_host(tmp_path: Path) -> None:
    """Kill the rotation after every possible mutating operation; a re-run must always
    converge to the endpoint serving a user-origin pinned key, never a stranded host."""
    for crash_after_operation_count in range(0, 4):
        host_id = HostId()
        target = _make_target(tmp_path / f"crash-{crash_after_operation_count}", host_id)
        target.host_state_dir.mkdir(parents=True, exist_ok=True)
        _pin_bootstrap_keys(target)
        access = _make_unadopted_access()
        access.operations_until_failure = crash_after_operation_count

        try:
            rotate_endpoint_host_key(access, target, AdoptionEndpointKind.VM)
        except AdoptionError:
            pass

        # Whatever the crash point: the endpoint's served key is either the old
        # pinned one, or the pending rotation's new one (recoverable from disk).
        pin_by_port = _endpoint_pin_map(target)
        pinned_key, _origin = pin_by_port[_VM_PORT]
        pending = load_pending_host_key_rotation(target.host_state_dir, AdoptionEndpointKind.VM)
        served = access.served_key_by_port[_VM_PORT]
        assert served == pinned_key or (pending is not None and served == pending.new_public_key)

        # A later run (no injected failure) converges.
        access.operations_until_failure = None
        new_public_key = rotate_endpoint_host_key(access, target, AdoptionEndpointKind.VM)
        pin_by_port_after = _endpoint_pin_map(target)
        assert pin_by_port_after[_VM_PORT] == (new_public_key, HostKeyOrigin.USER)
        assert access.served_key_by_port[_VM_PORT] == new_public_key
        assert load_pending_host_key_rotation(target.host_state_dir, AdoptionEndpointKind.VM) is None


def test_rotation_resume_pins_the_new_key_without_reinstalling(tmp_path: Path) -> None:
    """A crash between install and pin is recovered by the probe alone: the endpoint
    already serves the pending key, so the resume just pins it."""
    host_id = HostId()
    target = _make_target(tmp_path, host_id)
    _pin_bootstrap_keys(target)
    access = _make_unadopted_access()
    # Two mutations happen in a VM rotation before the pin: crash right after
    # the install (the first mutation).
    access.operations_until_failure = 0
    with pytest.raises(AdoptionError):
        rotate_endpoint_host_key(access, target, AdoptionEndpointKind.VM)
    pending = load_pending_host_key_rotation(target.host_state_dir, AdoptionEndpointKind.VM)
    assert pending is not None
    # Model the install having landed before the crash (the served key is new).
    access.operations_until_failure = None
    access.served_key_by_port[_VM_PORT] = pending.new_public_key
    mutations_before = access.call_count

    new_public_key = rotate_endpoint_host_key(access, target, AdoptionEndpointKind.VM)

    assert new_public_key == pending.new_public_key
    assert _endpoint_pin_map(target)[_VM_PORT] == (new_public_key, HostKeyOrigin.USER)
    # Only probes ran on the resume; no reinstall happened.
    assert access.served_key_by_port[_VM_PORT] == new_public_key
    assert access.call_count == mutations_before + 1


def test_rotate_client_key_swaps_local_files_and_retires_the_old_key(tmp_path: Path) -> None:
    host_id = HostId()
    target = _make_target(tmp_path, host_id)
    _pin_bootstrap_keys(target)
    access = _make_unadopted_access()
    ensure_adopted(access, target, is_full_verification=False)
    # The host's actual client keypair lives in the state dir; regenerate a real
    # one so authentication checks read a genuine .pub sibling.
    save_ssh_keypair(target.host_state_dir, "ssh_key")
    old_public_key = (target.host_state_dir / "ssh_key.pub").read_text().strip()
    # The active key is authorized on both endpoints, as after a real lease.
    access.install_reconciler(render_desired_authorized_keys(access.desired_authorized_keys, (old_public_key,)))
    access.append_container_authorized_key(old_public_key)

    new_public_key = rotate_client_key(access, target)

    assert (target.host_state_dir / "ssh_key.pub").read_text().strip() == new_public_key
    assert new_public_key != old_public_key
    assert access.desired_authorized_keys is not None
    desired_lines = set(access.desired_authorized_keys.splitlines())
    container_lines = set(access.container_authorized_keys.splitlines())
    assert new_public_key in desired_lines and new_public_key in container_lines
    assert old_public_key not in desired_lines and old_public_key not in container_lines
    # The pool and bake keys survive a client rotation (pre-strip posture).
    assert {_BAKE_KEY, _POOL_KEY} <= desired_lines
    assert not (target.host_state_dir / "pending_client_key_rotation.json").exists()


def test_rotate_client_key_aborts_before_swap_when_the_new_key_cannot_authenticate(tmp_path: Path) -> None:
    host_id = HostId()
    target = _make_target(tmp_path, host_id)
    _pin_bootstrap_keys(target)
    access = _make_unadopted_access()
    ensure_adopted(access, target, is_full_verification=False)
    save_ssh_keypair(target.host_state_dir, "ssh_key")
    old_public_key = (target.host_state_dir / "ssh_key.pub").read_text().strip()
    access.is_authentication_always_failing = True

    with pytest.raises(AdoptionError):
        rotate_client_key(access, target)

    # The local active key is untouched, so this machine can still open the host.
    assert (target.host_state_dir / "ssh_key.pub").read_text().strip() == old_public_key
    # The pending state survives for a later resume.
    pending_path = target.host_state_dir / "pending_client_key_rotation.json"
    assert pending_path.exists()
    assert json.loads(pending_path.read_text())["old_public_key"] == old_public_key


def test_rotate_client_key_recovers_from_a_crash_between_the_two_swap_renames(tmp_path: Path) -> None:
    """A crash after the private-key rename but before the public one leaves ssh_key
    holding the new private key while ssh_key.pub still holds the old one; a re-run
    must finish the swap and retire the old key rather than failing forever on the
    now-missing ssh_key_next file."""
    host_id = HostId()
    target = _make_target(tmp_path, host_id)
    _pin_bootstrap_keys(target)
    access = _make_unadopted_access()
    ensure_adopted(access, target, is_full_verification=False)
    save_ssh_keypair(target.host_state_dir, "ssh_key")
    old_public_key = (target.host_state_dir / "ssh_key.pub").read_text().strip()
    access.install_reconciler(render_desired_authorized_keys(access.desired_authorized_keys, (old_public_key,)))
    access.append_container_authorized_key(old_public_key)
    # First run: abort after the new key was generated, persisted, and authorized
    # (authentication check fails), leaving the pending state + next keypair on disk.
    access.is_authentication_always_failing = True
    with pytest.raises(AdoptionError):
        rotate_client_key(access, target)
    access.is_authentication_always_failing = False
    # Model the crash between the two swap renames: private half in place, public not.
    (target.host_state_dir / "ssh_key_next").replace(target.host_state_dir / "ssh_key")
    pending_path = target.host_state_dir / "pending_client_key_rotation.json"
    expected_new_public_key = json.loads(pending_path.read_text())["new_public_key"]

    new_public_key = rotate_client_key(access, target)

    assert new_public_key == expected_new_public_key
    assert (target.host_state_dir / "ssh_key.pub").read_text().strip() == new_public_key
    assert not (target.host_state_dir / "ssh_key_next.pub").exists()
    assert not pending_path.exists()
    assert access.desired_authorized_keys is not None
    assert old_public_key not in access.desired_authorized_keys.splitlines()
    assert old_public_key not in access.container_authorized_keys.splitlines()


def test_rotate_client_key_requires_an_adopted_host(tmp_path: Path) -> None:
    host_id = HostId()
    target = _make_target(tmp_path, host_id)
    access = _make_unadopted_access()
    save_ssh_keypair(target.host_state_dir, "ssh_key")

    with pytest.raises(AdoptionError, match="adopt"):
        rotate_client_key(access, target)


def _write_rsa_client_keypair(host_state_dir: Path) -> str:
    """Write a legacy-layout RSA client keypair (the pre-Ed25519 mngr PEM format); returns the public line.

    2048-bit for test speed -- production legacy keys are RSA-4096, but the
    migration detects the algorithm, not the size.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_text = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_line = (
        private_key.public_key()
        .public_bytes(encoding=serialization.Encoding.OpenSSH, format=serialization.PublicFormat.OpenSSH)
        .decode("utf-8")
    )
    (host_state_dir / "ssh_key").write_text(private_text)
    (host_state_dir / "ssh_key.pub").write_text(public_line)
    return public_line.strip()


def test_adoption_rotates_a_legacy_rsa_client_key_to_ed25519(tmp_path: Path) -> None:
    """The retired minds-side RSA -> Ed25519 migration, subsumed: adopting an
    RSA-keyed slice ends with a fresh Ed25519 client key authorized through the
    reconciler desired state (so it survives VM restarts) and the RSA key
    de-authorized everywhere."""
    host_id = HostId()
    target = _make_target(tmp_path, host_id)
    _pin_bootstrap_keys(target)
    rsa_public_key = _write_rsa_client_keypair(target.host_state_dir)
    access = MockSliceVmAccess(
        vm_port=_VM_PORT,
        container_port=_CONTAINER_PORT,
        served_key_by_port={_VM_PORT: _OLD_VM_HOST_KEY, _CONTAINER_PORT: _OLD_CONTAINER_HOST_KEY},
        vm_authorized_keys=f"{_BAKE_KEY}\n{_POOL_KEY}\n{rsa_public_key}\n",
        container_authorized_keys=f"{rsa_public_key}\n",
    )

    ensure_adopted(access, target, is_full_verification=False)

    new_public_key = (target.host_state_dir / "ssh_key.pub").read_text().strip()
    assert new_public_key.startswith("ssh-ed25519 ")
    assert access.desired_authorized_keys is not None
    desired_lines = set(access.desired_authorized_keys.splitlines())
    container_lines = set(access.container_authorized_keys.splitlines())
    assert new_public_key in desired_lines and new_public_key in container_lines
    assert rsa_public_key not in desired_lines and rsa_public_key not in container_lines
    # The pool and bake keys survive (pre-strip posture).
    assert {_BAKE_KEY, _POOL_KEY} <= desired_lines


def test_full_verification_rotates_an_rsa_client_key_on_an_already_adopted_host(tmp_path: Path) -> None:
    """A host adopted by an earlier client version can still hold an RSA client key;
    the next full verification rotates it."""
    host_id = HostId()
    target = _make_target(tmp_path, host_id)
    _pin_bootstrap_keys(target)
    access = _make_unadopted_access()
    ensure_adopted(access, target, is_full_verification=False)
    # The RSA keypair lands after adoption, modeling an adopted-then-synced host
    # whose materialized client key predates the Ed25519 switch.
    rsa_public_key = _write_rsa_client_keypair(target.host_state_dir)
    access.install_reconciler(render_desired_authorized_keys(access.desired_authorized_keys, (rsa_public_key,)))
    access.append_container_authorized_key(rsa_public_key)

    ensure_adopted(access, target, is_full_verification=True)

    new_public_key = (target.host_state_dir / "ssh_key.pub").read_text().strip()
    assert new_public_key.startswith("ssh-ed25519 ")
    assert access.desired_authorized_keys is not None
    assert rsa_public_key not in access.desired_authorized_keys.splitlines()
    assert rsa_public_key not in access.container_authorized_keys.splitlines()


def test_full_verification_leaves_an_ed25519_client_key_alone(tmp_path: Path) -> None:
    host_id = HostId()
    target = _make_target(tmp_path, host_id)
    _pin_bootstrap_keys(target)
    access = _make_unadopted_access()
    ensure_adopted(access, target, is_full_verification=False)
    save_ssh_keypair(target.host_state_dir, "ssh_key")
    public_key_before = (target.host_state_dir / "ssh_key.pub").read_text()

    ensure_adopted(access, target, is_full_verification=True)

    assert (target.host_state_dir / "ssh_key.pub").read_text() == public_key_before
    assert not (target.host_state_dir / "pending_client_key_rotation.json").exists()


def test_full_verification_resumes_a_crashed_client_key_rotation(tmp_path: Path) -> None:
    """A crash mid-rotation can leave the swapped-in key already Ed25519 with the old
    key still authorized; the pending state -- not the key algorithm -- is what makes
    the next full verification finish the job."""
    host_id = HostId()
    target = _make_target(tmp_path, host_id)
    _pin_bootstrap_keys(target)
    access = _make_unadopted_access()
    ensure_adopted(access, target, is_full_verification=False)
    save_ssh_keypair(target.host_state_dir, "ssh_key")
    old_public_key = (target.host_state_dir / "ssh_key.pub").read_text().strip()
    access.install_reconciler(render_desired_authorized_keys(access.desired_authorized_keys, (old_public_key,)))
    access.append_container_authorized_key(old_public_key)
    # Crash the rotation after authorization (authentication check fails), then
    # model the swap having completed before the crash.
    access.is_authentication_always_failing = True
    with pytest.raises(AdoptionError):
        rotate_client_key(access, target)
    access.is_authentication_always_failing = False
    (target.host_state_dir / "ssh_key_next").replace(target.host_state_dir / "ssh_key")
    (target.host_state_dir / "ssh_key_next.pub").replace(target.host_state_dir / "ssh_key.pub")

    ensure_adopted(access, target, is_full_verification=True)

    assert not (target.host_state_dir / "pending_client_key_rotation.json").exists()
    assert access.desired_authorized_keys is not None
    assert old_public_key not in access.desired_authorized_keys.splitlines()
    assert old_public_key not in access.container_authorized_keys.splitlines()


def test_adopting_a_host_another_device_already_adopted_verifies_instead_of_rerotating(tmp_path: Path) -> None:
    """The client-side marker is per-device, but adoption is per-host: a second
    device (synced pins, no local marker) must not rotate the host keys out from
    under the first -- the installed reconciler is the already-adopted fingerprint."""
    host_id = HostId()
    first_target = _make_target(tmp_path / "first", host_id)
    _pin_bootstrap_keys(first_target)
    access = _make_unadopted_access()
    ensure_adopted(access, first_target, is_full_verification=False)
    served_after_first = dict(access.served_key_by_port)
    first_pins = _endpoint_pin_map(first_target)

    # The second device: same synced user-origin pins (the record channel), its
    # own state dir, no marker.
    second_target = _make_second_device(tmp_path, first_target)
    ensure_adopted(access, second_target, is_full_verification=False)

    assert access.served_key_by_port == served_after_first
    assert _endpoint_pin_map(second_target) == first_pins
    assert load_adoption_marker(second_target.host_state_dir) is not None


def test_rotation_keeps_the_other_endpoints_bootstrap_pin_until_adoption_completes(tmp_path: Path) -> None:
    """Between the VM and container rotations the container still serves its bake-time
    key, so its bootstrap pin must survive the VM rotation: the bootstrap sweep happens
    only once both endpoints are verified. The rotation also records the endpoints."""
    host_id = HostId()
    target = _make_target(tmp_path, host_id)
    _pin_bootstrap_keys(target)
    access = _make_unadopted_access()

    new_vm_key = rotate_endpoint_host_key(access, target, AdoptionEndpointKind.VM)

    assert _endpoint_pin_map(target) == {
        _VM_PORT: (new_vm_key, HostKeyOrigin.USER),
        _CONTAINER_PORT: (_OLD_CONTAINER_HOST_KEY, HostKeyOrigin.BOOTSTRAP),
    }
    assert load_bound_endpoints(target.host_state_dir) == BoundEndpoints(
        vps_address=target.address, ssh_port=_VM_PORT, container_ssh_port=_CONTAINER_PORT
    )


def test_rerotation_replaces_the_previous_user_key_at_the_endpoint(tmp_path: Path) -> None:
    host_id = HostId()
    target, access = _adopt_fresh_slice(tmp_path, host_id)
    first_vm_key = _endpoint_pin_map(target)[_VM_PORT][0]

    second_vm_key = rotate_endpoint_host_key(access, target, AdoptionEndpointKind.VM)

    assert second_vm_key != first_vm_key
    assert first_vm_key not in target.known_hosts_path.read_text()
    assert _endpoint_pin_map(target)[_VM_PORT] == (second_vm_key, HostKeyOrigin.USER)


def test_full_verification_sweeps_bootstrap_pins_of_a_host_adopted_by_an_older_client(tmp_path: Path) -> None:
    """The bootstrap sweep runs after any verification, so a host whose bootstrap pins
    an older client left at stale endpoints is cleaned on its next verification."""
    host_id = HostId()
    target, access = _adopt_fresh_slice(tmp_path, host_id)
    add_host_to_known_hosts(target.known_hosts_path, target.address, 29999, _OLD_VM_HOST_KEY, host_id=host_id)
    invalidate_adoption_verification(target.host_state_dir)

    ensure_adopted(access, target, is_full_verification=True)

    record = load_host_key_record(target.known_hosts_path, host_id)
    assert record is not None
    assert all(pin.origin is HostKeyOrigin.USER for pin in record.pins)
    assert {pin.port for pin in record.pins} == {_VM_PORT, _CONTAINER_PORT}


# =============================================================================
# rebind_host_key_pins_to_endpoints
# =============================================================================


def test_rebind_moves_both_pins_to_the_new_endpoints_and_records_them(tmp_path: Path) -> None:
    """A restore at fresh ports (driven by anyone: this client, an admin, a rollback)
    is reachable without a client-side start: the pins follow the endpoints with
    their origins intact, and the old endpoints are gone."""
    host_id = HostId()
    target, access = _adopt_fresh_slice(tmp_path, host_id)
    vm_key, container_key = (access.served_key_by_port[port] for port in (_VM_PORT, _CONTAINER_PORT))

    _rebind(target, "198.51.100.9", 23010, 23011)

    assert load_pins_by_endpoint(target.known_hosts_path, host_id) == {
        ("198.51.100.9", 23010): (vm_key, HostKeyOrigin.USER),
        ("198.51.100.9", 23011): (container_key, HostKeyOrigin.USER),
    }
    assert load_bound_endpoints(target.host_state_dir) == BoundEndpoints(
        vps_address="198.51.100.9", ssh_port=23010, container_ssh_port=23011
    )
    assert target.address not in target.known_hosts_path.read_text()


@pytest.mark.parametrize(
    ("new_vm_port", "new_container_port"),
    [
        # A same-box restore can hand the host a new VM port equal to its old
        # container port (both pairs come from the box's first-free-port picker).
        (_CONTAINER_PORT, _CONTAINER_PORT + 1),
        # The mirror image: the new container port equals the old VM port.
        (_VM_PORT - 1, _VM_PORT),
    ],
)
def test_rebind_survives_a_new_port_reusing_the_other_endpoints_old_port(
    tmp_path: Path, new_vm_port: int, new_container_port: int
) -> None:
    """The moves must be ordered so neither evicts the other's not-yet-moved pin (a move
    clears whatever sits at its destination) -- either order mistake would strand an
    adopted host on wrong pins."""
    host_id = HostId()
    target, access = _adopt_fresh_slice(tmp_path, host_id)
    vm_key, container_key = (access.served_key_by_port[port] for port in (_VM_PORT, _CONTAINER_PORT))

    _rebind(target, target.address, new_vm_port, new_container_port)

    assert load_pins_by_endpoint(target.known_hosts_path, host_id) == {
        (target.address, new_vm_port): (vm_key, HostKeyOrigin.USER),
        (target.address, new_container_port): (container_key, HostKeyOrigin.USER),
    }


def test_rebind_is_a_noop_while_the_endpoints_are_unchanged(tmp_path: Path) -> None:
    host_id = HostId()
    target, _access = _adopt_fresh_slice(tmp_path, host_id)
    rendered_before = target.known_hosts_path.read_text()
    pins_before = load_current_host_key_pins(target.known_hosts_path)

    _rebind(target, target.address, _VM_PORT, _CONTAINER_PORT)

    assert target.known_hosts_path.read_text() == rendered_before
    assert load_current_host_key_pins(target.known_hosts_path) == pins_before


def test_rebind_seeds_the_record_from_pins_already_at_the_current_endpoints(tmp_path: Path) -> None:
    """A second device that synced the record after the last restore holds pins at the
    current endpoints and no record of them: nothing moves, the endpoints are recorded."""
    host_id = HostId()
    first_target, _access = _adopt_fresh_slice(tmp_path / "first", host_id)
    second_target = _make_second_device(tmp_path, first_target)

    _rebind(second_target, second_target.address, _VM_PORT, _CONTAINER_PORT)

    assert load_pins_by_endpoint(second_target.known_hosts_path, host_id) == load_pins_by_endpoint(
        first_target.known_hosts_path, host_id
    )
    assert load_bound_endpoints(second_target.host_state_dir) == BoundEndpoints(
        vps_address=second_target.address, ssh_port=_VM_PORT, container_ssh_port=_CONTAINER_PORT
    )


def test_rebind_seeds_the_record_by_port_order_when_the_synced_pins_predate_a_relocation(tmp_path: Path) -> None:
    """A second device whose synced record still names the pre-restore ports has no way
    to tell the VM pin from the container pin except port order (the box picker
    reserves the VM port below the container port)."""
    host_id = HostId()
    first_target, access = _adopt_fresh_slice(tmp_path / "first", host_id)
    vm_key, container_key = (access.served_key_by_port[port] for port in (_VM_PORT, _CONTAINER_PORT))
    second_target = _make_second_device(tmp_path, first_target)

    _rebind(second_target, "198.51.100.9", 23010, 23011)

    assert load_pins_by_endpoint(second_target.known_hosts_path, host_id) == {
        ("198.51.100.9", 23010): (vm_key, HostKeyOrigin.USER),
        ("198.51.100.9", 23011): (container_key, HostKeyOrigin.USER),
    }
    assert load_bound_endpoints(second_target.host_state_dir) == BoundEndpoints(
        vps_address="198.51.100.9", ssh_port=23010, container_ssh_port=23011
    )


def test_rebind_prefers_synced_user_pins_at_the_current_endpoints_over_a_stale_record(tmp_path: Path) -> None:
    """A sibling device relocated the host, rotated a key, and pushed the record before
    this device saw the new coordinates: the synced pins at the current endpoints are
    the newer trust, so they stay and the pins at the recorded (old) endpoints go."""
    host_id = HostId()
    target, access = _adopt_fresh_slice(tmp_path, host_id)
    stale_vm_key = access.served_key_by_port[_VM_PORT]
    container_key = access.served_key_by_port[_CONTAINER_PORT]
    add_host_to_known_hosts(
        target.known_hosts_path,
        target.address,
        23010,
        "ssh-ed25519 AAAAROTATED sibling",
        host_id=host_id,
        origin=HostKeyOrigin.USER,
    )
    add_host_to_known_hosts(
        target.known_hosts_path, target.address, 23011, container_key, host_id=host_id, origin=HostKeyOrigin.USER
    )

    _rebind(target, target.address, 23010, 23011)

    assert load_pins_by_endpoint(target.known_hosts_path, host_id) == {
        (target.address, 23010): ("ssh-ed25519 AAAAROTATED sibling", HostKeyOrigin.USER),
        (target.address, 23011): (container_key, HostKeyOrigin.USER),
    }
    assert stale_vm_key not in target.known_hosts_path.read_text()
    assert load_bound_endpoints(target.host_state_dir) == BoundEndpoints(
        vps_address=target.address, ssh_port=23010, container_ssh_port=23011
    )


def test_rebind_reseeds_a_record_naming_endpoints_the_host_has_nothing_pinned_at(tmp_path: Path) -> None:
    """A 0.5.2 client sharing the profile moves the pins itself (from lease.json) and
    leaves bound_endpoints.json behind; on the next restore the stale record must not
    be trusted, or the moves would no-op and every user pin would be dropped."""
    host_id = HostId()
    target, access = _adopt_fresh_slice(tmp_path, host_id)
    vm_key, container_key = (access.served_key_by_port[port] for port in (_VM_PORT, _CONTAINER_PORT))
    for old_port, new_port in ((_VM_PORT, 23010), (_CONTAINER_PORT, 23011)):
        move_host_endpoint_pins(target.known_hosts_path, host_id, target.address, old_port, target.address, new_port)

    _rebind(target, "198.51.100.9", 24010, 24011)

    assert load_pins_by_endpoint(target.known_hosts_path, host_id) == {
        ("198.51.100.9", 24010): (vm_key, HostKeyOrigin.USER),
        ("198.51.100.9", 24011): (container_key, HostKeyOrigin.USER),
    }
    assert load_bound_endpoints(target.host_state_dir) == BoundEndpoints(
        vps_address="198.51.100.9", ssh_port=24010, container_ssh_port=24011
    )


@pytest.mark.parametrize(
    ("new_address", "new_vm_port", "new_container_port"),
    [
        ("198.51.100.9", 24010, 24011),
        # The host can come back to the very ports the record names (a same-box
        # first-free-port scan), which must not pass for the pins being in place.
        (_ADDRESS, _VM_PORT, _CONTAINER_PORT),
    ],
)
def test_rebind_reseeds_a_record_whose_endpoints_hold_only_bootstrap_pins(
    tmp_path: Path, new_address: str, new_vm_port: int, new_container_port: int
) -> None:
    """This device leased the host (record and bootstrap pins at the lease endpoints) but
    never adopted it; a sibling adopted it, a restore moved it, and the sibling's push
    synced the user pins here; then another restore happened before this device
    connected. Bootstrap pins need no relocation (the connector key is re-pinned at the
    current endpoints anyway), so the synced user pins are what must follow the host."""
    host_id = HostId()
    target = _make_target(tmp_path, host_id)
    _pin_bootstrap_keys(target)
    record_bound_endpoints(
        target.host_state_dir,
        BoundEndpoints(vps_address=target.address, ssh_port=_VM_PORT, container_ssh_port=_CONTAINER_PORT),
    )
    sibling_vm_key = "ssh-ed25519 AAAASIBVM sibling-vm"
    sibling_container_key = "ssh-ed25519 AAAASIBC sibling-container"
    for port, key in ((23010, sibling_vm_key), (23011, sibling_container_key)):
        add_host_to_known_hosts(
            target.known_hosts_path, target.address, port, key, host_id=host_id, origin=HostKeyOrigin.USER
        )

    _rebind(target, new_address, new_vm_port, new_container_port)

    assert load_pins_by_endpoint(target.known_hosts_path, host_id) == {
        (new_address, new_vm_port): (sibling_vm_key, HostKeyOrigin.USER),
        (new_address, new_container_port): (sibling_container_key, HostKeyOrigin.USER),
    }
    assert load_bound_endpoints(target.host_state_dir) == BoundEndpoints(
        vps_address=new_address, ssh_port=new_vm_port, container_ssh_port=new_container_port
    )


def test_rebind_leaves_an_unadopted_host_alone(tmp_path: Path) -> None:
    """Bootstrap-only pins carry no role information and are re-pinned from the
    connector by the caller; nothing moves and no record is written."""
    host_id = HostId()
    target = _make_target(tmp_path, host_id)
    _pin_bootstrap_keys(target)
    pins_before = load_pins_by_endpoint(target.known_hosts_path, host_id)

    _rebind(target, "198.51.100.9", 23010, 23011)

    assert load_pins_by_endpoint(target.known_hosts_path, host_id) == pins_before
    assert load_bound_endpoints(target.host_state_dir) is None
    absent_target = _make_target(tmp_path / "absent", host_id)
    _rebind(absent_target, "198.51.100.9", 23010, 23011)
    assert not absent_target.known_hosts_path.exists()


def test_rebind_refuses_to_guess_roles_from_more_than_two_user_endpoints(tmp_path: Path) -> None:
    host_id = HostId()
    target, _access = _adopt_fresh_slice(tmp_path, host_id)
    bound_endpoints_path(target.host_state_dir).unlink()
    add_host_to_known_hosts(
        target.known_hosts_path, target.address, 29999, _OLD_VM_HOST_KEY, host_id=host_id, origin=HostKeyOrigin.USER
    )
    pins_before = load_pins_by_endpoint(target.known_hosts_path, host_id)

    with allow_warnings():
        _rebind(target, "198.51.100.9", 23010, 23011)

    assert load_pins_by_endpoint(target.known_hosts_path, host_id) == pins_before
    assert load_bound_endpoints(target.host_state_dir) is None


def test_rebind_treats_a_malformed_record_as_unrecorded(tmp_path: Path) -> None:
    host_id = HostId()
    target, access = _adopt_fresh_slice(tmp_path, host_id)
    vm_key = access.served_key_by_port[_VM_PORT]
    bound_endpoints_path(target.host_state_dir).write_text("{not json")

    with allow_warnings():
        _rebind(target, "198.51.100.9", 23010, 23011)

    assert load_pins_by_endpoint(target.known_hosts_path, host_id)[("198.51.100.9", 23010)] == (
        vm_key,
        HostKeyOrigin.USER,
    )
    assert load_bound_endpoints(target.host_state_dir) == BoundEndpoints(
        vps_address="198.51.100.9", ssh_port=23010, container_ssh_port=23011
    )


def test_record_bound_endpoints_round_trips(tmp_path: Path) -> None:
    endpoints = BoundEndpoints(vps_address="198.51.100.9", ssh_port=23010, container_ssh_port=23011)

    record_bound_endpoints(tmp_path, endpoints)

    assert load_bound_endpoints(tmp_path) == endpoints
    assert load_bound_endpoints(tmp_path / "absent") is None
