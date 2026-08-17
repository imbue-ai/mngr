import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from inline_snapshot import snapshot

from imbue.mngr.primitives import HostId
from imbue.mngr.providers.host_key_store import HostKeyOrigin
from imbue.mngr.providers.host_key_store import load_host_key_record
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr.providers.ssh_utils import save_ssh_keypair
from imbue.mngr_imbue_cloud.errors import AdoptionError
from imbue.mngr_imbue_cloud.errors import HostKeyDriftError
from imbue.mngr_imbue_cloud.interfaces import SliceReconcilerState
from imbue.mngr_imbue_cloud.providers.adoption import ADOPTION_SCHEMA_VERSION
from imbue.mngr_imbue_cloud.providers.adoption import AdoptionEndpointKind
from imbue.mngr_imbue_cloud.providers.adoption import SliceAdoptionTarget
from imbue.mngr_imbue_cloud.providers.adoption import ensure_adopted
from imbue.mngr_imbue_cloud.providers.adoption import invalidate_adoption_verification
from imbue.mngr_imbue_cloud.providers.adoption import is_slice_lease
from imbue.mngr_imbue_cloud.providers.adoption import load_adoption_marker
from imbue.mngr_imbue_cloud.providers.adoption import load_pending_host_key_rotation
from imbue.mngr_imbue_cloud.providers.adoption import parse_reconciler_state_output
from imbue.mngr_imbue_cloud.providers.adoption import remove_key_from_desired_authorized_keys
from imbue.mngr_imbue_cloud.providers.adoption import render_desired_authorized_keys
from imbue.mngr_imbue_cloud.providers.adoption import rotate_client_key
from imbue.mngr_imbue_cloud.providers.adoption import rotate_endpoint_host_key
from imbue.mngr_imbue_cloud.providers.mock_slice_vm_access_test import MockSliceVmAccess

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
        address="203.0.113.7",
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


def _endpoint_pin_map(target: SliceAdoptionTarget) -> dict[int, tuple[str, HostKeyOrigin]]:
    record = load_host_key_record(target.known_hosts_path, target.host_id)
    assert record is not None
    return {pin.port: (pin.public_key, pin.origin) for pin in record.pins}


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
    stdout = f"MNGR_RECONCILER_ENABLED=enabled\nMNGR_DESIRED_B64={encoded}\nMNGR_LIVE_MATCHES=1\n"
    state = parse_reconciler_state_output(stdout)
    assert state == SliceReconcilerState(
        is_unit_enabled=True, desired_authorized_keys=desired, is_live_matching_desired=True
    )


def test_parse_reconciler_state_output_reads_absent_desired_file() -> None:
    stdout = "MNGR_RECONCILER_ENABLED=unknown\nMNGR_DESIRED_B64=ABSENT\nMNGR_LIVE_MATCHES=0\n"
    state = parse_reconciler_state_output(stdout)
    assert state == SliceReconcilerState(
        is_unit_enabled=False, desired_authorized_keys=None, is_live_matching_desired=False
    )


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
    # Both endpoints serve fresh user-origin keys that are pinned in the store.
    pin_by_port = _endpoint_pin_map(target)
    for port in (_VM_PORT, _CONTAINER_PORT):
        pinned_key, origin = pin_by_port[port]
        assert origin is HostKeyOrigin.USER
        assert access.served_key_by_port[port] == pinned_key
        assert pinned_key not in (_OLD_VM_HOST_KEY, _OLD_CONTAINER_HOST_KEY)
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
    second_target = _make_target(tmp_path / "second", host_id)
    for port, (public_key, _origin) in first_pins.items():
        add_host_to_known_hosts(
            second_target.known_hosts_path,
            second_target.address,
            port,
            public_key,
            host_id=host_id,
            origin=HostKeyOrigin.USER,
        )
    ensure_adopted(access, second_target, is_full_verification=False)

    assert access.served_key_by_port == served_after_first
    assert _endpoint_pin_map(second_target) == first_pins
    assert load_adoption_marker(second_target.host_state_dir) is not None
