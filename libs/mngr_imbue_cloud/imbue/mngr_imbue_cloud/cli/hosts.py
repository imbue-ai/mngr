"""`mngr imbue_cloud hosts ...` subcommands.

Lease creation goes through ``mngr create --provider imbue_cloud_<account>
--new-host -b <attr>=<val> ...``; the provider implementation issues the
lease, runs the SSH bootstrap, and returns a host that the standard mngr
create pipeline finishes adopting under the caller's chosen agent name.
These subcommands are listing + release + key-rotation helpers on top of
that flow.
"""

from pathlib import Path

import click
from loguru import logger

from imbue.mngr.primitives import HostId
from imbue.mngr.providers.host_key_store import load_host_key_record
from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import fail_with_json
from imbue.mngr_imbue_cloud.cli._common import get_default_host_dir
from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors
from imbue.mngr_imbue_cloud.cli._common import make_connector_client
from imbue.mngr_imbue_cloud.cli._common import make_session_store
from imbue.mngr_imbue_cloud.cli._common import resolve_account_or_active
from imbue.mngr_imbue_cloud.config import ImbueCloudProviderConfig
from imbue.mngr_imbue_cloud.config import get_active_profile_dir
from imbue.mngr_imbue_cloud.config import get_provider_state_dir
from imbue.mngr_imbue_cloud.connector.auth_helper import get_active_token
from imbue.mngr_imbue_cloud.errors import HostKeyDriftError
from imbue.mngr_imbue_cloud.providers.adoption import AdoptionEndpointKind
from imbue.mngr_imbue_cloud.providers.adoption import ParamikoSliceVmAccess
from imbue.mngr_imbue_cloud.providers.adoption import SliceAdoptionTarget
from imbue.mngr_imbue_cloud.providers.adoption import ensure_adopted
from imbue.mngr_imbue_cloud.providers.adoption import invalidate_adoption_verification
from imbue.mngr_imbue_cloud.providers.adoption import is_slice_lease
from imbue.mngr_imbue_cloud.providers.adoption import load_adoption_marker
from imbue.mngr_imbue_cloud.providers.adoption import rotate_client_key
from imbue.mngr_imbue_cloud.providers.adoption import rotate_endpoint_host_key
from imbue.mngr_imbue_cloud.wire_types import LeasedHostInfo


@click.group(name="hosts")
def hosts() -> None:
    """List and release leased pool hosts."""


@hosts.command(name="list")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def list_hosts(account: str | None, connector_url: str | None) -> None:
    """List all hosts currently leased by this account."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    leased = client.list_hosts(token)
    payload = [
        {
            "host_db_id": str(entry.host_db_id),
            "host_id": entry.host_id,
            "agent_id": entry.agent_id,
            "vps_address": entry.vps_address,
            "ssh_user": entry.ssh_user,
            "ssh_port": entry.ssh_port,
            "container_ssh_port": entry.container_ssh_port,
            "attributes": entry.attributes,
            "leased_at": entry.leased_at,
        }
        for entry in leased
    ]
    emit_json(payload)


@hosts.command(name="release")
@click.argument("host_db_id")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def release_host(host_db_id: str, account: str | None, connector_url: str | None) -> None:
    """Release a leased host back to the pool."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    is_released = client.release_host(token, host_db_id)
    if not is_released:
        fail_with_json("Connector returned non-success on release", error_class="ReleaseFailed")
    emit_json({"released": True, "host_db_id": host_db_id})


def _find_lease_by_ref(leases: list[LeasedHostInfo], host_ref: str) -> LeasedHostInfo | None:
    """Match a lease by mngr host id, lease db id, or friendly host name."""
    for entry in leases:
        if host_ref in (entry.host_id, str(entry.host_db_id), entry.host_name):
            return entry
    return None


def _find_host_state_dirs(host_id: str) -> list[Path]:
    """Locate the per-host state dir(s) holding this host's client keypair, across provider instances."""
    profile_dir = get_active_profile_dir(get_default_host_dir())
    state_root = get_provider_state_dir(profile_dir)
    if not state_root.is_dir():
        return []
    return sorted(
        instance_dir / "hosts" / host_id
        for instance_dir in state_root.iterdir()
        if instance_dir.is_dir()
        and instance_dir.name != "sessions"
        and (instance_dir / "hosts" / host_id / "ssh_key").exists()
        and (instance_dir / "hosts" / host_id / "ssh_key.pub").exists()
    )


@hosts.command(name="rotate")
@click.argument("host_ref")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def rotate_host_keys(host_ref: str, account: str | None, connector_url: str | None) -> None:
    """Rotate everything for one leased host: its client key and both endpoints' SSH host keys.

    HOST_REF is the workspace's mngr host id (host-<32hex>), the lease's
    host_db_id (UUID), or its friendly name. Runs from a machine that leased
    the host (its per-host client key must be on disk). Adopts the host first
    if it has not been adopted from this machine, updates the in-VM reconciler
    desired state, and records the new user-origin pins in the local host-key
    store; other devices converge when the synced workspace record is next
    pushed and pulled.
    """
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    lease = _find_lease_by_ref(client.list_hosts(token), host_ref)
    if lease is None:
        fail_with_json(f"No running lease matching {host_ref!r} on this account", error_class="HostNotFound")
    if not is_slice_lease(lease.container_ssh_port, ImbueCloudProviderConfig().container_ssh_port):
        fail_with_json(
            f"Host {lease.host_id} is not a slice; key rotation covers slice-backed hosts only",
            error_class="NotASlice",
        )

    state_dirs = _find_host_state_dirs(lease.host_id)
    if not state_dirs:
        fail_with_json(
            f"No local key material for host {lease.host_id}; rotate from the machine that leased it",
            error_class="NoLocalKeyMaterial",
        )
    if len(state_dirs) > 1:
        fail_with_json(
            f"Host {lease.host_id} has key material under multiple provider instances ({state_dirs}); "
            "clean up the stale one first",
            error_class="AmbiguousLocalKeyMaterial",
        )
    host_state_dir = state_dirs[0]

    host_id = HostId(lease.host_id)
    target = SliceAdoptionTarget(
        host_id=host_id,
        address=lease.vps_address,
        vm_port=lease.ssh_port,
        container_port=lease.container_ssh_port,
        host_state_dir=host_state_dir,
        known_hosts_path=host_state_dir / "known_hosts",
        client_public_key=(host_state_dir / "ssh_key.pub").read_text().strip(),
    )
    access = ParamikoSliceVmAccess(
        host_id=host_id,
        address=lease.vps_address,
        vm_port=lease.ssh_port,
        ssh_user=lease.ssh_user,
        private_key_path=host_state_dir / "ssh_key",
        known_hosts_path=target.known_hosts_path,
    )

    # An unadopted host is adopted here first, which already rotates both host
    # keys as part of taking ownership; an adopted host gets verified/healed
    # and then explicitly re-rotated. This explicit rotate is the user's
    # recovery tool, so the durable already-verified stamp is cleared first --
    # the verify/heal pass (reconciler reinstall included) must actually run.
    is_already_adopted = load_adoption_marker(host_state_dir) is not None
    invalidate_adoption_verification(host_state_dir)
    try:
        ensure_adopted(access, target, is_full_verification=True)
    except HostKeyDriftError as exc:
        # This explicit, user-initiated rotate is the recovery the drift error
        # points at, so proceed instead of dying with it: drift on an endpoint
        # is only detectable after the VM door was opened against its
        # user-origin pin, and the rotations below install fresh user-generated
        # keys through that authenticated channel, pinning them only once each
        # endpoint provably serves them -- the foreign-served key is
        # overwritten, never trusted. Passive connects (the provider's
        # ensure-adopted pass) keep refusing this state.
        logger.warning("Rotating host {} over a drifted endpoint: {}", lease.host_id, exc)
    if is_already_adopted:
        vm_host_key = rotate_endpoint_host_key(access, target, AdoptionEndpointKind.VM)
        container_host_key = rotate_endpoint_host_key(access, target, AdoptionEndpointKind.CONTAINER)
    else:
        record = load_host_key_record(target.known_hosts_path, host_id)
        pin_by_port = {pin.port: pin.public_key for pin in (record.pins if record is not None else ())}
        vm_host_key = pin_by_port.get(lease.ssh_port, "")
        container_host_key = pin_by_port.get(lease.container_ssh_port, "")
    new_client_public_key = rotate_client_key(access, target)
    emit_json(
        {
            "host_id": lease.host_id,
            "adopted": True,
            "client_public_key": new_client_public_key,
            "vm_host_public_key": vm_host_key,
            "container_host_public_key": container_host_key,
        }
    )


@hosts.command(name="enable-sharing")
@click.argument("host_ref")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def enable_sharing(host_ref: str, account: str | None, connector_url: str | None) -> None:
    """Enable web access (server-side share bring-up) for a leased host.

    HOST_REF is the lease's host_db_id (UUID), or the workspace's mngr host id
    (host-<hex>), which is resolved against the account's leases first.
    """
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    host_db_id = host_ref
    if host_ref.startswith("host-"):
        leased = client.list_hosts(token)
        match = next((entry for entry in leased if entry.host_id == host_ref), None)
        if match is None:
            fail_with_json(f"No lease with host id {host_ref} on this account", error_class="HostNotFound")
        host_db_id = str(match.host_db_id)
    result = client.enable_host_sharing(token, host_db_id)
    emit_json(result)
