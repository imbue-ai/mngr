"""End-to-end driver for the Lima btrfs release test.

Invoked as a subprocess (via `runuser`) from `test_lima_btrfs_release.py`.
Lima refuses to run as root, so the release test installs Lima + qemu + a
non-root user as root, then re-enters this script under that user to drive
``LimaProviderInstance`` through the full create / verify / stop+start /
destroy flow.

Communicates via stdout: writes ``HELPER_RESULT: OK`` on success, otherwise
prints a Python traceback and exits non-zero.
"""

import json
import os
import sys
import tempfile
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
from imbue.mngr_lima.errors import LimaCommandError
from imbue.mngr_lima.instance import LimaProviderInstance
from imbue.mngr_lima.limactl import limactl_disk_delete

# Lima YAML override merged in via the build_args path: forces qemu+TCG
# (no KVM in modal sandboxes) and keeps the VM cheap. additionalDisks,
# mounts, and provision are left to the base (mngr-generated) YAML.
_QEMU64_OVERRIDE_YAML = """\
vmType: qemu
cpus: 2
memory: 2GiB
disk: 10GiB
mountType: 9p
cpuType:
  x86_64: qemu64
networks: []
"""


def _build_provider(profile_dir: Path) -> tuple[LimaProviderInstance, ConcurrencyGroup]:
    cg = ConcurrencyGroup(name="lima-btrfs-release")
    cg.__enter__()
    # host_dir is the IN-VM canonical path for mngr state -- the same
    # default (/mngr) every other mngr-on-lima install uses. The helper's
    # tempfile.TemporaryDirectory lives on the *host*; we use it only for
    # the per-provider profile dir (where the host-side host record + ssh
    # keys live), never as host_dir.
    config = LimaProviderConfig(
        host_dir=Path("/mngr"),
        is_host_data_volume_exposed=False,
        # Small disk so the modal sandbox finishes mkfs quickly.
        host_data_disk_size="2GiB",
        default_idle_timeout=3600,
        # Modal sandboxes have no /dev/kvm so qemu runs in TCG (software
        # emulation). Cold boot of a Debian cloud image under TCG is
        # ~10-15 min; the default 600s is for KVM-accelerated boots.
        vm_start_timeout_seconds=1500.0,
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


def _limactl_disk_list(cg: ConcurrencyGroup) -> list[dict[str, object]]:
    """Return raw `limactl disk list --json` output as a list of dicts."""
    result = cg.run_process_to_completion(["limactl", "disk", "list", "--json"], timeout=30.0)
    disks: list[dict[str, object]] = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        disks.append(json.loads(line))
    return disks


def _verify_btrfs_layout(host: Host) -> None:
    """Run shell checks inside the VM proving host_dir is btrfs and writable."""
    stat_result = host.execute_idempotent_command(f"stat -fc %T {host.host_dir}")
    if stat_result.stdout.strip() != "btrfs":
        raise AssertionError(
            f"Expected host_dir to be btrfs, got stdout={stat_result.stdout!r} stderr={stat_result.stderr!r}"
        )
    write_result = host.execute_idempotent_command(
        f"echo release-canary > {host.host_dir}/canary.txt && cat {host.host_dir}/canary.txt"
    )
    if "release-canary" not in write_result.stdout:
        raise AssertionError(
            f"Could not write to host_dir: stdout={write_result.stdout!r} stderr={write_result.stderr!r}"
        )
    btrfs_mounts = host.execute_idempotent_command("mount | grep -c 'type btrfs' || true")
    if btrfs_mounts.stdout.strip() == "0":
        raise AssertionError(
            f"No btrfs mount inside VM: stdout={btrfs_mounts.stdout!r} stderr={btrfs_mounts.stderr!r}"
        )


def _verify_persistence_across_restart(provider: LimaProviderInstance, host: Host) -> None:
    """Stop+start the VM, confirm canary.txt survives and host_dir is still btrfs."""
    provider.stop_host(host)
    host_after = provider.start_host(host.id)
    if not isinstance(host_after, Host):
        raise AssertionError(f"start_host returned non-Host: {type(host_after).__name__}")
    cat_result = host_after.execute_idempotent_command(f"cat {host_after.host_dir}/canary.txt")
    if "release-canary" not in cat_result.stdout:
        raise AssertionError(
            f"canary.txt did not survive stop/start: stdout={cat_result.stdout!r} stderr={cat_result.stderr!r}"
        )
    stat_after = host_after.execute_idempotent_command(f"stat -fc %T {host_after.host_dir}")
    if stat_after.stdout.strip() != "btrfs":
        raise AssertionError(f"After restart, host_dir not btrfs: {stat_after.stdout!r}")


def main() -> int:
    if os.geteuid() == 0:
        print("HELPER_RESULT: FAIL (helper must run as non-root; Lima refuses root)", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="mngr-lima-release-") as tmp:
        tmp_path = Path(tmp)
        profile_dir = tmp_path / "profile"
        profile_dir.mkdir()

        provider, cg = _build_provider(profile_dir)
        host_name = HostName("release-btrfs")
        override_yaml_path = tmp_path / "qemu64-override.yaml"
        override_yaml_path.write_text(_QEMU64_OVERRIDE_YAML)

        try:
            host = provider.create_host(
                name=host_name,
                build_args=(f"--file={override_yaml_path}",),
                start_args=("--timeout", "20m0s"),
            )
            if not isinstance(host, Host):
                raise AssertionError(f"create_host returned non-Host: {type(host).__name__}")

            # The persisted record locks in our btrfs decision and the disk name.
            # Reaching into _host_store is intentional: this release test is
            # the canonical confirmation that the new fields land on disk.
            record = provider._host_store.read_host_record(host.id, use_cache=False)
            if record is None or record.config is None:
                raise AssertionError("HostRecord not persisted after create_host")
            if record.config.is_host_data_volume_exposed is not False:
                raise AssertionError(
                    "Expected is_host_data_volume_exposed=False on record, "
                    f"got {record.config.is_host_data_volume_exposed}"
                )
            disk_name = record.config.host_data_disk_name
            if not disk_name:
                raise AssertionError("host_data_disk_name not persisted on record")
            disk_names_before = {d.get("name") for d in _limactl_disk_list(cg)}
            if disk_name not in disk_names_before:
                raise AssertionError(f"Created disk {disk_name} not in limactl disk list: {disk_names_before}")

            _verify_btrfs_layout(host)
            _verify_persistence_across_restart(provider, host)

            # get_volume_for_host must return None in btrfs mode.
            if provider.get_volume_for_host(host.id) is not None:
                raise AssertionError("get_volume_for_host should return None for btrfs-mode host")

            # destroy_host removes the VM AND the named disk.
            provider.destroy_host(host.id)
            disk_names_after = {d.get("name") for d in _limactl_disk_list(cg)}
            if disk_name in disk_names_after:
                raise AssertionError(f"Disk {disk_name} still present after destroy_host: {disk_names_after}")

            # delete_host removes records and tolerates the disk already being gone.
            offline = provider.to_offline_host(host.id)
            provider.delete_host(offline)
            try:
                provider.get_host(host.id)
            except HostNotFoundError:
                pass
            else:
                raise AssertionError("get_host after delete_host should raise HostNotFoundError")

            # Best-effort: clean up any leftover named disk (shouldn't be needed
            # since destroy_host already removed it; tolerates "not found").
            try:
                limactl_disk_delete(cg, disk_name, force=True)
            except LimaCommandError:
                pass

        finally:
            cg.__exit__(None, None, None)

    print("HELPER_RESULT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
