"""Tests for the provider-agnostic host-state-store / host_dir-backend strategy objects."""

from imbue.mngr.primitives import HostId
from imbue.mngr_vps_docker.host_state_store import NullHostDirBackend


def test_null_host_dir_backend_is_no_op() -> None:
    """The fallback host_dir backend offers nothing: no identity, no volume, and no-op syncs.

    This is the half of the select-once strategy a provider uses when offline
    host_dir is off or no state bucket exists, so every method must degrade
    silently rather than raise.
    """
    backend = NullHostDirBackend()
    host_id = HostId.generate()
    assert backend.create_identity() is None
    assert backend.volume_reference(host_id) is None
    assert backend.volume(host_id) is None
    # The sync hooks are no-ops that must not raise.
    backend.install_sync(host_id=host_id, vps_ip="203.0.113.4")
    backend.trigger_final_sync(host_id, "203.0.113.4")
