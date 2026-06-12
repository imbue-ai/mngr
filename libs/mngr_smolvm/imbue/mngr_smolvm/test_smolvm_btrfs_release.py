"""End-to-end release test for the smolvm btrfs host-data volume mode.

Drives ``SmolvmProviderInstance`` through a full create / verify /
stop+start / destroy cycle on a real smolvm machine with the btrfs data
disk layout (``is_host_data_volume_exposed=False``) -- the layout minds
workspaces use for in-guest snapshots.

The test needs hardware virtualization (/dev/kvm; smolvm has no emulation
fallback) and a smolvm build with btrfs data-disk support, which is not yet
publicly distributed. It therefore skips cleanly unless both are present,
and runs for real on developer machines: put a binary (or wrapper script
exporting SMOLVM_LIB_DIR / SMOLVM_AGENT_ROOTFS for a source checkout)
named exactly ``smolvm`` on PATH before starting pytest -- the resource
guard for the ``smolvm`` mark resolves the binary from PATH at session
start, so MNGR_SMOLVM_COMMAND alone is not sufficient.
"""

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_smolvm.config import SmolvmProviderConfig
from imbue.mngr_smolvm.instance import SmolvmProviderInstance

pytestmark = [pytest.mark.release, pytest.mark.smolvm]

_TEST_TIMEOUT_SECONDS = 900


def _smolvm_unavailable_reason() -> str | None:
    """Return why the e2e environment is unusable, or None if it is usable."""
    kvm = Path("/dev/kvm")
    if not kvm.exists():
        return "no /dev/kvm (smolvm has no emulation fallback)"
    if not os.access(kvm, os.R_OK | os.W_OK):
        return "/dev/kvm is not accessible to this user"
    smolvm_command = os.environ.get("MNGR_SMOLVM_COMMAND", "smolvm")
    if shutil.which(smolvm_command) is None:
        return f"smolvm binary not found ({smolvm_command}); set MNGR_SMOLVM_COMMAND"
    help_result = subprocess.run(
        [smolvm_command, "machine", "create", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if help_result.returncode != 0 or "--data-disk" not in help_result.stdout:
        return "installed smolvm build lacks --data-disk support"
    return None


@pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
def test_smolvm_btrfs_host_full_lifecycle(
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unavailable_reason = _smolvm_unavailable_reason()
    if unavailable_reason is not None:
        pytest.skip(unavailable_reason)

    # The shared test fixtures redirect HOME to a deep pytest temp dir, which
    # would push smolvm's per-machine vsock socket path past SUN_LEN (108
    # bytes). Point smolvm's XDG dirs at a short /tmp path instead; machine
    # data is removed by destroy/delete below and the dir is tiny.
    short_state_dir = Path(tempfile.mkdtemp(prefix="smolvm-rt-", dir="/tmp"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(short_state_dir / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(short_state_dir / "data"))

    config = SmolvmProviderConfig(
        smolvm_command=os.environ.get("MNGR_SMOLVM_COMMAND", "smolvm"),
        is_host_data_volume_exposed=False,
        host_data_disk_size_gb=2,
        default_idle_timeout=600,
    )
    provider = SmolvmProviderInstance(
        name=ProviderInstanceName("smolvm-release-test"),
        host_dir=Path("/mngr"),
        mngr_ctx=temp_mngr_ctx,
        config=config,
    )
    host_name = HostName(f"rt-{uuid.uuid4().hex}")

    host = provider.create_host(name=host_name)
    try:
        # host_dir is a btrfs mount with unprivileged subvolume management.
        mount_result = host.execute_idempotent_command("mount | grep ' /mngr '")
        assert "btrfs" in mount_result.stdout
        assert "user_subvol_rm_allowed" in mount_result.stdout

        # Subvolume + read-only snapshot round trip, then data persistence
        # across a stop/start cycle.
        marker = uuid.uuid4().hex
        snapshot_result = host.execute_idempotent_command(
            "btrfs subvolume create /mngr/ws"
            f" && echo {marker} > /mngr/ws/f"
            " && btrfs subvolume snapshot -r /mngr/ws /mngr/ws-snap"
            " && cat /mngr/ws-snap/f"
        )
        assert marker in snapshot_result.stdout

        provider.stop_host(host)
        restarted_host = provider.start_host(host.id)
        persisted_result = restarted_host.execute_idempotent_command("cat /mngr/ws/f /mngr/ws-snap/f")
        assert persisted_result.stdout.count(marker) == 2

        # The btrfs layout has no host-side volume directory.
        assert provider.get_volume_for_host(host.id) is None
    finally:
        provider.destroy_host(host.id)
        offline_host = provider.to_offline_host(host.id)
        provider.delete_host(offline_host)
        shutil.rmtree(short_state_dir, ignore_errors=True)
