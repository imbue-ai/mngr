import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr_imbue_cloud.errors import SliceReserveOutputError
from imbue.mngr_imbue_cloud.slices.lima_slice import build_slice_lima_yaml
from imbue.mngr_imbue_cloud.slices.lima_slice import build_slice_reserve_script
from imbue.mngr_imbue_cloud.slices.lima_slice import parse_reserved_ports

_ROOT_PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITESTKEYrootclient mngr-slice"
_HOST_PRIV = "-----BEGIN OPENSSH PRIVATE KEY-----\nTESTHOSTKEY\n-----END OPENSSH PRIVATE KEY-----"
_HOST_PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITESThostkey mngr-slice-host"


def _build() -> dict[str, Any]:
    return build_slice_lima_yaml(
        host_dir="/mngr",
        vcpus=3,
        memory_mib=7680,
        disk_gib=80,
        boot_disk_gib=16,
        disk_name="mngr-slice-deadbeef-data",
        root_authorized_public_key=_ROOT_PUBKEY,
        host_private_key_pem=_HOST_PRIV,
        host_public_key_openssh=_HOST_PUB,
        vm_ssh_host_port=22001,
        container_ssh_host_port=22002,
    )


def test_slice_yaml_sets_cpu_and_memory() -> None:
    config = _build()
    assert config["cpus"] == 3
    assert config["memory"] == "7680MiB"


def test_slice_yaml_attaches_named_btrfs_data_disk() -> None:
    config = _build()
    disks = config["additionalDisks"]
    assert len(disks) == 1
    assert disks[0]["name"] == "mngr-slice-deadbeef-data"
    assert disks[0]["fsType"] == "btrfs"
    assert disks[0]["size"] == "80GiB"


def test_slice_yaml_sets_explicit_boot_disk_size() -> None:
    # The boot disk is sized explicitly (not lima's 100GiB default) so it + the
    # data disk sum to the slice's disk budget (no disk overcommit).
    config = _build()
    assert config["disk"] == "16GiB"


def test_slice_yaml_forwards_exactly_vm_and_container_sshd_externally() -> None:
    config = _build()
    forwards = config["portForwards"]
    # The first two rules are the explicit external allows for the VM sshd and the
    # inner container sshd; both bound to 0.0.0.0 so the box exposes them.
    vm_rule, container_rule = forwards[0], forwards[1]
    # Guest 2200 (not 22, which Lima reserves) is the VM's extra sshd port.
    assert vm_rule == {"guestPort": 2200, "hostPort": 22001, "hostIP": "0.0.0.0"}
    assert container_rule == {"guestPort": 2222, "hostPort": 22002, "hostIP": "0.0.0.0"}
    # Everything after them is a catch-all ignore rule (no other guest port leaks).
    assert all(rule.get("ignore") is True for rule in forwards[2:])
    assert len(forwards) >= 4


def test_slice_yaml_authorizes_root_client_key_and_installs_docker() -> None:
    config = _build()
    scripts = [step["script"] for step in config["provision"]]
    joined = "\n".join(scripts)
    # Root client key authorized (so mngr can SSH the VM as root, VPS-style).
    assert _ROOT_PUBKEY in joined
    # Docker gets installed so the vps_docker bake can run a container on the VM.
    assert "get.docker.com" in joined
    # The pre-injected sshd host key avoids a TOFU race on first connect.
    assert "ssh_host_ed25519_key" in joined
    # sshd is made to listen on the extra forwardable port (Lima keeps 22 for itself).
    assert "Port 2200" in joined


def test_slice_yaml_provision_runs_base_setup_before_docker() -> None:
    config = _build()
    scripts = [step["script"] for step in config["provision"]]
    # The base setup (which mounts the btrfs disk and installs the host key) must
    # run before the docker install that the vps_docker bake depends on.
    docker_index = next(i for i, script in enumerate(scripts) if "get.docker.com" in script)
    assert any("btrfs" in script.lower() for script in scripts[:docker_index])


def test_slice_provision_installs_inotify_tools_for_snapshot_helper() -> None:
    config = _build()
    scripts = [step["script"] for step in config["provision"]]
    # inotify-tools must be installed so the vps_docker snapshot helper's systemd
    # unit (which execs inotifywait) can run on the slice VM rather than crash-loop.
    assert any("inotify-tools" in script for script in scripts)


_POOL_PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITESTpoolkey mngr-pool"


def test_slice_yaml_authorizes_extra_root_keys_without_dropping_bake_key() -> None:
    config = build_slice_lima_yaml(
        host_dir="/mngr",
        vcpus=3,
        memory_mib=7680,
        disk_gib=80,
        boot_disk_gib=16,
        disk_name="mngr-slice-deadbeef-data",
        root_authorized_public_key=_ROOT_PUBKEY,
        host_private_key_pem=_HOST_PRIV,
        host_public_key_openssh=_HOST_PUB,
        vm_ssh_host_port=22001,
        container_ssh_host_port=22002,
        extra_root_authorized_keys=(_POOL_PUBKEY,),
    )
    joined = "\n".join(step["script"] for step in config["provision"])
    # Both the bake key (root_authorized_public_key) and the extra pool key are authorized.
    assert _ROOT_PUBKEY in joined
    assert _POOL_PUBKEY in joined
    # The extra-key append is idempotent (guarded so re-provision doesn't duplicate).
    assert "grep -qxF" in joined


def test_slice_yaml_omits_extra_key_script_when_none_given() -> None:
    config = _build()
    joined = "\n".join(step["script"] for step in config["provision"])
    assert "grep -qxF" not in joined


def test_build_slice_reserve_script_is_valid_bash_and_holds_the_box_lock() -> None:
    script = build_slice_reserve_script(
        instance_name="mngr-slice-dev-josh-abc",
        disk_name="mngr-slice-dev-josh-abc-data",
        disk_gib=40,
        slot_count=6,
        port_range_start=22000,
        port_range_end=32000,
        yaml_template_text="cpus: 2\n",
        lima_service_user="limahost",
    )
    # The reserve must be syntactically valid bash.
    syntax_check = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
    assert syntax_check.returncode == 0, syntax_check.stderr
    # It serializes under the box lock, enforces capacity, and claims the slot WITHOUT
    # booting (disk create + limactl create), so the lock is held only briefly.
    assert "flock 9" in script
    assert "/home/limahost/.mngr-slice-alloc.lock" in script
    assert "-ge 6" in script
    # shlex.quote leaves these simple names unquoted.
    assert "limactl disk create mngr-slice-dev-josh-abc-data --size 40GiB" in script
    assert "limactl create --name=mngr-slice-dev-josh-abc" in script
    # It must NOT boot the VM (that long step runs after the lock is released).
    assert "limactl start" not in script


def test_build_slice_reserve_script_counts_only_slice_disks_for_capacity() -> None:
    script = build_slice_reserve_script(
        instance_name="mngr-slice-dev-josh-abc",
        disk_name="mngr-slice-dev-josh-abc-data",
        disk_gib=40,
        slot_count=6,
        port_range_start=22000,
        port_range_end=32000,
        yaml_template_text="cpus: 2\n",
        lima_service_user="limahost",
    )
    # Capacity is measured against ALL slice disks on the box (every env + legacy).
    assert '"mngr-slice-' in script
    assert "limactl disk list --json" in script


def test_build_slice_reserve_script_reaches_the_marker_on_an_empty_box() -> None:
    # Regression: the disk-count pipeline runs under `set -o pipefail`, so on an
    # empty box (grep matches no slice disks and exits non-zero) it must NOT abort
    # the script before the slot is reserved -- `disk_count` must read as 0 and the
    # script must proceed to print MNGR_SLICE_RESERVED. We execute the rendered
    # script with `limactl`/`ss` stubbed to simulate an empty box; the lock lines
    # (asserted elsewhere) are stripped so the test needs no writable /home path.
    script = build_slice_reserve_script(
        instance_name="mngr-slice-dev-josh-abc",
        disk_name="mngr-slice-dev-josh-abc-data",
        disk_gib=40,
        slot_count=6,
        port_range_start=22000,
        port_range_end=22002,
        yaml_template_text="cpus: 2\n",
        lima_service_user="limahost",
    )
    # Drop the lock acquisition (asserted elsewhere) so the test needs no writable
    # /home path: replace the fd open and the flock call with harmless no-ops.
    runnable = script.replace("exec 9>", "true 9>/dev/null ").replace("flock 9", "true")
    with tempfile.TemporaryDirectory() as tmp:
        bin_dir = Path(tmp) / "bin"
        bin_dir.mkdir()
        # Empty box: limactl reports no disks and succeeds for create/disk-create;
        # ss reports no listening ports. These mimic a freshly-prepped box.
        (bin_dir / "limactl").write_text("#!/bin/bash\nexit 0\n")
        (bin_dir / "ss").write_text("#!/bin/bash\nexit 0\n")
        for stub in ("limactl", "ss"):
            (bin_dir / stub).chmod(0o755)
        home = Path(tmp) / "home"
        (home / ".lima").mkdir(parents=True)
        result = subprocess.run(
            ["bash"],
            input=runnable,
            text=True,
            capture_output=True,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(home)},
        )
    assert result.returncode == 0, result.stderr
    # disk_count read as 0 (empty box), so the slot was reserved and the marker printed.
    assert "MNGR_SLICE_RESERVED 22000 22001" in result.stdout


def test_parse_reserved_ports_reads_the_marker_line() -> None:
    assert parse_reserved_ports("noise\nMNGR_SLICE_RESERVED 22001 22002\nmore noise") == (22001, 22002)


def test_parse_reserved_ports_raises_when_marker_missing_or_malformed() -> None:
    with pytest.raises(SliceReserveOutputError):
        parse_reserved_ports("no marker here")
    with pytest.raises(SliceReserveOutputError):
        parse_reserved_ports("MNGR_SLICE_RESERVED only-one")
