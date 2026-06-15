from typing import Any

from imbue.mngr_imbue_cloud.slices.lima_slice import build_slice_lima_yaml

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
    # The last provision step is the docker install; the base setup (which mounts
    # the btrfs disk and installs the host key) must run first.
    assert "get.docker.com" in scripts[-1]
    assert any("btrfs" in script.lower() for script in scripts[:-1])


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
