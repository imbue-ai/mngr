import os
import subprocess
import time
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml
from inline_snapshot import snapshot

from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import GEN2_CIDATA_FILE_NAMES
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import Gen2SliceCidata
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import build_qemu_slice_meta_data
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import build_qemu_slice_network_config
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import build_qemu_slice_user_data
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import render_guest_container_meminfo_publisher_script
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import render_guest_container_memory_script
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import render_guest_data_disk_selection_lines
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_MAC_PLACEHOLDER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_ORDINAL_PLACEHOLDER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_VM_IP_PLACEHOLDER


def _parse_user_data(user_data: str) -> dict[str, Any]:
    """The cloud-config mapping cloud-init will see for a rendered NoCloud ``user-data``."""
    return yaml.safe_load(user_data)


def test_user_data_installs_the_pinned_host_key_and_never_regenerates() -> None:
    user_data = build_qemu_slice_user_data(
        host_dir="/home/user/.mngr",
        host_private_key_pem="-----BEGIN OPENSSH PRIVATE KEY-----\nkey\n-----END OPENSSH PRIVATE KEY-----\n",
        host_public_key_openssh="ssh-ed25519 AAAAhost",
        trusted_user_ca_public_key="ssh-ed25519 AAAAtierca minds-dev-ca",
    )
    assert user_data.startswith("#cloud-config\n")
    parsed = _parse_user_data(user_data)
    # The pre-generated ed25519 key is installed verbatim; no other key type is
    # ever generated, and nothing deletes it on later boots.
    assert parsed["ssh_keys"]["ed25519_public"] == "ssh-ed25519 AAAAhost"
    assert parsed["ssh_genkeytypes"] == ["ed25519"]
    assert parsed["ssh_deletekeys"] is False
    # Management connections share few source addresses (see the renderer docstring).
    sshd_config = parsed["write_files"][0]["content"]
    assert "PerSourcePenalties no" in sshd_config
    assert "MaxStartups 100:30:200" in sshd_config


def test_user_data_trusts_the_tier_ca_for_the_vm_principal_and_needs_no_static_root_key() -> None:
    user_data = build_qemu_slice_user_data(
        host_dir="/home/user/.mngr",
        host_private_key_pem="pem",
        host_public_key_openssh="ssh-ed25519 AAAAhost",
        trusted_user_ca_public_key="ssh-ed25519 AAAAtierca minds-dev-ca",
    )
    parsed = _parse_user_data(user_data)
    # Omitted, not empty: cloud-config's schema rejects an empty list and marks
    # the guest degraded.
    assert parsed["users"] == [{"name": "root"}]
    content_by_path = {entry["path"]: entry["content"] for entry in parsed["write_files"]}
    assert content_by_path["/etc/ssh/mngr_user_ca.pub"] == "ssh-ed25519 AAAAtierca minds-dev-ca\n"
    assert content_by_path["/etc/ssh/sshd_config.d/61-mngr-user-ca.conf"] == (
        "TrustedUserCAKeys /etc/ssh/mngr_user_ca.pub\nAuthorizedPrincipalsFile /etc/ssh/principals/%u\n"
        "PasswordAuthentication no\nKbdInteractiveAuthentication no\n"
    )
    # Only the VM principal opens root: a connector or analytics certificate
    # carries it, an operator one too, but a container-only certificate does not.
    assert content_by_path["/etc/ssh/principals/root"] == "mngr-vm\n"


def test_user_data_installs_the_every_boot_units() -> None:
    user_data = build_qemu_slice_user_data(
        host_dir="/home/user/.mngr",
        host_private_key_pem="pem",
        host_public_key_openssh="ssh-ed25519 AAAAhost",
        trusted_user_ca_public_key="ssh-ed25519 AAAAtierca minds-dev-ca",
    )
    parsed = _parse_user_data(user_data)
    content_by_path = {entry["path"]: entry["content"] for entry in parsed["write_files"]}
    # The data-filesystem grow runs before docker sees the mount and is a
    # no-op until the first-boot script has mounted the disk.
    grow_unit = content_by_path["/etc/systemd/system/mngr-grow-data-fs.service"]
    assert "Before=docker.service" in grow_unit
    grow_script = content_by_path["/usr/local/sbin/mngr-grow-data-fs.sh"]
    assert 'btrfs filesystem resize max "$mount_point"' in grow_script
    # The host quota (everything the agent host writes) is re-derived from
    # the grown filesystem: the disk minus the 4 GiB system reserve.
    assert 'btrfs qgroup limit "$(( fs_bytes - 4 * 1024 * 1024 * 1024 ))" 1/0 "$mount_point"' in grow_script
    # First boot enables quotas, creates both engine roots and containerd's
    # overlayfs snapshotter (where image + container layers live) as a
    # subvolume inside the quota group, and sizes the limit before the engines start.
    firstboot = content_by_path["/usr/local/sbin/mngr-slice-firstboot.sh"]
    assert "btrfs quota enable --simple /mnt/mngr-data" in firstboot
    assert "btrfs subvolume create /mnt/mngr-data/docker" in firstboot
    assert "/mnt/mngr-data/containerd/io.containerd.snapshotter.v1.overlayfs" in firstboot
    assert "/mnt/mngr-data/containerd/io.containerd.content.v1.content" in firstboot
    assert 'btrfs subvolume create -i 1/0 "$quota_subvolume"' in firstboot
    assert firstboot.index("/usr/local/sbin/mngr-grow-data-fs.sh") < firstboot.index(
        "systemctl restart containerd docker"
    )
    # Both engines are stopped and their stray root-filesystem roots removed before
    # the data disk is mounted over them, and (re)started only once the disk is up.
    assert firstboot.index("systemctl stop docker.socket docker.service containerd.service") < firstboot.index(
        "mkfs.btrfs -f -L"
    )
    assert firstboot.index("rm -rf /mnt/mngr-data/docker /mnt/mngr-data/containerd") < firstboot.index(
        "mkfs.btrfs -f -L"
    )
    assert "for mount_point in /mnt/mngr-data; do" in grow_script
    # A transplanted data disk carries a partition table, which the qcow2
    # grow leaves untouched -- the partition must grow before the filesystem.
    assert 'growpart "/dev/$parent_disk" "$partition_number"' in grow_script
    # The container-memory reconciler runs after dockerd and before the
    # workspace's autostart, so the container starts under the cap it sets.
    memory_unit = content_by_path["/etc/systemd/system/mngr-reconcile-container-memory.service"]
    assert "After=docker.service" in memory_unit
    assert "Before=minds-autostart.service" in memory_unit
    assert "/usr/local/sbin/mngr-reconcile-container-memory.sh" in content_by_path
    # The meminfo publisher is a long-running service that docker's restarts do not end.
    publisher_unit = content_by_path["/etc/systemd/system/mngr-publish-container-meminfo.service"]
    assert "Restart=always" in publisher_unit
    assert "/usr/local/sbin/mngr-publish-container-meminfo.sh" in content_by_path
    # All three units are enabled and first-run by cloud-init.
    enable_command = " ".join(parsed["runcmd"][-1])
    assert (
        "systemctl enable --now mngr-grow-data-fs.service mngr-reconcile-container-memory.service "
        "mngr-publish-container-meminfo.service" in enable_command
    )


def test_meta_data_has_a_stable_instance_id_and_a_valid_hostname() -> None:
    # The stable instance-id is the load-bearing difference from lima: cloud-init
    # must never replay on later boots. Underscored env names must still yield a
    # valid hostname.
    assert build_qemu_slice_meta_data("mngr-slice-dev-a_b-abc123") == snapshot(
        "instance-id: mngr-slice-dev-a_b-abc123\nlocal-hostname: mngr-slice-dev-a-b-abc123\n"
    )


def test_cidata_files_are_keyed_by_the_names_the_meta_tar_carries() -> None:
    cidata = Gen2SliceCidata(user_data="u", meta_data="m", network_config="n")
    # The supplied-cidata reserve writes exactly the files the meta-tar reserve
    # copies, in the same order.
    assert tuple(cidata.content_by_file_name()) == GEN2_CIDATA_FILE_NAMES
    assert tuple(cidata.content_by_file_name().values()) == ("u", "m", "n")


def test_network_config_is_placement_free_dhcp_on_the_single_virtio_nic() -> None:
    network_config = build_qemu_slice_network_config()
    parsed = yaml.safe_load(network_config)
    (nic,) = parsed["ethernets"].values()
    # The NIC is matched by driver, never by MAC: the MAC is ordinal-derived
    # and changes when a restore lands on another ordinal, while this file is
    # copied verbatim from the artifact.
    assert nic["match"] == {"driver": "virtio_net"}
    assert nic["dhcp4"] is True
    # No address, route, or resolver is baked in -- the box's DHCP server on
    # the tap supplies them per placement. No rename either: a set-name never
    # took effect on the trixie guest and left the NIC unconfigured.
    assert "addresses" not in nic
    assert "routes" not in nic
    assert "nameservers" not in nic
    assert "set-name" not in nic
    for placeholder in (GEN2_MAC_PLACEHOLDER, GEN2_VM_IP_PLACEHOLDER, GEN2_ORDINAL_PLACEHOLDER):
        assert placeholder not in network_config
    assert network_config == snapshot(
        "version: 2\nethernets:\n  slice-nic:\n    match:\n      driver: virtio_net\n    dhcp4: true\n"
    )


def _write_lsblk_stub(bin_dir: Path, *, disks: Mapping[str, tuple[str, str]]) -> None:
    """A stub lsblk answering the exact queries the disk selection issues.

    ``disks`` maps a device name to its ``(RO, FSTYPE)`` columns; the root
    filesystem is always ``/dev/vda1`` on ``vda``.
    """
    ro_cases = "\n".join(f'    "-dn -o RO /dev/{name}") echo "{ro}" ;;' for name, (ro, _) in disks.items())
    fstype_cases = "\n".join(
        f'    "-dn -o FSTYPE /dev/{name}") echo "{fstype}" ;;' for name, (_, fstype) in disks.items()
    )
    table = "\n".join(f"{name} disk" for name in disks)
    stub = bin_dir / "lsblk"
    stub.write_text(
        "#!/bin/bash\n"
        'case "$*" in\n'
        '    "-no PKNAME /dev/vda1") echo vda ;;\n'
        f'    "-dn -o NAME,TYPE") printf \'%s\\n\' "{table}" ;;\n'
        f"{ro_cases}\n"
        f"{fstype_cases}\n"
        '    *) echo "unexpected lsblk args: $*" >&2; exit 99 ;;\n'
        "esac\n"
    )
    stub.chmod(0o755)


def _run_data_disk_selection(bin_dir: Path, *, labeled_device: str | None) -> subprocess.CompletedProcess[str]:
    findmnt = bin_dir / "findmnt"
    findmnt.write_text("#!/bin/bash\necho /dev/vda1\n")
    findmnt.chmod(0o755)
    blkid = bin_dir / "blkid"
    blkid_body = f"echo {labeled_device}\n" if labeled_device else "exit 2\n"
    blkid.write_text(f"#!/bin/bash\n{blkid_body}")
    blkid.chmod(0o755)
    probe = "set -eu -o pipefail\n" + render_guest_data_disk_selection_lines() + 'echo "SELECTED=$DATA_DEV"\n'
    return subprocess.run(
        ["bash", "-c", probe],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
    )


def test_data_disk_selection_skips_the_read_only_cidata_disk(tmp_path: Path) -> None:
    # The guest's device table with the cidata ISO as a virtio-blk disk: the
    # root disk, the blank data disk, and the read-only iso9660 cidata (which
    # a "first non-root disk" rule would have picked and mkfs'd).
    _write_lsblk_stub(tmp_path, disks={"vda": ("0", ""), "vdc": ("1", "iso9660"), "vdb": ("0", "")})
    result = _run_data_disk_selection(tmp_path, labeled_device=None)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "SELECTED=/dev/vdb"


def test_data_disk_selection_prefers_the_labeled_filesystem(tmp_path: Path) -> None:
    # A restored (already formatted) data disk is found by its label without
    # consulting the device table at all.
    _write_lsblk_stub(tmp_path, disks={"vda": ("0", "")})
    result = _run_data_disk_selection(tmp_path, labeled_device="/dev/vdb")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "SELECTED=/dev/vdb"


def test_data_disk_selection_refuses_when_only_formatted_or_read_only_disks_remain(tmp_path: Path) -> None:
    # A writable disk that already carries a foreign filesystem is never
    # formatted over, and the read-only cidata never qualifies.
    _write_lsblk_stub(tmp_path, disks={"vda": ("0", ""), "vdb": ("0", "ext4"), "vdc": ("1", "iso9660")})
    result = _run_data_disk_selection(tmp_path, labeled_device=None)
    assert result.returncode == 1
    assert "no blank writable data disk" in result.stderr
    assert "SELECTED=" not in result.stdout


# A stub docker for the reconciler: containers described by files in
# $FAKE_DOCKER_DIR (<id>.memory holds HostConfig.Memory, <id>.running is
# "true"/"false"); every call is appended to $FAKE_DOCKER_DIR/calls.
_RECONCILER_FAKE_DOCKER = """\
#!/bin/bash
echo "$*" >> "$FAKE_DOCKER_DIR/calls"
case "$1" in
    info) exit 0 ;;
    ps) for f in "$FAKE_DOCKER_DIR"/*.memory; do basename "$f" .memory; done ;;
    inspect)
        case "$3" in
            "{{.HostConfig.Memory}}") cat "$FAKE_DOCKER_DIR/$4.memory" ;;
            "{{.State.Running}}") cat "$FAKE_DOCKER_DIR/$4.running" ;;
            *) exit 99 ;;
        esac ;;
    update) echo "$6" ;;
    restart) echo "$2" ;;
    *) exit 99 ;;
esac
"""


def _run_reconciler(tmp_path: Path, container_memory_by_id: Mapping[str, tuple[int, bool]]) -> list[str]:
    """Run the rendered reconciler on a VM showing 7439 MiB; returns the docker calls it made."""
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir()
    for container_id, (memory_bytes, is_running) in container_memory_by_id.items():
        (docker_dir / f"{container_id}.memory").write_text(f"{memory_bytes}\n")
        (docker_dir / f"{container_id}.running").write_text("true\n" if is_running else "false\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(_RECONCILER_FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:        7617536 kB\nMemFree:          812344 kB\n")
    script = tmp_path / "reconcile.sh"
    script.write_text(render_guest_container_memory_script(str(meminfo)))
    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", "FAKE_DOCKER_DIR": str(docker_dir)},
    )
    assert result.returncode == 0, result.stderr
    return (docker_dir / "calls").read_text().splitlines()


_RECONCILED_CAP_BYTES = (7439 - 1024) * 1024 * 1024


def test_reconciler_restarts_a_running_container_onto_its_new_cap(tmp_path: Path) -> None:
    # A running gVisor sandbox keeps the MemTotal it read at start, so updating
    # its cgroup alone would leave it believing in memory it no longer has.
    calls = _run_reconciler(tmp_path, {"c0ffee": (6656 * 1024 * 1024, True)})
    assert "update --memory 6415m --memory-swap 6415m c0ffee" in calls
    assert calls.index("update --memory 6415m --memory-swap 6415m c0ffee") < calls.index("restart c0ffee")


def test_reconciler_only_updates_a_stopped_container(tmp_path: Path) -> None:
    # A stopped container starts under the new cap anyway: no restart.
    calls = _run_reconciler(tmp_path, {"c0ffee": (6656 * 1024 * 1024, False)})
    assert "update --memory 6415m --memory-swap 6415m c0ffee" in calls
    assert not any(call.startswith("restart") for call in calls)


def test_reconciler_leaves_a_container_already_on_the_cap_alone(tmp_path: Path) -> None:
    calls = _run_reconciler(tmp_path, {"c0ffee": (_RECONCILED_CAP_BYTES, True)})
    assert not any(call.startswith(("update", "restart")) for call in calls)


# A stub docker for the publisher: one running labeled container whose
# /mngr-vol volume's bind device is $FAKE_VOLUME_DIR.
_PUBLISHER_FAKE_DOCKER = """\
#!/bin/bash
case "$1" in
    ps) echo "$FAKE_CONTAINER_ID" ;;
    inspect) echo mngr-host-vol-feed ;;
    volume) echo "$FAKE_VOLUME_DIR" ;;
    *) exit 99 ;;
esac
"""


def _write_container_cgroup(cgroup_parent: Path, container_id: str, *, limit: str, usage: int, stat: str) -> None:
    cgroup_dir = cgroup_parent / f"docker-{container_id}.scope"
    cgroup_dir.mkdir(parents=True, exist_ok=True)
    (cgroup_dir / "memory.max").write_text(f"{limit}\n")
    (cgroup_dir / "memory.current").write_text(f"{usage}\n")
    (cgroup_dir / "memory.stat").write_text(stat)


def _published_meminfo_or_none(target: Path) -> dict[str, int] | None:
    if not target.is_file() or target.is_symlink():
        return None
    fields = {line.split()[0].rstrip(":"): int(line.split()[1]) for line in target.read_text().splitlines()}
    return fields if {"MemTotal", "MemAvailable", "Timestamp"} <= fields.keys() else None


def _read_published_meminfo(target: Path, timeout_seconds: float) -> dict[str, int]:
    fields, _, _ = poll_for_value(lambda: _published_meminfo_or_none(target), timeout_seconds, poll_interval=0.05)
    assert fields is not None, f"the publisher wrote no complete {target} within {timeout_seconds}s"
    return fields


@contextmanager
def _running_publisher(tmp_path: Path, container_id: str, cgroup_parent: Path, volume_dir: Path) -> Iterator[None]:
    """Run the rendered publisher against one fake container until the block exits."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(_PUBLISHER_FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    script = tmp_path / "publish.sh"
    script.write_text(render_guest_container_meminfo_publisher_script(str(cgroup_parent)))
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "FAKE_CONTAINER_ID": container_id,
        "FAKE_VOLUME_DIR": str(volume_dir),
    }
    publisher = subprocess.Popen(["bash", str(script)], env=env, stderr=subprocess.PIPE, text=True)
    try:
        yield
        exit_code = publisher.poll()
    finally:
        publisher.kill()
        _, stderr = publisher.communicate(timeout=10)
    assert exit_code is None, f"the publisher exited with {exit_code}: {stderr}"


_MIB = 1024 * 1024


def test_meminfo_publisher_reports_the_cgroup_limit_and_reclaimable_headroom(tmp_path: Path) -> None:
    container_id = "d0d0" * 16
    cgroup_parent = tmp_path / "cgroup"
    volume_dir = tmp_path / "volume"
    volume_dir.mkdir()
    # 6415 MiB limit, 6000 MiB used, of which 300 MiB is file cache and 50 MiB
    # reclaimable slab; the shmem gVisor keeps the sandbox's memory in is not
    # counted as reclaimable.
    _write_container_cgroup(
        cgroup_parent,
        container_id,
        limit=str(6415 * _MIB),
        usage=6000 * _MIB,
        stat=(
            f"anon {400 * _MIB}\nfile {5000 * _MIB}\nshmem {4700 * _MIB}\n"
            f"active_file {100 * _MIB}\ninactive_file {200 * _MIB}\nslab_reclaimable {50 * _MIB}\n"
        ),
    )
    with _running_publisher(tmp_path, container_id, cgroup_parent, volume_dir):
        before = int(time.time())
        published = _read_published_meminfo(volume_dir / ".host-meminfo", timeout_seconds=10)
        assert published["MemTotal"] == 6415 * 1024
        assert published["MemAvailable"] == (6415 - 6000 + 100 + 200 + 50) * 1024
        assert before - 1 <= published["Timestamp"] <= int(time.time()) + 1

        # Removing the limit withdraws the file rather than reporting a made-up one.
        (cgroup_parent / f"docker-{container_id}.scope" / "memory.max").write_text("max\n")
        assert poll_until(lambda: not (volume_dir / ".host-meminfo").exists(), timeout=10, poll_interval=0.05)


def test_meminfo_publisher_never_writes_through_a_symlink_the_container_planted(tmp_path: Path) -> None:
    # The publisher is VM root and the volume is the container's: a planted
    # symlink must not steer its write onto a VM file or into a VM directory.
    container_id = "d0d0" * 16
    cgroup_parent = tmp_path / "cgroup"
    volume_dir = tmp_path / "volume"
    volume_dir.mkdir()
    _write_container_cgroup(cgroup_parent, container_id, limit=str(6415 * _MIB), usage=6000 * _MIB, stat="")
    vm_file = tmp_path / "vm-file"
    vm_file.write_text("owned by VM root\n")
    vm_dir = tmp_path / "vm-dir"
    vm_dir.mkdir()
    (volume_dir / ".host-meminfo.tmp").symlink_to(vm_file)
    (volume_dir / ".host-meminfo").symlink_to(vm_dir)

    with _running_publisher(tmp_path, container_id, cgroup_parent, volume_dir):
        published = _read_published_meminfo(volume_dir / ".host-meminfo", timeout_seconds=10)

    assert published["MemTotal"] == 6415 * 1024
    assert vm_file.read_text() == "owned by VM root\n"
    assert list(vm_dir.iterdir()) == []
