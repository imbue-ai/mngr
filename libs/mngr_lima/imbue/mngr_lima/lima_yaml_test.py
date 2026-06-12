from pathlib import Path

import pytest

from imbue.mngr.errors import MngrError
from imbue.mngr_lima.lima_yaml import generate_default_lima_yaml
from imbue.mngr_lima.lima_yaml import load_user_lima_yaml
from imbue.mngr_lima.lima_yaml import merge_lima_yaml
from imbue.mngr_lima.lima_yaml import parse_build_args_for_yaml_path
from imbue.mngr_lima.lima_yaml import write_lima_yaml

# Independently spelled out (rather than imported from production) so the
# assertions still document the expected shape of the disabled-port-forwards
# rules rather than tautologically echoing the helper.
_EXPECTED_DISABLED_PORT_FORWARDS = [
    {
        "guestIPMustBeZero": True,
        "guestIP": "0.0.0.0",
        "proto": "any",
        "guestPortRange": [1, 65535],
        "ignore": True,
    },
    {
        "guestIP": "127.0.0.1",
        "proto": "any",
        "guestPortRange": [1, 65535],
        "ignore": True,
    },
]


def test_generate_default_lima_yaml(tmp_path: Path) -> None:
    volume_path = tmp_path / "volume"
    volume_path.mkdir()

    config = generate_default_lima_yaml(
        volume_host_path=volume_path,
        host_dir="/mngr",
    )

    assert "images" in config
    assert len(config["images"]) == 1
    assert "location" in config["images"][0]
    assert "arch" in config["images"][0]

    assert "mounts" in config
    assert len(config["mounts"]) == 1
    assert config["mounts"][0]["mountPoint"] == "/mngr"
    assert config["mounts"][0]["writable"] is True

    assert "provision" in config
    assert len(config["provision"]) == 1
    assert config["provision"][0]["mode"] == "system"

    assert config["portForwards"] == _EXPECTED_DISABLED_PORT_FORWARDS


def test_generate_default_lima_yaml_custom_image(tmp_path: Path) -> None:
    volume_path = tmp_path / "volume"
    volume_path.mkdir()

    config = generate_default_lima_yaml(
        volume_host_path=volume_path,
        host_dir="/mngr",
        custom_image_url="https://example.com/custom.qcow2",
    )

    assert config["images"][0]["location"] == "https://example.com/custom.qcow2"


def test_generate_default_lima_yaml_without_host_key_omits_key_block(tmp_path: Path) -> None:
    """When the optional keypair parameters are omitted, the provision script
    must NOT write any /etc/ssh/ssh_host_* file -- the helper's default leaves
    the guest's own host key untouched."""
    volume_path = tmp_path / "volume"
    volume_path.mkdir()
    config = generate_default_lima_yaml(volume_host_path=volume_path, host_dir="/mngr")
    script = config["provision"][0]["script"]
    assert "/etc/ssh/ssh_host_ed25519_key" not in script
    assert "MNGR_LIMA_HOST_PRIV_KEY" not in script


def test_generate_default_lima_yaml_with_host_key_injects_block(tmp_path: Path) -> None:
    """When a keypair is provided, the provision script must include both the
    private-key heredoc and the public-key heredoc, remove rsa/ecdsa keys, and
    trigger an sshd restart via SSH_KEY_CHANGED=1."""
    volume_path = tmp_path / "volume"
    volume_path.mkdir()
    fake_private = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC...\n-----END OPENSSH PRIVATE KEY-----\n"
    fake_public = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPv... mngr-lima@host\n"
    config = generate_default_lima_yaml(
        volume_host_path=volume_path,
        host_dir="/mngr",
        host_private_key_pem=fake_private,
        host_public_key_openssh=fake_public,
    )
    script = config["provision"][0]["script"]
    # Both heredocs land in the script.
    assert "BEGIN OPENSSH PRIVATE KEY" in script
    assert "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPv" in script
    # The script removes other key types so sshd only presents our ed25519.
    assert "rm -f /etc/ssh/ssh_host_rsa_key" in script
    assert "rm -f /etc/ssh/ssh_host_ecdsa_key" in script
    # And flags the swap so the trailing restart fires.
    assert "SSH_KEY_CHANGED=1" in script


def test_write_lima_yaml(tmp_path: Path) -> None:
    config = {"images": [{"location": "test.qcow2", "arch": "x86_64"}]}
    output_path = tmp_path / "test.yaml"
    result = write_lima_yaml(config, output_path)
    assert result == output_path
    assert output_path.exists()
    content = output_path.read_text()
    assert "test.qcow2" in content


def test_write_lima_yaml_temp_file() -> None:
    config = {"images": [{"location": "test.qcow2"}]}
    result = write_lima_yaml(config)
    assert result.exists()
    assert result.suffix == ".yaml"
    # Clean up
    result.unlink()


def test_load_user_lima_yaml(tmp_path: Path) -> None:
    yaml_path = tmp_path / "user.yaml"
    yaml_path.write_text("cpus: 8\nmemory: 16GiB\n")
    config = load_user_lima_yaml(yaml_path)
    assert config["cpus"] == 8
    assert config["memory"] == "16GiB"


def test_merge_lima_yaml() -> None:
    base = {"images": [{"location": "default.qcow2"}], "cpus": 4}
    override = {"cpus": 8, "memory": "16GiB"}
    merged = merge_lima_yaml(base, override)
    assert merged["cpus"] == 8
    assert merged["memory"] == "16GiB"
    assert merged["images"] == [{"location": "default.qcow2"}]


def test_merge_lima_yaml_extends_provision_and_mounts_replaces_images() -> None:
    # provision: a user-supplied list must not silently drop mngr's host-key
    # injection. mngr's entries come first so its provision script runs before
    # any user script (Lima executes provision[mode=system] in list order).
    base = {"provision": [{"mode": "system", "script": "MNGR_HOST_KEY_INJECTION"}]}
    override = {"provision": [{"mode": "system", "script": "apt-get install -y postgres"}]}
    merged = merge_lima_yaml(base, override)
    assert len(merged["provision"]) == 2
    assert merged["provision"][0]["script"] == "MNGR_HOST_KEY_INJECTION"
    assert merged["provision"][1]["script"] == "apt-get install -y postgres"

    # mounts: extend with base first; mngr's /mngr mount must survive.
    base = {"mounts": [{"location": "/host/vol", "mountPoint": "/mngr", "writable": True}]}
    override = {"mounts": [{"location": "/host/data", "mountPoint": "/data", "writable": False}]}
    merged = merge_lima_yaml(base, override)
    assert len(merged["mounts"]) == 2
    assert merged["mounts"][0]["mountPoint"] == "/mngr"
    assert merged["mounts"][1]["mountPoint"] == "/data"

    # images: a user supplying images: clearly means to override -- still replace.
    base = {"images": [{"location": "default.qcow2"}]}
    override = {"images": [{"location": "custom.qcow2"}]}
    merged = merge_lima_yaml(base, override)
    assert merged["images"] == [{"location": "custom.qcow2"}]


def test_merge_lima_yaml_forces_port_forwards_disabled() -> None:
    base = {"portForwards": _EXPECTED_DISABLED_PORT_FORWARDS, "cpus": 4}
    user_override = {"portForwards": [{"guestPort": 8082, "hostPort": 8082}], "cpus": 8}
    merged = merge_lima_yaml(base, user_override)
    assert merged["cpus"] == 8
    assert merged["portForwards"] == _EXPECTED_DISABLED_PORT_FORWARDS


def test_generate_default_lima_yaml_bind_mount_mode_omits_additional_disks(tmp_path: Path) -> None:
    """Today's default (is_host_data_volume_exposed=True equivalent): the YAML has
    a 9p mount and no additionalDisks; the provisioning script does not contain
    the host-data-disk block."""
    volume_path = tmp_path / "volume"
    volume_path.mkdir()
    config = generate_default_lima_yaml(volume_host_path=volume_path, host_dir="/mngr")
    assert "additionalDisks" not in config
    assert "mounts" in config and len(config["mounts"]) == 1
    assert config["mounts"][0]["mountPoint"] == "/mngr"
    script = config["provision"][0]["script"]
    assert "/mnt/lima-" not in script
    assert "ln -sfn" not in script


def test_generate_default_lima_yaml_btrfs_mode_omits_mounts_adds_disk(tmp_path: Path) -> None:
    """When host_data_disk_name is set and volume_host_path is None, the YAML
    omits the `mounts:` block entirely, attaches a btrfs additionalDisk with
    format: true, and the provisioning script symlinks host_dir to Lima's
    auto-mount path for that disk."""
    del tmp_path
    config = generate_default_lima_yaml(
        volume_host_path=None,
        host_dir="/mngr",
        host_data_disk_name="mngr-abc123-data",
        host_data_disk_size="100GiB",
    )
    assert "mounts" not in config
    assert "additionalDisks" in config
    assert len(config["additionalDisks"]) == 1
    disk_entry = config["additionalDisks"][0]
    assert disk_entry["name"] == "mngr-abc123-data"
    assert disk_entry["format"] is True
    assert disk_entry["fsType"] == "btrfs"
    assert disk_entry["size"] == "100GiB"

    script = config["provision"][0]["script"]
    # The symlink target is the disk's canonical mount path.
    assert "ln -sfn /mnt/lima-mngr-abc123-data /mngr" in script
    # We format + mount the disk ourselves (Lima can't on minimal images that lack
    # mkfs.btrfs): btrfs-progs is installed, the disk is formatted, and mounted at
    # the canonical path before host_dir is symlinked into it.
    assert "btrfs-progs" in script
    assert "mkfs.btrfs -f" in script
    assert "mountpoint -q /mnt/lima-mngr-abc123-data" in script
    # Opens the btrfs root for the Lima default non-root user (fresh mkfs.btrfs
    # leaves the root dir owned by root:root).
    assert "chmod 0777 /mnt/lima-mngr-abc123-data" in script
    # No intermediate bind-mount or fstab manipulation -- those caused
    # stacked-mount ordering quirks on reboot.
    assert "mount --bind" not in script
    assert "/etc/fstab" not in script


def test_generate_default_lima_yaml_disk_name_without_size_raises(tmp_path: Path) -> None:
    """host_data_disk_size is required whenever a disk name is set; the helper
    raises MngrError rather than silently producing a malformed YAML."""
    volume_path = tmp_path / "volume"
    volume_path.mkdir()
    with pytest.raises(MngrError):
        generate_default_lima_yaml(
            volume_host_path=volume_path,
            host_dir="/mngr",
            host_data_disk_name="mngr-abc-data",
            host_data_disk_size=None,
        )


def test_merge_lima_yaml_additional_disks_extends() -> None:
    """A user --file YAML adding its own additionalDisks must not silently drop
    mngr's btrfs host-data disk. _LIST_EXTEND_KEYS makes the merge concatenate
    rather than replace."""
    base = {"additionalDisks": [{"name": "mngr-host-data", "format": True, "fsType": "btrfs", "size": "100GiB"}]}
    override = {"additionalDisks": [{"name": "user-extra", "format": True, "fsType": "ext4", "size": "20GiB"}]}
    merged = merge_lima_yaml(base, override)
    assert len(merged["additionalDisks"]) == 2
    assert merged["additionalDisks"][0]["name"] == "mngr-host-data"
    assert merged["additionalDisks"][1]["name"] == "user-extra"


def test_parse_build_args_for_yaml_path() -> None:
    assert parse_build_args_for_yaml_path(("--file", "/path/to/config.yaml")) == Path("/path/to/config.yaml")
    assert parse_build_args_for_yaml_path(("--file=/path/to/config.yaml",)) == Path("/path/to/config.yaml")
    assert parse_build_args_for_yaml_path(("--other", "arg")) is None
    assert parse_build_args_for_yaml_path(()) is None


def test_generate_default_lima_yaml_without_root_key_omits_root_login() -> None:
    """Without root_authorized_public_key, the provisioning script must not enable
    root login or authorize a root key -- the default non-root path is untouched."""
    config = generate_default_lima_yaml(
        volume_host_path=None,
        host_dir="/mngr",
        host_data_disk_name="mngr-abc-data",
        host_data_disk_size="100GiB",
    )
    script = config["provision"][0]["script"]
    assert "PermitRootLogin" not in script
    assert "/root/.ssh/authorized_keys" not in script


def test_generate_default_lima_yaml_with_root_key_enables_root_login() -> None:
    """When a root client key is provided, the provisioning script enables
    key-based root login and authorizes that key for root."""
    config = generate_default_lima_yaml(
        volume_host_path=None,
        host_dir="/mngr",
        host_data_disk_name="mngr-abc-data",
        host_data_disk_size="100GiB",
        root_authorized_public_key="ssh-ed25519 AAAAROOTKEY mngr-lima-root",
    )
    script = config["provision"][0]["script"]
    assert "PermitRootLogin prohibit-password" in script
    assert "/root/.ssh/authorized_keys" in script
    assert "ssh-ed25519 AAAAROOTKEY mngr-lima-root" in script
    # The btrfs disk is still formatted + mounted; root mode doesn't change that.
    assert "mkfs.btrfs -f" in script
    assert "ln -sfn /mnt/lima-mngr-abc-data /mngr" in script
