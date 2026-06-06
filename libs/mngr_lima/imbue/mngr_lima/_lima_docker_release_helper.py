"""End-to-end driver for the Lima docker-in-VM (is_host_in_docker) release test.

Invoked as a subprocess (via ``runuser``) from ``test_lima_docker_release.py``.
Lima refuses to run as root, so the release test installs Lima + qemu + a
non-root user as root, then re-enters this script under that user to drive
``LimaProviderInstance`` (with ``is_host_in_docker=True``) through the full
create / verify / snapshot / stop+start / destroy flow on a real Lima VM.

It pulls a small ``debian:bookworm-slim`` base image rather than building the
project Dockerfile, so the test exercises every production-only behaviour of
the mode (root outer SSH, btrfs subvolume + bind volume, snapshot helper IPC,
container-as-host SSH over the Lima-forwarded port, VM stop/start) without the
multi-minute image build.

Communicates via stdout: writes ``HELPER_RESULT: OK`` on success, otherwise
prints a Python traceback and exits non-zero.
"""

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.hosts.host import Host
from imbue.mngr.main import create_plugin_manager
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.testing import make_mngr_ctx
from imbue.mngr_lima.config import LimaProviderConfig
from imbue.mngr_lima.instance import LimaProviderInstance

_BASE_IMAGE = "debian:bookworm-slim"


def _build_provider(profile_dir: Path) -> tuple[LimaProviderInstance, ConcurrencyGroup]:
    cg = ConcurrencyGroup(name="lima-docker-release")
    cg.__enter__()
    config = LimaProviderConfig(
        host_dir=Path("/mngr"),
        is_host_in_docker=True,
        is_host_data_volume_exposed=False,
        host_data_disk_size="3GiB",
        default_image=_BASE_IMAGE,
        default_idle_timeout=3600,
        # Cold boot of a Debian cloud image, even with KVM, plus an in-VM
        # apt install of Docker, can take a few minutes.
        vm_start_timeout_seconds=1500.0,
        docker_install_timeout=900.0,
        container_ssh_connect_timeout=240.0,
    )
    pm = create_plugin_manager()
    mngr_config = MngrConfig.model_construct(
        prefix="mngr-",
        default_host_dir=Path("/mngr"),
        agent_types={},
        providers={"lima": config},
        plugins={},
    )
    ctx = make_mngr_ctx(mngr_config, pm, profile_dir, concurrency_group=cg)
    provider = LimaProviderInstance(
        name=ProviderInstanceName("lima"),
        host_dir=Path("/mngr"),
        mngr_ctx=ctx,
        config=config,
    )
    return provider, cg


def _verify_container_is_host(host: Host) -> None:
    """Prove that ssh lands inside the debian container (not the host VM) and host_dir is btrfs."""
    os_release = host.execute_idempotent_command("cat /etc/os-release")
    if "debian" not in os_release.stdout.lower():
        raise AssertionError(f"Expected to be inside the debian container, got: {os_release.stdout!r}")
    fstype = host.execute_idempotent_command(f"stat -fc %T {host.host_dir}")
    if fstype.stdout.strip() != "btrfs":
        raise AssertionError(f"Expected host_dir on btrfs, got stdout={fstype.stdout!r} stderr={fstype.stderr!r}")
    write_result = host.execute_idempotent_command(
        f"echo docker-canary > {host.host_dir}/canary.txt && cat {host.host_dir}/canary.txt"
    )
    if "docker-canary" not in write_result.stdout:
        raise AssertionError(
            f"Could not write host_dir: stdout={write_result.stdout!r} stderr={write_result.stderr!r}"
        )


def _verify_snapshot_helper(host: Host) -> None:
    """Drive the snapshot-helper IPC from inside the container and confirm a btrfs snapshot is produced."""
    request_id = f"release-{int(time.time())}"
    request_json = json.dumps({"request_id": request_id, "operation": "snapshot", "timestamp_iso": "release"})
    # Write request.json atomically (the helper watches for moved_to/close_write).
    write_cmd = (
        f"printf '%s' {json.dumps(request_json)} > /mngr-snapshot/request.json.tmp "
        "&& mv /mngr-snapshot/request.json.tmp /mngr-snapshot/request.json"
    )
    host.execute_idempotent_command(write_cmd)

    deadline = time.monotonic() + 60.0
    last_stdout = ""
    while time.monotonic() < deadline:
        result = host.execute_idempotent_command("cat /mngr-snapshot/result.json 2>/dev/null || true")
        last_stdout = result.stdout.strip()
        if last_stdout:
            payload = json.loads(last_stdout)
            if payload.get("request_id") == request_id:
                if payload.get("exit_code") != 0:
                    raise AssertionError(f"Snapshot helper reported failure: {payload}")
                break
        threading.Event().wait(timeout=2.0)
    else:
        raise AssertionError(f"Snapshot helper did not produce a matching result.json in time: {last_stdout!r}")

    # The read-only snapshot must be visible in the container at /mngr-snapshots/current.
    listing = host.execute_idempotent_command("ls /mngr-snapshots/current")
    if not listing.success:
        raise AssertionError(f"Snapshot not readable at /mngr-snapshots/current: stderr={listing.stderr!r}")
    canary_in_snapshot = host.execute_idempotent_command(
        "cat /mngr-snapshots/current/host_dir/canary.txt 2>/dev/null || true"
    )
    if "docker-canary" not in canary_in_snapshot.stdout:
        raise AssertionError(f"Snapshot did not capture the canary file: stdout={canary_in_snapshot.stdout!r}")


def _verify_persistence_across_restart(provider: LimaProviderInstance, host: Host) -> None:
    """Stop the whole VM, start it again, confirm the container is back and the canary survived."""
    provider.stop_host(host)
    host_after = provider.start_host(host.id)
    if not isinstance(host_after, Host):
        raise AssertionError(f"start_host returned non-Host: {type(host_after).__name__}")
    cat_result = host_after.execute_idempotent_command(f"cat {host_after.host_dir}/canary.txt")
    if "docker-canary" not in cat_result.stdout:
        raise AssertionError(f"canary.txt did not survive stop/start: stdout={cat_result.stdout!r}")
    os_release = host_after.execute_idempotent_command("cat /etc/os-release")
    if "debian" not in os_release.stdout.lower():
        raise AssertionError(f"After restart, not inside the container: {os_release.stdout!r}")


def main() -> int:
    if os.geteuid() == 0:
        print("HELPER_RESULT: FAIL (helper must run as non-root; Lima refuses root)", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="mngr-lima-docker-release-") as tmp:
        tmp_path = Path(tmp)
        profile_dir = tmp_path / "profile"
        profile_dir.mkdir()

        provider, cg = _build_provider(profile_dir)
        host_name = HostName("release-docker")

        try:
            host = provider.create_host(name=host_name)
            if not isinstance(host, Host):
                raise AssertionError(f"create_host returned non-Host: {type(host).__name__}")

            record = provider._host_store.read_host_record(host.id, use_cache=False)
            if record is None or record.config is None:
                raise AssertionError("HostRecord not persisted after create_host")
            if not record.config.is_host_in_docker:
                raise AssertionError("Expected is_host_in_docker=True on persisted record")
            if not record.config.container_name or record.config.container_host_port is None:
                raise AssertionError(f"container_name/container_host_port not persisted: {record.config}")

            _verify_container_is_host(host)
            _verify_snapshot_helper(host)
            _verify_persistence_across_restart(provider, host)

            provider.destroy_host(host.id)
            offline = provider.to_offline_host(host.id)
            provider.delete_host(offline)
            try:
                provider.get_host(host.id)
            except HostNotFoundError:
                pass
            else:
                raise AssertionError("get_host after delete_host should raise HostNotFoundError")
        finally:
            cg.__exit__(None, None, None)

    print("HELPER_RESULT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
